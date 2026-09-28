"""
web_dashboard/app.py
Backend Server FastAPI untuk Dashboard Pemantauan Atensi Siswa & Adaptive Pomodoro NARA Project.

Menyediakan:
1. Web Interface (HTML5, Vanilla CSS, JS)
2. Live MJPEG Video Stream (/video_feed) dengan Real-time Computer Vision Overlay
3. WebSocket Telemetry (/ws) untuk update instan metrik ML (SVM), EAR, MAR, Gaze, dan Timer Pomodoro
4. REST API untuk kontrol Pomodoro (/api/pomodoro/...) & riwayat sesi SQLite
5. REST API untuk konfigurasi pomodoro (/api/config) & env settings

Environment Variables:
  CAMERA_SOURCE          : "0" untuk webcam bawaan, atau URL stream ESP32-CAM
  TELEMETRY_INTERVAL_SEC : Interval update telemetry ke WebSocket (default: 1.0 detik)
                           Contoh: set TELEMETRY_INTERVAL_SEC=3  -> update setiap 3 detik
                                   set TELEMETRY_INTERVAL_SEC=0.5 -> update setiap 0.5 detik
"""

import asyncio
import json
import os
import sys
import time
from typing import List
from contextlib import asynccontextmanager

import cv2
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
from fastapi.responses import HTMLResponse, StreamingResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

# Tambahkan root path agar bisa import modul root
ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

from cv_focus_engine import VideoSource
from focus_predictor import FocusPredictor
from pomodoro_service import PomodoroService

# Paths setup
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, "static")
TEMPLATES_DIR = os.path.join(BASE_DIR, "templates")

os.makedirs(STATIC_DIR, exist_ok=True)
os.makedirs(TEMPLATES_DIR, exist_ok=True)

# ==============================================================================
# KONFIGURASI SUMBER KAMERA
# ==============================================================================
# - "0" : Webcam Laptop Bawaan
# - URL Stream : ESP32-CAM (Contoh: "http://192.168.1.100:81/stream" atau "rtsp://...")
DEFAULT_CAMERA_SOURCE = os.getenv("CAMERA_SOURCE", "0")
TELEMETRY_INTERVAL_SEC = float(os.getenv("TELEMETRY_INTERVAL_SEC", "1.0"))

# Shared Global State
video_source = None
predictor = None
pomodoro = None
connected_websockets: List[WebSocket] = []
current_frame_bytes = None
# Telemetry interval dapat diubah saat runtime via POST /api/config
_telemetry_interval = TELEMETRY_INTERVAL_SEC


@asynccontextmanager
async def lifespan(app: FastAPI):
    global video_source, predictor, pomodoro, _telemetry_interval
    source = os.getenv("CAMERA_SOURCE", DEFAULT_CAMERA_SOURCE)
    _telemetry_interval = float(os.getenv("TELEMETRY_INTERVAL_SEC", "1.0"))
    print(f"[STARTUP] Menginisialisasi Video Source [{source}], Focus Predictor & Pomodoro Service...")
    print(f"[STARTUP] Telemetry interval: {_telemetry_interval}s")
    video_source = VideoSource(source)
    predictor = FocusPredictor()
    pomodoro = PomodoroService()

    worker_task = asyncio.create_task(video_processing_worker())
    yield

    worker_task.cancel()
    if video_source is not None:
        video_source.release()
    print("[SHUTDOWN] Kamera dan resources dilepaskan.")


app = FastAPI(
    title="Student Focus & Adaptive Pomodoro Monitoring Engine",
    lifespan=lifespan
)

app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
templates = Jinja2Templates(directory=TEMPLATES_DIR)


async def video_processing_worker():
    """Background worker yang menangkap frame video, menjalankan ML, dan broadcast telemetry."""
    global current_frame_bytes, video_source, predictor, pomodoro, connected_websockets, _telemetry_interval
    last_telemetry_time = 0

    while True:
        try:
            if video_source is None:
                await asyncio.sleep(0.05)
                continue

            ok, frame = video_source.read()
            if not ok or frame is None:
                await asyncio.sleep(0.03)
                continue

            # Process frame through ML & CV pipeline
            annotated_frame, prediction_data = predictor.process_frame(frame)

            # Update Pomodoro state
            now = time.time()
            pomodoro_data = pomodoro.update_telemetry(prediction_data)

            # Encode frame ke JPEG untuk stream
            ret, buffer = cv2.imencode(".jpg", annotated_frame, [cv2.IMWRITE_JPEG_QUALITY, 75])
            if ret:
                current_frame_bytes = buffer.tobytes()

            # Broadcast WebSocket Telemetry sesuai interval yang dikonfigurasi via env/API
            if now - last_telemetry_time >= _telemetry_interval:
                last_telemetry_time = now
                payload = {
                    "timestamp": now,
                    "prediction": prediction_data,
                    "pomodoro": pomodoro_data
                }
                dead_sockets = []
                for ws in connected_websockets:
                    try:
                        await ws.send_text(json.dumps(payload))
                    except Exception:
                        dead_sockets.append(ws)

                for dead in dead_sockets:
                    if dead in connected_websockets:
                        connected_websockets.remove(dead)

            await asyncio.sleep(0.01)

        except Exception as e:
            print(f"[WORKER ERROR] {e}")
            await asyncio.sleep(0.05)


