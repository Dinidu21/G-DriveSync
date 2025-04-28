import os
import flask
from flask import Flask, render_template, request, redirect, url_for, flash
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
import json
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler
from dotenv import load_dotenv

os.environ['OAUTHLIB_INSECURE_TRANSPORT'] = '1'

app = Flask(__name__)
app.secret_key = os.urandom(24).hex()
print(app.secret_key)


load_dotenv()
LOCAL_FOLDER = os.getenv('LOCAL_FOLDER')
SCOPES = [os.getenv('SCOPES')]
TOKEN_FILE = os.getenv('TOKEN_FILE')
CREDENTIALS_FILE = os.getenv('CREDENTIALS_FILE')
DRIVE_FOLDER_ID = os.getenv('DRIVE_FOLDER_ID')


unsync_files = []
drive_files_cache = {}
folder_id_map = {}


class AppHandler(FileSystemEventHandler):
    def on_created(self, event):
        if not event.is_directory:
            file_path = event.src_path
            if file_path not in unsync_files and not os.path.basename(file_path).startswith('.'):
                unsync_files.append(file_path)
                print(f"New file detected: {file_path}")


def start_observer():
    event_handler = AppHandler()
    observer = Observer()

    # Ensure main folder exists
    os.makedirs(LOCAL_FOLDER, exist_ok=True)

    # Monitor the main folder AND its subfolders
    observer.schedule(event_handler, LOCAL_FOLDER, recursive=True)
    observer.start()
    return observer


# Function to get all Drive folders under the main folder
def get_drive_folders(drive_service):
    if not drive_service:
        return {}

    # Get all folders in the main Drive folder
    query = f"'{DRIVE_FOLDER_ID}' in parents and mimeType='application/vnd.google-apps.folder'"
    results = drive_service.files().list(
        q=query,
        spaces='drive',
        fields='files(id, name)'
    ).execute()

    folders = {folder['name']: folder['id'] for folder in results.get('files', [])}
    folder_id_map[LOCAL_FOLDER] = DRIVE_FOLDER_ID

    # Map local folder paths to Drive folder IDs
    for folder_name, folder_id in folders.items():
        local_folder_path = os.path.join(LOCAL_FOLDER, folder_name)
        folder_id_map[local_folder_path] = folder_id

    return folders


# Function to get all files in Drive (in the main folder and subfolders)
def get_drive_files(drive_service):
    if not drive_service:
        return {}

    # First get the folders
    get_drive_folders(drive_service)

    # Get all files in the main Drive folder and its subfolders
    query = f"'{DRIVE_FOLDER_ID}' in parents and mimeType!='application/vnd.google-apps.folder'"
    results = drive_service.files().list(
        q=query,
        spaces='drive',
        fields='files(id, name, parents)'
    ).execute()

    main_files = {file['name']: file['id'] for file in results.get('files', [])}
    drive_files_cache.update(main_files)

    # Get files in each subfolder
    for local_path, drive_id in folder_id_map.items():
        if local_path == LOCAL_FOLDER:  # Skip main folder, we've already processed it
            continue

        query = f"'{drive_id}' in parents and mimeType!='application/vnd.google-apps.folder'"
        results = drive_service.files().list(
            q=query,
            spaces='drive',
            fields='files(id, name, parents)'
        ).execute()

        subfolder_files = {os.path.join(os.path.basename(local_path), file['name']): file['id']
                           for file in results.get('files', [])}
        drive_files_cache.update(subfolder_files)

    return drive_files_cache


