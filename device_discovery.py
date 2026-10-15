"""
device_discovery.py
Menemukan kamera NARA di jaringan lewat mDNS (_nara._tcp.local.) dan
fallback UDP broadcast "NARA_DISCOVER" di port 4210.
"""

import json
import socket
import sys
import threading
import time

DISCOVER_PORT = 4210
MDNS_SERVICE = "_nara._tcp.local."
STALE_SEC = 45.0
UDP_PROBE_SEC = 1.2

try:
    from zeroconf import ServiceBrowser, ServiceListener, Zeroconf
    _HAS_ZEROCONF = True
except Exception:
    ServiceBrowser = None
    ServiceListener = object
    Zeroconf = None
    _HAS_ZEROCONF = False


def _prop(info, key):
    props = getattr(info, "properties", None) or {}
    raw = props.get(key.encode("utf-8"))
    if raw is None:
        raw = props.get(key)
    if isinstance(raw, bytes):
        return raw.decode("utf-8", errors="replace").strip()
    if raw is None:
        return ""
    return str(raw).strip()


def _broadcast_targets():
    targets = {"255.255.255.255"}
    try:
        host = socket.gethostbyname(socket.gethostname())
        parts = host.split(".")
        if len(parts) == 4 and not host.startswith("127."):
            targets.add(".".join(parts[:3] + ["255"]))
    except OSError:
        pass
    return targets


