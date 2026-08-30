"""
dataset_collector.py
Modul Perekam Dataset Fitur 20-Dimensi untuk Pelatihan Model SVM

Modul ini menggunakan cv_focus_engine.py untuk mengekstrak 9 indikator visual mentah
per-frame, kemudian mengagregasikan data ke dalam Sliding Window (3.0 detik / ~90 frame)
untuk membentuk vektor fitur 20-dimensi dan menyimpannya ke CSV.

Fitur 20-Dimensi:
  A. Pola Pergerakan & Statistik (10): yaw_mean, yaw_std, pitch_mean, pitch_std,
     roll_std, ear_mean, ear_std, mar_mean, mar_max, head_velocity_mean
  B. Frekuensi Kejadian (4): blink_count, blink_rate_per_min, gaze_shift_count,
     yawn_event_count
  C. Durasi Kumulatif (6): drowsy_duration_ratio, phone_gaze_duration_ratio,
     side_gaze_duration_ratio, off_angle_duration_ratio, yawn_duration_ratio,
     face_presence_ratio

Cara pakai:
    python dataset_collector.py --source 0 --label 0 --output focus_features_dataset.csv
    python dataset_collector.py --source 0 --label 1 --output focus_features_dataset.csv

Label:
    0 = Risiko Rendah (Siswa Fokus)
    1 = Risiko Tinggi (Siswa Tidak Fokus / Distraksi)

Tekan 'q' atau klik [X] untuk berhenti merekam.
"""

import argparse
import csv
import math
import os
import sys
import time
from collections import deque

import cv2
import numpy as np
import mediapipe as mp
from mediapipe.tasks import python
from mediapipe.tasks.python import vision

# Import fungsi dari cv_focus_engine
from cv_focus_engine import (
    ensure_model_exists, MODEL_PATH, VideoSource,
    get_head_pose_from_matrix, eye_aspect_ratio, mouth_aspect_ratio,
    extract_blendshape_metrics,
    LEFT_EYE_EAR_IDX, RIGHT_EYE_EAR_IDX,
    BLINK_SCORE_THRESHOLD, EAR_THRESHOLD,
    GAZE_LOOK_DOWN_THRESHOLD, GAZE_LOOK_SIDE_THRESHOLD,
    MAR_YAWN_THRESHOLD, JAW_OPEN_THRESHOLD, YAWN_DURATION_SEC,
    YAW_THRESHOLD_DEG, PITCH_LOOK_DOWN_DEG, PITCH_LOOK_UP_DEG,
)

# ---------------------------------------------------------------------------
# KONFIGURASI SLIDING WINDOW
# ---------------------------------------------------------------------------
WINDOW_DURATION_SEC = 3.0     # Durasi sliding window (3 detik)
WINDOW_OVERLAP = 0.50         # Overlap 50%
TARGET_FPS = 30               # Target FPS kamera

# Nama kolom CSV (20 fitur + 1 label)
CSV_COLUMNS = [
    # Aspek 1: Pola Pergerakan & Statistik Spasial (10)
    "yaw_mean", "yaw_std", "pitch_mean", "pitch_std", "roll_std",
    "ear_mean", "ear_std", "mar_mean", "mar_max", "head_velocity_mean",
    # Aspek 2: Frekuensi Perubahan & Kejadian (4)
    "blink_count", "blink_rate_per_min", "gaze_shift_count", "yawn_event_count",
    # Aspek 3: Durasi Kumulatif & Rasio Waktu (6)
    "drowsy_duration_ratio", "phone_gaze_duration_ratio",
    "side_gaze_duration_ratio", "off_angle_duration_ratio",
    "yawn_duration_ratio", "face_presence_ratio",
    # Label
    "label"
]

FEATURE_COLUMNS = CSV_COLUMNS[:-1]


