"""
web_dashboard/app.py
Backend Server FastAPI untuk Dashboard Pemantauan Atensi Siswa & Adaptive Pomodoro NARA Project.

Menyediakan:
1. Web Interface (HTML5, Vanilla CSS, JS)
2. Live MJPEG Video Stream (/video_feed) dengan Real-time Computer Vision Overlay
3. WebSocket Telemetry (/ws) untuk update instan metrik ML (SVM), EAR, MAR, Gaze, dan Timer Pomodoro
4. REST API untuk kontrol Pomodoro (/api/pomodoro/...) & riwayat sesi SQLite
5. REST API untuk konfigurasi pomodoro (/api/config) & env settings
6. Pemilihan kamera (webcam / ESP) dan jembatan audio ke modul MP3

Environment Variables:
  CAMERA_SOURCE          : opsional, dipakai hanya jika --source tidak diisi dan settings kosong
  TELEMETRY_INTERVAL_SEC : Interval update telemetry ke WebSocket (default: 1.0 detik)
"""

import asyncio
import ipaddress
import json
import logging
import os
import sys
import time
from typing import List, Optional
from contextlib import asynccontextmanager
from urllib.parse import urlparse

import cv2
import httpx
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
from fastapi.responses import HTMLResponse, StreamingResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

# Tambahkan root path agar bisa import modul root
ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

from cv_focus_engine import VideoSource
from device_discovery import DeviceDiscovery, probe_webcams
from esp_bridge import EspBridge
from focus_predictor import FocusPredictor
from pomodoro_service import PomodoroService
from settings_store import SettingsStore

logger = logging.getLogger("nara")

# Paths setup
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, "static")
TEMPLATES_DIR = os.path.join(BASE_DIR, "templates")

os.makedirs(STATIC_DIR, exist_ok=True)
os.makedirs(TEMPLATES_DIR, exist_ok=True)

DEFAULT_CAMERA_SOURCE = os.getenv("CAMERA_SOURCE", "0")
TELEMETRY_INTERVAL_SEC = float(os.getenv("TELEMETRY_INTERVAL_SEC", "1.0"))
MAX_MJPEG_CLIENTS = 3
GENERIC_ERROR = "Terjadi kesalahan pada server."

# Shared Global State
video_source = None
predictor = None
pomodoro = None
settings_store = None
discovery = None
bridge = None
connected_websockets: List[WebSocket] = []
current_frame_bytes = None
last_prediction = None
_telemetry_interval = TELEMETRY_INTERVAL_SEC
_active_source = "0"
_video_clients = 0
_video_lock = asyncio.Lock()
_webcam_cache = {"ts": 0.0, "items": []}


def _resolve_startup_source(settings: SettingsStore):
    """--source menang. Tanpa argumen: kamera terakhir di settings, lalu webcam 0."""
    if os.environ.get("NARA_SOURCE_OVERRIDE") == "1":
        return os.environ.get("CAMERA_SOURCE", "0"), None
    last = settings.get_last_camera()
    if isinstance(last, dict) and last.get("source"):
        return str(last["source"]), last
    env_source = os.getenv("CAMERA_SOURCE")
    if env_source:
        return str(env_source), None
    return "0", None


def _active_webcam_index():
    text = str(_active_source).strip()
    if text.isdigit():
        return int(text)
    return None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global video_source, predictor, pomodoro, settings_store, discovery, bridge
    global _telemetry_interval, _active_source, last_prediction
    settings_store = SettingsStore()
    _telemetry_interval = float(os.getenv("TELEMETRY_INTERVAL_SEC", "1.0"))
    source, last_camera = _resolve_startup_source(settings_store)
    _active_source = source
    print(f"[STARTUP] Menginisialisasi Video Source [{source}], Focus Predictor & Pomodoro Service...")
    print(f"[STARTUP] Telemetry interval: {_telemetry_interval}s")
    discovery = DeviceDiscovery()
    discovery.start()
    video_source = VideoSource(source)
    predictor = FocusPredictor()
    pomodoro = PomodoroService()
    bridge = EspBridge(pomodoro, settings_store)
    loop = asyncio.get_running_loop()
    bridge.bind_loop(loop)
    bridge.start()
    if isinstance(last_camera, dict) and last_camera.get("kind") == "esp" and last_camera.get("ip"):
        await bridge.attach(last_camera)

    worker_task = asyncio.create_task(video_processing_worker())
    tick_task = asyncio.create_task(pomodoro_tick_loop())
    yield

    worker_task.cancel()
    tick_task.cancel()
    if bridge is not None:
        await bridge.close()
    if discovery is not None:
        discovery.close()
    if video_source is not None:
        video_source.release()
    print("[SHUTDOWN] Kamera dan resources dilepaskan.")