# Find files that exist locally but not in Drive
def find_unsynced_files(drive_service):
    # Get all Drive files first
    drive_files = get_drive_files(drive_service)

    # Now check each local file
    for root, dirs, files in os.walk(LOCAL_FOLDER):
        rel_path = os.path.relpath(root, LOCAL_FOLDER)

        for file in files:
            if file.startswith('.'):
                continue

            file_path = os.path.join(root, file)

            # Construct the file's "path" relative to the main folder for comparison
            if rel_path == '.':
                rel_file_path = file
            else:
                rel_file_path = os.path.join(rel_path, file)

            # If file isn't in Drive cache and not already in unsync_files, add it
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

    # Get the list of all files (synchronized and unsynchronized)
    all_files = []

    # Walk through all directories and subdirectories
    for root, dirs, files in os.walk(LOCAL_FOLDER):
        rel_path = os.path.relpath(root, LOCAL_FOLDER)

        for file in files:
            if file.startswith('.'):
                continue

            file_path = os.path.join(root, file)

            # Determine if file is synced or not
            is_synced = file_path not in unsync_files

            # Create display path (relative to main folder)
            if rel_path == '.':
                display_path = file
            else:
                display_path = os.path.join(rel_path, file)

            # Add file to the list
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

    flow = Flow.from_client_secrets_file(
        CREDENTIALS_FILE,
        scopes=SCOPES,
        redirect_uri=url_for('oauth2callback', _external=True)
    )

    authorization_url, state = flow.authorization_url(
        access_type='offline',
        include_granted_scopes='true'
    )

    flask.session['state'] = state
    return redirect(authorization_url)


@app.route('/oauth2callback')
def oauth2callback():
    state = flask.session['state']

    flow = Flow.from_client_secrets_file(
        CREDENTIALS_FILE,
        scopes=SCOPES,
        state=state,
        redirect_uri=url_for('oauth2callback', _external=True)
    )

    authorization_response = request.url
    flow.fetch_token(authorization_response=authorization_response)

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


# Find or create folder in Google Drive
def find_or_create_folder(drive_service, folder_name, parent_id):
    # Check if folder exists
    query = f"name='{folder_name}' and '{parent_id}' in parents and mimeType='application/vnd.google-apps.folder'"
    results = drive_service.files().list(
        q=query, spaces='drive', fields='files(id, name)'
    ).execute()

    folders = results.get('files', [])

    # Return ID if folder exists
    if folders:
        return folders[0]['id']

    # Create folder if it doesn't exist
    folder_metadata = {
        'name': folder_name,
        'mimeType': 'application/vnd.google-apps.folder',
        'parents': [parent_id]
    }

    folder = drive_service.files().create(
        body=folder_metadata, fields='id'
    ).execute()

    print(f"Created folder: {folder_name} with ID: {folder.get('id')}")
    return folder.get('id')


@app.route('/sync', methods=['POST'])
def sync():
    drive_service = get_drive_service()
    if not drive_service:
        flash('Authentication required')
        return redirect(url_for('authorize'))

    # Make sure folder_id_map is updated
    get_drive_folders(drive_service)

    success_count = 0
    failed_files = []

    for file_path in list(unsync_files):
        if os.path.exists(file_path):
            try:
                # Determine parent folder
                parent_folder = os.path.dirname(file_path)
                filename = os.path.basename(file_path)

                # Check if parent folder exists in Drive
                parent_id = folder_id_map.get(parent_folder)

                # If parent folder not found, create it
                if not parent_id:
                    # Get folder name (last part of the path)
                    folder_name = os.path.basename(parent_folder)
                    # Create folder in Drive
                    parent_id = find_or_create_folder(drive_service, folder_name, DRIVE_FOLDER_ID)
                    # Update the map
                    folder_id_map[parent_folder] = parent_id

                # Prepare file metadata with correct parent folder
                file_metadata = {
                    'name': filename,
                    'parents': [parent_id]
                }

                # Upload file
                media = MediaFileUpload(file_path)
                file = drive_service.files().create(
                    body=file_metadata,
                    media_body=media,
                    fields='id'
                ).execute()

                print(f"Uploaded {filename} to Google Drive with ID: {file.get('id')}")
                unsync_files.remove(file_path)
                success_count += 1

            except Exception as e:
                print(f"Error uploading {file_path}: {e}")
                failed_files.append(file_path)

    if success_count > 0:
        flash(f'Successfully synced {success_count} file(s)')

    if failed_files:
        flash(f'Failed to sync {len(failed_files)} file(s)')

    return redirect(url_for('index'))


if __name__ == '__main__':
    observer = start_observer()
    try:
        app.run(debug=True)
    finally:
        observer.stop()
        observer.join()