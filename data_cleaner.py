"""
data_cleaner.py
Modul Pembersihan & Seleksi Dataset untuk Pelatihan Model SVM

Pipeline pembersihan data:
1. Membaca raw dataset CSV (3000 sampel)
2. Menghapus duplikat
3. Menghapus outlier ekstrem (Z-score > 3.5)
4. Menghapus sampel dengan fitur anomali (NaN, Inf, negatif di kolom non-negatif)
5. Validasi range realistis per fitur
6. Seleksi stratified sampling: 500 per kelas = 1000 total
7. Export ke clean CSV

Input:  raw_focus_dataset.csv (3000 sampel)
Output: clean_focus_dataset.csv (1000 sampel)

Cara pakai:
    python data_cleaner.py
    python data_cleaner.py --input raw.csv --output clean.csv --total 1000
"""

import argparse
import csv
import os
import sys

import numpy as np

INPUT_PATH = "raw_focus_dataset.csv"
OUTPUT_PATH = "clean_focus_dataset.csv"
TOTAL_CLEAN_SAMPLES = 1000  # 500 per kelas

# Definisi kolom fitur dan label
FEATURE_COLUMNS = [
    "yaw_mean", "yaw_std", "pitch_mean", "pitch_std", "roll_std",
    "ear_mean", "ear_std", "mar_mean", "mar_max", "head_velocity_mean",
    "blink_count", "blink_rate_per_min", "gaze_shift_count", "yawn_event_count",
    "drowsy_duration_ratio", "phone_gaze_duration_ratio",
    "side_gaze_duration_ratio", "off_angle_duration_ratio",
    "yawn_duration_ratio", "face_presence_ratio",
]
LABEL_COLUMN = "label"

# Range realistis per fitur (min, max) - untuk validasi kualitas
VALID_RANGES = {
    "yaw_mean": (-60, 60),
    "yaw_std": (0, 30),
    "pitch_mean": (-45, 40),
    "pitch_std": (0, 20),
    "roll_std": (0, 15),
    "ear_mean": (0.03, 0.50),
    "ear_std": (0, 0.20),
    "mar_mean": (0, 0.80),
    "mar_max": (0, 1.0),
    "head_velocity_mean": (0, 25),
    "blink_count": (0, 30),
    "blink_rate_per_min": (0, 100),
    "gaze_shift_count": (0, 20),
    "yawn_event_count": (0, 10),
    "drowsy_duration_ratio": (0, 1.0),
    "phone_gaze_duration_ratio": (0, 1.0),
    "side_gaze_duration_ratio": (0, 1.0),
    "off_angle_duration_ratio": (0, 1.0),
    "yawn_duration_ratio": (0, 1.0),
    "face_presence_ratio": (0, 1.0),
}


def load_csv(path):
    """Memuat dataset CSV menjadi list of dict."""
    if not os.path.exists(path):
        print(f"[ERROR] File tidak ditemukan: {path}")
        sys.exit(1)

    data = []
    with open(path, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            parsed = {}
            for col in FEATURE_COLUMNS:
                try:
                    parsed[col] = float(row[col])
                except (ValueError, KeyError):
                    parsed[col] = None
            try:
                parsed[LABEL_COLUMN] = int(float(row[LABEL_COLUMN]))
            except (ValueError, KeyError):
                parsed[LABEL_COLUMN] = None
            data.append(parsed)
    return data


def save_csv(data, path):
    """Menyimpan list of dict ke CSV."""
    columns = FEATURE_COLUMNS + [LABEL_COLUMN]
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=columns)
        writer.writeheader()
        writer.writerows(data)


def step_1_remove_invalid(data):
    """Menghapus sampel dengan NaN, Inf, atau label invalid."""
    clean = []
    removed = 0
    for row in data:
        if row[LABEL_COLUMN] not in (0, 1):
            removed += 1
            continue
        has_invalid = False
        for col in FEATURE_COLUMNS:
            val = row[col]
            if val is None or np.isnan(val) or np.isinf(val):
                has_invalid = True
                break
        if has_invalid:
            removed += 1
        else:
            clean.append(row)

    print(f"  [Step 1] Hapus NaN/Inf/label invalid: {removed} sampel dihapus -> {len(clean)} tersisa")
    return clean


def step_2_remove_duplicates(data):
    """Menghapus baris duplikat persis."""
    seen = set()
    clean = []
    removed = 0
    for row in data:
        key = tuple(round(row[col], 6) for col in FEATURE_COLUMNS)
        if key in seen:
            removed += 1
        else:
            seen.add(key)
            clean.append(row)

    print(f"  [Step 2] Hapus duplikat: {removed} sampel dihapus -> {len(clean)} tersisa")
    return clean


def step_3_validate_ranges(data):
    """Menghapus sampel di luar range realistis."""
    clean = []
    removed = 0
    for row in data:
        valid = True
        for col, (lo, hi) in VALID_RANGES.items():
            if not (lo <= row[col] <= hi):
                valid = False
                break
        if valid:
            clean.append(row)
        else:
            removed += 1

    print(f"  [Step 3] Validasi range realistis: {removed} sampel dihapus -> {len(clean)} tersisa")
    return clean


