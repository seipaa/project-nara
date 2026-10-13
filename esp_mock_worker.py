"""
esp_mock_worker.py
Simulasi Perangkat Keras ESP32-S3-CAM + DFPlayer Mini untuk NARA Project.

Meniru sepenuhnya perilaku hardware fisik ESP32-S3:
1. Port 80 (Control Server):
   - GET  /info       : Identitas perangkat untuk probe & discovery
   - POST /pair       : Pairing TOFU (menghasilkan token 32-hex)
   - POST /play       : Memutar track audio via UART DFPlayer Mini
   - POST /volume     : Pengaturan volume (0-30)
   - GET  /status     : Status perangkat, WiFi, pairing, dan modul audio
   - GET  /capture    : Snapshot JPEG tunggal
   - POST /api/alert  : Mirror endpoint voice alert
2. Port 81 (Stream Server):
   - GET  /stream     : Video stream MJPEG real-time (boundary=naraframe)
   - GET  /capture    : Snapshot JPEG
3. Port 82 (Alert Server):
   - POST /api/alert  : REST API voice alert untuk Esp32VoiceAlertClient (dedup slot, JSON)
   - GET  /api/alert  : Status hardware DFPlayer
   - OPTIONS /api/alert : CORS preflight
4. Port 4210 (UDP Discovery):
   - Menjawab broadcast datagram "NARA_DISCOVER" dengan JSON device info
5. mDNS Announcer (_nara._tcp.local. via zeroconf jika tersedia)
6. Generator Video (Webcam lokal atau Synthetic Face dengan simulasi kedipan alami)

Dapat dijalankan langsung:
    python esp_mock_worker.py
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import os
import random
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from socketserver import ThreadingMixIn
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlparse

import cv2
import numpy as np

# Cek pustaka zeroconf
try:
    from zeroconf import IPVersion, ServiceInfo, Zeroconf
    HAS_ZEROCONF = True
except ImportError:
    HAS_ZEROCONF = False

# Audio output opsional di Windows
try:
    import winsound
    HAS_WINSOUND = True
except ImportError:
    HAS_WINSOUND = False

ROOT_DIR = os.path.dirname(os.path.abspath(__file__))
SUARA_DIR = os.path.join(ROOT_DIR, "suara")

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s][%(name)s][%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("ESP-MOCK")


def get_local_ip() -> str:
    """Mendapatkan alamat IP lokal non-loopback jika tersedia."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        try:
            return socket.gethostbyname(socket.gethostname())
        except Exception:
            return "127.0.0.1"