# ---------------------------------------------------------------------------
# KELAS SLIDING WINDOW AGGREGATOR
# ---------------------------------------------------------------------------
class SlidingWindowAggregator:
    """
    Mengumpulkan data mentah per-frame ke dalam buffer sliding window,
    lalu menghitung vektor fitur 20-dimensi saat window penuh.
    """

    def __init__(self, window_sec=3.0, fps=30):
        self.window_size = int(window_sec * fps)
        self.fps = fps

        # Buffer per-frame data
        self.yaw_buffer = deque(maxlen=self.window_size)
        self.pitch_buffer = deque(maxlen=self.window_size)
        self.roll_buffer = deque(maxlen=self.window_size)
        self.ear_buffer = deque(maxlen=self.window_size)
        self.mar_buffer = deque(maxlen=self.window_size)
        self.blink_score_buffer = deque(maxlen=self.window_size)
        self.gaze_down_buffer = deque(maxlen=self.window_size)
        self.gaze_up_buffer = deque(maxlen=self.window_size)
        self.gaze_side_buffer = deque(maxlen=self.window_size)
        self.jaw_open_buffer = deque(maxlen=self.window_size)
        self.face_present_buffer = deque(maxlen=self.window_size)  # 1=detected, 0=not

        # Untuk head velocity
        self.prev_yaw = None
        self.prev_pitch = None
        self.head_velocity_buffer = deque(maxlen=self.window_size)

        # Untuk blink count (edge detection)
        self.prev_blink_state = False
        self.blink_events = deque(maxlen=self.window_size)

        # Untuk gaze shift count
        self.prev_gaze_state = None
        self.gaze_shift_events = deque(maxlen=self.window_size)

        # Untuk yawn event count
        self.yawn_active = False
        self.yawn_start_time = 0
        self.yawn_events = deque(maxlen=self.window_size)

    def push_frame(self, face_detected, yaw=0, pitch=0, roll=0, ear=0,
                   mar=0, blink_score=0, gaze_down=0, gaze_up=0,
                   gaze_side=0, jaw_open=0):
        """Memasukkan data 1 frame ke dalam buffer sliding window."""
        self.face_present_buffer.append(1.0 if face_detected else 0.0)

        if not face_detected:
            self.yaw_buffer.append(0.0)
            self.pitch_buffer.append(0.0)
            self.roll_buffer.append(0.0)
            self.ear_buffer.append(0.0)
            self.mar_buffer.append(0.0)
            self.blink_score_buffer.append(0.0)
            self.gaze_down_buffer.append(0.0)
            self.gaze_up_buffer.append(0.0)
            self.gaze_side_buffer.append(0.0)
            self.jaw_open_buffer.append(0.0)
            self.head_velocity_buffer.append(0.0)
            self.blink_events.append(0)
            self.gaze_shift_events.append(0)
            self.yawn_events.append(0)
            return

        self.yaw_buffer.append(yaw)
        self.pitch_buffer.append(pitch)
        self.roll_buffer.append(roll)
        self.ear_buffer.append(ear)
        self.mar_buffer.append(mar)
        self.blink_score_buffer.append(blink_score)
        self.gaze_down_buffer.append(gaze_down)
        self.gaze_up_buffer.append(gaze_up)
        self.gaze_side_buffer.append(gaze_side)
        self.jaw_open_buffer.append(jaw_open)

        # Head velocity (delta angle / delta time)
        if self.prev_yaw is not None:
            delta_yaw = abs(yaw - self.prev_yaw)
            delta_pitch = abs(pitch - self.prev_pitch)
            velocity = math.sqrt(delta_yaw**2 + delta_pitch**2) * self.fps
            self.head_velocity_buffer.append(velocity)
        else:
            self.head_velocity_buffer.append(0.0)
        self.prev_yaw, self.prev_pitch = yaw, pitch

        # Blink event detection (rising edge: NOT blink -> blink)
        is_blink = blink_score > BLINK_SCORE_THRESHOLD
        blink_event = 1 if (is_blink and not self.prev_blink_state) else 0
        self.blink_events.append(blink_event)
        self.prev_blink_state = is_blink

        # Gaze shift detection
        current_gaze = "down" if gaze_down > GAZE_LOOK_DOWN_THRESHOLD else \
                       "side" if gaze_side > 0.45 else "center"
        gaze_shift = 1 if (self.prev_gaze_state is not None and
                           current_gaze != self.prev_gaze_state) else 0
        self.gaze_shift_events.append(gaze_shift)
        self.prev_gaze_state = current_gaze

        # Yawn event detection (MAR > threshold for > YAWN_DURATION_SEC)
        is_yawning = (mar > MAR_YAWN_THRESHOLD and jaw_open > JAW_OPEN_THRESHOLD)
        yawn_event = 0
        if is_yawning:
            if not self.yawn_active:
                self.yawn_active = True
                self.yawn_start_time = time.time()
            elif time.time() - self.yawn_start_time >= YAWN_DURATION_SEC:
                yawn_event = 1
                self.yawn_start_time = time.time()  # Reset untuk event berikutnya
        else:
            self.yawn_active = False
        self.yawn_events.append(yawn_event)

    def is_ready(self):
        """Cek apakah buffer sudah penuh (= 1 sliding window)."""
        return len(self.yaw_buffer) >= self.window_size

    def compute_features(self):
        """
        Menghitung vektor fitur 20-dimensi dari buffer sliding window.
        Return: list of 20 float values.
        """
        yaw = np.array(self.yaw_buffer)
        pitch = np.array(self.pitch_buffer)
        roll = np.array(self.roll_buffer)
        ear = np.array(self.ear_buffer)
        mar = np.array(self.mar_buffer)
        blink_s = np.array(self.blink_score_buffer)
        g_down = np.array(self.gaze_down_buffer)
        g_side = np.array(self.gaze_side_buffer)
        face_p = np.array(self.face_present_buffer)
        hvel = np.array(self.head_velocity_buffer)

        n_frames = len(yaw)
        window_sec = n_frames / max(self.fps, 1)

        # === Aspek 1: Pola Pergerakan & Statistik Spasial (10 Fitur) ===
        yaw_mean = float(np.mean(yaw))
        yaw_std = float(np.std(yaw))
        pitch_mean = float(np.mean(pitch))
        pitch_std = float(np.std(pitch))
        roll_std = float(np.std(roll))
        ear_mean = float(np.mean(ear))
        ear_std = float(np.std(ear))
        mar_mean = float(np.mean(mar))
        mar_max = float(np.max(mar))
        head_velocity_mean = float(np.mean(hvel))

        # === Aspek 2: Frekuensi Perubahan & Kejadian (4 Fitur) ===
        blink_count = int(sum(self.blink_events))
        blink_rate_per_min = (blink_count / window_sec) * 60.0 if window_sec > 0 else 0
        gaze_shift_count = int(sum(self.gaze_shift_events))
        yawn_event_count = int(sum(self.yawn_events))

        # === Aspek 3: Durasi Kumulatif & Rasio Waktu (6 Fitur) ===
        drowsy_ratio = float(np.mean(blink_s > BLINK_SCORE_THRESHOLD))
        phone_ratio = float(np.mean(g_down > GAZE_LOOK_DOWN_THRESHOLD))
        side_ratio = float(np.mean(g_side > 0.45))
        off_angle_ratio = float(np.mean(
            (np.abs(yaw) > YAW_THRESHOLD_DEG) |
            (pitch < PITCH_LOOK_DOWN_DEG) |
            (pitch > PITCH_LOOK_UP_DEG)
        ))
        yawn_ratio = float(np.mean(mar > MAR_YAWN_THRESHOLD))
        face_ratio = float(np.mean(face_p))

        return [
            yaw_mean, yaw_std, pitch_mean, pitch_std, roll_std,
            ear_mean, ear_std, mar_mean, mar_max, head_velocity_mean,
            blink_count, blink_rate_per_min, gaze_shift_count, yawn_event_count,
            drowsy_ratio, phone_ratio, side_ratio, off_angle_ratio,
            yawn_ratio, face_ratio
        ]

    def clear_half(self):
        """Menghapus 50% data tertua (overlap 50%)."""
        half = self.window_size // 2
        for _ in range(half):
            if self.yaw_buffer:
                self.yaw_buffer.popleft()
                self.pitch_buffer.popleft()
                self.roll_buffer.popleft()
                self.ear_buffer.popleft()
                self.mar_buffer.popleft()
                self.blink_score_buffer.popleft()
                self.gaze_down_buffer.popleft()
                self.gaze_up_buffer.popleft()
                self.gaze_side_buffer.popleft()
                self.jaw_open_buffer.popleft()
                self.face_present_buffer.popleft()
                self.head_velocity_buffer.popleft()
                self.blink_events.popleft()
                self.gaze_shift_events.popleft()
                self.yawn_events.popleft()


