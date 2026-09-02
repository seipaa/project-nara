"""
cv_focus_engine.py
Modul Computer Vision Canggih untuk Deteksi Status Fokus Siswa (2 Klasifikasi: FOKUS vs TIDAK FOKUS)
Menggunakan Google MediaPipe Tasks FaceLandmarker (Face Mesh + 52 Blendshapes + 3D Transformation Matrix).

Fitur Analisis Cerdas:
1. Orientasi Kepala 3D (Yaw, Pitch, Roll) — Menoleh, Menunduk ke Meja, Mendongak.
2. Analisis Menatap ke Atas (Smart Thinking Buffer) — Membedakan proses berpikir (<= 3.0s: FOKUS) vs melamun (> 3.0s: TIDAK FOKUS).
3. Deteksi Tidur / Merem Presisi Tinggi — Dual-Signal (Blendshape Blink Score + EAR + Sleep Timer).
4. Pandangan Mata / Gaze Tracking — Menghadap layar tapi mata melirik HP di bawah atau melirik ke samping.
5. Deteksi Menguap & Kelelahan (MAR / Mouth Aspect Ratio + Blendshape jawOpen).
6. Kehadiran Siswa — Wajah tidak ada di depan kamera.

Install requirement:
    pip install opencv-python mediapipe numpy

Cara pakai:
    python cv_focus_engine.py --source 0
    python cv_focus_engine.py --source http://192.168.1.50/stream

Tekan 'q' atau klik tombol [X] untuk keluar.
"""

import argparse
import math
import os
import sys
import time
import urllib.request
from collections import deque

import cv2
import numpy as np
import mediapipe as mp
from mediapipe.tasks import python
from mediapipe.tasks.python import vision


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
MODEL_URL = "https://storage.googleapis.com/mediapipe-models/face_landmarker/face_landmarker/float16/1/face_landmarker.task"
if os.path.exists(os.path.join(BASE_DIR, "face_landmarker.task")):
    MODEL_PATH = os.path.join(BASE_DIR, "face_landmarker.task")
elif os.path.exists(os.path.join(BASE_DIR, "web_dashboard", "face_landmarker.task")):
    MODEL_PATH = os.path.join(BASE_DIR, "web_dashboard", "face_landmarker.task")
else:
    MODEL_PATH = os.path.join(BASE_DIR, "face_landmarker.task")

# --- Threshold Sudut Kepala (Head Pose Degrees) ---
# Normal menatap laptop: Yaw ±18°, Pitch -15° s/d +12° (setelah kalibrasi baseline laptop)
YAW_THRESHOLD_DEG = 18.0          # Menoleh kiri/kanan > 18° -> Tidak Fokus
PITCH_LOOK_UP_DEG = 12.0          # Mendongak ke atas > +12° -> Masuk evaluasi menatap atas
PITCH_LOOK_DOWN_DEG = -15.0       # Menunduk ke bawah < -15° (lihat meja/paha) -> Tidak Fokus

# --- Threshold Pandangan Mata (Gaze Blendshapes 0.0 - 1.0) ---
GAZE_LOOK_UP_THRESHOLD = 0.35     # Mata menatap ke atas (bisa berpikir atau melamun)
GAZE_LOOK_DOWN_THRESHOLD = 0.40   # Mata menatap ke bawah (main HP di tangan/meja)
GAZE_LOOK_SIDE_THRESHOLD = 0.45   # Mata melirik ke samping

# --- Threshold Mata Terpejam / Tidur (High Precision Dual-Signal) ---
BLINK_SCORE_THRESHOLD = 0.55      # Blink score > 0.55
EAR_THRESHOLD = 0.22              # EAR < 0.22
SLEEP_DURATION_SEC = 1.0          # Merem terus-menerus >= 1.0 detik dianggap tidur (kedipan normal 0.2-0.4s diabaikan)

# --- Threshold Menguap / Yawning (MAR & jawOpen) ---
MAR_YAWN_THRESHOLD = 0.50         # Mouth Aspect Ratio > 0.50 dianggap mulut terbuka lebar
JAW_OPEN_THRESHOLD = 0.45         # Blendshape jawOpen > 0.45
YAWN_DURATION_SEC = 1.5           # Menguap >= 1.5 detik dianggap tanda kelelahan kognitif