app = FastAPI(
    title="Student Focus & Adaptive Pomodoro Monitoring Engine",
    lifespan=lifespan
)

app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
templates = Jinja2Templates(directory=TEMPLATES_DIR)


def _origin_allowed(headers) -> bool:
    host = (headers.get("host") or "").strip().lower()
    if not host:
        return False
    hostname = host.split(":")[0]
    bind = os.environ.get("NARA_BIND_HOST", "127.0.0.1").strip().lower()
    if bind in ("127.0.0.1", "localhost", "::1"):
        if hostname not in ("127.0.0.1", "localhost", "::1"):
            return False
    origin = headers.get("origin")
    if origin is None or origin == "":
        return True
    if origin.strip().lower() == "null":
        return False
    parsed = urlparse(origin)
    if parsed.scheme not in ("http", "https"):
        return False
    return (parsed.netloc or "").lower() == host


def _viewer_allowed(headers) -> bool:
    """Halaman yang sama boleh melihat stream. Origin asing dan img lintas situs tidak."""
    if not _origin_allowed(headers):
        return False
    site = (headers.get("sec-fetch-site") or "").strip().lower()
    if site in ("cross-site", "same-site"):
        return False
    referer = (headers.get("referer") or "").strip()
    if referer:
        host = (headers.get("host") or "").strip().lower()
        if (urlparse(referer).netloc or "").lower() != host:
            return False
    return True


@app.middleware("http")
async def same_origin_guard(request: Request, call_next):
    if request.method == "POST" and not _origin_allowed(request.headers):
        return JSONResponse({"ok": False, "error": "Forbidden"}, status_code=403)
    return await call_next(request)


@app.exception_handler(Exception)
async def unhandled_exception(request: Request, exc: Exception):
    from fastapi import HTTPException
    from fastapi.exception_handlers import http_exception_handler
    if isinstance(exc, HTTPException):
        return await http_exception_handler(request, exc)
    logger.exception("Permintaan gagal: %s", request.url.path)
    return JSONResponse({"ok": False, "error": GENERIC_ERROR}, status_code=500)


def _fail():
    return JSONResponse({"ok": False, "error": GENERIC_ERROR}, status_code=500)


def _device_status():
    if bridge is None:
        return None
    return bridge.public_status()


async def _broadcast(payload):
    dead_sockets = []
    for ws in list(connected_websockets):
        try:
            await ws.send_text(json.dumps(payload))
        except Exception:
            dead_sockets.append(ws)
    for dead in dead_sockets:
        if dead in connected_websockets:
            connected_websockets.remove(dead)


