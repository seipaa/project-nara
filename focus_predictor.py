"""
focus_predictor.py
Inference Engine Real-Time untuk Prediksi Status Atensi Siswa
Menggabungkan:
1. MediaPipe FaceLandmarker (9 Indikator Visual Mentah per-frame)
2. SlidingWindowAggregator (3.0 Detik / ~90 frame) -> 20-Dimensional Feature Vector
3. StandardScaler (models/feature_scaler.joblib)
4. Model SVM RBF (models/focus_svm_model.joblib)

Output Prediksi:
- label: 0 (Risiko Rendah / Fokus) atau 1 (Risiko Tinggi / Tidak Fokus)
- label_text: "Fokus" vs "Tidak Fokus"
- p_risk: Probabilitas risiko penurunan atensi (0.0 - 1.0)
- focus_score: Skor fokus siswa (0 - 100%)
- telemetry: Nilai indikator mentah (Yaw, Pitch, Roll, EAR, MAR, Gaze, Sleep, Yawn)
"""

import os
import sys
import time
import math
from collections import deque

import cv2
import numpy as np
from joblib import load
import mediapipe as mp
from mediapipe.tasks import python
from mediapipe.tasks.python import vision

# Import fungsi dasar dari cv_focus_engine & dataset_collector
from cv_focus_engine import (
    ensure_model_exists, MODEL_PATH, VideoSource,
    get_head_pose_from_matrix, eye_aspect_ratio, mouth_aspect_ratio,
    extract_blendshape_metrics,
    LEFT_EYE_EAR_IDX, RIGHT_EYE_EAR_IDX,
    BLINK_SCORE_THRESHOLD, EAR_THRESHOLD,
    GAZE_LOOK_DOWN_THRESHOLD, GAZE_LOOK_SIDE_THRESHOLD, GAZE_LOOK_UP_THRESHOLD,
    MAR_YAWN_THRESHOLD, JAW_OPEN_THRESHOLD, YAWN_DURATION_SEC,
    YAW_THRESHOLD_DEG, PITCH_LOOK_DOWN_DEG, PITCH_LOOK_UP_DEG,
    draw_face_mesh_contours, draw_head_pose_axes, draw_gaze_and_eye_status
)
from dataset_collector import SlidingWindowAggregator, FEATURE_COLUMNS

DEFAULT_MODEL_PATH = os.path.join("models", "focus_svm_model.joblib")
DEFAULT_SCALER_PATH = os.path.join("models", "feature_scaler.joblib")