# --- Threshold Waktu & Toleransi Berpikir (Cognitive Gaze Aversion) ---
THINKING_MAX_DURATION_SEC = 3.0   # Toleransi menatap ke atas saat berpikir:
                                  # <= 3.0 detik = FOKUS (Sedang Berpikir)
                                  # >  3.0 detik = TIDAK FOKUS (Melamun / Zoning Out)

PHONE_DISTRACTION_SEC = 1.0       # Melihat HP / melirik samping >= 1.0s -> TIDAK FOKUS
UNFOCUSED_DURATION_SEC = 1.0      # Durasi umum kondisi tidak fokus (menoleh/menunduk)
SMOOTHING_WINDOW = 5              # Buffer smoothing frame untuk meredam jitter kamera


# ---------------------------------------------------------------------------
# 2. AUTO DOWNLOAD MODEL MEDIAPIPE
# ---------------------------------------------------------------------------
def ensure_model_exists(model_path=MODEL_PATH):
    """
    Memastikan file model .task tersedia. Jika belum ada, download otomatis dari CDN Google.
    Jika jaringan memblokir storage.googleapis.com, ada pesan error dengan link download manual.
    """
    if os.path.exists(model_path) and os.path.getsize(model_path) > 0:
        return model_path

    print(f"[INFO] Model '{model_path}' tidak ditemukan. Mengunduh otomatis dari Google (~3.8 MB)...")
    try:
        urllib.request.urlretrieve(MODEL_URL, model_path)
        print(f"[SUCCESS] Model berhasil diunduh dan disimpan ke: {os.path.abspath(model_path)}")
        return model_path
    except Exception as e:
        print("\n" + "=" * 70)
        print("[ERROR] Gagal mengunduh file model 'face_landmarker.task' secara otomatis!")
        print(f"Detail error: {e}")
        print("-" * 70)
        print("Penyebab: Koneksi internet terputus atau domain 'storage.googleapis.com' diblokir firewall.")
        print("\nSOLUSI MANUAL:")
        print("1. Unduh file model secara manual dari link berikut:")
        print(f"   {MODEL_URL}")
        print("2. Simpan file tersebut dengan nama 'face_landmarker.task' di folder project ini:")
        print(f"   {os.path.abspath(os.path.dirname(model_path) or '.')}")
        print("=" * 70 + "\n")
        sys.exit(1)


# ---------------------------------------------------------------------------
# 3. SUMBER VIDEO — Webcam Laptop atau Stream ESP32 (MJPEG / RTSP)
# ---------------------------------------------------------------------------
class VideoSource:
    def __init__(self, source, reconnect_delay=2.0):
        self.source = int(source) if str(source).isdigit() else source
        self.reconnect_delay = reconnect_delay
        self.cap = None
        self._open()

    def _open(self):
        self.cap = cv2.VideoCapture(self.source)

    def read(self):
        if self.cap is None or not self.cap.isOpened():
            self._open()
        ok, frame = self.cap.read()
        if not ok:
            print("[WARN] Gagal ambil frame (WiFi/stream putus?), reconnecting...")
            time.sleep(self.reconnect_delay)
            self._open()
            ok, frame = self.cap.read()
        return ok, frame

    def release(self):
        if self.cap is not None:
            self.cap.release()


# ---------------------------------------------------------------------------
# 4. KONEKSI LANDMARK WAJAH & IRIS
# ---------------------------------------------------------------------------
LEFT_EYE_EAR_IDX = [362, 385, 387, 263, 373, 380]
RIGHT_EYE_EAR_IDX = [33, 160, 158, 133, 153, 144]