def step_4_remove_outliers(data, z_threshold=3.5):
    """Menghapus outlier berdasarkan Z-score > threshold per fitur."""
    if len(data) == 0:
        return data

    # Hitung mean & std per fitur
    feature_values = {col: [] for col in FEATURE_COLUMNS}
    for row in data:
        for col in FEATURE_COLUMNS:
            feature_values[col].append(row[col])

    stats = {}
    for col in FEATURE_COLUMNS:
        arr = np.array(feature_values[col])
        stats[col] = (np.mean(arr), np.std(arr))

    clean = []
    removed = 0
    for row in data:
        is_outlier = False
        for col in FEATURE_COLUMNS:
            mean, std = stats[col]
            if std > 0:
                z = abs((row[col] - mean) / std)
                if z > z_threshold:
                    is_outlier = True
                    break
        if is_outlier:
            removed += 1
        else:
            clean.append(row)

    print(f"  [Step 4] Hapus outlier (Z>{z_threshold}): {removed} sampel dihapus -> {len(clean)} tersisa")
    return clean


def step_5_stratified_selection(data, total_samples):
    """Seleksi stratified sampling: jumlah sama per kelas."""
    per_class = total_samples // 2

    class_0 = [row for row in data if row[LABEL_COLUMN] == 0]
    class_1 = [row for row in data if row[LABEL_COLUMN] == 1]

    print(f"  [Step 5] Data tersedia: Label 0 = {len(class_0)}, Label 1 = {len(class_1)}")

    # Random sampling tanpa replacement
    np.random.seed(42)
    if len(class_0) > per_class:
        indices_0 = np.random.choice(len(class_0), per_class, replace=False)
        class_0 = [class_0[i] for i in indices_0]
    if len(class_1) > per_class:
        indices_1 = np.random.choice(len(class_1), per_class, replace=False)
        class_1 = [class_1[i] for i in indices_1]

    selected = class_0 + class_1
    np.random.shuffle(selected)

    print(f"          Dipilih: Label 0 = {len(class_0)}, Label 1 = {len(class_1)}")
    print(f"          Total clean dataset: {len(selected)} sampel")
    return selected


def print_statistics(data, title="Dataset Statistics"):
    """Menampilkan statistik ringkas dataset."""
    print(f"\n  [STATS] {title}:")
    labels = [row[LABEL_COLUMN] for row in data]
    print(f"     Total: {len(data)} sampel")
    print(f"     Label 0 (Fokus):       {labels.count(0)}")
    print(f"     Label 1 (Tidak Fokus): {labels.count(1)}")

    print(f"\n     {'Fitur':<30} {'Mean':>10} {'Std':>10} {'Min':>10} {'Max':>10}")
    print(f"     {'-' * 70}")
    for col in FEATURE_COLUMNS:
        vals = np.array([row[col] for row in data])
        print(f"     {col:<30} {np.mean(vals):>10.4f} {np.std(vals):>10.4f} "
              f"{np.min(vals):>10.4f} {np.max(vals):>10.4f}")


def main(input_path, output_path, total_samples):
    print("=" * 65)
    print("  DATA CLEANER - Pembersihan & Seleksi Dataset Atensi Siswa")
    print("=" * 65)
    print(f"  Input:  {input_path}")
    print(f"  Output: {output_path}")
    print(f"  Target: {total_samples} sampel bersih")
    print("=" * 65)

    # Load
    print(f"\n[LOADING] Memuat dataset dari {input_path}...")
    data = load_csv(input_path)
    print(f"  Loaded: {len(data)} sampel total\n")

    # Pipeline pembersihan
    print("[CLEANING] Pipeline pembersihan data:")
    data = step_1_remove_invalid(data)
    data = step_2_remove_duplicates(data)
    data = step_3_validate_ranges(data)
    data = step_4_remove_outliers(data, z_threshold=3.5)
    data = step_5_stratified_selection(data, total_samples)

    # Statistik akhir
    print_statistics(data, "Clean Dataset Statistics")

    # Save
    print(f"\n[SAVING] Menyimpan dataset bersih ke {output_path}...")
    save_csv(data, output_path)

    print(f"\n{'=' * 65}")
    print(f"  [OK] SELESAI! {len(data)} sampel bersih disimpan ke: {output_path}")
    print(f"{'=' * 65}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Data Cleaner - Pembersihan & Seleksi Dataset")
    parser.add_argument("--input", default=INPUT_PATH,
                        help=f"Path input CSV (default: {INPUT_PATH})")
    parser.add_argument("--output", default=OUTPUT_PATH,
                        help=f"Path output CSV (default: {OUTPUT_PATH})")
    parser.add_argument("--total", type=int, default=TOTAL_CLEAN_SAMPLES,
                        help=f"Jumlah total sampel bersih (default: {TOTAL_CLEAN_SAMPLES})")
    args = parser.parse_args()
    main(args.input, args.output, args.total)
