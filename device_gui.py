# device_gui.py
"""
Cross-platform Device GUI for Easy Print
Features:
 - Reads device_credentials.json
 - Requests session QR from server (/api/device/session)
 - Shows QR on screen (PNG)
 - Refresh QR button
 - Show device status (poll /api/devices)
 - Connect to MQTT and listen to print commands
 - When receives print command: download URL and try to print locally (Windows: win32print, Linux: cups)
 - Logging pane
"""

import os
import sys
import json
import time
import threading
import platform
import tempfile
import requests
from io import BytesIO

from PyQt5 import QtWidgets, QtGui, QtCore

try:
    import paho.mqtt.client as mqtt
except Exception:
    mqtt = None

IS_WINDOWS = platform.system().lower().startswith("win")
IS_LINUX = platform.system().lower().startswith("linux")

# Optional print libs
win32print = None
cups = None
if IS_WINDOWS:
    try:
        import win32print
        import win32api
    except Exception:
        win32print = None
else:
    try:
        import cups
    except Exception:
        cups = None

# Config / defaults
PRINT_SERVICE_API = os.getenv("PRINT_SERVICE_API", "http://localhost:8000")
CREDENTIALS_FILE = os.path.join(os.getcwd(), "device_credentials.json")
POLL_DEVICES_INTERVAL = 10  # seconds
MQTT_RECONNECT_INTERVAL = 5  # seconds

def log(msg):
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] {msg}")

class WorkerSignals(QtCore.QObject):
    log = QtCore.pyqtSignal(str)
    qr_image = QtCore.pyqtSignal(QtGui.QPixmap)
    status = QtCore.pyqtSignal(str)

