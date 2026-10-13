"""
voice_alert_service.py
Kirim notifikasi suara ke ESP32-S3-CAM (DFPlayer Mini) setelah prediksi ML terkonfirmasi.

Tidak memblokir worker video: POST HTTPX dijalankan sebagai task asyncio.
Alamat ESP32 diambil dari IP hasil mDNS nara-camera.local atau dari VideoSource / DeviceDiscovery.
"""

from __future__ import annotations

import asyncio
import os
import time
import uuid
from typing import Any, Dict, Optional
from urllib.parse import urlsplit

import httpx

NARA_CAMERA_HOSTNAME = os.getenv("NARA_CAMERA_HOSTNAME", "nara-camera.local")
NARA_ALERT_PORT = int(os.getenv("NARA_ALERT_PORT", "82"))
NARA_ALERT_PATH = os.getenv("NARA_ALERT_PATH", "/api/alert")
NARA_ALERT_LANGUAGE = os.getenv("NARA_ALERT_LANGUAGE", "id")
NARA_ALERT_ENABLED = os.getenv("NARA_ALERT_ENABLED", "1").strip() not in ("0", "false", "False")
NARA_ALERT_CONFIRM_SEC = float(os.getenv("NARA_ALERT_CONFIRM_SEC", "2.0"))
NARA_ALERT_COOLDOWN_SEC = float(os.getenv("NARA_ALERT_COOLDOWN_SEC", "45.0"))

VALID_CODES = (
    "SLEEPING",
    "FACE_ABSENT",
    "YAWNING",
    "NOT_FOCUSED",
    "BREAK",
    "ISTIRAHAT",
    "SESSION_END",
    "WAKTU_HABIS",
    "SESSION_START",
    "MULAI",
    "START",
)
VALID_LANGUAGES = ("id", "en")


def set_alert_language(lang: str) -> str:
    global NARA_ALERT_LANGUAGE
    clean = str(lang or "").strip().lower()
    if clean in VALID_LANGUAGES:
        NARA_ALERT_LANGUAGE = clean
    return NARA_ALERT_LANGUAGE


def get_track_filename(code: str) -> str:
    if code in ("SESSION_START", "MULAI", "START"):
        return "004_mulai.mp3"
    if code in ("SESSION_END", "WAKTU_HABIS", "TIME_UP"):
        return "002_waktu_habis.mp3"
    if code in ("BREAK_START", "BREAK_END", "ISTIRAHAT", "BREAK"):
        return "003_istirahat.mp3"
    return "001_not_focus.mp3"


def get_language_folder(lang: Optional[str] = None) -> str:
    use_lang = (lang or NARA_ALERT_LANGUAGE).strip().lower()
    return "02" if use_lang == "en" else "01"


def classify_ml_event(prediction_data: Optional[Dict[str, Any]]) -> Optional[str]:
    """Petakan struktur prediksi FocusPredictor ke kode event ESP32.

    Hanya mengembalikan kode jika prediksi sudah punya sinyal yang jelas.
    Sliding window SVM (is_ready) diperlukan untuk NOT_FOCUSED.
    """
    if not prediction_data:
        return None

    label = prediction_data.get("label", -1)
    telemetry = prediction_data.get("telemetry") or {}
    face_detected = bool(telemetry.get("face_detected", False))
    is_sleeping = bool(telemetry.get("is_sleeping", False))
    is_yawning = bool(telemetry.get("is_yawning", False))
    is_ready = bool(prediction_data.get("is_ready", False))

    if is_sleeping:
        return "SLEEPING"
    if label == 1 and not face_detected:
        return "FACE_ABSENT"
    if is_yawning:
        return "YAWNING"
    if label == 1 and is_ready:
        return "NOT_FOCUSED"
    return None


