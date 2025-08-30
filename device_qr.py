# device_qr.py - device GUI: request session QR and save for display
import os, json, base64, requests, io, sys
from dotenv import load_dotenv
load_dotenv()
API = os.getenv('PRINT_SERVICE_API','http://localhost:8000')

def load_creds():
    if not os.path.exists('device_credentials.json'):
        print('Run provision_client first and save device_credentials.json')
        sys.exit(1)
    return json.load(open('device_credentials.json'))

def show_image_bytes(b64):
    data = base64.b64decode(b64)
    with open('session_qr.png','wb') as f:
        f.write(data)
    print('Saved session_qr.png — open it on the device screen for users to scan.')

def request_and_show(lifetime=300):
    cred = load_creds()
    headers = {'Authorization': f"Bearer {cred.get('client_secret')}"}
    r = requests.post(API + '/api/device/session', headers=headers, json={'lifetime': lifetime})
    print(r.status_code, r.text)
    if r.ok:
        j = r.json()
        print('Upload URL:', j.get('upload_url'))
        show_image_bytes(j.get('qr_base64'))

if __name__ == '__main__':
    request_and_show(300)