def generate_mjpeg_frames():
    """Generator untuk HTTP MJPEG streaming."""
    global current_frame_bytes
    while True:
        if current_frame_bytes is not None:
            yield (b"--frame\r\n"
                   b"Content-Type: image/jpeg\r\n\r\n" + current_frame_bytes + b"\r\n")
        time.sleep(0.03)


# ---------------------------------------------------------------------------
# ROUTES
# ---------------------------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    """Halaman utama Dashboard."""
    return templates.TemplateResponse(request=request, name="index.html")


@app.get("/video_feed")
async def video_feed():
    """Route stream video MJPEG untuk browser."""
    return StreamingResponse(
        generate_mjpeg_frames(),
        media_type="multipart/x-mixed-replace; boundary=frame"
    )


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    """WebSocket route untuk stream telemetry real-time."""
    await websocket.accept()
    connected_websockets.append(websocket)
    try:
        while True:
            # Tetap listen untuk pesan/heartbeat dari client jika ada
            data = await websocket.receive_text()
    except WebSocketDisconnect:
        if websocket in connected_websockets:
            connected_websockets.remove(websocket)


# ---------------------------------------------------------------------------
# REST API CONTROLLERS FOR POMODORO
# ---------------------------------------------------------------------------
@app.post("/api/pomodoro/start")
async def api_pomodoro_start(mode: str = "STUDY"):
    status = pomodoro.start(mode)
    return JSONResponse(status)


@app.post("/api/pomodoro/pause")
async def api_pomodoro_pause():
    status = pomodoro.pause()
    return JSONResponse(status)


@app.post("/api/pomodoro/resume")
async def api_pomodoro_resume():
    status = pomodoro.resume()
    return JSONResponse(status)


@app.post("/api/pomodoro/reset")
async def api_pomodoro_reset():
    status = pomodoro.reset()
    return JSONResponse(status)


@app.post("/api/pomodoro/skip")
async def api_pomodoro_skip():
    status = pomodoro.skip()
    return JSONResponse(status)


@app.post("/api/pomodoro/accept_break")
async def api_pomodoro_accept_break():
    status = pomodoro.accept_suggested_break()
    return JSONResponse(status)


@app.post("/api/pomodoro/dismiss_alert")
async def api_pomodoro_dismiss_alert():
    status = pomodoro.dismiss_alert()
    return JSONResponse(status)


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

        # Validasi rentang aman
        study_min = max(1, min(study_min, 120))
        break_min = max(1, min(break_min, 60))
        long_break_min = max(1, min(long_break_min, 120))

        pomodoro.study_duration = study_min * 60
        pomodoro.break_duration = break_min * 60
        pomodoro.long_break_duration = long_break_min * 60

        # Reset timer ke durasi baru jika sedang IDLE
        if pomodoro.mode == "IDLE":
            pomodoro.time_remaining = pomodoro.study_duration

        return JSONResponse({
            "ok": True,
            "study_minutes": study_min,
            "break_minutes": break_min,
            "long_break_minutes": long_break_min
        })
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)


@app.get("/api/config")
async def api_get_config():
    """Mengambil konfigurasi sistem saat ini."""
    return JSONResponse({
        "camera_source": os.getenv("CAMERA_SOURCE", DEFAULT_CAMERA_SOURCE),
        "telemetry_interval_sec": _telemetry_interval,
        "study_minutes": pomodoro.study_duration // 60,
        "break_minutes": pomodoro.break_duration // 60,
        "long_break_minutes": pomodoro.long_break_duration // 60,
    })


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
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)


@app.get("/api/history")
async def api_history(limit: int = 20):
    """Mengambil riwayat sesi terakhir dari database."""
    history = pomodoro.get_history(limit)
    return JSONResponse({"history": history})


@app.get("/api/session/summary")
async def api_session_summary():
    """Mengambil ringkasan sesi aktif saat ini."""
    status = pomodoro.get_status()
    return JSONResponse(status)


if __name__ == "__main__":
    import argparse
    import uvicorn

    parser = argparse.ArgumentParser(
        description="NARA Project — Web Dashboard Student Focus Monitoring & Adaptive Pomodoro"
    )
    parser.add_argument(
        "--source",
        default=DEFAULT_CAMERA_SOURCE,
        help="Sumber video: '0' untuk webcam laptop, atau URL Stream ESP32-CAM"
    )
    parser.add_argument(
        "--host",
        default="0.0.0.0",
        help="Host binding (default: 0.0.0.0)"
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

    os.environ["CAMERA_SOURCE"] = str(args.source)
    os.environ["TELEMETRY_INTERVAL_SEC"] = str(args.telemetry_interval)

    print("\n" + "=" * 70)
    print("  NARA PROJECT — STUDENT FOCUS MONITORING & POMODORO DASHBOARD")
    print(f"  Sumber Video       : {args.source}")
    print(f"  Telemetry Interval : {args.telemetry_interval}s")
    print(f"  Buka browser       : http://localhost:{args.port}")
    print("=" * 70 + "\n")
    uvicorn.run(app, host=args.host, port=args.port)