class FocusPredictor:
    """
    Kelas Inference Real-time yang memproses frame webcam dan menghasilkan
    prediksi atensi siswa berbasis ML (SVM).
    """

    def __init__(self, model_path=DEFAULT_MODEL_PATH, scaler_path=DEFAULT_SCALER_PATH,
                 window_sec=3.0, fps=30):
        ensure_model_exists()

        if not os.path.exists(model_path) or not os.path.exists(scaler_path):
            raise FileNotFoundError(
                f"Model SVM ({model_path}) atau Scaler ({scaler_path}) tidak ditemukan! "
                "Jalankan train_svm.py terlebih dahulu."
            )

        print(f"[INFO] Memuat Model SVM dari: {model_path}")
        self.svm_model = load(model_path)
        print(f"[INFO] Memuat Feature Scaler dari: {scaler_path}")
        self.scaler = load(scaler_path)

        # Inisialisasi MediaPipe Tasks FaceLandmarker
        base_options = python.BaseOptions(model_asset_path=MODEL_PATH)
        options = vision.FaceLandmarkerOptions(
            base_options=base_options,
            output_face_blendshapes=True,
            output_facial_transformation_matrixes=True,
            num_faces=1,
            min_face_detection_confidence=0.5,
            min_face_presence_confidence=0.5,
            min_tracking_confidence=0.5
        )
        self.landmarker = vision.FaceLandmarker.create_from_options(options)

        # Sliding window buffer
        self.aggregator = SlidingWindowAggregator(window_sec=window_sec, fps=fps)
        self.fps = fps

        # Hysteresis Thresholding & EMA Smoothing untuk kasus 50:50
        # Mencegah flickering/jumping saat probabilitas berada di dekat batas 0.50
        self.HIGH_RISK_ENTER_THRESH = 0.55  # Butuh probabilitas >= 55% untuk pindah ke Risiko Tinggi
        self.LOW_RISK_ENTER_THRESH = 0.45   # Butuh probabilitas < 45% untuk kembali ke Risiko Rendah
        self.current_label = 0              # State memory status aktif
        self.smoothed_p_risk = 0.05         # Exponential moving average probabilitas

        # Tracking Keberadaan Wajah
        self.absent_start_time = None
        self.has_detected_face_ever = False

        # Status State (Default: Standby / Menunggu Deteksi Wajah)
        self.latest_prediction = {
            "label": -1,
            "label_text": "Menunggu Deteksi Wajah...",
            "p_risk": 0.0,
            "focus_score": 0.0,
            "is_ready": False,
            "status_color": (128, 128, 128),
            "telemetry": {
                "face_detected": False,
                "yaw": 0.0,
                "pitch": 0.0,
                "roll": 0.0,
                "ear": 0.0,
                "mar": 0.0,
                "blink_score": 0.0,
                "gaze_down": 0.0,
                "gaze_up": 0.0,
                "gaze_side": 0.0,
                "jaw_open": 0.0,
                "is_sleeping": False,
                "is_yawning": False,
                "reason": "Wajah Belum Terdeteksi di Depan Kamera"
            }
        }

        # Timer untuk Sleep dan Yawn per frame
        self.sleep_start_time = None
        self.yawn_start_time = None
        self.look_up_start_time = None

    def process_frame(self, frame):
        """
        Memproses 1 frame video:
        1. MediaPipe extraction
        2. Push ke sliding window
        3. Jika window siap: ekstrak 20 fitur -> scale -> SVM predict_proba
        4. Render HUD ke frame
        Return: (annotated_frame, prediction_dict)
        """
        h, w = frame.shape[:2]
        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        mp_image = mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb)

        try:
            result = self.landmarker.detect(mp_image)
        except Exception as e:
            result = None

        face_detected = result is not None and len(result.face_landmarks) > 0
        now = time.time()

        yaw, pitch, roll = 0.0, 0.0, 0.0
        ear = 0.0
        mar = 0.0
        blink_score, gaze_down, gaze_up, gaze_side, jaw_open = 0.0, 0.0, 0.0, 0.0, 0.0
        is_sleeping = False
        is_yawning = False
        reason = "Normal"

        if face_detected:
            # Jika wajah baru kembali setelah sempat hilang, reset buffer dan smoothed risk
            if self.absent_start_time is not None:
                self.smoothed_p_risk = 0.10
                self.current_label = 0

            self.has_detected_face_ever = True
            self.absent_start_time = None

            lm = result.face_landmarks[0]
            landmarks_px = [(int(l.x * w), int(l.y * h)) for l in lm]

            # Head Pose 3D
            R, yaw, pitch, roll = get_head_pose_from_matrix(result.facial_transformation_matrixes[0])

            # EAR & MAR
            ear_l = eye_aspect_ratio(landmarks_px, LEFT_EYE_EAR_IDX)
            ear_r = eye_aspect_ratio(landmarks_px, RIGHT_EYE_EAR_IDX)
            ear = (ear_l + ear_r) / 2.0
            mar = mouth_aspect_ratio(landmarks_px)

            # Blendshapes
            blink_score, gaze_down, gaze_up, gaze_side, jaw_open = extract_blendshape_metrics(result.face_blendshapes)

            # Sleep timer check (dual signal)
            is_eyes_closed = (blink_score > BLINK_SCORE_THRESHOLD) or (ear < EAR_THRESHOLD)
            if is_eyes_closed:
                if self.sleep_start_time is None:
                    self.sleep_start_time = now
                elif now - self.sleep_start_time >= 1.0:
                    is_sleeping = True
                    reason = "Mata Terpejam / Tidur"
            else:
                self.sleep_start_time = None

            # Yawn timer check
            if mar > MAR_YAWN_THRESHOLD and jaw_open > JAW_OPEN_THRESHOLD:
                if self.yawn_start_time is None:
                    self.yawn_start_time = now
                elif now - self.yawn_start_time >= YAWN_DURATION_SEC:
                    is_yawning = True
                    reason = "Menguap / Kelelahan"
            else:
                self.yawn_start_time = None

            # Rule heuristic check untuk reason message
            if not is_sleeping and not is_yawning:
                if abs(yaw) > YAW_THRESHOLD_DEG:
                    reason = "Menoleh ke Samping"
                elif pitch < PITCH_LOOK_DOWN_DEG or gaze_down > GAZE_LOOK_DOWN_THRESHOLD:
                    reason = "Menunduk / Main HP"
                elif pitch > PITCH_LOOK_UP_DEG or gaze_up > GAZE_LOOK_UP_THRESHOLD:
                    if self.look_up_start_time is None:
                        self.look_up_start_time = now
                    elif now - self.look_up_start_time > 3.0:
                        reason = "Melamun ke Atas"
                    else:
                        reason = "Sedang Berpikir"
                elif gaze_side > GAZE_LOOK_SIDE_THRESHOLD:
                    reason = "Melirik ke Samping"
                else:
                    self.look_up_start_time = None
                    reason = "Fokus Menatap Materi"

            # Push ke aggregator
            self.aggregator.push_frame(
                True, yaw, pitch, roll, ear, mar,
                blink_score, gaze_down, gaze_up, gaze_side, jaw_open
            )

            # Gambar visual HUD di frame
            status_color = (0, 255, 0) if self.latest_prediction["label"] == 0 else (0, 0, 255)
            draw_face_mesh_contours(frame, landmarks_px, status_color)
            nose_pt = landmarks_px[1]
            draw_head_pose_axes(frame, R, yaw, pitch, roll, nose_pt)
            draw_gaze_and_eye_status(frame, landmarks_px, is_eyes_closed, gaze_down, gaze_up, is_yawning)

        else:
            self.aggregator.push_frame(False)

            if not self.has_detected_face_ever:
                reason = "Wajah Belum Terdeteksi di Depan Kamera"
                self.latest_prediction.update({
                    "label": -1,
                    "label_text": "Menunggu Deteksi Wajah...",
                    "p_risk": 0.0,
                    "focus_score": 0.0,
                    "status_color": (128, 128, 128)
                })
            else:
                if self.absent_start_time is None:
                    self.absent_start_time = now

                absent_dur = now - self.absent_start_time
                if absent_dur >= 1.5:
                    reason = "Wajah Tidak Terdeteksi di Layar"
                    self.current_label = 1
                    self.smoothed_p_risk = 1.0
                    self.latest_prediction.update({
                        "label": 1,
                        "label_text": "Tidak Ada Siswa (Tidak Terdeteksi)",
                        "p_risk": 1.0,
                        "focus_score": 0.0,
                        "status_color": (0, 0, 255)
                    })
                else:
                    reason = "Mendeteksi Posisi Wajah..."

        # Update telemetry
        self.latest_prediction["telemetry"] = {
            "face_detected": face_detected,
            "yaw": round(yaw, 1),
            "pitch": round(pitch, 1),
            "roll": round(roll, 1),
            "ear": round(ear, 3),
            "mar": round(mar, 3),
            "blink_score": round(blink_score, 2),
            "gaze_down": round(gaze_down, 2),
            "gaze_up": round(gaze_up, 2),
            "gaze_side": round(gaze_side, 2),
            "jaw_open": round(jaw_open, 2),
            "is_sleeping": is_sleeping,
            "is_yawning": is_yawning,
            "reason": reason
        }

        # Cek apakah buffer sliding window sudah penuh untuk prediksi ML (hanya saat wajah aktif)
        if face_detected and self.aggregator.is_ready():
            features = self.aggregator.compute_features()
            feat_array = np.array(features).reshape(1, -1)
            scaled_feat = self.scaler.transform(feat_array)

            # Prediksi SVM Probabilitas
            prob = self.svm_model.predict_proba(scaled_feat)[0]
            p_risk_raw = float(prob[1])  # Probabilitas kelas 1 (Risiko Tinggi)

            # Fast Recovery EMA: Jika risiko turun (kembali fokus), respons dipercepat (alpha=0.85)
            # Jika risiko naik, gunakan alpha=0.70
            alpha = 0.85 if p_risk_raw < self.smoothed_p_risk else 0.70
            self.smoothed_p_risk = alpha * p_risk_raw + (1.0 - alpha) * self.smoothed_p_risk
            p_risk = self.smoothed_p_risk

            # Hysteresis Thresholding Seimbang & Responsif
            if self.current_label == 0:
                if p_risk >= 0.52:
                    self.current_label = 1  # Pindah ke Tidak Fokus
            else:
                if p_risk < 0.48:
                    self.current_label = 0  # Cepat kembali ke Fokus

            label = self.current_label
            focus_score = round((1.0 - p_risk) * 100.0, 1)

            # Inisialisasi teks status & warna default berdasarkan label
            label_text = "Fokus (Risiko Rendah)" if label == 0 else "Tidak Fokus (Risiko Tinggi)"
            status_color = (0, 255, 0) if label == 0 else (0, 0, 255)

            # Sinkronisasi Keterangan dengan Keputusan SVM
            # Mencegah kontradiksi visual saat posisi saat ini sudah tegak tapi window 3s masih membawa sisa memori distraksi
            if label == 1 and reason == "Fokus Menatap Materi":
                # Posisi saat ini sudah lurus, tapi SVM masih menahan memori window distraksi sebelumnya
                # Jika pose saat ini stabil menatap layar, langsung percepat pemulihan ke Fokus (0)
                is_currently_neutral = (abs(yaw) < 12.0 and -14.0 < pitch < 10.0 and gaze_down < 0.35 and ear > 0.24)
                if is_currently_neutral:
                    self.current_label = 0
                    label = 0
                    p_risk = min(p_risk, 0.35)
                    focus_score = round((1.0 - p_risk) * 100.0, 1)
                    label_text = "Fokus (Risiko Rendah)"
                    status_color = (0, 255, 0)
                    reason = "Fokus Menatap Materi"
                else:
                    reason = "Memulihkan Fokus (Transisi Window)"

            self.latest_prediction["telemetry"]["reason"] = reason

            self.latest_prediction.update({
                "label": label,
                "label_text": label_text,
                "p_risk": round(p_risk, 3),
                "focus_score": focus_score,
                "is_ready": True,
                "status_color": status_color
            })

            # Overlap 50%
            self.aggregator.clear_half()

        # Overlay text di video
        self._draw_overlay_hud(frame, h, w)

        return frame, self.latest_prediction

    def _draw_overlay_hud(self, frame, h, w):
        """Menggambar box status ML di atas frame video."""
        pred = self.latest_prediction
        label = pred["label"]
        label_text = pred["label_text"]
        p_risk = pred["p_risk"]
        focus_score = pred["focus_score"]
        reason = pred["telemetry"]["reason"]

        bg_color = (30, 120, 30) if label == 0 else (30, 30, 180)
        cv2.rectangle(frame, (10, 10), (320, 85), bg_color, -1)
        cv2.rectangle(frame, (10, 10), (320, 85), (255, 255, 255), 1)

        cv2.putText(frame, f"STATUS: {label_text}", (20, 35),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.55, (255, 255, 255), 2)
        cv2.putText(frame, f"Focus Score: {focus_score:.1f}% | Risk: {p_risk*100:.1f}%", (20, 58),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.45, (200, 255, 200) if label == 0 else (200, 200, 255), 1)
        cv2.putText(frame, f"Info: {reason}", (20, 78),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.40, (230, 230, 230), 1)
