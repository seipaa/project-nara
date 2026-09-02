"""
train_svm.py
Modul Pelatihan Model SVM & Evaluasi Kinerja (Confusion Matrix, ROC-AUC, Classification Report)

Pipeline Pelatihan:
1. Memuat dataset bersih (clean_focus_dataset.csv, 1000 sampel)
2. Splitting: 80% training (800) + 20% testing (200) — Stratified
3. Feature Scaling: StandardScaler (z-normalization)
4. Model: SVM dengan Kernel RBF (Radial Basis Function)
5. Hyperparameter Tuning: GridSearchCV (5-Fold Cross Validation)
6. Evaluasi: Confusion Matrix, Classification Report, ROC-AUC, Specificity
7. Visualisasi: Heatmap Confusion Matrix + ROC Curve + Feature Importance
8. Export: Model (.joblib) + Scaler (.joblib)

Output:
    models/focus_svm_model.joblib    — Model SVM terlatih
    models/feature_scaler.joblib     — Fitted StandardScaler
    models/confusion_matrix.png      — Heatmap Confusion Matrix
    models/roc_curve.png             — ROC-AUC Curve
    models/feature_importance.png    — Feature Importance (SVM Coefficients)
    models/training_report.txt       — Laporan evaluasi lengkap

Cara pakai:
    python train_svm.py
    python train_svm.py --dataset clean_focus_dataset.csv
"""

import argparse
import csv
import os
import sys
import time
from datetime import datetime

import numpy as np

# Machine Learning
from sklearn.svm import SVC
from sklearn.preprocessing import StandardScaler
from sklearn.model_selection import train_test_split, GridSearchCV, StratifiedKFold
from sklearn.metrics import (
    confusion_matrix, classification_report, accuracy_score,
    precision_score, recall_score, f1_score, roc_auc_score, roc_curve
)
from joblib import dump

# Visualization
import matplotlib
matplotlib.use("Agg")  # Non-interactive backend (untuk server / tanpa display)
import matplotlib.pyplot as plt
import matplotlib.ticker as mticker

# Coba import seaborn untuk heatmap cantik, fallback ke matplotlib jika tidak ada
try:
    import seaborn as sns
    HAS_SEABORN = True
except ImportError:
    HAS_SEABORN = False
    print("[INFO] Seaborn tidak terinstall. Menggunakan matplotlib untuk visualisasi.")

# ---------------------------------------------------------------------------
# KONFIGURASI
# ---------------------------------------------------------------------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATASET_PATH = os.path.join(BASE_DIR, "clean_focus_dataset.csv")
MODEL_DIR = os.path.join(BASE_DIR, "models")
MODEL_PATH = os.path.join(MODEL_DIR, "focus_svm_model.joblib")
SCALER_PATH = os.path.join(MODEL_DIR, "feature_scaler.joblib")
CONFUSION_MATRIX_PATH = os.path.join(MODEL_DIR, "confusion_matrix.png")
ROC_CURVE_PATH = os.path.join(MODEL_DIR, "roc_curve.png")
FEATURE_IMPORTANCE_PATH = os.path.join(MODEL_DIR, "feature_importance.png")
TRAINING_REPORT_PATH = os.path.join(MODEL_DIR, "training_report.txt")

TEST_SIZE = 0.20          # 20% data untuk testing
RANDOM_STATE = 42         # Reproducibility
CV_FOLDS = 5              # K-Fold Cross Validation

FEATURE_COLUMNS = [
    "yaw_mean", "yaw_std", "pitch_mean", "pitch_std", "roll_std",
    "ear_mean", "ear_std", "mar_mean", "mar_max", "head_velocity_mean",
    "blink_count", "blink_rate_per_min", "gaze_shift_count", "yawn_event_count",
    "drowsy_duration_ratio", "phone_gaze_duration_ratio",
    "side_gaze_duration_ratio", "off_angle_duration_ratio",
    "yawn_duration_ratio", "face_presence_ratio",
]
LABEL_COLUMN = "label"

CLASS_NAMES = ["Risiko Rendah (0)", "Risiko Tinggi (1)"]