FACE_OVAL = [10, 338, 297, 332, 284, 251, 389, 356, 454, 323, 361, 288, 397, 365, 379, 378, 400, 377, 152, 148, 176, 149, 150, 136, 172, 58, 132, 93, 234, 127, 162, 21, 54, 103, 67, 109, 10]
LIPS_OUTER = [61, 146, 91, 181, 84, 17, 314, 405, 321, 375, 291, 409, 270, 269, 267, 0, 37, 39, 40, 185, 61]
LEFT_EYE_CONTOUR = [33, 7, 163, 144, 145, 153, 154, 155, 133, 173, 157, 158, 159, 160, 161, 246, 33]
RIGHT_EYE_CONTOUR = [362, 382, 381, 380, 374, 373, 390, 249, 263, 466, 388, 387, 386, 385, 384, 398, 362]
LEFT_EYEBROW = [70, 63, 105, 66, 107]
RIGHT_EYEBROW = [336, 296, 334, 293, 300]
NOSE_BRIDGE = [168, 6, 197, 195, 5, 4, 1, 2]
NOSE_TIP_IDX = 1

# Iris Center Landmarks
LEFT_PUPIL_IDX = 468
RIGHT_PUPIL_IDX = 473

# Mouth Landmarks for MAR
LIPS_INNER_TOP = 13
LIPS_INNER_BOTTOM = 14
MOUTH_CORNER_LEFT = 61
MOUTH_CORNER_RIGHT = 291


# ---------------------------------------------------------------------------
# 5. KALKULASI HEAD POSE, EAR, MAR & GAZE BLENDSHAPES
# ---------------------------------------------------------------------------
def get_head_pose_from_matrix(facial_transformation_matrix):
    """
    Mengekstrak sudut Euler (Yaw, Pitch, Roll) dalam derajat langsung dari
    matrix orientasi 4x4 MediaPipe dengan kalibrasi orientasi:
    - Pitch > 0 : Mendongak ke Atas (Looking UP)
    - Pitch < 0 : Menunduk ke Bawah (Looking DOWN)
    - Yaw > 0   : Menoleh ke Kanan (Looking RIGHT)
    - Yaw < 0   : Menoleh ke Kiri (Looking LEFT)
    """
    mat = np.array(facial_transformation_matrix)
    if mat.ndim == 1:
        mat = mat.reshape(4, 4)

    rotation_matrix = mat[:3, :3]
    angles, _, _, _, _, _ = cv2.RQDecomp3x3(rotation_matrix)
    pitch_raw, yaw_raw, roll_raw = angles[0], angles[1], angles[2]

    # Kalibrasi konvensi tanda & baseline laptop
    pitch = -pitch_raw - 8.0
    yaw = float(yaw_raw)
    roll = float(roll_raw)

    return rotation_matrix, yaw, float(pitch), roll


def eye_aspect_ratio(landmarks_px, idx):
    """
    Menghitung Eye Aspect Ratio (EAR) berdasarkan 6 titik landmark mata.
    """
    p = [np.array(landmarks_px[i]) for i in idx]
    vertical1 = np.linalg.norm(p[1] - p[5])
    vertical2 = np.linalg.norm(p[2] - p[4])
    horizontal = np.linalg.norm(p[0] - p[3])
    return (vertical1 + vertical2) / (2.0 * horizontal + 1e-6)


def mouth_aspect_ratio(landmarks_px):
    """
    Menghitung Mouth Aspect Ratio (MAR) berdasarkan titik bibir dalam dan sudut mulut.
    """
    if len(landmarks_px) <= MOUTH_CORNER_RIGHT:
        return 0.0

    p_top = np.array(landmarks_px[LIPS_INNER_TOP])
    p_bot = np.array(landmarks_px[LIPS_INNER_BOTTOM])
    p_left = np.array(landmarks_px[MOUTH_CORNER_LEFT])
    p_right = np.array(landmarks_px[MOUTH_CORNER_RIGHT])

    vertical = np.linalg.norm(p_top - p_bot)
    horizontal = np.linalg.norm(p_left - p_right)
    return vertical / (horizontal + 1e-6)