class VoiceAlertGate:
    """Tahan event sampai kondisi sama bertahan confirm_sec, lalu cooldown per kode."""

    def __init__(self, confirm_sec: float = NARA_ALERT_CONFIRM_SEC, cooldown_sec: float = NARA_ALERT_COOLDOWN_SEC):
        self.confirm_sec = confirm_sec
        self.cooldown_sec = cooldown_sec
        self._pending_code: Optional[str] = None
        self._pending_since: Optional[float] = None
        self._fired_code: Optional[str] = None
        self._last_sent_at: Dict[str, float] = {}

    def evaluate(self, prediction_data: Optional[Dict[str, Any]], now: Optional[float] = None) -> Optional[Dict[str, str]]:
        now = time.time() if now is None else now
        code = classify_ml_event(prediction_data)
        if code is None:
            self._pending_code = None
            self._pending_since = None
            self._fired_code = None
            return None

        if self._fired_code == code:
            return None

        if self._pending_code != code:
            self._pending_code = code
            self._pending_since = now
            self._fired_code = None
            return None

        if self._pending_since is None or (now - self._pending_since) < self.confirm_sec:
            return None

        last = self._last_sent_at.get(code)
        if last is not None and (now - last) < self.cooldown_sec:
            return None

        event_id = f"{code}-{uuid.uuid4().hex[:12]}"
        self._last_sent_at[code] = now
        self._fired_code = code
        language = NARA_ALERT_LANGUAGE.strip().lower()
        if language not in VALID_LANGUAGES:
            language = "id"
        return {
            "event_id": event_id,
            "code": code,
            "language": language,
        }


class Esp32VoiceAlertClient:
    def __init__(self, video_source=None):
        self.video_source = video_source
        self._client: Optional[httpx.AsyncClient] = None
        self.camera_ip: Optional[str] = None
        self.last_error: Optional[str] = None
        self.last_status: Optional[int] = None
        self._resolve_fail_at = 0.0

    async def start(self) -> None:
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(2.5, connect=1.5))
        await self.refresh_camera_ip()

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def _ip_from_video_source(self) -> Optional[str]:
        if self.video_source is None:
            return None
        ip = getattr(self.video_source, "resolved_ip", None)
        if ip:
            return ip
        url = getattr(self.video_source, "resolved_http_url", None)
        if url:
            host = urlsplit(url).hostname
            if host and not host.lower().endswith(".local"):
                return host
        return None

    async def refresh_camera_ip(self, force: bool = False) -> Optional[str]:
        if self.camera_ip and not force:
            return self.camera_ip

        ip = self._ip_from_video_source()
        if not ip:
            from cv_focus_engine import VideoSource
            ip = await asyncio.to_thread(
                VideoSource._resolve_local_hostname,
                NARA_CAMERA_HOSTNAME,
                5.0,
            )
        self.camera_ip = ip
        if ip:
            print(f"[NARA-ALERT] ESP32 control IP via mDNS: {ip}:{NARA_ALERT_PORT}")
        else:
            print(f"[NARA-ALERT] Hostname {NARA_CAMERA_HOSTNAME} belum ditemukan untuk /api/alert")
        return ip

    def alert_url(self) -> Optional[str]:
        if not self.camera_ip:
            return None
        return f"http://{self.camera_ip}:{NARA_ALERT_PORT}{NARA_ALERT_PATH}"

    async def send(self, payload: Dict[str, str]) -> bool:
        if not NARA_ALERT_ENABLED:
            return False
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=httpx.Timeout(2.5, connect=1.5))

        if not self.camera_ip:
            now = time.time()
            if now - self._resolve_fail_at < 5.0:
                return False
            await self.refresh_camera_ip(force=True)
            if not self.camera_ip:
                self._resolve_fail_at = now
                self.last_error = "mdns_unresolved"
                return False

        url = self.alert_url()
        try:
            response = await self._client.post(url, json=payload)
            self.last_status = response.status_code
            if response.status_code in (200, 202):
                self.last_error = None
                print(f"[NARA-ALERT] terkirim {payload['code']} {payload['event_id']} -> {url}")
                return True
            if response.status_code == 409:
                self.last_error = "duplicate_event_id"
                return False
            self.last_error = f"http_{response.status_code}"
            print(f"[NARA-ALERT] ESP32 menolak {response.status_code}: {response.text[:180]}")
            if response.status_code in (404, 502, 503, 504):
                await self.refresh_camera_ip(force=True)
            return False
        except Exception as exc:
            self.last_error = str(exc)
            print(f"[NARA-ALERT] gagal POST {url}: {exc}")
            self.camera_ip = None
            self._resolve_fail_at = time.time()
            return False
