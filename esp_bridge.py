"""
esp_bridge.py
Mengirim event Pomodoro ke ESP (POST /play, /volume) dengan token pairing TOFU.
"""

import asyncio
import time

import httpx

EVENT_PRIORITY = {
    "SESSION_END": 0,
    "BREAK_END": 1,
    "SESSION_START": 2,
    "UNFOCUSED": 3,
}
UNFOCUSED_MAX_AGE_SEC = 5.0
HTTP_TIMEOUT = 2.0


class EspBridge:
    def __init__(self, pomodoro, settings):
        self.pomodoro = pomodoro
        self.settings = settings
        self.language = settings.get_language()
        self.volume = settings.get_volume()
        self.audio_enabled = settings.get_audio_enabled()
        self.device = None
        self._loop = None
        self._queue = None
        self._client = None
        self._task = None
        self._status_task = None
        self._lock = asyncio.Lock()
        self._reachable = None
        self._paired = False
        self._last_event = None
        self._pair_lock = asyncio.Lock()
        pomodoro.add_listener(self._on_pomodoro_event)

    def bind_loop(self, loop):
        self._loop = loop
        self._queue = asyncio.Queue(maxsize=32)

    def start(self):
        if self._task is None and self._loop is not None:
            self._task = self._loop.create_task(self._run())
            self._status_task = self._loop.create_task(self._status_loop())

    async def close(self):
        for task in (self._task, self._status_task):
            if task is not None:
                task.cancel()
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def _on_pomodoro_event(self, payload):
        loop = self._loop
        queue = self._queue
        if loop is None or queue is None:
            return
        try:
            loop.call_soon_threadsafe(self._push, payload)
        except RuntimeError:
            pass

    def _push(self, payload):
        if self._queue is None:
            return
        try:
            self._queue.put_nowait(payload)
        except asyncio.QueueFull:
            pass

    def public_status(self):
        device = self.device or {}
        return {
            "selected": bool(device),
            "id": device.get("id"),
            "name": device.get("name"),
            "ip": device.get("ip"),
            "paired": bool(self._paired),
            "reachable": self._reachable,
            "language": self.language,
            "volume": self.volume,
            "audio_enabled": self.audio_enabled,
            "last_event": self._last_event,
        }

    def clear_device(self):
        self.device = None
        self._paired = False
        self._reachable = None

    async def attach(self, device):
        """Pilih ESP dan lakukan pairing TOFU jika token belum tersimpan."""
        self.device = {
            "id": str(device.get("id")),
            "name": device.get("name") or device.get("id"),
            "ip": device.get("ip"),
            "stream_port": int(device.get("stream_port") or 81),
        }
        token = self.settings.get_token(self.device["id"])
        self._paired = bool(token)
        if not token:
            await self._pair()
        if self._paired:
            await self._push_volume()

    async def configure(self, language=None, volume=None, enabled=None):
        saved = self.settings.set_audio(language=language, volume=volume, enabled=enabled)
        self.language = saved["language"]
        self.volume = saved["volume"]
        self.audio_enabled = saved["audio_enabled"]
        if volume is not None and self.device and self._paired:
            await self._push_volume()
        return self.public_status()

    async def test_audio(self):
        """Putar track SESSION_START sebagai tes suara, meski audio sesi dimatikan."""
        if not self.device:
            return {"ok": False, "error": "no_device"}
        ok = await self._post_play("SESSION_START", force=True)
        return {"ok": ok, "device": self.public_status()}

    async def _ensure_client(self):
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=HTTP_TIMEOUT)

    async def _run(self):
        await self._ensure_client()
        while True:
            first = await self._queue.get()
            batch = [first]
            while True:
                try:
                    batch.append(self._queue.get_nowait())
                except asyncio.QueueEmpty:
                    break
            for item in self._order(batch):
                await self._post_play(item["event"], force=False)

    def _order(self, batch):
        """Buang UNFOCUSED basi. Event lebih penting mengalahkan UNFOCUSED di antrean yang sama."""
        now = time.time()
        fresh = []
        for item in batch:
            event = item.get("event")
            if event not in EVENT_PRIORITY:
                continue
            ts = float(item.get("ts") or now)
            if event == "UNFOCUSED" and (now - ts) > UNFOCUSED_MAX_AGE_SEC:
                continue
            fresh.append(item)
        if any(item["event"] != "UNFOCUSED" for item in fresh):
            fresh = [item for item in fresh if item["event"] != "UNFOCUSED"]
        best = {}
        for item in fresh:
            prev = best.get(item["event"])
            if prev is None or float(item.get("ts") or 0) >= float(prev.get("ts") or 0):
                best[item["event"]] = item
        return sorted(best.values(), key=lambda item: EVENT_PRIORITY[item["event"]])

    async def _pair(self):
        await self._ensure_client()
        device = self.device
        if not device:
            return False
        async with self._pair_lock:
            existing = self.settings.get_token(device["id"])
            if existing:
                self._paired = True
                return True
            url = f"http://{device['ip']}/pair"
            try:
                response = await self._request("POST", url, json={})
            except Exception:
                self._reachable = False
                self._paired = False
                return False
            if response is None or response.status_code != 200:
                self._paired = False
                self._reachable = False if response is None else True
                return False
            try:
                body = response.json()
            except Exception:
                body = {}
            token = body.get("token") if isinstance(body, dict) else None
            if not token:
                self._paired = False
                return False
            self.settings.set_token(device["id"], token)
            self._paired = True
            self._reachable = True
            return True

    async def _push_volume(self):
        await self._ensure_client()
        device = self.device
        if not device or not self._paired:
            return False
        token = self.settings.get_token(device["id"])
        if not token:
            return False
        url = f"http://{device['ip']}/volume"
        headers = {"X-NARA-Token": token}
        try:
            response = await self._request(
                "POST",
                url,
                json={"volume": int(self.volume)},
                headers=headers,
            )
        except Exception:
            self._reachable = False
            return False
        self._reachable = response is not None and response.status_code < 500
        return bool(response is not None and response.status_code == 200)

    async def _post_play(self, event, force=False):
        if not force and not self.audio_enabled:
            return False
        device = self.device
        if not device:
            return False
        await self._ensure_client()
        if not self._paired:
            await self._pair()
        token = self.settings.get_token(device["id"])
        if not token:
            return False
        url = f"http://{device['ip']}/play"
        headers = {"X-NARA-Token": token}
        body = {"lang": self.language if self.language in ("id", "en") else "id", "event": event}
        try:
            response = await self._request("POST", url, json=body, headers=headers)
        except Exception:
            self._reachable = False
            return False
        if response is None:
            self._reachable = False
            return False
        self._reachable = True
        if response.status_code == 200:
            self._last_event = event
            return True
        return False

    async def _request(self, method, url, **kwargs):
        """Satu percobaan ulang, hanya jika timeout atau status HTTP 5xx."""
        last_exc = None
        response = None
        for attempt in range(2):
            try:
                response = await self._client.request(method, url, **kwargs)
                last_exc = None
                if response.status_code < 500:
                    return response
            except httpx.TimeoutException as exc:
                last_exc = exc
                response = None
            if attempt == 0:
                await asyncio.sleep(0.05)
        if last_exc is not None and response is None:
            raise last_exc
        return response

    async def _status_loop(self):
        while True:
            await asyncio.sleep(10)
            device = self.device
            if not device or self._client is None:
                continue
            url = f"http://{device['ip']}/status"
            try:
                response = await self._client.get(url)
                self._reachable = response.status_code == 200
            except Exception:
                self._reachable = False