def extract_blendshape_metrics(face_blendshapes):
    """
    Mengekstrak skor mata terpejam, menatap atas, menatap bawah (HP), melirik, dan menguap (jawOpen).
    """
    if not face_blendshapes:
        return 0.0, 0.0, 0.0, 0.0, 0.0

    b_dict = {b.category_name: b.score for b in face_blendshapes[0]}

    # 1. Skor Mata Terpejam (Blink Score 0.0 - 1.0)
    blink_left = b_dict.get("eyeBlinkLeft", 0.0)
    blink_right = b_dict.get("eyeBlinkRight", 0.0)
    avg_blink = (blink_left + blink_right) / 2.0

    # 2. Skor Pandangan Mata ke Bawah (Look Down - melihat HP di meja/paha)
    look_down_left = b_dict.get("eyeLookDownLeft", 0.0)
    look_down_right = b_dict.get("eyeLookDownRight", 0.0)
    avg_look_down = (look_down_left + look_down_right) / 2.0

    # 3. Skor Pandangan Mata ke Atas (Look Up - berpikir / melamun ke plafon)
    look_up_left = b_dict.get("eyeLookUpLeft", 0.0)
    look_up_right = b_dict.get("eyeLookUpRight", 0.0)
    avg_look_up = (look_up_left + look_up_right) / 2.0

    # 4. Skor Pandangan Mata ke Samping (Look Side - melirik tanpa putar kepala)
    look_in_left = b_dict.get("eyeLookInLeft", 0.0)
    look_out_left = b_dict.get("eyeLookOutLeft", 0.0)
    look_in_right = b_dict.get("eyeLookInRight", 0.0)
    look_out_right = b_dict.get("eyeLookOutRight", 0.0)
    avg_look_side = max((look_out_left + look_in_right) / 2.0, (look_in_left + look_out_right) / 2.0)

    # 5. Skor Bukaan Rahang / Menguap (jawOpen)
    jaw_open = b_dict.get("jawOpen", 0.0)

    return avg_blink, avg_look_down, avg_look_up, avg_look_side, jaw_open


# ---------------------------------------------------------------------------
# 6. VISUALISASI VEKTOR, GAZE RAY, & MESH
# ---------------------------------------------------------------------------
def draw_face_mesh_contours(frame, landmarks_px, status_color):
    """
    Menggambar kontur fitur wajah (wajah, alis, mata, hidung, bibir).
    """
    contours = [
        (FACE_OVAL, False, (70, 70, 70), 1),
        (LEFT_EYEBROW, False, (0, 220, 255), 1),
        (RIGHT_EYEBROW, False, (0, 220, 255), 1),
        (LEFT_EYE_CONTOUR, True, (0, 255, 120), 1),
        (RIGHT_EYE_CONTOUR, True, (0, 255, 120), 1),
        (NOSE_BRIDGE, False, (255, 200, 0), 1),
        (LIPS_OUTER, True, (180, 100, 255), 1),
    ]

    for indices, is_closed, color, thickness in contours:
        pts = np.array([landmarks_px[i] for i in indices if i < len(landmarks_px)], dtype=np.int32)
        if len(pts) > 1:
            cv2.polylines(frame, [pts], isClosed=is_closed, color=color, thickness=thickness)


def draw_head_pose_axes(frame, R, yaw, pitch, roll, nose_pt, axis_length=65):
    """
    Menggambar Vektor Orientasi Kepala 3D dari ujung hidung dengan panah hadap kepala:
    - Pitch > 0 (Mendongak) -> Panah mengarah ke ATAS layar
    - Pitch < 0 (Menunduk)   -> Panah mengarah ke BAWAH layar
    - Yaw > 0   (Kanan)      -> Panah mengarah ke KANAN layar
    - Yaw < 0   (Kiri)       -> Panah mengarah ke KIRI layar
    """
    nx, ny = nose_pt

    x_axis = (int(nx + axis_length * R[0, 0]), int(ny - axis_length * R[1, 0]))
    y_axis = (int(nx + axis_length * R[0, 1]), int(ny - axis_length * R[1, 1]))

    ray_x = int(nx + (axis_length * 1.5) * math.sin(math.radians(yaw)))
    ray_y = int(ny - (axis_length * 1.5) * math.sin(math.radians(pitch)))

    cv2.line(frame, (nx, ny), x_axis, (0, 0, 255), 2)
    cv2.line(frame, (nx, ny), y_axis, (0, 255, 0), 2)
    cv2.arrowedLine(frame, (nx, ny), (ray_x, ray_y), (255, 255, 0), 3, tipLength=0.25)
    cv2.circle(frame, (nx, ny), 4, (0, 255, 255), -1)


