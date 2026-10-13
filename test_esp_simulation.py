import asyncio
import json
import socket
import sys
import time
import urllib.request
import httpx
import numpy as np

if sys.stdout.encoding.lower() != "utf-8":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

from esp_mock_worker import EspMockWorker
from device_discovery import DeviceDiscovery
from esp_bridge import EspBridge
from voice_alert_service import Esp32VoiceAlertClient
from pomodoro_service import PomodoroService
from settings_store import SettingsStore
from cv_focus_engine import HttpCameraReader


def test_esp_mock_worker_lifecycle_and_endpoints():
    print("\n--- TEST 1: Lifecycle & Endpoints ESP Mock Worker ---")
    worker = EspMockWorker(
        device_id="test-esp-1",
        device_name="Test ESP Simulator",
        http_port=80,
        stream_port=81,
        alert_port=82,
        udp_port=4210,
        video_mode="synthetic",
        audio_enabled=False,
    )
    worker.start()
    time.sleep(1.0)

    try:
        # 1. Test GET /info
        req = urllib.request.urlopen("http://127.0.0.1:80/info", timeout=3.0)
        assert req.status == 200
        info_data = json.loads(req.read().decode())
        print("GET /info:", info_data)
        assert info_data["id"] == "test-esp-1"
        assert info_data["stream_port"] == 81
        assert info_data["paired"] is False

        # 2. Test GET /capture
        req_cap = urllib.request.urlopen("http://127.0.0.1:80/capture", timeout=3.0)
        assert req_cap.status == 200
        assert req_cap.headers.get_content_type() == "image/jpeg"
        cap_bytes = req_cap.read()
        assert len(cap_bytes) > 1000
        assert cap_bytes[:2] == b"\xff\xd8"
        print("GET /capture: Valid JPEG, size =", len(cap_bytes), "bytes")

        # 3. Test POST /pair
        with httpx.Client(timeout=3.0) as client:
            resp_pair = client.post("http://127.0.0.1:80/pair")
            assert resp_pair.status_code == 200
            pair_data = resp_pair.json()
            token = pair_data["token"]
            print("POST /pair: Token received =", token)
            assert len(token) == 32

            # 4. Test POST /volume
            resp_vol = client.post(
                "http://127.0.0.1:80/volume",
                headers={"X-NARA-Token": token},
                json={"volume": 25},
            )
            assert resp_vol.status_code == 200
            assert resp_vol.json()["volume"] == 25
            print("POST /volume: Set to 25 OK")

            # 5. Test POST /play for SESSION_START (Track 4)
            resp_play = client.post(
                "http://127.0.0.1:80/play",
                headers={"X-NARA-Token": token},
                json={"lang": "id", "event": "SESSION_START"},
            )
            assert resp_play.status_code == 200
            play_data = resp_play.json()
            print("POST /play (SESSION_START):", play_data)
            assert play_data["track"] == 4
            assert play_data["folder"] == 1

            # 6. Test POST /play for UNFOCUSED (Track 1)
            resp_play1 = client.post(
                "http://127.0.0.1:80/play",
                headers={"X-NARA-Token": token},
                json={"lang": "id", "event": "UNFOCUSED"},
            )
            assert resp_play1.status_code == 200
            assert resp_play1.json()["track"] == 1

            # 7. Test POST /play for SESSION_END (Track 2)
            resp_play2 = client.post(
                "http://127.0.0.1:80/play",
                headers={"X-NARA-Token": token},
                json={"lang": "id", "event": "SESSION_END"},
            )
            assert resp_play2.status_code == 200
            assert resp_play2.json()["track"] == 2

            # 8. Test POST /play for BREAK (Track 3)
            resp_play3 = client.post(
                "http://127.0.0.1:80/play",
                headers={"X-NARA-Token": token},
                json={"lang": "id", "event": "BREAK"},
            )
            assert resp_play3.status_code == 200
            assert resp_play3.json()["track"] == 3

            # 9. Test Port 82 /api/alert
            resp_alert = client.post(
                "http://127.0.0.1:82/api/alert",
                json={"event_id": "test-uuid-001", "code": "SESSION_START", "language": "id"},
            )
            assert resp_alert.status_code == 202
            alert_json = resp_alert.json()
            print("POST :82/api/alert:", alert_json)
            assert alert_json["file"] == 4

            # Duplicate event_id should return 409
            resp_alert_dup = client.post(
                "http://127.0.0.1:82/api/alert",
                json={"event_id": "test-uuid-001", "code": "SESSION_START", "language": "id"},
            )
            assert resp_alert_dup.status_code == 409
            print("Deduplication 409 returned correctly!")

    finally:
        worker.stop()
        time.sleep(0.5)


