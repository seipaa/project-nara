"""
web_dashboard/app.py
Backend Server FastAPI untuk Dashboard Pemantauan Atensi Siswa & Adaptive Pomodoro

Menyediakan:
1. Web Interface (HTML5, Vanilla CSS, JS HUD)
2. Live MJPEG Video Stream (/video_feed) dengan Real-time Computer Vision Overlay
3. WebSocket Telemetry (/ws) untuk update instan metrik ML (SVM), EAR, MAR, Gaze, dan Timer Pomodoro
4. REST API untuk kontrol Pomodoro (/api/pomodoro/...) & riwayat sesi SQLite
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

# Shared Global State
video_source = None
predictor = None
pomodoro = None
connected_websockets: List[WebSocket] = []
current_frame_bytes = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global video_source, predictor, pomodoro
    source = os.getenv("CAMERA_SOURCE", DEFAULT_CAMERA_SOURCE)
    print(f"[STARTUP] Menginisialisasi Video Source [{source}], Focus Predictor & Pomodoro Service...")
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
    global current_frame_bytes, video_source, predictor, pomodoro, connected_websockets
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

            # Broadcast WebSocket Telemetry setiap ~100ms (10 FPS update rate untuk dashboard)
            if now - last_telemetry_time >= 0.10:
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


@app.get("/api/history")
async def api_history():
    history = pomodoro.get_history(10)
    return JSONResponse({"history": history})


if __name__ == "__main__":
    import argparse
    import uvicorn

    parser = argparse.ArgumentParser(
        description="Web Dashboard Student Focus Monitoring & Adaptive Pomodoro"
    )
    parser.add_argument(
        "--source",
        default=DEFAULT_CAMERA_SOURCE,
        help="Sumber video: '0' untuk webcam laptop, atau URL Stream ESP32-CAM (misal: 'http://192.168.1.100:81/stream')"
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
    args = parser.parse_args()

    os.environ["CAMERA_SOURCE"] = str(args.source)

    print("\n" + "=" * 70)
    print("  [WEB] MENJALANKAN SMART STUDENT MONITORING & POMODORO DASHBOARD")
    print(f"  Sumber Video : {args.source}")
    print(f"  Buka browser : http://localhost:{args.port}")
    print("=" * 70 + "\n")
    uvicorn.run(app, host=args.host, port=args.port)