def draw_gaze_and_eye_status(frame, landmarks_px, is_closed, look_down_score, look_up_score, is_yawning):
    """
    Menggambar titik pupil / iris, indikator pandangan mata, dan indikator menguap.
    """
    for p_idx in [LEFT_PUPIL_IDX, RIGHT_PUPIL_IDX]:
        if p_idx < len(landmarks_px):
            px, py = landmarks_px[p_idx]
            if is_closed:
                cv2.circle(frame, (px, py), 4, (0, 0, 255), -1)
            else:
                cv2.circle(frame, (px, py), 3, (0, 255, 255), -1)

                if look_down_score > GAZE_LOOK_DOWN_THRESHOLD:
                    gaze_target = (px, py + int(25 * look_down_score))
                    cv2.arrowedLine(frame, (px, py), gaze_target, (0, 140, 255), 2, tipLength=0.3)
                elif look_up_score > GAZE_LOOK_UP_THRESHOLD:
                    gaze_target = (px, py - int(25 * look_up_score))
                    cv2.arrowedLine(frame, (px, py), gaze_target, (0, 255, 255), 2, tipLength=0.3)

    if is_yawning and len(landmarks_px) > LIPS_INNER_BOTTOM:
        mx, my = landmarks_px[LIPS_INNER_BOTTOM]
        cv2.putText(frame, "YAWN", (mx - 20, my + 20), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (0, 0, 255), 1)


def draw_face_bounding_box(frame, landmarks_px, status_color):
    """
    Menggambar bounding box di sekeliling wajah dengan aksen sudut modern.
    """
    xs = [p[0] for p in landmarks_px]
    ys = [p[1] for p in landmarks_px]
    x_min, x_max = max(0, min(xs) - 15), min(frame.shape[1], max(xs) + 15)
    y_min, y_max = max(0, min(ys) - 25), min(frame.shape[0], max(ys) + 15)

    cv2.rectangle(frame, (x_min, y_min), (x_max, y_max), status_color, 1)

    corner_len = 15
    cv2.line(frame, (x_min, y_min), (x_min + corner_len, y_min), status_color, 3)
    cv2.line(frame, (x_min, y_min), (x_min, y_min + corner_len), status_color, 3)
    cv2.line(frame, (x_max, y_min), (x_max - corner_len, y_min), status_color, 3)
    cv2.line(frame, (x_max, y_min), (x_max - corner_len, y_min), status_color, 3)
    cv2.line(frame, (x_min, y_max), (x_min + corner_len, y_max), status_color, 3)
    cv2.line(frame, (x_min, y_max), (x_min, y_max - corner_len), status_color, 3)
    cv2.line(frame, (x_max, y_max), (x_max - corner_len, y_max), status_color, 3)
    cv2.line(frame, (x_max, y_max), (x_max - corner_len, y_max), status_color, 3)


