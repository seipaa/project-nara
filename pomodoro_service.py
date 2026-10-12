"""
pomodoro_service.py
Layanan Manajemen State Pomodoro & Action Trigger Intervensi Adaptif

Fitur:
1. State Machine Pomodoro:
   - STUDY (Belajar 25 Menit)
   - BREAK (Istirahat Pendek 5 Menit)
   - LONG_BREAK (Istirahat Panjang 15 Menit)
   - PAUSED / IDLE
2. Evaluasi Ambang Batas Penurunan Atensi (Adaptive Interventions):
   - Atensi drop (Risiko Tinggi / P_risk > 70%) selama > 15 detik berturut-turut
   - Menguap >= 2 kali dalam 2 menit
   - Mata terpejam >= 3 detik
3. Pemicu Aksi (Action Triggers):
   - Visual Nudge (Peringatan lembut di UI)
   - Adaptive Break Proposal (Rekomendasi jeda istirahat 5 menit / aturan 20-20-20)
4. Logging Riwayat Sesi ke Database SQLite (pomodoro_sessions.db)
5. Event suara (server-authoritative): SESSION_START, UNFOCUSED, BREAK_END, SESSION_END
"""

import os
import time
import sqlite3
import threading
from datetime import datetime
from collections import deque

ROOT_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(ROOT_DIR, "pomodoro_sessions.db")

# Mode Pomodoro (dalam detik)
DEFAULT_STUDY_DURATION = 25 * 60      # 25 Menit
DEFAULT_BREAK_DURATION = 5 * 60       # 5 Menit
DEFAULT_LONG_BREAK_DURATION = 15 * 60 # 15 Menit

# Ambang Batas Intervensi
UNFOCUSED_ALERT_DURATION_SEC = 15.0   # 15 detik tidak fokus -> Peringatan visual
UNFOCUSED_COOLDOWN_SEC = 30.0         # Cooldown event UNFOCUSED / nudge
MAX_YAWNS_IN_WINDOW = 2               # 2x menguap dalam window -> Saran istirahat
SLEEP_ALERT_DURATION_SEC = 3.0        # 3 detik tidur -> Alarm bangunkan
SLEEP_COOLDOWN_SEC = 30.0             # Jangan spam SLEEP_DETECTED tiap frame
YAWN_ALERT_COOLDOWN_SEC = 45.0
TELEMETRY_DT_CAP_SEC = 2.0            # Batas delta agar stall kamera tidak menumpuk
TICK_DELTA_CAP_SEC = 2.0

ALLOWED_MODES = ("STUDY", "BREAK", "LONG_BREAK")
SUBJECT_WHITELIST = (
    "Matematika",
    "Fisika",
    "Kimia",
    "Biologi",
    "Bahasa Inggris",
    "Bahasa Indonesia",
    "Sejarah",
    "Ekonomi",
    "Lainnya",
    "Belajar",
)
SUBJECT_FALLBACK = "Belajar"
SUBJECT_MAX_LEN = 40

AUDIO_EVENTS = ("SESSION_START", "UNFOCUSED", "BREAK_END", "SESSION_END")


def sanitize_subject(subject):
    """Whitelist mapel yang ada di UI, plus fallback pendek, maksimal 40 karakter."""
    if subject is None:
        return SUBJECT_FALLBACK
    text = str(subject).replace("\x00", " ").strip()
    text = " ".join(text.split())
    if len(text) > SUBJECT_MAX_LEN:
        text = text[:SUBJECT_MAX_LEN].strip()
    if text in SUBJECT_WHITELIST:
        return text
    return SUBJECT_FALLBACK