def test_udp_discovery_integration():
    print("\n--- TEST 2: UDP Discovery Integration ---")
    worker = EspMockWorker(
        device_id="nara-cam-mock-udp",
        device_name="NARA UDP Mock",
        http_port=80,
        stream_port=81,
        alert_port=82,
        udp_port=4210,
        video_mode="synthetic",
        audio_enabled=False,
    )
    worker.start()
    time.sleep(0.8)

    try:
        discovery = DeviceDiscovery()
        found = discovery.probe_udp(timeout=1.0)
        print("UDP probe found devices:", found)
        assert found >= 1

        dev = discovery.get("nara-cam-mock-udp")
        assert dev is not None
        print("Discovered device record:", dev)
        assert dev["id"] == "nara-cam-mock-udp"
        assert dev["stream_port"] == 81
        assert "http://" in dev["stream_url"]
    finally:
        worker.stop()
        time.sleep(0.5)


def test_http_camera_reader_mjpeg():
    print("\n--- TEST 3: HttpCameraReader MJPEG Streaming ---")
    worker = EspMockWorker(
        device_id="nara-stream-test",
        http_port=80,
        stream_port=81,
        video_mode="synthetic",
        audio_enabled=False,
    )
    worker.start()
    time.sleep(1.0)

    try:
        reader = HttpCameraReader("http://127.0.0.1:81/stream")
        ret, frame = reader.read()
        print("Reader result:", ret, "Frame shape:", frame.shape if frame is not None else None)
        assert ret is True
        assert frame is not None
        assert frame.shape == (480, 640, 3)
        reader.release()
    finally:
        worker.stop()
        time.sleep(0.5)


def test_esp_bridge_end_to_end():
    print("\n--- TEST 4: EspBridge End-to-End Test ---")
    worker = EspMockWorker(
        device_id="bridge-test-cam",
        http_port=80,
        stream_port=81,
        alert_port=82,
        video_mode="synthetic",
        audio_enabled=False,
    )
    worker.start()
    time.sleep(1.0)

    try:
        async def run_bridge_test():
            pomo = PomodoroService()
            settings = SettingsStore()
            bridge = EspBridge(pomo, settings)
            loop = asyncio.get_running_loop()
            bridge.bind_loop(loop)
            bridge.start()

            device_info = {
                "id": "bridge-test-cam",
                "name": "Bridge Test Cam",
                "ip": "127.0.0.1",
                "stream_port": 81,
            }
            await bridge.attach(device_info)
            assert bridge._paired is True
            print("Bridge attached and paired successfully!")

            test_res = await bridge.test_audio()
            print("Bridge test_audio result:", test_res)
            assert test_res["ok"] is True

            await bridge.close()

        asyncio.run(run_bridge_test())
    finally:
        worker.stop()
        time.sleep(0.5)


if __name__ == "__main__":
    test_esp_mock_worker_lifecycle_and_endpoints()
    test_udp_discovery_integration()
    test_http_camera_reader_mjpeg()
    test_esp_bridge_end_to_end()
    print("\n[SUCCESS] ALL 4 TEST SUITES PASSED FLAWLESSLY!\n")