# ---------------------------------------------------------------------------
# 7. STATE MACHINE FOKUS MULTI-MODAL CERDAS (2 KLASIFIKASI)
# ---------------------------------------------------------------------------
class MultiModalFocusStateMachine:
    def __init__(self):
        self.vote_buffer = deque(maxlen=SMOOTHING_WINDOW)
        self.unfocused_since = None
        self.sleep_since = None
        self.phone_gaze_since = None
        self.side_gaze_since = None
        self.thinking_since = None
        self.yawn_since = None
        self.current_state = "FOKUS"
        self.reason = "Menghadap Layar & Mata Terbuka"

    def update(self, yaw, pitch, ear_avg, mar_val, blink_score, look_down_score, look_up_score, look_side_score, jaw_open_score, face_detected):
        now = time.time()
        instant_flag = "FOCUSED"
        instant_reason = "Menghadap Layar & Mata Terbuka"

        # Deteksi mata terpejam presisi tinggi
        is_eye_closed = (blink_score > 0.65) or (ear_avg < 0.19) or (blink_score > 0.50 and ear_avg < 0.23)
        # Deteksi menguap (MAR + Blendshape jawOpen)
        is_yawning = (mar_val > MAR_YAWN_THRESHOLD) or (jaw_open_score > JAW_OPEN_THRESHOLD)

        # 1. Deteksi Wajah Hilang
        if not face_detected:
            instant_flag = "NOT_FOCUSED"
            instant_reason = "Wajah Tidak Terdeteksi"
            self.sleep_since = None
            self.thinking_since = None
            self.phone_gaze_since = None
            self.yawn_since = None

        # 2. Deteksi Menguap / Yawning (Kelelahan Kognitif)
        elif is_yawning:
            if self.yawn_since is None:
                self.yawn_since = now
            yawn_duration = now - self.yawn_since
            if yawn_duration >= YAWN_DURATION_SEC:
                instant_flag = "NOT_FOCUSED"
                instant_reason = f"Menguap / Kelelahan ({yawn_duration:.1f}s | MAR: {mar_val:.2f})"
            else:
                instant_reason = f"Membuka Mulut ({yawn_duration:.1f}s)"
        else:
            self.yawn_since = None

            # 3. Deteksi Mata Terpejam / Tidur Presisi
            if is_eye_closed:
                self.thinking_since = None
                self.phone_gaze_since = None
                if self.sleep_since is None:
                    self.sleep_since = now
                closed_duration = now - self.sleep_since
                if closed_duration >= SLEEP_DURATION_SEC:
                    instant_flag = "NOT_FOCUSED"
                    instant_reason = f"Mata Terpejam / Tidur ({closed_duration:.1f}s | Blink: {int(blink_score*100)}%)"
                else:
                    instant_reason = f"Kedipan Mata ({closed_duration:.1f}s)"
            else:
                self.sleep_since = None

                # 4. Deteksi Menoleh (Yaw)
                if abs(yaw) > YAW_THRESHOLD_DEG:
                    self.thinking_since = None
                    self.phone_gaze_since = None
                    instant_flag = "NOT_FOCUSED"
                    arah = "Kanan" if yaw > 0 else "Kiri"
                    instant_reason = f"Menoleh ke {arah} ({abs(yaw):.1f} deg)"

                # 5. Deteksi Menunduk ke Meja (Pitch Negatif)
                elif pitch < PITCH_LOOK_DOWN_DEG:
                    self.thinking_since = None
                    self.phone_gaze_since = None
                    instant_flag = "NOT_FOCUSED"
                    instant_reason = f"Menunduk ke Meja ({abs(pitch):.1f} deg)"

                # 6. Deteksi Menatap ke Atas (Mikir vs Melamun / Smart Cognitive Gaze Aversion)
                elif pitch > PITCH_LOOK_UP_DEG or look_up_score > GAZE_LOOK_UP_THRESHOLD:
                    self.phone_gaze_since = None
                    if self.thinking_since is None:
                        self.thinking_since = now
                    thinking_duration = now - self.thinking_since

                    if thinking_duration <= THINKING_MAX_DURATION_SEC:
                        # Masih dalam toleransi berpikir (0 - 3.0 detik) -> TETAP FOKUS
                        instant_flag = "FOCUSED"
                        instant_reason = f"Sedang Berpikir / Mengingat ({thinking_duration:.1f}s)"
                    else:
                        # Melebihi 3.0 detik -> TIDAK FOKUS (Melamun ke Plafon)
                        instant_flag = "NOT_FOCUSED"
                        instant_reason = f"Melamun / Menatap ke Atas ({thinking_duration:.1f}s)"
                else:
                    self.thinking_since = None

                    # 7. Deteksi Pandangan Mata ke Bawah (Main HP / Meja saat kepala lurus)
                    if look_down_score > GAZE_LOOK_DOWN_THRESHOLD:
                        if self.phone_gaze_since is None:
                            self.phone_gaze_since = now
                        phone_duration = now - self.phone_gaze_since
                        if phone_duration >= PHONE_DISTRACTION_SEC:
                            instant_flag = "NOT_FOCUSED"
                            instant_reason = f"Pandangan ke Bawah (Main HP/Meja {int(look_down_score*100)}%)"
                        else:
                            instant_reason = f"Melirik ke Bawah ({phone_duration:.1f}s)"
                    else:
                        self.phone_gaze_since = None

                        # 8. Deteksi Pandangan Mata Melirik ke Samping
                        if look_side_score > GAZE_LOOK_SIDE_THRESHOLD:
                            if self.side_gaze_since is None:
                                self.side_gaze_since = now
                            side_duration = now - self.side_gaze_since
                            if side_duration >= PHONE_DISTRACTION_SEC:
                                instant_flag = "NOT_FOCUSED"
                                instant_reason = f"Melirik ke Samping ({int(look_side_score*100)}%)"
                        else:
                            self.side_gaze_since = None

        # Smoothing majority vote
        self.vote_buffer.append(instant_flag)
        majority = max(set(self.vote_buffer), key=self.vote_buffer.count)

        if majority == "NOT_FOCUSED":
            if self.unfocused_since is None:
                self.unfocused_since = now
            elapsed = now - self.unfocused_since

            is_sleep_trigger = self.sleep_since and (now - self.sleep_since) >= SLEEP_DURATION_SEC
            is_yawn_trigger = self.yawn_since and (now - self.yawn_since) >= YAWN_DURATION_SEC
            is_thinking_distract_trigger = self.thinking_since and (now - self.thinking_since) > THINKING_MAX_DURATION_SEC
            is_phone_trigger = self.phone_gaze_since and (now - self.phone_gaze_since) >= PHONE_DISTRACTION_SEC

            if elapsed >= UNFOCUSED_DURATION_SEC or is_sleep_trigger or is_yawn_trigger or is_thinking_distract_trigger or is_phone_trigger:
                self.current_state = "TIDAK FOKUS"
                self.reason = instant_reason
        else:
            self.unfocused_since = None
            self.current_state = "FOKUS"
            self.reason = instant_reason

        return self.current_state, self.reason, is_eye_closed, is_yawning


