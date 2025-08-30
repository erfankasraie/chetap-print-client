

# device_client.py - sample device MQTT client
import os, json, requests, time
import paho.mqtt.client as mqtt

if not os.path.exists('device_credentials.json'):
    print('Run provision_client first.')
    exit(1)
cred = json.load(open('device_credentials.json'))
device_uuid = cred.get('device_uuid')

def on_connect(client, userdata, flags, rc, properties=None):
    print('connected', rc)
    client.subscribe(f'devices/{device_uuid}/commands', qos=1)

def on_message(client, userdata, msg):
    import json
    print('msg', msg.topic, msg.payload)
    data = json.loads(msg.payload.decode('utf-8','ignore'))
    if data.get('command')=='print':
        url = data.get('url'); job = data.get('job_id')
        print('Downloading', url)
        r = requests.get(url)
        open(job + '.bin', 'wb').write(r.content)
        print('Saved', job)
        client.publish(f'devices/{device_uuid}/logs', json.dumps({'job_id': job, 'status': 'completed'}), qos=1)

client = mqtt.Client(client_id=device_uuid)
client.on_connect = on_connect
client.on_message = on_message
client.connect('192.168.100.13', 2222, 60)
client.loop_forever()