class DeviceGUI(QtWidgets.QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Easy Print - Device GUI")
        self.setMinimumSize(480, 640)
        self.signals = WorkerSignals()

        self.client_secret = None
        self.device_uuid = None
        self.mqtt_client = None
        self.mqtt_connected = False

        self._load_credentials()

        self._build_ui()
        self._connect_signals()

        if self.client_secret and self.device_uuid:
            # start background threads
            threading.Thread(target=self._poll_devices_loop, daemon=True).start()
            threading.Thread(target=self._mqtt_connect_loop, daemon=True).start()
            # initial QR fetch
            self.request_session_qr()

    def _load_credentials(self):
        if not os.path.exists(CREDENTIALS_FILE):
            self.signals.log.emit(f"credentials file not found: {CREDENTIALS_FILE}")
            return
        try:
            data = json.load(open(CREDENTIALS_FILE, "r", encoding="utf-8"))
            self.client_secret = data.get("client_secret") or data.get("client_secret".lower())
            self.device_uuid = data.get("device_uuid")
            if not self.client_secret or not self.device_uuid:
                self.signals.log.emit("credentials file missing device_uuid or client_secret")
        except Exception as e:
            self.signals.log.emit(f"error reading credentials: {e}")

    def _build_ui(self):
        layout = QtWidgets.QVBoxLayout(self)

        # header
        h = QtWidgets.QHBoxLayout()
        self.lbl_device = QtWidgets.QLabel(f"Device: {self.device_uuid or '---'}")
        self.lbl_status = QtWidgets.QLabel("Status: unknown")
        h.addWidget(self.lbl_device)
        h.addStretch()
        h.addWidget(self.lbl_status)
        layout.addLayout(h)

        # QR area
        self.qr_label = QtWidgets.QLabel()
        self.qr_label.setFixedSize(360,360)
        self.qr_label.setStyleSheet("background: #eee; border: 1px solid #ccc;")
        self.qr_label.setAlignment(QtCore.Qt.AlignCenter)
        layout.addWidget(self.qr_label, alignment=QtCore.Qt.AlignCenter)

        # buttons
        btns = QtWidgets.QHBoxLayout()
        self.btn_refresh = QtWidgets.QPushButton("Refresh QR")
        self.btn_copy_url = QtWidgets.QPushButton("Copy upload URL")
        btns.addWidget(self.btn_refresh)
        btns.addWidget(self.btn_copy_url)
        layout.addLayout(btns)

        # Logs
        self.log_view = QtWidgets.QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(1000)
        layout.addWidget(self.log_view)

        # Footer controls
        footer = QtWidgets.QHBoxLayout()
        self.btn_open_logs = QtWidgets.QPushButton("Open logs folder")
        self.btn_quit = QtWidgets.QPushButton("Quit")
        footer.addWidget(self.btn_open_logs)
        footer.addStretch()
        footer.addWidget(self.btn_quit)
        layout.addLayout(footer)

        # store state
        self.current_upload_url = None
        self.last_session_token = None

        # connections
        self.btn_refresh.clicked.connect(lambda: threading.Thread(target=self.request_session_qr, daemon=True).start())
        self.btn_copy_url.clicked.connect(self.copy_upload_url)
        self.btn_open_logs.clicked.connect(self.open_logs_folder)
        self.btn_quit.clicked.connect(QtWidgets.qApp.quit)

    def _connect_signals(self):
        self.signals.log.connect(self._append_log)
        self.signals.qr_image.connect(self._set_qr_image)
        self.signals.status.connect(self._set_status)

    def _append_log(self, txt):
        self.log_view.appendPlainText(txt)

    def _set_qr_image(self, pixmap):
        self.qr_label.setPixmap(pixmap.scaled(self.qr_label.size(), QtCore.Qt.KeepAspectRatio, QtCore.Qt.SmoothTransformation))

    def _set_status(self, s):
        self.lbl_status.setText(f"Status: {s}")

    def copy_upload_url(self):
        if not self.current_upload_url:
            self.signals.log.emit("No upload URL to copy")
            return
        cb = QtWidgets.QApplication.clipboard()
        cb.setText(self.current_upload_url)
        self.signals.log.emit("Upload URL copied to clipboard")

    def open_logs_folder(self):
        logs_dir = os.path.join(os.getcwd(), "logs")
        os.makedirs(logs_dir, exist_ok=True)
        if IS_WINDOWS:
            os.startfile(logs_dir)
        elif IS_LINUX:
            import subprocess
            subprocess.Popen(["xdg-open", logs_dir])
        else:
            self.signals.log.emit(f"Open logs at: {logs_dir}")

    def request_session_qr(self, lifetime=300):
        if not self.client_secret:
            self.signals.log.emit("No client_secret available. Provision first.")
            return
        url = PRINT_SERVICE_API.rstrip("/") + "/api/device/session"
        headers = {"Authorization": f"Bearer {self.client_secret}", "Content-Type": "application/json"}
        try:
            self.signals.log.emit(f"Requesting session (lifetime={lifetime}) from {url}")
            r = requests.post(url, headers=headers, json={"lifetime": lifetime}, timeout=10)
        except Exception as e:
            self.signals.log.emit(f"Request failed: {e}")
            return
        if r.status_code != 200:
            self.signals.log.emit(f"Server returned {r.status_code} {r.text}")
            return
        try:
            j = r.json()
        except Exception:
            self.signals.log.emit("Invalid JSON response")
            return
        # expected keys: upload_url, qr_base64 or token
        upload_url = j.get("upload_url") or j.get("uploadUrl") or j.get("upload_url")
        qr_b64 = j.get("qr_base64") or j.get("qr")
        token = j.get("token") or j.get("session_token")
        self.current_upload_url = upload_url or (PRINT_SERVICE_API.rstrip("/") + "/session_upload?token=" + token if token else None)
        self.last_session_token = token

        self.signals.log.emit(f"Got upload_url: {self.current_upload_url}")

        if qr_b64:
            import base64
            try:
                raw = base64.b64decode(qr_b64)
                pix = QtGui.QPixmap()
                pix.loadFromData(raw)
                self.signals.qr_image.emit(pix)
                self.signals.log.emit("QR image updated")
            except Exception as e:
                self.signals.log.emit(f"Failed to load QR image: {e}")
        else:
            # fallback: generate QR locally from upload_url if qrcode lib exists
            if self.current_upload_url:
                try:
                    import qrcode
                    img = qrcode.make(self.current_upload_url)
                    buf = BytesIO()
                    img.save(buf, format="PNG")
                    pix = QtGui.QPixmap()
                    pix.loadFromData(buf.getvalue())
                    self.signals.qr_image.emit(pix)
                    self.signals.log.emit("QR image generated locally")
                except Exception as e:
                    self.signals.log.emit(f"Could not generate QR locally: {e}")
            else:
                self.signals.log.emit("No QR available in response")

    def _poll_devices_loop(self):
        while True:
            try:
                url = PRINT_SERVICE_API.rstrip("/") + "/api/devices/"
                r = requests.get(url, timeout=6)
                if r.status_code == 200:
                    arr = r.json()
                    status = "offline"
                    for dev in arr:
                        if dev.get("uuid") == self.device_uuid:
                            status = dev.get("status","offline")
                            break
                    self.signals.status.emit(status)
                else:
                    self.signals.log.emit(f"devices endpoint returned {r.status_code}")
            except Exception as e:
                self.signals.log.emit(f"devices poll error: {e}")
            time.sleep(POLL_DEVICES_INTERVAL)

    # -------- MQTT section ----------
    def _on_mqtt_connect(self, client, userdata, flags, rc):
        self.mqtt_connected = True
        self.signals.log.emit(f"MQTT connected (rc={rc})")
        # subscribe to commands topic
        topic = f"devices/{self.device_uuid}/commands"
        client.subscribe(topic, qos=1)
        self.signals.log.emit(f"Subscribed to {topic}")

    def _on_mqtt_disconnect(self, client, userdata, rc):
        self.mqtt_connected = False
        self.signals.log.emit("MQTT disconnected")

    def _on_mqtt_message(self, client, userdata, msg):
        try:
            payload = msg.payload.decode("utf-8")
        except Exception:
            payload = msg.payload
        self.signals.log.emit(f"MQTT msg on {msg.topic}: {str(payload)[:400]}")
        # try parse json
        try:
            job = json.loads(msg.payload.decode("utf-8","ignore"))
        except Exception:
            self.signals.log.emit("Failed to parse MQTT payload as JSON")
            return
        if job.get("command") == "print":
            threading.Thread(target=self._handle_print_job, args=(job,), daemon=True).start()

    def _mqtt_connect_loop(self):
        # connect using env vars for broker or fallback to localhost
        broker = os.getenv("MQTT_BROKER", "localhost")
        port = int(os.getenv("MQTT_PORT", "1883"))
        username = os.getenv("MQTT_USERNAME") or None
        password = os.getenv("MQTT_PASSWORD") or None

        while True:
            if mqtt is None:
                self.signals.log.emit("paho-mqtt not available; MQTT disabled")
                return
            try:
                client = mqtt.Client(client_id=self.device_uuid)
                if username:
                    client.username_pw_set(username, password)
                client.on_connect = self._on_mqtt_connect
                client.on_disconnect = self._on_mqtt_disconnect
                client.on_message = self._on_mqtt_message
                client.connect(broker, port, 60)
                self.mqtt_client = client
                client.loop_forever()
            except Exception as e:
                self.signals.log.emit(f"MQTT connection error: {e}. retry in {MQTT_RECONNECT_INTERVAL}s")
                time.sleep(MQTT_RECONNECT_INTERVAL)

    def _handle_print_job(self, job):
        url = job.get("url")
        job_id = job.get("job_id") or f"job_{int(time.time())}"
        self.signals.log.emit(f"Starting print job {job_id} from {url}")

        # download the file
        try:
            r = requests.get(url, timeout=15)
            r.raise_for_status()
            content = r.content
        except Exception as e:
            self.signals.log.emit(f"Download failed: {e}")
            return

        # Save to temp
        tmpfd = os.path.join(tempfile.gettempdir(), f"{job_id}.bin")
        try:
            with open(tmpfd, "wb") as fh:
                fh.write(content)
            self.signals.log.emit(f"Saved job to {tmpfd}")
        except Exception as e:
            self.signals.log.emit(f"Failed to save job: {e}")
            return

        # Try platform-specific print
        if IS_WINDOWS and win32print:
            try:
                # open default printer and send raw data
                printer_name = win32print.GetDefaultPrinter()
                self.signals.log.emit(f"Printing to {printer_name} (Windows raw mode)")
                # Use ShellExecute to print (works for many filetypes) - fallback raw
                try:
                    win32api.ShellExecute(0, "print", tmpfd, None, ".", 0)
                    self.signals.log.emit("Sent to printer via ShellExecute")
                except Exception as e:
                    self.signals.log.emit(f"ShellExecute print failed: {e}")
            except Exception as e:
                self.signals.log.emit(f"Windows printing failed: {e}")

        elif IS_LINUX and cups:
            try:
                conn = cups.Connection()
                printers = conn.getPrinters()
                default_printer = next(iter(printers.keys())) if printers else None
                if default_printer:
                    jobid = conn.printFile(default_printer, tmpfd, job_id, {})
                    self.signals.log.emit(f"Sent to CUPS printer {default_printer}, jobid={jobid}")
                else:
                    self.signals.log.emit("No CUPS printer found; saved file for manual printing")
            except Exception as e:
                self.signals.log.emit(f"CUPS printing error: {e}")
        else:
            self.signals.log.emit("No system printing available; file saved for manual printing")

        # publish log ack to MQTT logs topic if available
        try:
            if self.mqtt_client and self.mqtt_connected:
                topic = f"devices/{self.device_uuid}/logs"
                payload = json.dumps({"job_id": job_id, "status": "completed", "timestamp": int(time.time())})
                self.mqtt_client.publish(topic, payload, qos=1)
                self.signals.log.emit("Published completion to MQTT logs")
        except Exception as e:
            self.signals.log.emit(f"Could not publish log ack: {e}")


def main():
    app = QtWidgets.QApplication(sys.argv)
    w = DeviceGUI()
    w.show()
    sys.exit(app.exec_())

if __name__ == "__main__":
    main()
