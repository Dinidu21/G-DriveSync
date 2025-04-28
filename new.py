import json
import os
from threading import Thread, Lock

import flask
from dotenv import load_dotenv
from flask import Flask, render_template, request, redirect, url_for, jsonify
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

os.environ['OAUTHLIB_INSECURE_TRANSPORT'] = '1'

app = Flask(__name__)
app.secret_key = os.urandom(24).hex()

load_dotenv()
LOCAL_FOLDER = os.getenv('LOCAL_FOLDER')
SCOPES = [os.getenv('SCOPES')]
TOKEN_FILE = os.getenv('TOKEN_FILE')
CREDENTIALS_FILE = os.getenv('CREDENTIALS_FILE')
DRIVE_FOLDER_ID = os.getenv('DRIVE_FOLDER_ID')

unsync_files = []
drive_files_cache = {}
folder_id_map = {}
sync_status = {'in_progress': [], 'completed': [], 'failed': []}
sync_lock = Lock()
file_lock = Lock()
folder_id_cache = {}


class CertificateHandler(FileSystemEventHandler):
    def on_created(self, event):
        if not event.is_directory:
            file_path = event.src_path
            with file_lock:
                if file_path not in unsync_files and not os.path.basename(file_path).startswith('.'):
                    unsync_files.append(file_path)
                    print(f"New file detected: {file_path}")


def start_observer():
    event_handler = CertificateHandler()
    observer = Observer()
    os.makedirs(LOCAL_FOLDER, exist_ok=True)
    observer.schedule(event_handler, LOCAL_FOLDER, recursive=True)
    observer.start()
    return observer


def get_or_create_folder_path(drive_service, relative_path, root_folder_id):
    """Create or find a nested folder structure in Google Drive."""
    if relative_path in folder_id_cache:
        return folder_id_cache[relative_path]

    folders = relative_path.split(os.sep)
    current_parent = root_folder_id
    current_path = []

    for folder in folders:
        current_path.append(folder)
        path_key = os.sep.join(current_path)
        if path_key in folder_id_cache:
            current_parent = folder_id_cache[path_key]
            continue

        query = f"name='{folder}' and '{current_parent}' in parents and mimeType='application/vnd.google-apps.folder'"
        results = drive_service.files().list(q=query, spaces='drive', fields='files(id, name)').execute()
        files = results.get('files', [])

        if files:
            current_parent = files[0]['id']
        else:
            folder_metadata = {
                'name': folder,
                'mimeType': 'application/vnd.google-apps.folder',
                'parents': [current_parent]
            }
            folder_file = drive_service.files().create(body=folder_metadata, fields='id').execute()
            current_parent = folder_file.get('id')

        folder_id_cache[path_key] = current_parent

    return current_parent


def get_drive_folders(drive_service):
    if not drive_service:
        return {}
    query = f"'{DRIVE_FOLDER_ID}' in parents and mimeType='application/vnd.google-apps.folder'"
    results = drive_service.files().list(q=query, spaces='drive', fields='files(id, name)').execute()
    folders = {folder['name']: folder['id'] for folder in results.get('files', [])}
    folder_id_map[LOCAL_FOLDER] = DRIVE_FOLDER_ID
    for folder_name, folder_id in folders.items():
        local_folder_path = os.path.join(LOCAL_FOLDER, folder_name)
        folder_id_map[local_folder_path] = folder_id
    return folders


def get_drive_files(drive_service):
    if not drive_service:
        return {}
    get_drive_folders(drive_service)
    query = f"'{DRIVE_FOLDER_ID}' in parents and mimeType!='application/vnd.google-apps.folder'"
    results = drive_service.files().list(q=query, spaces='drive', fields='files(id, name, parents)').execute()
    main_files = {file['name']: file['id'] for file in results.get('files', [])}
    drive_files_cache.update(main_files)
    for local_path, drive_id in folder_id_map.items():
        if local_path == LOCAL_FOLDER:
            continue
        query = f"'{drive_id}' in parents and mimeType!='application/vnd.google-apps.folder'"
        results = drive_service.files().list(q=query, spaces='drive', fields='files(id, name, parents)').execute()
        subfolder_files = {os.path.join(os.path.basename(local_path), file['name']): file['id'] for file in
                           results.get('files', [])}
        drive_files_cache.update(subfolder_files)
    return drive_files_cache


def find_unsynced_files(drive_service):
    drive_files = get_drive_files(drive_service)
    for root, dirs, files in os.walk(LOCAL_FOLDER):
        rel_path = os.path.relpath(root, LOCAL_FOLDER)
        for file in files:
            if file.startswith('.'):
                continue
            file_path = os.path.join(root, file)
            rel_file_path = file if rel_path == '.' else os.path.join(rel_path, file)
            with file_lock:
                if rel_file_path not in drive_files and file_path not in unsync_files:
                    unsync_files.append(file_path)
                    print(f"Unsynced file detected: {file_path}")


