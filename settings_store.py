"""
settings_store.py
Penyimpanan lokal settings.json: kamera terakhir, bahasa suara, volume, dan token per device.
"""

import json
import os
import tempfile
import threading

ROOT_DIR = os.path.dirname(os.path.abspath(__file__))
SETTINGS_PATH = os.path.join(ROOT_DIR, "settings.json")

DEFAULT_CAMERA = {
    "id": "esp:nara-cam-0000",
    "kind": "esp",
    "source": "http://nara-camera.local:81/stream",
    "device_id": "nara-cam-0000",
    "ip": "",
    "name": "NARA Device",
    "stream_port": 81,
}

DEFAULTS = {
    "last_camera": DEFAULT_CAMERA,
    "language": "id",
    "volume": 25,
    "audio_enabled": True,
    "tokens": {},
}


class SettingsStore:
    def __init__(self, path=SETTINGS_PATH):
        self.path = path
        self._lock = threading.Lock()
        self._data = self._load()

    def _load(self):
        data = dict(DEFAULTS)
        data["last_camera"] = dict(DEFAULT_CAMERA)
        data["tokens"] = {}
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                raw = json.load(fh)
            if isinstance(raw, dict):
                data.update({k: raw[k] for k in data if k in raw})
                if not isinstance(data.get("last_camera"), dict):
                    data["last_camera"] = dict(DEFAULT_CAMERA)
                if not isinstance(data["tokens"], dict):
                    data["tokens"] = {}
        except (OSError, json.JSONDecodeError):
            pass
        data["language"] = data["language"] if data["language"] in ("id", "en") else "id"
        try:
            data["volume"] = max(0, min(int(data["volume"]), 30))
        except (TypeError, ValueError):
            data["volume"] = 20
        data["audio_enabled"] = bool(data["audio_enabled"])
        return data

    def _save(self):
        directory = os.path.dirname(self.path) or "."
        os.makedirs(directory, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix="settings-", suffix=".json", dir=directory)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(self._data, fh, indent=2)
            os.replace(tmp, self.path)
        except Exception:
            try:
                os.remove(tmp)
            except OSError:
                pass
            raise

    def snapshot(self):
        with self._lock:
            return json.loads(json.dumps(self._data))

    def get_last_camera(self):
        with self._lock:
            cam = self._data.get("last_camera")
            return dict(cam) if isinstance(cam, dict) else None

    def set_last_camera(self, camera):
        with self._lock:
            self._data["last_camera"] = camera
            self._save()

    def get_language(self):
        with self._lock:
            return self._data.get("language", "id")

    def get_volume(self):
        with self._lock:
            return int(self._data.get("volume", 20))

    def get_audio_enabled(self):
        with self._lock:
            return bool(self._data.get("audio_enabled", True))

    def set_audio(self, language=None, volume=None, enabled=None):
        with self._lock:
            if language in ("id", "en"):
                self._data["language"] = language
            if volume is not None:
                self._data["volume"] = max(0, min(int(volume), 30))
            if enabled is not None:
                self._data["audio_enabled"] = bool(enabled)
            self._save()
            return {
                "language": self._data["language"],
                "volume": self._data["volume"],
                "audio_enabled": self._data["audio_enabled"],
            }

    def get_token(self, device_id):
        if not device_id:
            return None
        with self._lock:
            token = self._data.get("tokens", {}).get(str(device_id))
            return token if isinstance(token, str) and token else None

    def set_token(self, device_id, token):
        """Simpan token pertama. Panggilan berikutnya tidak menimpa token yang sudah ada."""
        if not device_id or not token:
            return False
        with self._lock:
            tokens = dict(self._data.get("tokens") or {})
            key = str(device_id)
            if tokens.get(key):
                return False
            tokens[key] = str(token)
            self._data["tokens"] = tokens
            self._save()
            return True