async def video_processing_worker():
    """Tangkap frame dan jalankan ML di thread lain supaya event loop tetap bebas."""
    global current_frame_bytes, last_prediction, _telemetry_interval
    last_telemetry_time = 0.0

    while True:
        try:
            if video_source is None or predictor is None or pomodoro is None:
                await asyncio.sleep(0.05)
                continue

            ok, frame = await asyncio.to_thread(video_source.read)
            if not ok or frame is None:
                await asyncio.sleep(0.05)
                continue

            mode = pomodoro.mode
            if mode in ("BREAK", "LONG_BREAK"):
                await asyncio.sleep(0.2)
                continue

            annotated_frame, prediction_data = await asyncio.to_thread(predictor.process_frame, frame)
            last_prediction = prediction_data
            pomodoro_data = await asyncio.to_thread(pomodoro.update_telemetry, prediction_data)

            ret, buffer = cv2.imencode(".jpg", annotated_frame, [cv2.IMWRITE_JPEG_QUALITY, 75])
            if ret:
                current_frame_bytes = buffer.tobytes()

            now = time.time()
            if now - last_telemetry_time >= _telemetry_interval:
                last_telemetry_time = now
                await _broadcast({
                    "timestamp": now,
                    "prediction": prediction_data,
                    "pomodoro": pomodoro_data,
                    "device": _device_status(),
                })

            await asyncio.sleep(0.01)

        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Worker video gagal")
            await asyncio.sleep(0.05)


async def pomodoro_tick_loop():
    """Timer 1 Hz. Tetap jalan saat kamera dimatikan selama istirahat."""
    while True:
        try:
            if pomodoro is None:
                await asyncio.sleep(1.0)
                continue
            status = await asyncio.to_thread(pomodoro.tick)
            await _broadcast({
                "timestamp": time.time(),
                "prediction": last_prediction,
                "pomodoro": status,
                "device": _device_status(),
            })
            await asyncio.sleep(1.0)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Tick pomodoro gagal")
            await asyncio.sleep(1.0)


async def mjpeg_frames():
    global _video_clients
    try:
        while True:
            frame = current_frame_bytes
            if frame is not None:
                yield (
                    b"--frame\r\n"
                    b"Content-Type: image/jpeg\r\n\r\n" + frame + b"\r\n"
                )
            await asyncio.sleep(0.03)
    finally:
        async with _video_lock:
            _video_clients = max(0, _video_clients - 1)


# ---------------------------------------------------------------------------
# ROUTES
# ---------------------------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    """Halaman utama Dashboard."""
    return templates.TemplateResponse(request=request, name="index.html")