# ---------------------------------------------------------------------------
# 1. MEMUAT DATASET
# ---------------------------------------------------------------------------
def load_dataset(path):
    """Memuat dataset CSV menjadi array fitur (X) dan label (y)."""
    if not os.path.exists(path):
        print(f"[ERROR] Dataset tidak ditemukan: {path}")
        print("Jalankan dulu: python generate_synthetic_data.py && python data_cleaner.py")
        sys.exit(1)

    X, y = [], []
    with open(path, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            features = [float(row[col]) for col in FEATURE_COLUMNS]
            label = int(float(row[LABEL_COLUMN]))
            X.append(features)
            y.append(label)

    X = np.array(X)
    y = np.array(y)

    print(f"  Dataset dimuat: {X.shape[0]} sampel, {X.shape[1]} fitur")
    print(f"  Label 0 (Fokus):       {np.sum(y == 0)} sampel")
    print(f"  Label 1 (Tidak Fokus): {np.sum(y == 1)} sampel")

    return X, y


# ---------------------------------------------------------------------------
# 2. NORMALISASI FITUR
# ---------------------------------------------------------------------------
def scale_features(X_train, X_test):
    """Menerapkan StandardScaler: z = (x - mean) / std"""
    scaler = StandardScaler()
    X_train_scaled = scaler.fit_transform(X_train)
    X_test_scaled = scaler.transform(X_test)
    print(f"  StandardScaler fitted: {X_train_scaled.shape[1]} fitur dinormalisasi")
    return scaler, X_train_scaled, X_test_scaled


# ---------------------------------------------------------------------------
# 3. PELATIHAN SVM DENGAN GRIDSEARCHCV
# ---------------------------------------------------------------------------
def train_svm(X_train, y_train):
    """
    Melatih model SVM (RBF Kernel) dengan GridSearchCV untuk menemukan
    hyperparameter optimal (C, gamma).
    """
    print(f"\n  Hyperparameter Grid Search (5-Fold CV):")
    param_grid = {
        "C": [0.1, 1, 10, 100],
        "gamma": ["scale", "auto", 0.01, 0.1],
        "kernel": ["rbf"]
    }

    svm_base = SVC(probability=True, random_state=RANDOM_STATE, class_weight="balanced")

    cv = StratifiedKFold(n_splits=CV_FOLDS, shuffle=True, random_state=RANDOM_STATE)
    grid_search = GridSearchCV(
        svm_base, param_grid, cv=cv,
        scoring="f1", n_jobs=-1, verbose=0, refit=True
    )

    start_time = time.time()
    grid_search.fit(X_train, y_train)
    elapsed = time.time() - start_time

    best = grid_search.best_params_
    print(f"  Best Params:  C={best['C']}, gamma={best['gamma']}, kernel={best['kernel']}")
    print(f"  Best CV F1:   {grid_search.best_score_:.4f}")
    print(f"  Training Time: {elapsed:.2f} detik")

    return grid_search.best_estimator_, grid_search


# ---------------------------------------------------------------------------
# 4. EVALUASI MODEL
# ---------------------------------------------------------------------------
def evaluate_model(model, X_test, y_test):
    """Evaluasi model dengan Confusion Matrix dan metrik performa."""
    y_pred = model.predict(X_test)
    y_prob = model.predict_proba(X_test)[:, 1]

    # Confusion Matrix
    cm = confusion_matrix(y_test, y_pred)
    tn, fp, fn, tp = cm.ravel()

    # Metrik
    accuracy = accuracy_score(y_test, y_pred) * 100
    precision = precision_score(y_test, y_pred) * 100
    recall = recall_score(y_test, y_pred) * 100
    f1 = f1_score(y_test, y_pred)
    specificity = (tn / (tn + fp)) * 100 if (tn + fp) > 0 else 0
    roc_auc = roc_auc_score(y_test, y_prob)

    print(f"\n  {'=' * 55}")
    print(f"  {'CONFUSION MATRIX':^55}")
    print(f"  {'=' * 55}")
    print(f"                          PREDIKSI MODEL (SVM)")
    print(f"                    Risiko Rendah (0)  Risiko Tinggi (1)")
    print(f"  Aktual: Rendah (0)    {tn:>6} (TN)         {fp:>6} (FP)")
    print(f"  Aktual: Tinggi (1)    {fn:>6} (FN)         {tp:>6} (TP)")
    print(f"  {'=' * 55}")

    print(f"\n  [METRICS] METRIK EVALUASI KINERJA MODEL:")
    print(f"  {'-' * 50}")
    print(f"  {'Metrik':<35} {'Nilai':>12}")
    print(f"  {'-' * 50}")
    print(f"  {'Akurasi (Accuracy)':<35} {accuracy:>10.2f} %")
    print(f"  {'Presisi (Precision)':<35} {precision:>10.2f} %")
    print(f"  {'Recall / Sensitivitas':<35} {recall:>10.2f} %")
    print(f"  {'F1-Score':<35} {f1:>10.4f}")
    print(f"  {'Specificity':<35} {specificity:>10.2f} %")
    print(f"  {'ROC-AUC Score':<35} {roc_auc:>10.4f}")
    print(f"  {'-' * 50}")

    # Interpretasi hasil
    print(f"\n  [DETAIL] INTERPRETASI CONFUSION MATRIX:")
    print(f"  * True Positive  (TP = {tp:>3}): Siswa tidak fokus -> Terdeteksi benar sebagai Risiko Tinggi")
    print(f"  * True Negative  (TN = {tn:>3}): Siswa fokus -> Terdeteksi benar sebagai Risiko Rendah")
    print(f"  * False Positive (FP = {fp:>3}): Siswa fokus -> SALAH dituduh Risiko Tinggi (Alarm Palsu)")
    print(f"  * False Negative (FN = {fn:>3}): Siswa tidak fokus -> LOLOS deteksi (Missed)")

    # Classification Report
    report = classification_report(y_test, y_pred, target_names=CLASS_NAMES)
    print(f"\n  Classification Report:")
    for line in report.split("\n"):
        print(f"  {line}")

    return {
        "cm": cm, "tn": tn, "fp": fp, "fn": fn, "tp": tp,
        "accuracy": accuracy, "precision": precision, "recall": recall,
        "f1": f1, "specificity": specificity, "roc_auc": roc_auc,
        "y_pred": y_pred, "y_prob": y_prob, "report": report
    }


# ---------------------------------------------------------------------------
# 5. VISUALISASI
# ---------------------------------------------------------------------------
def plot_confusion_matrix(cm, save_path):
    """Membuat heatmap Confusion Matrix."""
    fig, ax = plt.subplots(figsize=(8, 6))

    if HAS_SEABORN:
        sns.heatmap(cm, annot=True, fmt="d", cmap="Blues",
                    xticklabels=CLASS_NAMES, yticklabels=CLASS_NAMES,
                    annot_kws={"size": 18, "weight": "bold"},
                    linewidths=2, linecolor="white",
                    cbar_kws={"label": "Jumlah Sampel"}, ax=ax)
    else:
        im = ax.imshow(cm, interpolation="nearest", cmap=plt.cm.Blues)
        plt.colorbar(im, ax=ax, label="Jumlah Sampel")
        for i in range(cm.shape[0]):
            for j in range(cm.shape[1]):
                ax.text(j, i, str(cm[i, j]),
                        ha="center", va="center", fontsize=18, fontweight="bold",
                        color="white" if cm[i, j] > cm.max() / 2 else "black")
        ax.set_xticks([0, 1])
        ax.set_yticks([0, 1])
        ax.set_xticklabels(CLASS_NAMES)
        ax.set_yticklabels(CLASS_NAMES)

    ax.set_xlabel("Prediksi Model (SVM)", fontsize=12, fontweight="bold")
    ax.set_ylabel("Label Aktual", fontsize=12, fontweight="bold")
    ax.set_title("Confusion Matrix - SVM Atensi Siswa\n(RBF Kernel, StandardScaler)", fontsize=14, fontweight="bold")

    plt.tight_layout()
    plt.savefig(save_path, dpi=150, bbox_inches="tight")
    plt.close()
    print(f"  [SAVED] Confusion Matrix Heatmap: {save_path}")


def plot_roc_curve(y_test, y_prob, roc_auc, save_path):
    """Membuat ROC-AUC Curve."""
    fpr, tpr, _ = roc_curve(y_test, y_prob)

    fig, ax = plt.subplots(figsize=(8, 6))
    ax.plot(fpr, tpr, color="#2196F3", lw=2.5,
            label=f"SVM RBF (AUC = {roc_auc:.4f})")
    ax.plot([0, 1], [0, 1], color="#BDBDBD", lw=1.5, linestyle="--",
            label="Random Classifier (AUC = 0.50)")

    ax.fill_between(fpr, tpr, alpha=0.15, color="#2196F3")
    ax.set_xlim([0.0, 1.0])
    ax.set_ylim([0.0, 1.05])
    ax.set_xlabel("False Positive Rate (1 - Specificity)", fontsize=12)
    ax.set_ylabel("True Positive Rate (Recall / Sensitivity)", fontsize=12)
    ax.set_title("ROC Curve - SVM Klasifikasi Atensi Siswa", fontsize=14, fontweight="bold")
    ax.legend(loc="lower right", fontsize=11)
    ax.grid(True, alpha=0.3)

    plt.tight_layout()
    plt.savefig(save_path, dpi=150, bbox_inches="tight")
    plt.close()
    print(f"  [SAVED] ROC-AUC Curve: {save_path}")


def plot_feature_importance(model, scaler, save_path):
    """
    Visualisasi kontribusi relatif fitur menggunakan jarak rata-rata
    dari support vectors ke hyperplane per fitur (approximate importance).
    """
    if not hasattr(model, "support_vectors_"):
        print("  [SKIP] Feature importance tidak tersedia untuk model ini.")
        return

    # Menggunakan rata-rata absolute dari support vectors sebagai proxy importance
    sv = model.support_vectors_
    importance = np.mean(np.abs(sv), axis=0)

    # Sort by importance
    sorted_idx = np.argsort(importance)
    sorted_features = [FEATURE_COLUMNS[i] for i in sorted_idx]
    sorted_importance = importance[sorted_idx]

    fig, ax = plt.subplots(figsize=(10, 8))
    colors = plt.cm.viridis(np.linspace(0.2, 0.9, len(sorted_features)))
    bars = ax.barh(range(len(sorted_features)), sorted_importance, color=colors)

    ax.set_yticks(range(len(sorted_features)))
    ax.set_yticklabels(sorted_features, fontsize=10)
    ax.set_xlabel("Mean |Support Vector Value| (Scaled)", fontsize=12)
    ax.set_title("Feature Importance - SVM Support Vector Analysis", fontsize=14, fontweight="bold")
    ax.grid(axis="x", alpha=0.3)

    # Annotate bars
    for bar, val in zip(bars, sorted_importance):
        ax.text(bar.get_width() + 0.01, bar.get_y() + bar.get_height() / 2,
                f"{val:.3f}", va="center", fontsize=9)

    plt.tight_layout()
    plt.savefig(save_path, dpi=150, bbox_inches="tight")
    plt.close()
    print(f"  [SAVED] Feature Importance Chart: {save_path}")


# ---------------------------------------------------------------------------
# 6. EXPORT MODEL & LAPORAN
# ---------------------------------------------------------------------------
def save_model_and_scaler(model, scaler):
    """Menyimpan model SVM dan StandardScaler ke file .joblib."""
    os.makedirs(MODEL_DIR, exist_ok=True)
    dump(model, MODEL_PATH)
    dump(scaler, SCALER_PATH)
    print(f"  [SAVED] Model SVM:      {MODEL_PATH}")
    print(f"  [SAVED] Feature Scaler: {SCALER_PATH}")


def save_training_report(metrics, grid_search, X_train_shape, X_test_shape):
    """Menyimpan laporan evaluasi lengkap ke file teks."""
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    best_params = grid_search.best_params_

    report_lines = [
        "=" * 70,
        "LAPORAN EVALUASI MODEL SVM - Sistem Pemantauan Atensi Siswa",
        f"Tanggal: {timestamp}",
        "=" * 70,
        "",
        "1. KONFIGURASI MODEL",
        f"   Kernel:         {best_params['kernel']}",
        f"   C (Regularization): {best_params['C']}",
        f"   Gamma:          {best_params['gamma']}",
        f"   Class Weight:   balanced",
        f"   Probability:    True (Platt Scaling)",
        "",
        "2. DATASET",
        f"   Total:    {X_train_shape[0] + X_test_shape[0]} sampel",
        f"   Training: {X_train_shape[0]} sampel ({(1-TEST_SIZE)*100:.0f}%)",
        f"   Testing:  {X_test_shape[0]} sampel ({TEST_SIZE*100:.0f}%)",
        f"   Fitur:    {X_train_shape[1]} dimensi",
        f"   Scaling:  StandardScaler (z-normalization)",
        "",
        "3. CONFUSION MATRIX",
        f"   True Positive  (TP): {metrics['tp']}",
        f"   True Negative  (TN): {metrics['tn']}",
        f"   False Positive (FP): {metrics['fp']}",
        f"   False Negative (FN): {metrics['fn']}",
        "",
        "4. METRIK EVALUASI",
        f"   Akurasi:     {metrics['accuracy']:.2f}%",
        f"   Presisi:     {metrics['precision']:.2f}%",
        f"   Recall:      {metrics['recall']:.2f}%",
        f"   F1-Score:    {metrics['f1']:.4f}",
        f"   Specificity: {metrics['specificity']:.2f}%",
        f"   ROC-AUC:     {metrics['roc_auc']:.4f}",
        "",
        "5. CLASSIFICATION REPORT",
        metrics["report"],
        "",
        "6. FILE OUTPUT",
        f"   Model:              {MODEL_PATH}",
        f"   Scaler:             {SCALER_PATH}",
        f"   Confusion Matrix:   {CONFUSION_MATRIX_PATH}",
        f"   ROC Curve:          {ROC_CURVE_PATH}",
        f"   Feature Importance: {FEATURE_IMPORTANCE_PATH}",
        "",
        "=" * 70,
    ]

    with open(TRAINING_REPORT_PATH, "w", encoding="utf-8") as f:
        f.write("\n".join(report_lines))

    print(f"  [SAVED] Training Report: {TRAINING_REPORT_PATH}")


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------
def main(dataset_path):
    print("\n" + "=" * 70)
    print("  [ML] PELATIHAN MODEL SVM - Sistem Pemantauan Atensi Siswa")
    print("     Kernel: RBF | Scaler: StandardScaler | CV: 5-Fold")
    print("=" * 70)

    # 1. Load dataset
    print(f"\n[1/6] MEMUAT DATASET: {dataset_path}")
    X, y = load_dataset(dataset_path)

    # 2. Split data
    print(f"\n[2/6] SPLITTING DATA (Train {(1-TEST_SIZE)*100:.0f}% / Test {TEST_SIZE*100:.0f}%):")
    X_train, X_test, y_train, y_test = train_test_split(
        X, y, test_size=TEST_SIZE, random_state=RANDOM_STATE, stratify=y
    )
    print(f"  Training: {X_train.shape[0]} sampel")
    print(f"  Testing:  {X_test.shape[0]} sampel")

    # 3. Feature scaling
    print(f"\n[3/6] NORMALISASI FITUR (StandardScaler):")
    scaler, X_train_scaled, X_test_scaled = scale_features(X_train, X_test)

    # 4. Train SVM
    print(f"\n[4/6] PELATIHAN SVM (GridSearchCV + 5-Fold CV):")
    model, grid_search = train_svm(X_train_scaled, y_train)

    # 5. Evaluate
    print(f"\n[5/6] EVALUASI MODEL:")
    metrics = evaluate_model(model, X_test_scaled, y_test)

    # 6. Save everything
    print(f"\n[6/6] MENYIMPAN MODEL, SCALER, & VISUALISASI:")
    os.makedirs(MODEL_DIR, exist_ok=True)
    save_model_and_scaler(model, scaler)
    plot_confusion_matrix(metrics["cm"], CONFUSION_MATRIX_PATH)
    plot_roc_curve(y_test, metrics["y_prob"], metrics["roc_auc"], ROC_CURVE_PATH)
    plot_feature_importance(model, scaler, FEATURE_IMPORTANCE_PATH)
    save_training_report(metrics, grid_search, X_train.shape, X_test.shape)

    # Final summary
    print(f"\n{'=' * 70}")
    print(f"  [OK] PELATIHAN SELESAI!")
    print(f"  Akurasi: {metrics['accuracy']:.2f}% | F1: {metrics['f1']:.4f} | ROC-AUC: {metrics['roc_auc']:.4f}")
    print(f"  Model tersimpan di: {MODEL_DIR}/")
    print(f"{'=' * 70}\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Train SVM Model untuk Klasifikasi Atensi Siswa")
    parser.add_argument("--dataset", default=DATASET_PATH,
                        help=f"Path dataset CSV (default: {DATASET_PATH})")
    args = parser.parse_args()
    main(args.dataset)