# ---------------------------------------------------------------------------
# MAIN: PEREKAM DATASET REAL-TIME
# ---------------------------------------------------------------------------
def main(source, label, output_path):
    """
    Menjalankan perekaman dataset dari webcam secara real-time.
    Setiap 1 sliding window (3 detik), 1 vektor fitur 20-dimensi disimpan ke CSV.
    """
    ensure_model_exists()

    # Setup MediaPipe FaceLandmarker
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
    landmarker = vision.FaceLandmarker.create_from_options(options)

    # Setup video source
    video = VideoSource(source)

    # Setup CSV writer
    file_exists = os.path.exists(output_path)
    csv_file = open(output_path, "a", newline="", encoding="utf-8")
    writer = csv.writer(csv_file)
    if not file_exists or os.path.getsize(output_path) == 0:
        writer.writerow(CSV_COLUMNS)

    # Setup sliding window
    aggregator = SlidingWindowAggregator(WINDOW_DURATION_SEC, TARGET_FPS)
    sample_count = 0
    label_text = "FOKUS (Risiko Rendah)" if label == 0 else "TIDAK FOKUS (Risiko Tinggi)"

    print("=" * 60)
    print(f"  DATASET COLLECTOR - Merekam Label: {label} ({label_text})")
    print(f"  Output: {output_path}")
    print(f"  Sliding Window: {WINDOW_DURATION_SEC}s, Overlap: {WINDOW_OVERLAP*100:.0f}%")
    print("=" * 60)
    print("Tekan 'q' atau klik [X] untuk berhenti.\n")

    window_name = f"Dataset Collector - Label {label}: {label_text}"
    cv2.namedWindow(window_name, cv2.WINDOW_NORMAL)

    try:
        while True:
            ok, frame = video.read()
            if not ok or frame is None:
                continue

            h, w = frame.shape[:2]
            rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            mp_image = mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb)

            try:
                result = landmarker.detect(mp_image)
            except Exception:
                continue

            face_detected = len(result.face_landmarks) > 0

            if face_detected:
                lm = result.face_landmarks[0]
                landmarks_px = [(int(l.x * w), int(l.y * h)) for l in lm]

                # Head pose
                _, yaw, pitch, roll = get_head_pose_from_matrix(
                    result.facial_transformation_matrixes[0])

                # EAR (rata-rata kedua mata)
                ear_l = eye_aspect_ratio(landmarks_px, LEFT_EYE_EAR_IDX)
                ear_r = eye_aspect_ratio(landmarks_px, RIGHT_EYE_EAR_IDX)
                ear = (ear_l + ear_r) / 2.0

                # MAR
                mar = mouth_aspect_ratio(landmarks_px)

                # Blendshapes
                blink_score, gaze_down, gaze_up, gaze_side, jaw_open = \
                    extract_blendshape_metrics(result.face_blendshapes)

                aggregator.push_frame(
                    True, yaw, pitch, roll, ear, mar,
                    blink_score, gaze_down, gaze_up, gaze_side, jaw_open
                )

                # Visual feedback
                color = (0, 255, 0) if label == 0 else (0, 0, 255)
                cv2.putText(frame, f"Label: {label} ({label_text})",
                            (10, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.7, color, 2)
                cv2.putText(frame, f"Yaw: {yaw:.1f}  Pitch: {pitch:.1f}  Roll: {roll:.1f}",
                            (10, 60), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (200, 200, 200), 1)
                cv2.putText(frame, f"EAR: {ear:.3f}  MAR: {mar:.3f}  Blink: {blink_score:.2f}",
                            (10, 85), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (200, 200, 200), 1)
            else:
                aggregator.push_frame(False)
                cv2.putText(frame, "WAJAH TIDAK TERDETEKSI",
                            (10, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 0, 255), 2)

            # Progress bar sliding window
            progress = len(aggregator.yaw_buffer) / aggregator.window_size
            bar_w = int(w * 0.6)
            bar_x = (w - bar_w) // 2
            bar_y = h - 30
            cv2.rectangle(frame, (bar_x, bar_y), (bar_x + bar_w, bar_y + 15), (50, 50, 50), -1)
            cv2.rectangle(frame, (bar_x, bar_y), (bar_x + int(bar_w * progress), bar_y + 15), (0, 200, 255), -1)
            cv2.putText(frame, f"Window: {progress*100:.0f}%  |  Samples: {sample_count}",
                        (bar_x, bar_y - 8), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 200, 255), 1)

            cv2.imshow(window_name, frame)

            # Cek window penuh -> simpan fitur
            if aggregator.is_ready():
                features = aggregator.compute_features()
                writer.writerow(features + [label])
                csv_file.flush()
                sample_count += 1
                print(f"  [SAVED] Sample #{sample_count} (Label {label})")
                aggregator.clear_half()  # Overlap 50%

            # Exit
            key = cv2.waitKey(1) & 0xFF
            if key == ord('q'):
                break
            if cv2.getWindowProperty(window_name, cv2.WND_PROP_VISIBLE) < 1:
                break

    except KeyboardInterrupt:
        print("\n[INFO] Perekaman dihentikan oleh user (Ctrl+C).")

    finally:
        csv_file.close()
        video.release()
        cv2.destroyAllWindows()
        print(f"\n{'=' * 60}")
        print(f"  SELESAI! Total {sample_count} sampel disimpan ke: {output_path}")
        print(f"{'=' * 60}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Dataset Collector - Merekam Fitur 20-Dimensi dari Webcam")
    parser.add_argument("--source", default="0",
                        help="Sumber video: 0 (webcam) atau URL stream ESP32")
    parser.add_argument("--label", type=int, required=True, choices=[0, 1],
                        help="Label kelas: 0=Risiko Rendah (Fokus), 1=Risiko Tinggi (Tidak Fokus)")
    parser.add_argument("--output", default="focus_features_dataset.csv",
                        help="Path file CSV output (default: focus_features_dataset.csv)")
    args = parser.parse_args()
    main(args.source, args.label, args.output)
