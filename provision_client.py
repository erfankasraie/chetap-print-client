# provision_client.py - run on device to claim a provisioning token
import requests, json, os

API = input('Print service API (e.g. http://localhost:5555): ').strip()
token = input('Provision token (paste): ').strip()
r = requests.post(API+'/api/provision/claim', json={'token': token})
print(r.status_code, r.text)
if r.ok:
    data = r.json()
    with open('device_credentials.json','w') as f:
        json.dump(data, f, indent=2)
    print('Saved device_credentials.json')