class PomodoroService:
    def __init__(self, db_path=DB_PATH):
        self.db_path = db_path
        self._init_db()
        self._lock = threading.RLock()
        self.listeners = []
        self._pending = []

        # State
        self.mode = "IDLE"  # "IDLE", "STUDY", "BREAK", "LONG_BREAK"
        self.study_duration = DEFAULT_STUDY_DURATION
        self.break_duration = DEFAULT_BREAK_DURATION
        self.long_break_duration = DEFAULT_LONG_BREAK_DURATION
        self.completed_sessions = 0
        self.subject = SUBJECT_FALLBACK

        # Timers
        self.time_remaining = self.study_duration
        self.is_running = False
        self.last_tick = None
        self._tick_fraction = 0.0
        self.session_start_time = None

        # Stats akumulasi sesi
        self.total_focus_seconds = 0.0
        self.total_unfocused_seconds = 0.0
        self.total_distraction_events = 0
        self.total_yawns = 0
        self.total_sleep_events = 0

        # State Tracking untuk Intervensi Adaptif
        self.unfocused_start_time = None
        self.sleep_start_time = None
        self.yawn_timestamps = deque(maxlen=20)
        self._was_yawning = False
        self._telemetry_ts = None
        self.active_alert = None          # Visual nudge string
        self.suggested_break = False      # Rekomendasi istirahat aktif
        self.last_intervention_time = 0.0
        self.last_unfocused_emit = 0.0
        self.last_sleep_log = 0.0

    def add_listener(self, fn):
        """Daftarkan callback(payload) untuk event suara. Dipanggil di luar lock."""
        with self._lock:
            self.listeners.append(fn)

    def _init_db(self):
        """Inisialisasi tabel database SQLite."""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS session_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT,
                session_type TEXT,
                duration_sec INTEGER,
                focus_percentage REAL,
                distraction_count INTEGER,
                yawn_count INTEGER,
                notes TEXT
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS intervention_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT,
                trigger_type TEXT,
                reason TEXT,
                action_taken TEXT
            )
        """)
        conn.commit()
        conn.close()

    def _take_events(self):
        pending = self._pending
        self._pending = []
        return pending, list(self.listeners)

    def _dispatch(self, pending, listeners):
        for event in pending:
            if event not in AUDIO_EVENTS:
                continue
            payload = {"event": event, "ts": time.time(), "mode": self.mode}
            self._emit(payload, listeners)

    def _emit(self, payload, listeners=None):
        targets = listeners if listeners is not None else list(self.listeners)
        for fn in targets:
            try:
                fn(payload)
            except Exception as e:
                print(f"[WARN] Listener event {payload.get('event')} gagal: {e}")

    def _queue(self, event):
        if event in AUDIO_EVENTS:
            self._pending.append(event)

    def _duration_for_mode(self, mode=None):
        mode = mode or self.mode
        if mode == "BREAK":
            return self.break_duration
        if mode == "LONG_BREAK":
            return self.long_break_duration
        return self.study_duration

    def _reset_session_stats(self):
        self.total_focus_seconds = 0.0
        self.total_unfocused_seconds = 0.0
        self.total_distraction_events = 0
        self.total_yawns = 0
        self.total_sleep_events = 0
        self.unfocused_start_time = None
        self.sleep_start_time = None
        self.yawn_timestamps.clear()
        self._was_yawning = False
        self._telemetry_ts = None
        self.active_alert = None
        self.suggested_break = False

    def _start_locked(self, mode, subject=None, emit_session_start=False):
        """Mulai fase baru. Pemanggil memegang lock dan mengirim event setelahnya."""
        if mode not in ALLOWED_MODES:
            return
        self.mode = mode
        if mode == "STUDY":
            if subject is not None:
                self.subject = sanitize_subject(subject)
            elif not self.subject:
                self.subject = SUBJECT_FALLBACK
        self.time_remaining = self._duration_for_mode(mode)
        self.is_running = True
        self.last_tick = time.time()
        self._tick_fraction = 0.0
        self.session_start_time = datetime.now().isoformat()
        self._reset_session_stats()
        if emit_session_start and mode == "STUDY":
            self._queue("SESSION_START")

    def start(self, mode="STUDY", subject=None):
        """Memulai timer Pomodoro. Mode di luar whitelist diabaikan."""
        with self._lock:
            if mode not in ALLOWED_MODES:
                return self.get_status()
            self._start_locked(mode, subject, emit_session_start=(mode == "STUDY"))
            status = self.get_status()
            pending, listeners = self._take_events()
        self._dispatch(pending, listeners)
        return status

    def pause(self):
        """Menjeda timer."""
        with self._lock:
            self.is_running = False
            return self.get_status()

    def resume(self):
        """Melanjutkan timer."""
        with self._lock:
            if self.mode != "IDLE":
                self.is_running = True
                self.last_tick = time.time()
                self._tick_fraction = 0.0
            return self.get_status()

    def reset(self):
        """Mereset timer ke awal tanpa event suara."""
        with self._lock:
            self.is_running = False
            self.mode = "IDLE"
            self.time_remaining = self.study_duration
            self.active_alert = None
            self.suggested_break = False
            self._tick_fraction = 0.0
            return self.get_status()

    def end_session(self):
        """User mengakhiri sesi. SESSION_END hanya saat masih di STUDY."""
        with self._lock:
            if self.mode == "STUDY":
                self._save_session_log("STUDY")
                self._queue("SESSION_END")
            elif self.mode in ("BREAK", "LONG_BREAK"):
                self._save_session_log(self.mode)
            self.is_running = False
            self.mode = "IDLE"
            self.time_remaining = self.study_duration
            self.active_alert = None
            self.suggested_break = False
            self._tick_fraction = 0.0
            status = self.get_status()
            pending, listeners = self._take_events()
        self._dispatch(pending, listeners)
        return status

    def skip(self):
        """Melompati sesi saat ini ke sesi berikutnya (tanpa event timer-habis)."""
        with self._lock:
            if self.mode == "STUDY":
                self._save_session_log("STUDY")
                self.completed_sessions += 1
                if self.completed_sessions % 4 == 0:
                    self._start_locked("LONG_BREAK")
                else:
                    self._start_locked("BREAK")
            elif self.mode in ("BREAK", "LONG_BREAK"):
                self._save_session_log(self.mode)
                self._start_locked("STUDY", emit_session_start=True)
            status = self.get_status()
            pending, listeners = self._take_events()
        self._dispatch(pending, listeners)
        return status

    def _expire_locked(self):
        """Timer habis. STUDY -> BREAK/LONG_BREAK, istirahat -> STUDY."""
        if self.mode == "STUDY":
            self._save_session_log("STUDY")
            self._queue("SESSION_END")
            self.completed_sessions += 1
            if self.completed_sessions % 4 == 0:
                self._start_locked("LONG_BREAK")
            else:
                self._start_locked("BREAK")
        elif self.mode in ("BREAK", "LONG_BREAK"):
            self._save_session_log(self.mode)
            self._queue("BREAK_END")
            self._start_locked("STUDY", emit_session_start=True)

    def accept_suggested_break(self):
        """Pengguna menyetujui saran jeda adaptif."""
        with self._lock:
            self._log_intervention(
                "USER_ACCEPTED_BREAK",
                "User accepted adaptive break recommendation",
                "SWITCH_TO_BREAK",
            )
            if self.mode == "STUDY":
                self._save_session_log("STUDY (ADAPTIVE INTERRUPT)")
            self._start_locked("BREAK")
            self.suggested_break = False
            self.active_alert = None
            return self.get_status()

    def dismiss_alert(self):
        """Menutup notifikasi alert."""
        with self._lock:
            self.active_alert = None
            return self.get_status()

    def configure(self, study_min, break_min, long_break_min):
        """Set durasi (menit) dalam rentang aman."""
        study_min = max(1, min(int(study_min), 120))
        break_min = max(1, min(int(break_min), 60))
        long_break_min = max(1, min(int(long_break_min), 120))
        with self._lock:
            self.study_duration = study_min * 60
            self.break_duration = break_min * 60
            self.long_break_duration = long_break_min * 60
            if self.mode == "IDLE":
                self.time_remaining = self.study_duration
        return {
            "ok": True,
            "study_minutes": study_min,
            "break_minutes": break_min,
            "long_break_minutes": long_break_min,
        }

    def tick(self):
        """
        Majukan timer berdasarkan waktu nyata. Dipanggil ~1 Hz dari loop terpisah,
        termasuk saat kamera mati selama istirahat.
        """
        with self._lock:
            if self.is_running and self.mode in ALLOWED_MODES:
                now = time.time()
                if self.last_tick is None:
                    self.last_tick = now
                delta = now - self.last_tick
                self.last_tick = now
                if delta < 0:
                    delta = 0.0
                delta = min(delta, TICK_DELTA_CAP_SEC)
                self._tick_fraction += delta
                whole = int(self._tick_fraction)
                if whole > 0:
                    self._tick_fraction -= whole
                    self.time_remaining = max(0, self.time_remaining - whole)
                if self.time_remaining <= 0:
                    self._expire_locked()
            status = self.get_status()
            pending, listeners = self._take_events()
        self._dispatch(pending, listeners)
        return status

    def _maybe_emit_unfocused(self, now):
        """UNFOCUSED hanya saat STUDY, cooldown 30 detik."""
        if self.mode != "STUDY" or not self.is_running:
            return
        if now - self.last_unfocused_emit < UNFOCUSED_COOLDOWN_SEC:
            return
        self.last_unfocused_emit = now
        self._queue("UNFOCUSED")

    def update_telemetry(self, prediction_data):
        """
        Akumulasi atensi memakai delta waktu nyata (bukan +1 per frame).
        Tidak menggerakkan timer — itu tugas tick().
        """
        now = time.time()
        with self._lock:
            if not self.is_running or self.mode != "STUDY":
                self._telemetry_ts = now
                self._was_yawning = False
                self.unfocused_start_time = None
                self.sleep_start_time = None
                return self.get_status()

            if self._telemetry_ts is None:
                dt = 0.0
            else:
                dt = now - self._telemetry_ts
            self._telemetry_ts = now
            if dt < 0:
                dt = 0.0
            dt = min(dt, TELEMETRY_DT_CAP_SEC)

            label = prediction_data.get("label", 0)
            telemetry = prediction_data.get("telemetry", {}) or {}
            is_sleeping = bool(telemetry.get("is_sleeping", False))
            is_yawning = bool(telemetry.get("is_yawning", False))
            reason = telemetry.get("reason", "Normal")
            face_detected = bool(telemetry.get("face_detected", False))

            if label == 0 and face_detected:
                self.total_focus_seconds += dt
                self.unfocused_start_time = None
            elif label == 1 or not face_detected:
                self.total_unfocused_seconds += dt
                if self.unfocused_start_time is None:
                    self.unfocused_start_time = now
                    self.total_distraction_events += 1

            # Menguap dihitung sekali pada transisi False -> True
            if is_yawning and not self._was_yawning:
                self.yawn_timestamps.append(now)
                self.total_yawns += 1
            self._was_yawning = is_yawning

            if is_sleeping:
                if self.sleep_start_time is None:
                    self.sleep_start_time = now
                elif now - self.sleep_start_time >= SLEEP_ALERT_DURATION_SEC:
                    if now - self.last_sleep_log >= SLEEP_COOLDOWN_SEC:
                        self.total_sleep_events += 1
                        self.active_alert = "[PERINGATAN] Bangun! Kamu terdeteksi tertidur di depan materi."
                        self.suggested_break = True
                        self.last_sleep_log = now
                        self.last_intervention_time = now
                        self._log_intervention(
                            "SLEEP_DETECTED",
                            "Siswa tertidur > 3 detik",
                            "VISUAL_ALERT_AND_BREAK_PROPOSAL",
                        )
                        self._maybe_emit_unfocused(now)
            else:
                self.sleep_start_time = None

            if self.unfocused_start_time and (now - self.unfocused_start_time >= UNFOCUSED_ALERT_DURATION_SEC):
                if now - self.last_intervention_time > UNFOCUSED_COOLDOWN_SEC:
                    self.active_alert = f"[INFO] Konsentrasi menurun ({reason}). Tarik napas dan fokus kembali ke materi!"
                    self.last_intervention_time = now
                    self._log_intervention(
                        "PERSISTENT_UNFOCUSED",
                        f"Tidak fokus > 15s ({reason})",
                        "VISUAL_NUDGE",
                    )
                    self._maybe_emit_unfocused(now)

            recent_yawns = [t for t in self.yawn_timestamps if now - t <= 120.0]
            if len(recent_yawns) >= MAX_YAWNS_IN_WINDOW:
                if now - self.last_intervention_time > YAWN_ALERT_COOLDOWN_SEC:
                    self.active_alert = "[SARAN] Kamu sering menguap. Tingkat kelelahan kognitif tinggi!"
                    self.suggested_break = True
                    self.last_intervention_time = now
                    self._log_intervention(
                        "HIGH_FATIGUE_YAWNS",
                        "Menguap >= 2 kali dalam 2 menit",
                        "BREAK_PROPOSAL",
                    )

            status = self.get_status()
            pending, listeners = self._take_events()
        self._dispatch(pending, listeners)
        return status

    def get_status(self):
        """Mengambil dictionary status terkini dari layanan Pomodoro."""
        total_time = self.total_focus_seconds + self.total_unfocused_seconds
        focus_pct = (self.total_focus_seconds / total_time * 100.0) if total_time > 0 else 100.0

        mins = int(self.time_remaining) // 60
        secs = int(self.time_remaining) % 60
        time_formatted = f"{mins:02d}:{secs:02d}"

        return {
            "mode": self.mode,
            "is_running": self.is_running,
            "time_remaining": int(self.time_remaining),
            "time_formatted": time_formatted,
            "duration_sec": int(self._duration_for_mode()),
            "completed_sessions": self.completed_sessions,
            "subject": self.subject,
            "focus_percentage": round(focus_pct, 1),
            "total_focus_sec": int(self.total_focus_seconds),
            "total_unfocused_sec": int(self.total_unfocused_seconds),
            "distraction_count": self.total_distraction_events,
            "yawn_count": self.total_yawns,
            "sleep_count": self.total_sleep_events,
            "active_alert": self.active_alert,
            "suggested_break": self.suggested_break,
        }

    def _save_session_log(self, session_type):
        """Menyimpan riwayat sesi ke database SQLite."""
        total_time = self.total_focus_seconds + self.total_unfocused_seconds
        focus_pct = (self.total_focus_seconds / total_time * 100.0) if total_time > 0 else 100.0

        saved_type = session_type
        if "STUDY" in session_type:
            saved_type = getattr(self, "subject", None) or SUBJECT_FALLBACK

        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO session_logs (
                    timestamp, session_type, duration_sec, focus_percentage,
                    distraction_count, yawn_count, notes
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                saved_type,
                int(total_time),
                round(focus_pct, 1),
                self.total_distraction_events,
                self.total_yawns,
                f"Sleep events: {self.total_sleep_events}"
            ))
            conn.commit()
            conn.close()
        except Exception as e:
            print(f"[ERROR] Gagal menyimpan log sesi SQLite: {e}")

    def _log_intervention(self, trigger_type, reason, action_taken):
        """Menyimpan log intervensi ke database SQLite."""
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO intervention_logs (timestamp, trigger_type, reason, action_taken)
                VALUES (?, ?, ?, ?)
            """, (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), trigger_type, reason, action_taken))
            conn.commit()
            conn.close()
        except Exception as e:
            print(f"[ERROR] Gagal menyimpan log intervensi: {e}")

    def get_history(self, limit=10):
        """Mengambil data riwayat sesi terakhir untuk ditampilkan di web."""
        try:
            conn = sqlite3.connect(self.db_path)
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute("""
                SELECT * FROM session_logs ORDER BY id DESC LIMIT ?
            """, (limit,))
            rows = cursor.fetchall()
            conn.close()
            return [dict(r) for r in rows]
        except Exception:
            return []