@app.get("/video_feed")
async def video_feed(request: Request):
    """Stream MJPEG. Jumlah klien dibatasi supaya capture tidak kebanjiran."""
    global _video_clients
    if not _viewer_allowed(request.headers):
        return JSONResponse({"ok": False, "error": "Forbidden"}, status_code=403)
    async with _video_lock:
        if _video_clients >= MAX_MJPEG_CLIENTS:
            return JSONResponse({"ok": False, "error": "Stream penuh."}, status_code=503)
        _video_clients += 1
    return StreamingResponse(
        mjpeg_frames(),
        media_type="multipart/x-mixed-replace; boundary=frame"
    )


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    """WebSocket telemetry. Origin asing ditolak sebelum sesi dipakai."""
    if not _viewer_allowed(websocket.headers):
        await websocket.close(code=1008)
        return
    await websocket.accept()
    connected_websockets.append(websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        if websocket in connected_websockets:
            connected_websockets.remove(websocket)
    except Exception:
        logger.exception("WebSocket terputus")
        if websocket in connected_websockets:
            connected_websockets.remove(websocket)


# ---------------------------------------------------------------------------
# REST API CONTROLLERS FOR POMODORO
# ---------------------------------------------------------------------------
@app.post("/api/pomodoro/start")
async def api_pomodoro_start(mode: str = "STUDY", subject: Optional[str] = None):
    try:
        status = await asyncio.to_thread(pomodoro.start, mode, subject)
        return JSONResponse(status)
    except Exception:
        logger.exception("start pomodoro")
        return _fail()


@app.post("/api/pomodoro/pause")
async def api_pomodoro_pause():
    try:
        return JSONResponse(await asyncio.to_thread(pomodoro.pause))
    except Exception:
        logger.exception("pause pomodoro")
        return _fail()


@app.post("/api/pomodoro/resume")
async def api_pomodoro_resume():
    try:
        return JSONResponse(await asyncio.to_thread(pomodoro.resume))
    except Exception:
        logger.exception("resume pomodoro")
        return _fail()


@app.post("/api/pomodoro/reset")
async def api_pomodoro_reset():
    try:
        return JSONResponse(await asyncio.to_thread(pomodoro.reset))
    except Exception:
        logger.exception("reset pomodoro")
        return _fail()


@app.post("/api/pomodoro/end")
async def api_pomodoro_end():
    try:
        return JSONResponse(await asyncio.to_thread(pomodoro.end_session))
    except Exception:
        logger.exception("end pomodoro")
        return _fail()


@app.post("/api/pomodoro/skip")
async def api_pomodoro_skip():
    try:
        return JSONResponse(await asyncio.to_thread(pomodoro.skip))
    except Exception:
        logger.exception("skip pomodoro")
        return _fail()


@app.post("/api/pomodoro/accept_break")
async def api_pomodoro_accept_break():
    try:
        return JSONResponse(await asyncio.to_thread(pomodoro.accept_suggested_break))
    except Exception:
        logger.exception("accept break")
        return _fail()


@app.post("/api/pomodoro/dismiss_alert")
async def api_pomodoro_dismiss_alert():
    try:
        return JSONResponse(await asyncio.to_thread(pomodoro.dismiss_alert))
    except Exception:
        logger.exception("dismiss alert")
        return _fail()


@app.post("/api/pomodoro/configure")
async def api_pomodoro_configure(request: Request):
    """
    Konfigurasi durasi sesi Pomodoro secara dinamis.
    Body JSON: { "study_minutes": 25, "break_minutes": 5, "long_break_minutes": 15 }
    """
    try:
        body = await request.json()
        study_min = int(body.get("study_minutes", 25))
        break_min = int(body.get("break_minutes", 5))
        long_break_min = int(body.get("long_break_minutes", 15))
        result = await asyncio.to_thread(pomodoro.configure, study_min, break_min, long_break_min)
        return JSONResponse(result)
    except Exception:
        logger.exception("configure pomodoro")
        return JSONResponse({"ok": False, "error": GENERIC_ERROR}, status_code=400)


def _selected_id():
    if settings_store is None:
        return None
    last = settings_store.get_last_camera()
    if isinstance(last, dict):
        return last.get("id")
    return None


def _audio_fields():
    if settings_store is None:
        return {"language": "id", "volume": 20, "audio_enabled": True}
    return {
        "language": settings_store.get_language(),
        "volume": settings_store.get_volume(),
        "audio_enabled": settings_store.get_audio_enabled(),
    }


@app.get("/api/cameras")
async def api_cameras():
    """Webcam lokal 0-3 plus ESP yang ditemukan di jaringan."""
    try:
        now = time.time()
        active = _active_webcam_index()
        if now - _webcam_cache["ts"] > 8.0 or not _webcam_cache["items"]:
            items = await asyncio.to_thread(probe_webcams, range(4), active)
            _webcam_cache["ts"] = now
            _webcam_cache["items"] = items
        else:
            items = []
            for cam in _webcam_cache["items"]:
                row = dict(cam)
                if active is not None and row.get("index") == active:
                    row["available"] = True
                items.append(row)
        cameras = list(items)
        if discovery is not None:
            for dev in discovery.list_devices():
                cameras.append({
                    "id": f"esp:{dev['id']}",
                    "name": dev.get("name") or dev["id"],
                    "kind": "esp",
                    "source": dev.get("stream_url"),
                    "ip": dev.get("ip"),
                    "device_id": dev.get("id"),
                    "stream_port": dev.get("stream_port"),
                    "paired": dev.get("paired"),
                    "available": True,
                    "last_seen": dev.get("last_seen"),
                })
        body = {"cameras": cameras, "selected_id": _selected_id()}
        body.update(_audio_fields())
        return JSONResponse(body)
    except Exception:
        logger.exception("daftar kamera")
        return _fail()


def _remember_camera(record):
    settings_store.set_last_camera(record)
    _webcam_cache["ts"] = 0.0


@app.post("/api/camera/select")
async def api_camera_select(request: Request):
    global _active_source
    try:
        body = await request.json()
        camera_id = str(body.get("id") or "").strip()
        if camera_id.startswith("webcam:"):
            index = int(camera_id.split(":", 1)[1])
            if index < 0 or index > 3:
                return JSONResponse({"ok": False, "error": "Kamera tidak ditemukan."}, status_code=400)
            source = str(index)
            await asyncio.to_thread(video_source.switch, source)
            _active_source = source
            if bridge is not None:
                bridge.clear_device()
            record = {"id": camera_id, "kind": "webcam", "source": source, "index": index}
            _remember_camera(record)
            return JSONResponse({"ok": True, "camera": record, "device": _device_status()})
        if camera_id.startswith("esp:"):
            device_id = camera_id.split(":", 1)[1]
            device = discovery.get(device_id) if discovery is not None else None
            if device is None:
                return JSONResponse({"ok": False, "error": "Kamera tidak ditemukan."}, status_code=404)
            await asyncio.to_thread(video_source.switch, device["stream_url"])
            _active_source = device["stream_url"]
            if bridge is not None:
                await bridge.attach(device)
            record = {
                "id": camera_id,
                "kind": "esp",
                "source": device["stream_url"],
                "device_id": device["id"],
                "ip": device["ip"],
                "name": device.get("name"),
                "stream_port": device.get("stream_port"),
            }
            _remember_camera(record)
            return JSONResponse({"ok": True, "camera": record, "device": _device_status()})
        return JSONResponse({"ok": False, "error": "Kamera tidak ditemukan."}, status_code=400)
    except Exception:
        logger.exception("pilih kamera")
        return JSONResponse({"ok": False, "error": GENERIC_ERROR}, status_code=400)


@app.post("/api/devices/rescan")
async def api_devices_rescan():
    try:
        found = 0
        if discovery is not None:
            found = await asyncio.to_thread(discovery.probe_udp)
        return JSONResponse({"ok": True, "found": found, "devices": discovery.list_devices() if discovery else []})
    except Exception:
        logger.exception("rescan perangkat")
        return _fail()


@app.post("/api/devices/manual")
async def api_devices_manual(request: Request):
    try:
        body = await request.json()
        raw_ip = str(body.get("ip") or "").strip()
        try:
            ip = str(ipaddress.ip_address(raw_ip))
        except ValueError:
            return JSONResponse({"ok": False, "error": "Alamat IP tidak valid."}, status_code=400)
        if discovery is None:
            return _fail()
        info = await _fetch_device_info(ip)
        if info is None:
            return JSONResponse({"ok": False, "error": "Perangkat tidak menjawab."}, status_code=404)
        record = discovery.add_manual_ip(ip, info)
        return JSONResponse({"ok": True, "device": record})
    except Exception:
        logger.exception("tambah IP manual")
        return JSONResponse({"ok": False, "error": GENERIC_ERROR}, status_code=400)


async def _fetch_device_info(ip):
    url = f"http://{ip}/info"
    try:
        async with httpx.AsyncClient(timeout=2.0) as client:
            response = await client.get(url)
        if response.status_code != 200:
            return None
        data = response.json()
        if not isinstance(data, dict) or not data.get("id"):
            return None
        data.setdefault("stream_port", 81)
        return data
    except Exception:
        logger.exception("probe /info %s", ip)
        return None


@app.post("/api/audio/config")
async def api_audio_config(request: Request):
    try:
        body = await request.json()
        language = body.get("language")
        volume = body.get("volume")
        enabled = body.get("enabled")
        if enabled is None and "audio_enabled" in body:
            enabled = body.get("audio_enabled")
        if language is not None and language not in ("id", "en"):
            return JSONResponse({"ok": False, "error": "Bahasa tidak dikenal."}, status_code=400)
        status = await bridge.configure(language=language, volume=volume, enabled=enabled)
        return JSONResponse({"ok": True, "device": status})
    except Exception:
        logger.exception("audio config")
        return JSONResponse({"ok": False, "error": GENERIC_ERROR}, status_code=400)


@app.post("/api/audio/test")
async def api_audio_test():
    try:
        result = await bridge.test_audio()
        if not result.get("ok"):
            return JSONResponse({"ok": False, "error": "Tes suara gagal.", "device": result.get("device")}, status_code=502)
        return JSONResponse(result)
    except Exception:
        logger.exception("audio test")
        return _fail()


@app.get("/api/config")
async def api_get_config():
    """Mengambil konfigurasi sistem saat ini."""
    body = {
        "camera_source": _active_source,
        "telemetry_interval_sec": _telemetry_interval,
        "study_minutes": pomodoro.study_duration // 60,
        "break_minutes": pomodoro.break_duration // 60,
        "long_break_minutes": pomodoro.long_break_duration // 60,
        "selected_id": _selected_id(),
        "device": _device_status(),
    }
    body.update(_audio_fields())
    return JSONResponse(body)


@app.post("/api/config")
async def api_set_config(request: Request):
    """
    Update konfigurasi sistem saat runtime.
    Body JSON: { "telemetry_interval_sec": 1.0 }
    """
    global _telemetry_interval
    try:
        body = await request.json()
        if "telemetry_interval_sec" in body:
            interval = float(body["telemetry_interval_sec"])
            _telemetry_interval = max(0.1, min(interval, 30.0))

        return JSONResponse({
            "ok": True,
            "telemetry_interval_sec": _telemetry_interval
        })
    except Exception:
        logger.exception("set config")
        return JSONResponse({"ok": False, "error": GENERIC_ERROR}, status_code=400)


@app.get("/api/history")
async def api_history(limit: int = 20):
    """Mengambil riwayat sesi terakhir dari database."""
    try:
        history = await asyncio.to_thread(pomodoro.get_history, limit)
        return JSONResponse({"history": history})
    except Exception:
        logger.exception("history")
        return JSONResponse({"history": []})


@app.get("/api/session/summary")
async def api_session_summary():
    """Mengambil ringkasan sesi aktif saat ini."""
    try:
        status = await asyncio.to_thread(pomodoro.get_status)
        return JSONResponse(status)
    except Exception:
        logger.exception("summary")
        return _fail()


if __name__ == "__main__":
    import argparse
    import uvicorn

    logging.basicConfig(level=logging.INFO)

    parser = argparse.ArgumentParser(
        description="NARA Project — Web Dashboard Student Focus Monitoring & Adaptive Pomodoro"
    )
    parser.add_argument(
        "--source",
        default=None,
        help="Override sumber video: indeks webcam atau URL stream. Kosongkan untuk memakai settings.json"
    )
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="Host binding (default: 127.0.0.1, isi 0.0.0.0 jika ingin akses LAN)"
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8000,
        help="Port web server (default: 8000)"
    )
    parser.add_argument(
        "--telemetry-interval",
        type=float,
        default=TELEMETRY_INTERVAL_SEC,
        help="Interval update telemetry dalam detik (default: 1.0)"
    )
    args = parser.parse_args()

    if args.source is not None:
        os.environ["CAMERA_SOURCE"] = str(args.source)
        os.environ["NARA_SOURCE_OVERRIDE"] = "1"
    os.environ["TELEMETRY_INTERVAL_SEC"] = str(args.telemetry_interval)
    os.environ["NARA_BIND_HOST"] = str(args.host)

    shown = args.source if args.source is not None else "settings / webcam 0"
    print("\n" + "=" * 70)
    print("  NARA PROJECT — STUDENT FOCUS MONITORING & POMODORO DASHBOARD")
    print(f"  Sumber Video       : {shown}")
    print(f"  Telemetry Interval : {args.telemetry_interval}s")
    print(f"  Bind               : {args.host}:{args.port}")
    print(f"  Buka browser       : http://127.0.0.1:{args.port}")
    print("=" * 70 + "\n")
    uvicorn.run(app, host=args.host, port=args.port)