@app.route('/')
def index():
    if not os.path.exists(TOKEN_FILE):
        return redirect(url_for('authorize'))
    drive_service = get_drive_service()
    if drive_service:
        find_unsynced_files(drive_service)
    all_files = []
    for root, dirs, files in os.walk(LOCAL_FOLDER):
        rel_path = os.path.relpath(root, LOCAL_FOLDER)
        for file in files:
            if file.startswith('.'):
                continue
            file_path = os.path.join(root, file)
            is_synced = file_path not in unsync_files
            display_path = file if rel_path == '.' else os.path.join(rel_path, file)
            all_files.append({
                'name': display_path,
                'path': file_path,
                'sync_status': 'Synced' if is_synced else 'Not Synced'
            })
    return render_template('index.html', files=all_files, LOCAL_FOLDER=LOCAL_FOLDER)


@app.route('/authorize')
def authorize():
    if not os.path.exists(CREDENTIALS_FILE):
        return "Missing credentials.json file. Please follow setup instructions."
    flow = Flow.from_client_secrets_file(CREDENTIALS_FILE, scopes=SCOPES,
                                         redirect_uri=url_for('oauth2callback', _external=True))
    authorization_url, state = flow.authorization_url(access_type='offline', include_granted_scopes='true')
    flask.session['state'] = state
    return redirect(authorization_url)


@app.route('/oauth2callback')
def oauth2callback():
    state = flask.session['state']
    flow = Flow.from_client_secrets_file(CREDENTIALS_FILE, scopes=SCOPES, state=state,
                                         redirect_uri=url_for('oauth2callback', _external=True))
    flow.fetch_token(authorization_response=request.url)
    creds = flow.credentials
    with open(TOKEN_FILE, 'w') as token:
        token.write(creds.to_json())
    return redirect(url_for('index'))


def get_drive_service():
    if not os.path.exists(TOKEN_FILE):
        return None
    with open(TOKEN_FILE, 'r') as token:
        creds = Credentials.from_authorized_user_info(json.loads(token.read()))
    return build('drive', 'v3', credentials=creds)


@app.route('/sync', methods=['POST'])
def sync():
    drive_service = get_drive_service()
    if not drive_service:
        return jsonify({'error': 'Authentication required'}), 401
    with sync_lock:
        if sync_status['in_progress']:
            return jsonify({'error': 'Sync already in progress'}), 400
    with file_lock:
        files_to_sync = list(unsync_files)
    if not files_to_sync:
        return jsonify({'message': 'No files to sync'})
    thread = Thread(
        target=perform_sync,
        args=(
        drive_service, files_to_sync, sync_status, unsync_files, sync_lock, file_lock, LOCAL_FOLDER, DRIVE_FOLDER_ID)
    )
    thread.start()
    return jsonify({'message': 'Sync started', 'files': files_to_sync})


def perform_sync(drive_service, files_to_sync, sync_status, unsync_files, sync_lock, file_lock, LOCAL_FOLDER,
                 DRIVE_FOLDER_ID):
    with sync_lock:
        sync_status['in_progress'] = files_to_sync
        sync_status['completed'] = []
        sync_status['failed'] = []
    for file_path in files_to_sync:
        if os.path.exists(file_path):
            try:
                # Log the start of the upload
                print(f"Starting upload for {file_path}", flush=True)

                # Determine the parent folder ID
                parent_folder = os.path.dirname(file_path)
                rel_parent_path = os.path.relpath(parent_folder, LOCAL_FOLDER)
                if rel_parent_path == '.':
                    parent_id = DRIVE_FOLDER_ID
                else:
                    parent_id = get_or_create_folder_path(drive_service, rel_parent_path, DRIVE_FOLDER_ID)

                # Prepare file metadata and media
                filename = os.path.basename(file_path)
                file_metadata = {'name': filename, 'parents': [parent_id]}
                media = MediaFileUpload(file_path)

                # Perform the upload
                file = drive_service.files().create(body=file_metadata, media_body=media, fields='id').execute()

                # Log the completion of the upload
                print(f"Finished upload for {file_path}", flush=True)
                print(f"Uploaded {filename} to Google Drive with ID: {file.get('id')}", flush=True)

                # Update sync status
                with sync_lock:
                    sync_status['completed'].append(file_path)
                    sync_status['in_progress'].remove(file_path)
                with file_lock:
                    if file_path in unsync_files:
                        unsync_files.remove(file_path)
            except Exception as e:
                # Log any errors
                print(f"Error uploading {file_path}: {e}", flush=True)
                with sync_lock:
                    sync_status['failed'].append(file_path)
                    sync_status['in_progress'].remove(file_path)


@app.route('/sync_status')
def sync_status_route():
    with sync_lock:
        status = {
            'in_progress': sync_status['in_progress'],
            'completed': sync_status['completed'],
            'failed': sync_status['failed']
        }
    return jsonify(status)


if __name__ == '__main__':
    observer = start_observer()
    try:
        app.run(debug=True)
    finally:
        observer.stop()
        observer.join()