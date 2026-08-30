# Smart Student Focus Monitoring 

Sistem pemantauan atensi siswa secara real-time berbasis **Computer Vision (MediaPipe Tasks)**, **Feature Engineering 20-Dimensi (Sliding Window)**, **Support Vector Machine (SVM RBF)**, dan **Web Dashboard**.

---

## Struktur File Proyek

```
CV-Student/
├── cv_focus_engine.py          # Ekstraktor 9 Indikator Visual Mentah (MediaPipe)
├── dataset_collector.py        # Perekam Fitur 20-Dimensi Real-Time ke CSV (Webcam/ESP32)
├── data_cleaner.py             # Pembersihan Data (Hapus Outlier & Stratified Sampling)
├── train_svm.py                # Pelatihan SVM RBF, GridSearchCV & Evaluasi Confusion Matrix
├── focus_predictor.py          # Inference Engine Real-Time (Fast-Recovery & Hysteresis)
├── pomodoro_service.py         # State Machine Pomodoro & Adaptive Interventions
├── web_dashboard/              # Antarmuka Web Dashboard
│   ├── app.py                  # Backend FastAPI & WebSockets
│   ├── templates/index.html    # Frontend Template 3-Kolom
│   └── static/
│       ├── style.css           # Modern Dark-Themed Stylesheet
│       └── app.js              # Client WebSocket Telemetry Controller
├── models/                     # Model Terlatih & Artefak Evaluasi
│   ├── focus_svm_model.joblib  # Model SVM Terlatih
│   ├── feature_scaler.joblib   # Fitted StandardScaler
│   ├── confusion_matrix.png    # Heatmap Confusion Matrix
│   ├── roc_curve.png           # ROC-AUC Curve
│   ├── feature_importance.png  # SVM Feature Importance
│   └── training_report.txt     # Laporan Metrik Lengkap
├── clean_focus_dataset.csv     # Dataset Bersih Siap Latih
├── requirements.txt            # Daftar Dependensi Library Python
└── README.md                   # Dokumentasi Panduan Proyek
```

---

## Panduan Instalasi & Menjalankan

### 1. Clone Repository & Buat Virtual Environment
```bash
git clone <URL_REPOSITORY>
cd CV-Student

python -m venv venv
# Windows:
venv\Scripts\activate
# Linux/macOS:
source venv/bin/activate
```

### 2. Install Dependensi
```bash
pip install -r requirements.txt
```

### 3. Jalankan Web Dashboard

#### Opsi A: Menggunakan Webcam Laptop Bawaan (Default Index 0)
```bash
python web_dashboard/app.py
```

#### Opsi B: Menggunakan Webcam Eksternal USB (Index 1 atau 2)
Jika kamu mencolokkan webcam eksternal via kabel USB:
```bash
python web_dashboard/app.py --source 1
```
*(Ganti ke `--source 2` jika laptop memiliki 2 kamera internal).*

#### Opsi C: Menggunakan ESP32-CAM (WiFi Stream)
Masukkan URL IP streaming yang didapat dari Serial Monitor Arduino ESP32-CAM:
```bash
# Format MJPEG Stream (Umum):
python web_dashboard/app.py --source http://192.168.1.100:81/stream

# Atau Format RTSP Stream:
python web_dashboard/app.py --source rtsp://192.168.1.100:554/mjpeg/1
```

Buka browser di:
**http://localhost:8000**

---

## Pipeline Pengumpulan Data & Pelatihan Model

### 1. Perekaman Dataset Real-Time (`dataset_collector.py`)

Script ini merekam data wajah secara temporal (Sliding Window 3 detik) dan mengekstrak **20 fitur statistik** ke dalam file CSV (`raw_focus_dataset.csv`).

#### A. Rekam Sampel Siswa FOKUS (Label 0):
Posisikan diri menatap layar laptop secara wajar (membaca/mengetik).
```bash
python dataset_collector.py --source 0 --label 0 --output raw_focus_dataset.csv
```

#### B. Rekam Sampel Siswa TIDAK FOKUS (Label 1):
Lakukan variasi distraksi (menoleh samping, menunduk main HP, menguap, mata terpejam/tidur, melamun).
```bash
python dataset_collector.py --source 0 --label 1 --output raw_focus_dataset.csv
```
> *Catatan: Tekan **`q`** pada jendela video untuk berhenti merekam.*

---

### 2. Pembersihan Dataset (`data_cleaner.py`)
Membersihkan data kosong/duplikat, membuang outlier ekstrim ($Z\text{-score} > 3.5$), dan menyeimbangkan proporsi data (stratified 50:50).

```bash
python data_cleaner.py
```
Output: `clean_focus_dataset.csv`

---

### 3. Pelatihan Model & Evaluasi (`train_svm.py`)
Melatih Support Vector Machine (Kernel RBF), tuning hyperparameter ($C, \gamma$) via 5-Fold Cross Validation, dan menghasilkan visualisasi evaluasi:

```bash
python train_svm.py
```

Output artefak di folder `models/`:
- `focus_svm_model.joblib`
- `feature_scaler.joblib`
- `confusion_matrix.png`
- `roc_curve.png`
- `training_report.txt`
