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
os.environ['OAUTHLIB_INSECURE_TRANSPORT'] = '1'

app = Flask(__name__)
app.secret_key = os.urandom(24).hex()
print(app.secret_key)

unsync_files = []

class AppHandler(FileSystemEventHandler):
    def on_created(self, event):
        if not event.is_directory:
            filename = os.path.basename(event.src_path)
            if filename not in unsync_files and not filename.startswith('.'):
                unsync_files.append(event.src_path)
                print(f"New file detected: {event.src_path}")

def start_observer():
    event_handler = AppHandler()
    observer = Observer()
    os.makedirs(LOCAL_FOLDER, exist_ok=True)
    observer.schedule(event_handler, LOCAL_FOLDER, recursive=True)
    observer.start()
    return observer

@app.route('/')
def index():
    if not os.path.exists(TOKEN_FILE):
        return redirect(url_for('authorize'))
    
    # Get the list of files in the local folder
    local_files = []
    for root, dirs, files in os.walk(LOCAL_FOLDER):
        for filename in files:
            if not filename.startswith('.'):
                file_path = os.path.join(root, filename)
                if file_path not in unsync_files:
                    local_files.append({
                        'name': filename,
                        'path': file_path,
                        'sync_status': 'Synced',
                        'folder': os.path.basename(root)
                    })

    # Add unsync files
    for file_path in unsync_files:
        if os.path.exists(file_path):
            local_files.append({
                'name': os.path.basename(file_path),
                'path': file_path,
                'sync_status': 'Not Synced'
            })
    
    return render_template('index.html', files=local_files)

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

@app.route('/sync', methods=['POST'])
def sync():
    drive_service = get_drive_service()
    if not drive_service:
        flash('Authentication required')
        return redirect(url_for('authorize'))
    
    success_count = 0
    failed_files = []
    
    for file_path in list(unsync_files):
        if os.path.exists(file_path):
            try:
                filename = os.path.basename(file_path)
                file_metadata = {
                    'name': filename,
                }
                
                if DRIVE_FOLDER_ID:
                    file_metadata['parents'] = [DRIVE_FOLDER_ID]
                
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