class DeviceDiscovery(ServiceListener):
    def __init__(self, on_device_updated=None):
        super().__init__()
        self._lock = threading.Lock()
        self._devices = {}
        self._zc = None
        self._browser = None
        self._running = False
        self.on_device_updated = on_device_updated

    @property
    def mdns_available(self):
        return _HAS_ZEROCONF

    def start(self):
        if self._running:
            return
        self._running = True
        if _HAS_ZEROCONF:
            try:
                self._zc = Zeroconf()
                self._browser = ServiceBrowser(self._zc, MDNS_SERVICE, self)
            except Exception as e:
                print(f"[WARN] mDNS tidak aktif: {e}")
                self._zc = None
                self._browser = None
        else:
            print("[WARN] Paket zeroconf belum terpasang. Discovery memakai UDP saja.")
        threading.Thread(target=self._udp_loop, name="nara-udp-discover", daemon=True).start()

    def close(self):
        self._running = False
        browser = self._browser
        zc = self._zc
        self._browser = None
        self._zc = None
        try:
            if browser is not None:
                browser.cancel()
        except Exception:
            pass
        try:
            if zc is not None:
                zc.close()
        except Exception:
            pass

    def add_service(self, zc, type_, name):
        self._ingest_mdns(zc, name)

    def update_service(self, zc, type_, name):
        self._ingest_mdns(zc, name)

    def remove_service(self, zc, type_, name):
        # Biarkan entri kadaluarsa lewat last_seen supaya balasan UDP tetap dipakai.
        return None

    def _ingest_mdns(self, zc, name):
        try:
            info = zc.get_service_info(MDNS_SERVICE, name, timeout=1500)
        except Exception:
            info = None
        if info is None:
            return
        addresses = []
        try:
            addresses = list(info.parsed_addresses() or [])
        except Exception:
            addresses = []
        ip = next((a for a in addresses if ":" not in a), None)
        if not ip and addresses:
            ip = addresses[0]
        if not ip:
            return
        device_id = _prop(info, "id") or name
        stream_port = _prop(info, "stream_port") or "81"
        try:
            stream_port = int(stream_port)
        except ValueError:
            stream_port = 81
        mdns_name = _prop(info, "name")
        self.upsert({
            "id": str(device_id),
            "name": mdns_name or "NARA Device",
            "ip": ip,
            "stream_port": stream_port,
            "paired": _prop(info, "paired").lower() in ("1", "true", "yes"),
            "fw": _prop(info, "fw"),
            "manual": False,
        })

    def upsert(self, info):
        device_id = str(info.get("id") or "").strip()
        ip = str(info.get("ip") or "").strip()
        if not device_id or not ip:
            return None
        try:
            stream_port = int(info.get("stream_port") or 81)
        except (TypeError, ValueError):
            stream_port = 81
        record = {
            "id": device_id,
            "name": str(info.get("name") or f"nara-cam-{device_id}"),
            "ip": ip,
            "stream_port": stream_port,
            "stream_url": f"http://{ip}:{stream_port}/stream",
            "last_seen": time.time(),
            "paired": bool(info.get("paired", False)),
            "fw": str(info.get("fw") or ""),
            "manual": bool(info.get("manual", False)),
        }
        old_record = None
        with self._lock:
            if device_id in self._devices:
                old_record = dict(self._devices[device_id])
            self._devices[device_id] = record
            res = dict(record)

        if self.on_device_updated is not None:
            try:
                self.on_device_updated(res, old_record)
            except Exception as e:
                print(f"[WARN] on_device_updated callback error: {e}")

        return res

    def list_devices(self, include_stale=False):
        now = time.time()
        with self._lock:
            items = []
            for rec in self._devices.values():
                age = now - rec["last_seen"]
                if not include_stale and not rec.get("manual") and age > STALE_SEC:
                    continue
                item = dict(rec)
                item["age_sec"] = round(age, 1)
                items.append(item)
        items.sort(key=lambda d: d.get("name") or d["id"])
        return items

    def get(self, device_id):
        with self._lock:
            rec = self._devices.get(str(device_id))
            return dict(rec) if rec else None

    def probe_udp(self, timeout=UDP_PROBE_SEC):
        """Broadcast NARA_DISCOVER dan kumpulkan balasan JSON."""
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        found = 0
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind(("", 0))
            sock.settimeout(0.3)
            payload = b"NARA_DISCOVER"
            for dest in _broadcast_targets():
                try:
                    sock.sendto(payload, (dest, DISCOVER_PORT))
                except OSError:
                    continue
            deadline = time.time() + timeout
            while time.time() < deadline:
                try:
                    data, addr = sock.recvfrom(4096)
                except socket.timeout:
                    continue
                except OSError:
                    break
                if self._ingest_udp(data, addr):
                    found += 1
        finally:
            sock.close()
        return found

    def _ingest_udp(self, data, addr):
        try:
            text = data.decode("utf-8", errors="replace").strip()
            info = json.loads(text)
        except (UnicodeDecodeError, json.JSONDecodeError):
            return False
        if not isinstance(info, dict):
            return False
        ip = str(info.get("ip") or addr[0]).strip()
        self.upsert({
            "id": info.get("id") or ip,
            "name": info.get("name") or "NARA Device",
            "ip": ip,
            "stream_port": info.get("stream_port") or 81,
            "paired": bool(info.get("paired", False)),
            "fw": info.get("fw") or "",
            "manual": False,
        })
        return True

    def _udp_loop(self):
        while self._running:
            try:
                self.probe_udp()
            except Exception as e:
                print(f"[WARN] UDP discovery: {e}")
            # Tidur dalam potongan pendek supaya close() tidak menunggu lama.
            for _ in range(20):
                if not self._running:
                    return
                time.sleep(1.0)

    def add_manual_ip(self, ip, info):
        """Masukkan perangkat yang dijawab GET /info."""
        return self.upsert({
            "id": info.get("id") or ip,
            "name": info.get("name") or f"nara-cam-{ip}",
            "ip": ip,
            "stream_port": info.get("stream_port") or 81,
            "paired": bool(info.get("paired", False)),
            "fw": info.get("fw") or "",
            "manual": True,
        })


def probe_webcams(indexes=range(4), active_index=None):
    """
    Cek indeks webcam lokal 0-3. Indeks yang sedang dipakai capture thread
    ditandai tersedia tanpa membuka perangkat kedua kali.
    """
    cameras = []
    for index in indexes:
        source = str(index)
        display_name = "Kamera Terintegrasi" if int(index) == 0 else f"Kamera {index}"
        item = {
            "id": f"webcam:{index}",
            "name": display_name,
            "kind": "webcam",
            "source": source,
            "index": index,
            "available": False,
        }
        if active_index is not None and int(active_index) == int(index):
            item["available"] = True
            cameras.append(item)
            continue
        item["available"] = _webcam_opens(index)
        cameras.append(item)
    return cameras


def _webcam_opens(index):
    try:
        import cv2
    except Exception:
        return False
    cap = None
    try:
        if sys.platform == "win32":
            cap = cv2.VideoCapture(int(index), cv2.CAP_DSHOW)
        else:
            cap = cv2.VideoCapture(int(index))
        return bool(cap is not None and cap.isOpened())
    except Exception:
        return False
    finally:
        if cap is not None:
            try:
                cap.release()
            except Exception:
                pass