# ---------------------------------------------------------------------------
# 8. MAIN LOOP (Debug, Calibration & HUD Dashboard)
# ---------------------------------------------------------------------------
def main(source):
    model_path = ensure_model_exists(MODEL_PATH)

    base_options = python.BaseOptions(model_asset_path=model_path)
    options = vision.FaceLandmarkerOptions(
        base_options=base_options,
        running_mode=vision.RunningMode.IMAGE,
        output_face_blendshapes=True,
        output_facial_transformation_matrixes=True,
        num_faces=1,
        min_face_detection_confidence=0.5,
        min_face_presence_confidence=0.5,
        min_tracking_confidence=0.5,
    )
    landmarker = vision.FaceLandmarker.create_from_options(options)

    video = VideoSource(source)
    fsm = MultiModalFocusStateMachine()

    prev_time = time.time()
    fps = 0.0

    print(f"[INFO] Focus Engine Multi-Modal Cerdas (FOKUS vs TIDAK FOKUS) berjalan pada: {source}")
    print("[INFO] Tekan 'q' atau klik tombol [X] untuk keluar.")

    try:
        while True:
            ok, frame = video.read()
            if not ok or frame is None:
                continue

            h, w = frame.shape[:2]
            rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            mp_image = mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb)

            detection_result = landmarker.detect(mp_image)

            face_detected = (
                detection_result.face_landmarks is not None
                and len(detection_result.face_landmarks) > 0
            )

            yaw = pitch = roll = 0.0
            ear_avg = 1.0
            mar_val = 0.0
            blink_score = look_down_score = look_up_score = look_side_score = jaw_open_score = 0.0
            R_matrix = None
            nose_pt = None
            landmarks_px = []

            if face_detected:
                # 1. Ekstrak Head Pose dari matrix transformasi Google
                if (
                    detection_result.facial_transformation_matrixes is not None
                    and len(detection_result.facial_transformation_matrixes) > 0
                ):
                    matrix = detection_result.facial_transformation_matrixes[0]
                    R_matrix, yaw, pitch, roll = get_head_pose_from_matrix(matrix)

                # 2. Ekstrak Landmark Piksel, EAR, dan MAR
                lm = detection_result.face_landmarks[0]
                landmarks_px = [(int(p.x * w), int(p.y * h)) for p in lm]

                ear_left = eye_aspect_ratio(landmarks_px, LEFT_EYE_EAR_IDX)
                ear_right = eye_aspect_ratio(landmarks_px, RIGHT_EYE_EAR_IDX)
                ear_avg = (ear_left + ear_right) / 2.0
                mar_val = mouth_aspect_ratio(landmarks_px)

                if NOSE_TIP_IDX < len(landmarks_px):
                    nose_pt = landmarks_px[NOSE_TIP_IDX]

                # 3. Ekstrak Blendshapes (Blink, Gaze Down, Gaze Up, Gaze Side, jawOpen)
                if detection_result.face_blendshapes:
                    blink_score, look_down_score, look_up_score, look_side_score, jaw_open_score = extract_blendshape_metrics(
                        detection_result.face_blendshapes
                    )

            # Update State Machine
            state, reason, is_eye_closed, is_yawning = fsm.update(
                yaw, pitch, ear_avg, mar_val, blink_score, look_down_score, look_up_score, look_side_score, jaw_open_score, face_detected
            )

            # Warna Status UI
            status_color = (0, 220, 0) if state == "FOKUS" else (0, 0, 255)

            # Gambar Visualisasi Lengkap
            if face_detected and len(landmarks_px) > 0:
                draw_face_mesh_contours(frame, landmarks_px, status_color)
                draw_face_bounding_box(frame, landmarks_px, status_color)
                draw_gaze_and_eye_status(frame, landmarks_px, is_eye_closed, look_down_score, look_up_score, is_yawning)

                if R_matrix is not None and nose_pt is not None:
                    draw_head_pose_axes(frame, R_matrix, yaw, pitch, roll, nose_pt, axis_length=65)

            # Kalkulasi FPS
            curr_time = time.time()
            fps = 0.9 * fps + 0.1 * (1.0 / max(curr_time - prev_time, 1e-5))
            prev_time = curr_time

            # Panel HUD Dashboard Modern
            cv2.rectangle(frame, (10, 10), (440, 210), (20, 20, 20), -1)
            cv2.rectangle(frame, (10, 10), (440, 210), status_color, 2)

            cv2.putText(frame, f"STATUS: {state}", (25, 45),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.85, status_color, 2)
            cv2.putText(frame, f"Keterangan: {reason}", (25, 75),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.48, (220, 220, 220), 1)

            # Baris Metrik Pose, Mata, dan Mulut
            cv2.putText(frame, f"Yaw: {yaw:+5.1f} deg  |  Pitch: {pitch:+5.1f} deg", (25, 105),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.52, (240, 240, 240), 1)
            cv2.putText(frame, f"EAR: {ear_avg:.2f}  |  MAR (Mulut): {mar_val:.2f}", (25, 130),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.52, (240, 240, 240), 1)
            cv2.putText(frame, f"Blink: {int(blink_score*100)}%  |  JawOpen: {int(jaw_open_score*100)}%", (25, 155),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.52, (240, 240, 240), 1)
            cv2.putText(frame, f"Gaze Down: {int(look_down_score*100)}% | Up: {int(look_up_score*100)}% | FPS: {fps:.1f}", (25, 182),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.46, (240, 240, 240), 1)

            cv2.imshow("Focus Detection - Debug & Calibration View", frame)

            key = cv2.waitKey(1) & 0xFF
            if key == ord("q"):
                print("[INFO] Tombol 'q' ditekan. Menutup aplikasi...")
                break
            if cv2.getWindowProperty("Focus Detection - Debug & Calibration View", cv2.WND_PROP_VISIBLE) < 1:
                print("[INFO] Jendela visualisasi ditutup. Menutup aplikasi...")
                break

    except KeyboardInterrupt:
        print("\n[INFO] Program dihentikan oleh pengguna (Ctrl+C). Membersihkan resource...")
    finally:
        landmarker.close()
        video.release()
        cv2.destroyAllWindows()
        print("[INFO] Selesai. Kamera dan window berhasil ditutup.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Student Focus & Attention Engine (Multi-Modal)")
    parser.add_argument(
        "--source",
        default="0",
        help="0 untuk webcam laptop, atau URL stream ESP32 (contoh: http://192.168.1.50/stream)"
    )
    args = parser.parse_args()
    main(args.source)