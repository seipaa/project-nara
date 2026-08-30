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
"""

import os
import time
import sqlite3
from datetime import datetime
from collections import deque

DB_PATH = "pomodoro_sessions.db"

# Mode Pomodoro (dalam detik)
DEFAULT_STUDY_DURATION = 25 * 60      # 25 Menit
DEFAULT_BREAK_DURATION = 5 * 60       # 5 Menit
DEFAULT_LONG_BREAK_DURATION = 15 * 60 # 15 Menit

# Ambang Batas Intervensi
UNFOCUSED_ALERT_DURATION_SEC = 15.0   # 15 detik tidak fokus -> Peringatan visual
MAX_YAWNS_IN_WINDOW = 2               # 2x menguap dalam window -> Saran istirahat
SLEEP_ALERT_DURATION_SEC = 3.0        # 3 detik tidur -> Alarm bangunkan


class PomodoroService:
    def __init__(self, db_path=DB_PATH):
        self.db_path = db_path
        self._init_db()

        # State
        self.mode = "IDLE"  # "IDLE", "STUDY", "BREAK", "LONG_BREAK", "PAUSED"
        self.study_duration = DEFAULT_STUDY_DURATION
        self.break_duration = DEFAULT_BREAK_DURATION
        self.long_break_duration = DEFAULT_LONG_BREAK_DURATION
        self.completed_sessions = 0

        # Timers
        self.time_remaining = self.study_duration
        self.is_running = False
        self.last_tick = None
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
        self.active_alert = None          # Visual nudge string
        self.suggested_break = False      # Rekomendasi istirahat aktif
        self.last_intervention_time = 0

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

    def start(self, mode="STUDY"):
        """Memulai timer Pomodoro."""
        self.mode = mode
        if mode == "STUDY":
            self.time_remaining = self.study_duration
        elif mode == "BREAK":
            self.time_remaining = self.break_duration
        elif mode == "LONG_BREAK":
            self.time_remaining = self.long_break_duration

        self.is_running = True
        self.last_tick = time.time()
        self.session_start_time = datetime.now().isoformat()
        self.total_focus_seconds = 0.0
        self.total_unfocused_seconds = 0.0
        self.total_distraction_events = 0
        self.total_yawns = 0
        self.total_sleep_events = 0
        self.active_alert = None
        self.suggested_break = False
        return self.get_status()

    def pause(self):
        """Menjeda timer."""
        self.is_running = False
        return self.get_status()

    def resume(self):
        """Melanjutkan timer."""
        if self.mode != "IDLE":
            self.is_running = True
            self.last_tick = time.time()
        return self.get_status()

    def reset(self):
        """Mereset timer ke awal."""
        self.is_running = False
        self.mode = "IDLE"
        self.time_remaining = self.study_duration
        self.active_alert = None
        self.suggested_break = False
        return self.get_status()

    def skip(self):
        """Melompati sesi saat ini ke sesi berikutnya."""
        if self.mode == "STUDY":
            self._save_session_log("STUDY")
            self.completed_sessions += 1
            if self.completed_sessions % 4 == 0:
                self.start("LONG_BREAK")
            else:
                self.start("BREAK")
        else:
            self._save_session_log(self.mode)
            self.start("STUDY")
        return self.get_status()

    def accept_suggested_break(self):
        """Pengguna menyetujui saran jeda adaptif."""
        self._log_intervention("USER_ACCEPTED_BREAK", "User accepted adaptive break recommendation", "SWITCH_TO_BREAK")
        self._save_session_log("STUDY (ADAPTIVE INTERRUPT)")
        self.start("BREAK")
        self.suggested_break = False
        self.active_alert = None
        return self.get_status()

    def dismiss_alert(self):
        """Menutup notifikasi alert."""
        self.active_alert = None
        return self.get_status()

    def update_telemetry(self, prediction_data):
        """
        Menerima data prediksi ML dari focus_predictor.py per interval (misal 1 detik)
        dan mengevaluasi kondisi penurunan atensi.
        """
        now = time.time()

        # Update waktu timer jika berjalan
        if self.is_running and self.last_tick is not None:
            delta = now - self.last_tick
            self.time_remaining = max(0, self.time_remaining - int(delta))
            self.last_tick = now

            if self.time_remaining <= 0:
                self.skip()

        if not self.is_running or self.mode != "STUDY":
            return self.get_status()

        # Evaluasi atensi hanya saat dalam mode STUDY
        label = prediction_data.get("label", 0)
        p_risk = prediction_data.get("p_risk", 0.0)
        telemetry = prediction_data.get("telemetry", {})
        is_sleeping = telemetry.get("is_sleeping", False)
        is_yawning = telemetry.get("is_yawning", False)
        reason = telemetry.get("reason", "Normal")

        face_detected = telemetry.get("face_detected", False)

        # Akumulasi durasi
        if label == 0 and face_detected:
            self.total_focus_seconds += 1.0
            self.unfocused_start_time = None
        elif label == 1 or not face_detected:
            self.total_unfocused_seconds += 1.0
            if self.unfocused_start_time is None:
                self.unfocused_start_time = now
                self.total_distraction_events += 1

        # Track Yawn
        if is_yawning:
            self.yawn_timestamps.append(now)
            self.total_yawns += 1

        # Track Sleep
        if is_sleeping:
            if self.sleep_start_time is None:
                self.sleep_start_time = now
            elif now - self.sleep_start_time >= SLEEP_ALERT_DURATION_SEC:
                self.total_sleep_events += 1
                self.active_alert = "[PERINGATAN] Bangun! Kamu terdeteksi tertidur di depan materi."
                self.suggested_break = True
                self._log_intervention("SLEEP_DETECTED", "Siswa tertidur > 3 detik", "VISUAL_ALERT_AND_BREAK_PROPOSAL")
        else:
            self.sleep_start_time = None

        # Evaluasi Durasi Tidak Fokus Persisten
        if self.unfocused_start_time and (now - self.unfocused_start_time >= UNFOCUSED_ALERT_DURATION_SEC):
            if now - self.last_intervention_time > 30.0:  # Cooldown 30s
                self.active_alert = f"[INFO] Konsentrasi menurun ({reason}). Tarik napas dan fokus kembali ke materi!"
                self.last_intervention_time = now
                self._log_intervention("PERSISTENT_UNFOCUSED", f"Tidak fokus > 15s ({reason})", "VISUAL_NUDGE")

        # Evaluasi Frekuensi Menguap (Kelelahan Kognitif)
        recent_yawns = [t for t in self.yawn_timestamps if now - t <= 120.0]
        if len(recent_yawns) >= MAX_YAWNS_IN_WINDOW:
            if now - self.last_intervention_time > 45.0:
                self.active_alert = "[SARAN] Kamu sering menguap. Tingkat kelelahan kognitif tinggi!"
                self.suggested_break = True
                self.last_intervention_time = now
                self._log_intervention("HIGH_FATIGUE_YAWNS", "Menguap >= 2 kali dalam 2 menit", "BREAK_PROPOSAL")

        return self.get_status()

    def get_status(self):
        """Mengambil dictionary status terkini dari layanan Pomodoro."""
        total_time = self.total_focus_seconds + self.total_unfocused_seconds
        focus_pct = (self.total_focus_seconds / total_time * 100.0) if total_time > 0 else 100.0

        mins = self.time_remaining // 60
        secs = self.time_remaining % 60
        time_formatted = f"{mins:02d}:{secs:02d}"

        return {
            "mode": self.mode,
            "is_running": self.is_running,
            "time_remaining": self.time_remaining,
            "time_formatted": time_formatted,
            "completed_sessions": self.completed_sessions,
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
                session_type,
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
        except Exception as e:
            return []