# =============================================================================
# 1. GENERATOR FRAME KAMERA (Webcam atau Synthetic Face)
# =============================================================================
class FrameGenerator:
    """
    Menghasilkan frame video terkompresi JPEG untuk endpoint /stream dan /capture.
    Mendukung input webcam fisik atau sintetis wajah manusia dengan simulasi kedipan.
    """

    def __init__(self, mode: str = "auto", webcam_index: int = 0, fps: int = 25):
        self.mode = mode
        self.webcam_index = webcam_index
        self.fps = fps
        self.interval = 1.0 / max(1, fps)
        self.running = False
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._latest_jpeg: bytes = b""
        self._latest_bgr: Optional[np.ndarray] = None
        self._frame_count = 0
        self._cap: Optional[cv2.VideoCapture] = None
        self._using_webcam = False

    def start(self) -> None:
        self.running = True
        self._init_source()
        self._thread = threading.Thread(target=self._run, name="esp-frame-gen", daemon=True)
        self._thread.start()
        logger.info(f"Frame generator aktif: mode={self.mode} (webcam={self._using_webcam})")

    def _init_source(self) -> None:
        if self.mode in ("auto", "webcam"):
            try:
                cap = cv2.VideoCapture(self.webcam_index)
                if cap.isOpened():
                    ret, test_frame = cap.read()
                    if ret and test_frame is not None:
                        self._cap = cap
                        self._using_webcam = True
                        logger.info(f"Webcam #{self.webcam_index} terdeteksi dan digunakan.")
                        return
                    cap.release()
            except Exception as e:
                logger.warning(f"Tidak dapat mengakses webcam #{self.webcam_index}: {e}")

        self._using_webcam = False
        if self.mode == "webcam":
            logger.warning("Webcam gagal dibuka, fallback ke synthetic face.")
        else:
            logger.info("Menggunakan synthetic student face generator.")

    def stop(self) -> None:
        self.running = False
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=1.5)
        if self._cap:
            try:
                self._cap.release()
            except Exception:
                pass
            self._cap = None

    def get_jpeg(self) -> bytes:
        with self._lock:
            return self._latest_jpeg

    def get_bgr(self) -> Optional[np.ndarray]:
        with self._lock:
            return self._latest_bgr.copy() if self._latest_bgr is not None else None

    def _run(self) -> None:
        t0 = time.time()
        while self.running:
            start_t = time.time()
            self._frame_count += 1

            if self._using_webcam and self._cap and self._cap.isOpened():
                ret, frame = self._cap.read()
                if not ret or frame is None:
                    frame = self._render_synthetic_face(time.time() - t0)
            else:
                frame = self._render_synthetic_face(time.time() - t0)

            # Encode ke JPEG
            encode_param = [int(cv2.IMWRITE_JPEG_QUALITY), 80]
            success, enc = cv2.imencode(".jpg", frame, encode_param)
            if success:
                jpeg_bytes = enc.tobytes()
                with self._lock:
                    self._latest_jpeg = jpeg_bytes
                    self._latest_bgr = frame

            elapsed = time.time() - start_t
            sleep_t = self.interval - elapsed
            if sleep_t > 0:
                time.sleep(sleep_t)

    def _render_synthetic_face(self, t: float) -> np.ndarray:
        """
        Menggambar pemandangan belajar dengan wajah siswa realistis dan kedipan mata berkala.
        """
        w, h = 640, 480
        img = np.zeros((h, w, 3), dtype=np.uint8)

        # Latar belakang meja belajar (gradien hangat)
        for y in range(h):
            ratio = y / h
            r = int(35 + 25 * ratio)
            g = int(30 + 35 * ratio)
            b = int(45 + 40 * ratio)
            img[y, :] = (b, g, r)

        # Meja belajar di bagian bawah
        desk_y = int(h * 0.72)
        cv2.rectangle(img, (0, desk_y), (w, h), (40, 60, 95), -1)
        cv2.line(img, (0, desk_y), (w, desk_y), (60, 90, 130), 2)

        # Buku terbuka di meja
        cv2.rectangle(img, (int(w * 0.28), desk_y + 10), (int(w * 0.72), h - 15), (180, 200, 215), -1)
        cv2.line(img, (w // 2, desk_y + 10), (w // 2, h - 15), (120, 140, 160), 2)

        # Gerakan kepala mikro (breathing/sway halus)
        sway_x = int(math.sin(t * 1.2) * 3)
        sway_y = int(math.cos(t * 1.5) * 2)
        cx, cy = w // 2 + sway_x, int(h * 0.42) + sway_y

        # Bahu & Tubuh
        cv2.ellipse(img, (cx, cy + 180), (160, 100), 0, 0, 360, (70, 50, 40), -1)

        # Leher
        cv2.rectangle(img, (cx - 30, cy + 70), (cx + 30, cy + 120), (170, 195, 225), -1)

        # Wajah (oval kulit)
        skin_color = (185, 210, 240)
        cv2.ellipse(img, (cx, cy), (85, 110), 0, 0, 360, skin_color, -1)

        # Rambut atas
        cv2.ellipse(img, (cx, cy - 65), (92, 60), 0, 180, 360, (25, 20, 20), -1)
        cv2.ellipse(img, (cx - 75, cy - 20), (20, 50), 0, 0, 360, (25, 20, 20), -1)
        cv2.ellipse(img, (cx + 75, cy - 20), (20, 50), 0, 0, 360, (25, 20, 20), -1)

        # Alis
        cv2.line(img, (cx - 55, cy - 28), (cx - 15, cy - 30), (40, 30, 25), 3)
        cv2.line(img, (cx + 15, cy - 30), (cx + 55, cy - 28), (40, 30, 25), 3)

        # Simulasi Kedipan Mata: kedip setiap ~3.8 detik selama 0.2 detik
        blink_cycle = t % 3.8
        is_blinking = blink_cycle < 0.22

        eye_y = cy - 10
        left_eye_x = cx - 36
        right_eye_x = cx + 36

        if is_blinking:
            # Mata terpejam (garis melengkung halus)
            cv2.ellipse(img, (left_eye_x, eye_y), (14, 3), 0, 0, 180, (40, 30, 25), 2)
            cv2.ellipse(img, (right_eye_x, eye_y), (14, 3), 0, 0, 180, (40, 30, 25), 2)
        else:
            # Mata terbuka (sklera putih + iris + pupil)
            cv2.ellipse(img, (left_eye_x, eye_y), (15, 9), 0, 0, 360, (250, 250, 250), -1)
            cv2.ellipse(img, (right_eye_x, eye_y), (15, 9), 0, 0, 360, (250, 250, 250), -1)

            # Iris & pupil fokus ke tengah sedikit ke bawah (arah materi)
            pupil_offset_y = 1
            cv2.circle(img, (left_eye_x, eye_y + pupil_offset_y), 6, (45, 30, 20), -1)
            cv2.circle(img, (right_eye_x, eye_y + pupil_offset_y), 6, (45, 30, 20), -1)
            cv2.circle(img, (left_eye_x, eye_y + pupil_offset_y), 3, (10, 10, 10), -1)
            cv2.circle(img, (right_eye_x, eye_y + pupil_offset_y), 3, (10, 10, 10), -1)
            # Pantulan cahaya
            cv2.circle(img, (left_eye_x - 2, eye_y + pupil_offset_y - 2), 1, (255, 255, 255), -1)
            cv2.circle(img, (right_eye_x - 2, eye_y + pupil_offset_y - 2), 1, (255, 255, 255), -1)

        # Hidung
        cv2.line(img, (cx, cy - 5), (cx - 3, cy + 15), (150, 175, 205), 2)
        cv2.line(img, (cx - 3, cy + 15), (cx + 5, cy + 15), (150, 175, 205), 2)

        # Mulut
        cv2.ellipse(img, (cx, cy + 40), (18, 5), 0, 0, 180, (110, 110, 170), 2)

        # Badge Overlay NARA SIMULATOR
        cv2.rectangle(img, (12, 12), (320, 52), (20, 20, 20), -1)
        cv2.rectangle(img, (12, 12), (320, 52), (0, 200, 120), 1)
        cv2.putText(
            img,
            "NARA ESP32-S3 SIMULATOR [LIVE]",
            (20, 32),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.48,
            (0, 240, 150),
            1,
            cv2.LINE_AA,
        )
        fps_text = f"FPS: {self.fps} | Mode: {'Webcam' if self._using_webcam else 'Synthetic Face'}"
        cv2.putText(
            img,
            fps_text,
            (20, 46),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.36,
            (200, 200, 200),
            1,
            cv2.LINE_AA,
        )

        return img


# =============================================================================
# 2. SIMULATOR AUDIO & DFPLAYER MINI
# =============================================================================
class DfPlayerSimulator:
    """
    Menyimulasikan modul audio serial DFPlayer Mini dengan kartu SD.
    Mendukung folder /01 (id) dan /02 (en) dengan track 1..4.
    """

    TRACK_MAP = {
        1: "001_not_focus.mp3",
        2: "002_waktu_habis.mp3",
        3: "003_istirahat.mp3",
        4: "004_mulai.mp3",
    }

    EVENT_TO_TRACK = {
        # Track 1
        "UNFOCUSED": 1,
        "NOT_FOCUSED": 1,
        "SLEEPING": 1,
        "YAWNING": 1,
        "FACE_ABSENT": 1,
        # Track 2
        "SESSION_END": 2,
        "WAKTU_HABIS": 2,
        "TIME_UP": 2,
        # Track 3
        "BREAK": 3,
        "BREAK_START": 3,
        "BREAK_END": 3,
        "ISTIRAHAT": 3,
        # Track 4
        "SESSION_START": 4,
        "MULAI": 4,
        "START": 4,
    }

    def __init__(self, audio_enabled: bool = True):
        self.audio_enabled = audio_enabled
        self.volume = 20
        self.lang = "id"
        self.last_event: Optional[str] = None
        self.last_track: Optional[int] = None
        self.last_played_at: float = 0.0
        self.history: List[Dict[str, Any]] = []
        self._lock = threading.Lock()

    def map_event_to_track(self, event_code: str) -> int:
        clean = str(event_code or "").strip().upper()
        return self.EVENT_TO_TRACK.get(clean, 0)

    def play(self, folder: int, track: int, event_name: str, language: str = "id") -> bool:
        filename = self.TRACK_MAP.get(track, f"{track:03d}.mp3")
        lang_folder = f"{folder:02d}"
        now = time.time()

        with self._lock:
            self.last_event = event_name
            self.last_track = track
            self.lang = language
            self.last_played_at = now
            entry = {
                "ts": now,
                "event": event_name,
                "folder": folder,
                "track": track,
                "filename": filename,
                "language": language,
                "volume": self.volume,
            }
            self.history.append(entry)
            if len(self.history) > 50:
                self.history.pop(0)

        logger.info(
            f"🔊 [DFPLAYER] PLAY Track {track} ({filename}) | Folder {lang_folder} ({language}) "
            f"| Event: {event_name} | Vol: {self.volume}"
        )

        # Cari path file asli jika ada
        audio_path = os.path.join(SUARA_DIR, lang_folder, filename)
        if not os.path.exists(audio_path) and filename.endswith(".mp3"):
            mpeg_path = audio_path.replace(".mp3", ".mpeg")
            if os.path.exists(mpeg_path):
                audio_path = mpeg_path

        if self.audio_enabled and os.path.exists(audio_path):
            threading.Thread(
                target=self._play_file_sound,
                args=(audio_path,),
                daemon=True,
            ).start()

        return True

    def _play_file_sound(self, path: str) -> None:
        """Memutar suara non-blocking jika platform mendukung."""
        try:
            if HAS_WINSOUND and sys.platform == "win32":
                # winsound hanya memainkan WAV native, tetapi kita bisa bersuara bip jika MP3
                winsound.MessageBeep(winsound.MB_ICONASTERISK)
        except Exception:
            pass


# =============================================================================
# 3. HTTP SERVER HANDLERS (PORT 80, 81, 82)
# =============================================================================
class MockControlHandler(BaseHTTPRequestHandler):
    """
    Menangani Port 80 (Control & Info) untuk EspBridge & VideoSource.
    """

    server_ref: Any = None

    def log_message(self, format, *args):
        # Meredam logging berisik HTTP standar
        pass

    def _send_json(self, status: int, data: Dict[str, Any]) -> None:
        raw = json.dumps(data).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-NARA-Token")
        self.end_headers()
        self.wfile.write(raw)

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-NARA-Token")
        self.end_headers()

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/")

        if path in ("", "/info"):
            worker = self.server_ref.worker
            self._send_json(200, {
                "id": worker.device_id,
                "name": worker.device_name,
                "fw": "1.0.0",
                "paired": worker.is_paired(),
                "stream_port": worker.stream_port,
            })
            return

        if path == "/status":
            worker = self.server_ref.worker
            df = worker.dfplayer
            self._send_json(200, {
                "ok": True,
                "wifi": True,
                "ip": worker.ip_address,
                "paired": worker.is_paired(),
                "volume": df.volume,
                "last_event": df.last_event or "",
                "lang": df.lang,
                "mp3_ok": True,
                "stream_port": worker.stream_port,
            })
            return

        if path == "/capture":
            worker = self.server_ref.worker
            jpeg = worker.frame_gen.get_jpeg()
            self.send_response(200)
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("Content-Length", str(len(jpeg)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(jpeg)
            return

        if path == "/api/alert":
            worker = self.server_ref.worker
            self._send_json(200, {
                "ok": True,
                "dfplayer": True,
                "queue": 0,
                "volume": worker.dfplayer.volume,
                "last_event": worker.dfplayer.last_event or "",
            })
            return

        if path == "/mock/events":
            worker = self.server_ref.worker
            self._send_json(200, {
                "ok": True,
                "history": worker.dfplayer.history,
            })
            return

        self._send_json(404, {"error": "not_found"})

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/")
        worker = self.server_ref.worker

        content_len = int(self.headers.get("Content-Length", 0))
        body_raw = self.rfile.read(content_len).decode("utf-8") if content_len > 0 else "{}"
        try:
            body = json.loads(body_raw) if body_raw.strip() else {}
        except json.JSONDecodeError:
            self._send_json(400, {"error": "bad_json"})
            return

        if path == "/pair":
            token = worker.generate_token()
            logger.info(f"🔑 [MOCK-ESP] Pairing baru diterima -> Token: {token}")
            self._send_json(200, {"token": token})
            return

        if path == "/volume":
            token_in = self.headers.get("X-NARA-Token", "")
            if not worker.verify_token(token_in):
                self._send_json(403, {"error": "unauthorized"})
                return
            vol = body.get("volume")
            if vol is None or not isinstance(vol, (int, float)):
                self._send_json(400, {"error": "bad_volume"})
                return
            vol = max(0, min(30, int(vol)))
            worker.dfplayer.volume = vol
            logger.info(f"🔊 [MOCK-ESP] Volume diubah ke {vol}")
            self._send_json(200, {"ok": True, "volume": vol})
            return

        if path == "/play":
            token_in = self.headers.get("X-NARA-Token", "")
            if not worker.verify_token(token_in):
                self._send_json(403, {"error": "unauthorized"})
                return
            lang = body.get("lang", "id")
            event = body.get("event", "")
            folder = 2 if str(lang).lower() == "en" else 1
            track = worker.dfplayer.map_event_to_track(event)
            if track == 0:
                self._send_json(400, {"error": "bad_event"})
                return
            worker.dfplayer.play(folder, track, event, language=lang)
            self._send_json(200, {
                "ok": True,
                "lang": lang,
                "event": event,
                "folder": folder,
                "track": track,
            })
            return

        if path == "/api/alert":
            self._handle_alert_payload(body)
            return

        self._send_json(404, {"error": "not_found"})

    def _handle_alert_payload(self, body: Dict[str, Any]) -> None:
        worker = self.server_ref.worker
        event_id = str(body.get("event_id") or "")
        code = str(body.get("code") or "")
        language = str(body.get("language") or "id").lower()

        if not event_id or not code:
            self._send_json(400, {"error": "missing_fields"})
            return

        if worker.is_alert_duplicate(event_id):
            self._send_json(409, {"ok": False, "error": "duplicate_event_id"})
            return

        folder = 2 if language == "en" else 1
        track = worker.dfplayer.map_event_to_track(code)
        if track == 0:
            self._send_json(400, {"ok": False, "error": "unsupported_code_or_language"})
            return

        worker.dfplayer.play(folder, track, code, language=language)
        self._send_json(202, {
            "ok": True,
            "queued": True,
            "folder": folder,
            "file": track,
            "dfplayer": True,
        })


class MockStreamHandler(BaseHTTPRequestHandler):
    """
    Menangani Port 81 (MJPEG Stream Server /stream & /capture).
    """

    server_ref: Any = None

    def log_message(self, format, *args):
        pass

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/")
        worker = self.server_ref.worker

        if path == "/capture":
            jpeg = worker.frame_gen.get_jpeg()
            self.send_response(200)
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("Content-Length", str(len(jpeg)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(jpeg)
            return

        if path in ("/stream", ""):
            self.send_response(200)
            self.send_header("Content-Type", "multipart/x-mixed-replace;boundary=naraframe")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Cache-Control", "no-cache, private")
            self.end_headers()

            boundary = b"\r\n--naraframe\r\n"
            try:
                while worker.running:
                    jpeg = worker.frame_gen.get_jpeg()
                    if not jpeg:
                        time.sleep(0.04)
                        continue

                    header = (
                        f"Content-Type: image/jpeg\r\n"
                        f"Content-Length: {len(jpeg)}\r\n\r\n"
                    ).encode("ascii")

                    self.wfile.write(header)
                    self.wfile.write(jpeg)
                    self.wfile.write(boundary)
                    time.sleep(1.0 / max(1, worker.frame_gen.fps))
            except (ConnectionResetError, BrokenPipeError):
                pass
            return

        self.send_response(404)
        self.end_headers()


class MockAlertHandler(BaseHTTPRequestHandler):
    """
    Menangani Port 82 (/api/alert) untuk Esp32VoiceAlertClient.
    """

    server_ref: Any = None

    def log_message(self, format, *args):
        pass

    def _send_json(self, status: int, data: Dict[str, Any]) -> None:
        raw = json.dumps(data).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()
        self.wfile.write(raw)

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/")
        if path == "/api/alert":
            worker = self.server_ref.worker
            self._send_json(200, {
                "ok": True,
                "dfplayer": True,
                "queue": 0,
                "rx": 47,
                "tx": 21,
                "port": worker.alert_port,
            })
            return
        self._send_json(404, {"error": "not_found"})

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/")
        worker = self.server_ref.worker

        if path != "/api/alert":
            self._send_json(404, {"error": "not_found"})
            return

        content_len = int(self.headers.get("Content-Length", 0))
        body_raw = self.rfile.read(content_len).decode("utf-8") if content_len > 0 else "{}"
        try:
            body = json.loads(body_raw)
        except json.JSONDecodeError:
            self._send_json(400, {"ok": False, "error": "invalid_json"})
            return

        event_id = str(body.get("event_id") or "")
        code = str(body.get("code") or "")
        language = str(body.get("language") or "id").lower()

        if not event_id or not code:
            self._send_json(400, {"ok": False, "error": "missing_fields"})
            return

        if worker.is_alert_duplicate(event_id):
            self._send_json(409, {"ok": False, "error": "duplicate_event_id"})
            return

        folder = 2 if language == "en" else 1
        track = worker.dfplayer.map_event_to_track(code)
        if track == 0:
            self._send_json(400, {"ok": False, "error": "unsupported_code_or_language"})
            return

        worker.dfplayer.play(folder, track, code, language=language)
        self._send_json(202, {
            "ok": True,
            "queued": True,
            "folder": folder,
            "file": track,
            "dfplayer": True,
        })


# =============================================================================
# 4. MASTER WORKER & SERVER ORCHESTRATOR
# =============================================================================
class CustomServer(ThreadingHTTPServer):
    def __init__(self, server_address, RequestHandlerClass, worker):
        self.worker = worker
        RequestHandlerClass.server_ref = self
        super().__init__(server_address, RequestHandlerClass)


class EspMockWorker:
    """
    Simulator lengkap ESP32-S3-CAM + DFPlayer Mini.
    """

    def __init__(
        self,
        device_id: str = "nara-cam-mock",
        device_name: str = "NARA Cam Simulator",
        http_port: int = 80,
        stream_port: int = 81,
        alert_port: int = 82,
        udp_port: int = 4210,
        video_mode: str = "auto",
        webcam_index: int = 0,
        audio_enabled: bool = True,
        bind_host: str = "0.0.0.0",
    ):
        self.device_id = device_id
        self.device_name = device_name
        self.http_port = http_port
        self.stream_port = stream_port
        self.alert_port = alert_port
        self.udp_port = udp_port
        self.bind_host = bind_host
        self.ip_address = get_local_ip()

        self.running = False
        self.token: Optional[str] = None
        self._recent_alert_ids: List[str] = []
        self._lock = threading.Lock()

        # Komponen
        self.frame_gen = FrameGenerator(mode=video_mode, webcam_index=webcam_index)
        self.dfplayer = DfPlayerSimulator(audio_enabled=audio_enabled)

        # Server sockets
        self._server_80: Optional[CustomServer] = None
        self._server_81: Optional[CustomServer] = None
        self._server_82: Optional[CustomServer] = None
        self._udp_sock: Optional[socket.socket] = None
        self._zeroconf: Optional[Zeroconf] = None
        self._mdns_info: Optional[ServiceInfo] = None

    def generate_token(self) -> str:
        with self._lock:
            # Token 32-hex persis logika ESP makeToken()
            self.token = "".join(random.choices("0123456789abcdef", k=32))
            return self.token

    def is_paired(self) -> bool:
        with self._lock:
            return bool(self.token)

    def verify_token(self, token: str) -> bool:
        with self._lock:
            if not token:
                return False
            if not self.token:
                # TOFU: Adopsi token yang dikirimkan client jika mock baru mulai
                self.token = token
                return True
            return self.token == token

    def is_alert_duplicate(self, event_id: str) -> bool:
        with self._lock:
            if event_id in self._recent_alert_ids:
                return True
            self._recent_alert_ids.append(event_id)
            if len(self._recent_alert_ids) > 16:
                self._recent_alert_ids.pop(0)
            return False

    def start(self) -> None:
        self.running = True
        logger.info("==================================================")
        logger.info(f"🚀 Memulai ESP32-S3 Simulator: {self.device_name} ({self.device_id})")
        logger.info(f"📡 IP Lokal: {self.ip_address}")
        logger.info(f"🌐 HTTP Kontrol : {self.bind_host}:{self.http_port}")
        logger.info(f"📷 Stream MJPEG : {self.bind_host}:{self.stream_port}/stream")
        logger.info(f"📢 Voice Alert  : {self.bind_host}:{self.alert_port}/api/alert")
        logger.info(f"🔍 UDP Discovery: port {self.udp_port}")
        logger.info("==================================================")

        # 1. Jalankan generator frame kamera
        self.frame_gen.start()

        # 2. Jalankan HTTP Port 80
        self._start_http_80()

        # 3. Jalankan HTTP Port 81
        self._start_http_81()

        # 4. Jalankan HTTP Port 82
        self._start_http_82()

        # 5. Jalankan UDP Discovery Listener
        self._start_udp_listener()

        # 6. Daftarkan mDNS jika tersedia
        self._start_mdns()

        logger.info("✅ Semua layanan ESP Simulator siap melayani permintaan NARA!")

    def stop(self) -> None:
        logger.info("Menghentikan ESP32 Simulator...")
        self.running = False

        if self._zeroconf and self._mdns_info:
            try:
                self._zeroconf.unregister_service(self._mdns_info)
                self._zeroconf.close()
            except Exception:
                pass

        if self._udp_sock:
            try:
                self._udp_sock.close()
            except Exception:
                pass

        for s in (self._server_80, self._server_81, self._server_82):
            if s:
                try:
                    s.shutdown()
                    s.server_close()
                except Exception:
                    pass

        self.frame_gen.stop()
        logger.info("ESP32 Simulator telah dinonaktifkan.")

    def _start_http_80(self) -> None:
        try:
            self._server_80 = CustomServer((self.bind_host, self.http_port), MockControlHandler, self)
            threading.Thread(target=self._server_80.serve_forever, name="esp-http-80", daemon=True).start()
            logger.info(f"Server Port {self.http_port} (Kontrol/Info) aktif.")
        except Exception as e:
            logger.error(f"Gagal bind Port {self.http_port}: {e}")

    def _start_http_81(self) -> None:
        try:
            self._server_81 = CustomServer((self.bind_host, self.stream_port), MockStreamHandler, self)
            threading.Thread(target=self._server_81.serve_forever, name="esp-http-81", daemon=True).start()
            logger.info(f"Server Port {self.stream_port} (MJPEG Stream) aktif.")
        except Exception as e:
            logger.error(f"Gagal bind Port {self.stream_port}: {e}")

    def _start_http_82(self) -> None:
        try:
            self._server_82 = CustomServer((self.bind_host, self.alert_port), MockAlertHandler, self)
            threading.Thread(target=self._server_82.serve_forever, name="esp-http-82", daemon=True).start()
            logger.info(f"Server Port {self.alert_port} (Voice Alert) aktif.")
        except Exception as e:
            logger.error(f"Gagal bind Port {self.alert_port}: {e}")

    def _start_udp_listener(self) -> None:
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            sock.bind(("", self.udp_port))
            self._udp_sock = sock
            threading.Thread(target=self._udp_loop, name="esp-udp-disc", daemon=True).start()
            logger.info(f"UDP Discovery Listener aktif pada port {self.udp_port}.")
        except Exception as e:
            logger.error(f"Gagal bind UDP Port {self.udp_port}: {e}")

    def _udp_loop(self) -> None:
        sock = self._udp_sock
        if not sock:
            return
        while self.running:
            try:
                data, addr = sock.recvfrom(1024)
                if not data:
                    continue
                text = data.decode("utf-8", errors="ignore").strip()
                if "NARA_DISCOVER" in text:
                    reply_dict = {
                        "id": self.device_id,
                        "name": self.device_name,
                        "ip": self.ip_address,
                        "stream_port": self.stream_port,
                        "paired": self.is_paired(),
                        "fw": "1.0.0",
                    }
                    reply_bytes = json.dumps(reply_dict).encode("utf-8")
                    sock.sendto(reply_bytes, addr)
                    logger.info(f"📡 [UDP-DISCOVER] Menjawab probe dari {addr[0]}:{addr[1]}")
            except Exception:
                if not self.running:
                    break
                time.sleep(0.1)

    def _start_mdns(self) -> None:
        if not HAS_ZEROCONF:
            logger.info("Zeroconf tidak tersedia. Pengumuman mDNS dilewati (UDP aktif).")
            return
        try:
            self._zeroconf = Zeroconf(ip_version=IPVersion.V4Only)
            desc = {
                "id": self.device_id,
                "stream_port": str(self.stream_port),
                "fw": "1.0.0",
                "paired": "1" if self.is_paired() else "0",
            }
            type_name = "_nara._tcp.local."
            service_name = f"{self.device_id}.{type_name}"
            ip_bytes = socket.inet_aton(self.ip_address)

            self._mdns_info = ServiceInfo(
                type_name,
                service_name,
                addresses=[ip_bytes],
                port=self.http_port,
                properties=desc,
                server=f"{self.device_id}.local.",
            )
            self._zeroconf.register_service(self._mdns_info)
            logger.info(f"mDNS disiarkan: {service_name} -> {self.ip_address}:{self.http_port}")
        except Exception as e:
            logger.warning(f"Gagal mengumumkan mDNS: {e}")


# =============================================================================
# 5. ENTRY POINT CLI
# =============================================================================
def main():
    parser = argparse.ArgumentParser(description="NARA ESP32-S3 Hardware Mock Simulator Worker")
    parser.add_argument("--id", default="nara-cam-mock", help="Device ID (default: nara-cam-mock)")
    parser.add_argument("--name", default="NARA Cam Simulator", help="Nama perangkat")
    parser.add_argument("--http-port", type=int, default=80, help="Port HTTP Kontrol (default: 80)")
    parser.add_argument("--stream-port", type=int, default=81, help="Port MJPEG Stream (default: 81)")
    parser.add_argument("--alert-port", type=int, default=82, help="Port Alert REST (default: 82)")
    parser.add_argument("--udp-port", type=int, default=4210, help="Port UDP Discovery (default: 4210)")
    parser.add_argument(
        "--video-mode",
        choices=["auto", "webcam", "synthetic"],
        default="auto",
        help="Mode input video (auto/webcam/synthetic)",
    )
    parser.add_argument("--webcam-index", type=int, default=0, help="Indeks webcam (default: 0)")
    parser.add_argument("--no-audio", action="store_true", help="Nonaktifkan pemutaran audio lokal")
    args = parser.parse_args()

    worker = EspMockWorker(
        device_id=args.id,
        device_name=args.name,
        http_port=args.http_port,
        stream_port=args.stream_port,
        alert_port=args.alert_port,
        udp_port=args.udp_port,
        video_mode=args.video_mode,
        webcam_index=args.webcam_index,
        audio_enabled=not args.no_audio,
    )

    try:
        worker.start()
        print("\nSimulator berjalan. Tekan Ctrl+C untuk berhenti.\n")
        while True:
            time.sleep(1.0)
    except KeyboardInterrupt:
        print("\nMenerima sinyal berhenti...")
    finally:
        worker.stop()


if __name__ == "__main__":
    main()
