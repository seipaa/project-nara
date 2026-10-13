# NARA — Smart Student Focus Monitoring & Adaptive Pomodoro

Sistem pemantauan atensi dan konsentrasi belajar siswa secara *real-time* berbasis **Computer Vision (MediaPipe Tasks)**, **Feature Engineering 20-Dimensi (Sliding Window)**, **Support Vector Machine (SVM RBF)**, dan **Web Dashboard Interaktif (Giwangkara Design)**.

---

## Fitur Utama Sistem

1. **Pemantauan Atensi Multimodal Real-Time**:
   - Deteksi orientasi kepala (*Head Pose Pitch, Yaw, Roll*), arah pandangan mata (*Gaze Direction*).
   - Metrik bukaan mata (*Eye Aspect Ratio / EAR*) untuk deteksi kedipan dan mata terpejam/kantuk.
   - Metrik bukaan mulut (*Mouth Aspect Ratio / MAR*) untuk deteksi menguap (*yawning*).
   - Klasifikasi atensi siswa (Fokus vs Tidak Fokus) dengan model **SVM RBF** terkalibrasi.

2. **Adaptive Pomodoro State Machine**:
   - Timer belajar, istirahat pendek, dan istirahat panjang dijalankan di server (tetap jalan saat kamera dimatikan pada waktu istirahat).
   - Tampilan jam di browser mengikuti sisa waktu dari WebSocket.
   - **Intervensi Cerdas (Nudge & Break Proposal)**:
     - Peringatan jika siswa terdeteksi tertidur di depan materi (> 3 detik).
     - *Nudge* ramah jika siswa tidak fokus secara persisten (> 15 detik).
     - Usulan istirahat adaptif jika mendeteksi kelelahan kognitif tinggi (menguap $\ge 2$ kali dalam 2 menit).

3. **Kamera NARA & suara di modul MP3**:
   - Aplikasi mencari ESP32-S3-CAM lewat mDNS (`_nara._tcp`) dan siaran UDP `NARA_DISCOVER` port `4210`.
   - Di layar Persiapan: pilih webcam atau kamera NARA, bahasa suara (Indonesia / English), volume, dan tes suara.
   - Empat pengumuman dikirim server ke `POST /play`: mulai sesi, tidak fokus, istirahat selesai, sesi selesai.
   - Token pairing disimpan lokal di `settings.json` dan dikirim sebagai header `X-NARA-Token`.

4. **Integrasi Mata Pelajaran & Validasi**:
   - Pemilihan mata pelajaran belajar (Matematika, Fisika, Kimia, Biologi, Bahasa Inggris, Bahasa Indonesia, Sejarah, Ekonomi, Lainnya).
   - Teks *placeholder* default yang memandu siswa memilih mapel sebelum mulai belajar, dilengkapi validasi otomatis.
   - Riwayat sesi tersimpan spesifik per mata pelajaran di database SQLite.

5. **Web Dashboard Responsif & Berestetika Tinggi (Giwangkara Design)**:
   - **Pemeriksaan Pra-Sesi Real-Time**: Status *Kamera Terhubung*, *Wajah Terdeteksi*, dan *Sistem ML Siap* diperbarui otomatis sebelum sesi dimulai.
   - **Live MJPEG Video Feed**: Overlay visualisasi landmark wajah real-time.
   - **4 Kartu KPI Interaktif**: Jumlah Sesi, Rata-rata Skor Fokus, Total Menit Belajar, dan Skor Fokus Hari Ini.
   - **Diagram Mingguan (7 Hari Terakhir)**: Batang visual atensi harian lengkap dengan garis ambang batas fokus (70%).
   - **Daftar Riwayat dengan Pagination**: Menampilkan 4 item per halaman lengkap dengan kontrol navigasi slide (`‹ ›`) dan modal ringkasan per sesi.
   - **Ringkasan Komprehensif Pasca-Sesi**: Grafik timeline 30 detik, persentase breakdown jenis gangguan, catatan durasi fokus terpanjang, dan *insight* evaluasi personal.
   - **Dukungan Tema**: Tombol pergantian Tema Gelap / Terang (*Dark/Light Mode*).

---

## Struktur File Proyek

```
project-nara/
├── app.py                      # Root Entry Point untuk menjalankan web dashboard langsung
├── cv_focus_engine.py          # Ekstraktor 9 Indikator Visual Mentah (MediaPipe Face Landmarker)
├── dataset_collector.py        # Perekam Fitur 20-Dimensi Real-Time ke CSV (Webcam/ESP32)
├── data_cleaner.py             # Pembersihan Data (Hapus Outlier Z-score & Stratified Balancing)
├── train_svm.py                # Pelatihan SVM RBF, GridSearchCV Tuning, & Visualisasi Evaluasi
├── focus_predictor.py          # Inference Engine Real-Time (Fast-Recovery, Sliding Window & Hysteresis)
├── pomodoro_service.py         # State Machine Pomodoro, Logging SQLite, event suara
├── device_discovery.py         # mDNS _nara._tcp + UDP 4210
├── esp_bridge.py               # Antrean event ke ESP (httpx, pairing, volume)
├── settings_store.py           # settings.json (kamera, bahasa, volume, token)
├── pomodoro_sessions.db        # Database SQLite penyimpanan riwayat sesi belajar & intervensi
├── face_landmarker.task        # Binary model MediaPipe Tasks Face Landmarker
├── clean_focus_dataset.csv     # Dataset bersih siap latih (20 fitur statistik temporal)
├── requirements.txt            # Daftar dependensi library Python
├── .env.example                # Template konfigurasi variabel lingkungan (kamera & telemetry)
├── .env                        # Konfigurasi aktif lokal (opsional)
├── Bahan_Kajian_Presentasi.docx# Dokumen bahan kajian & referensi presentasi
├── README.md                   # Dokumentasi panduan proyek lengkap
├── suara/                      # File audio notifikasi suara bilingual (id: 01, en: 02)
│   ├── 01/                     # Bahasa Indonesia (001: tidak fokus, 002: waktu habis, 003: istirahat, 004: mulai)
│   └── 02/                     # Bahasa Inggris
├── models/                     # Model Terlatih & Artefak Evaluasi Machine Learning
│   ├── focus_svm_model.joblib  # Model SVM RBF Terlatih
│   ├── feature_scaler.joblib   # Fitted StandardScaler untuk normalisasi 20 fitur
│   ├── confusion_matrix.png    # Heatmap Confusion Matrix evaluasi model
│   ├── roc_curve.png           # Grafik ROC-AUC Curve
│   ├── feature_importance.png  # Visualisasi kontribusi fitur pada SVM
│   └── training_report.txt     # Laporan metrik akurasi, presisi, recall, & F1-score
└── web_dashboard/              # Modul Antarmuka Web Dashboard FastAPI
    ├── app.py                  # Server FastAPI, WebSockets (/ws), MJPEG Feed, & REST API
    ├── face_landmarker.task    # Salinan binary model MediaPipe untuk dashboard
    ├── static/                 # Aset statis pendukung (CSS, JS, ikon)
    │   ├── style.css           # Styling dasar
    │   └── app.js              # Script helper telemetry
    └── templates/              # Template HTML Jinja2
        └── index.html          # Frontend utama Giwangkara Single Page Application
```

---

## Panduan Instalasi & Menjalankan

### 1. Clone Repository & Buat Virtual Environment
```bash
git clone <URL_REPOSITORY>
cd project-nara

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

### 3. Konfigurasi Lingkungan (`.env`) *(Opsional)*
Salin file `.env.example` menjadi `.env` jika ingin mengubah pengaturan default:
```bash
cp .env.example .env
```
Isi konfigurasi pada file `.env`:
- `CAMERA_SOURCE`: cadangan jika `settings.json` belum menyimpan kamera. Indeks webcam atau URL stream.
- `TELEMETRY_INTERVAL_SEC`: Frekuensi pengiriman data ke browser (default: `1.0` detik).

Kamera yang dipakai sehari-hari dipilih di langkah **Pilih kamera**, lalu diingat di `settings.json`. File itu berisi token perangkat dan tidak ikut di-commit.

---

### 4. Jalankan Web Dashboard

Anda dapat menjalankan server langsung dari root folder dengan:
```bash
python app.py
```
atau:
```bash
python web_dashboard/app.py
```

Server hanya mendengarkan di komputer ini (`127.0.0.1`). Untuk membuka dari perangkat lain di LAN, jalankan dengan `--host 0.0.0.0`.

#### Sumber kamera
Tanpa argumen, aplikasi memakai kamera terakhir di `settings.json`, atau webcam `0` jika belum ada.

```bash
python app.py
python app.py --source 1
python app.py --source http://192.168.1.100:81/stream
```

Di layar Persiapan, pilih webcam atau kamera NARA yang muncul setelah **Pindai**. Jika jaringan memisahkan klien (client isolation), isi IP lewat **Tambah IP**.

Windows Firewall sering memblokir pencarian otomatis. Izinkan `python.exe` untuk UDP **5353** (mDNS) dan **4210** (siaran `NARA_DISCOVER`) pada profil jaringan privat.

Pin, pairing, dan kontrak HTTP/UDP perangkat ada di [`firmware/nara_esp32s3/README.md`](firmware/nara_esp32s3/README.md).

Buka peramban di: **`http://127.0.0.1:8000`**

---

### 5. Simulasi Perangkat ESP32-S3 (ESP Mock Worker)

Jika Anda ingin menguji sistem secara lengkap tanpa menghubungkan board fisik ESP32-S3-CAM:
```bash
python esp_mock_worker.py
```
Worker simulasi ini otomatis meniru hardware fisik secara utuh:
- **Port 80**: HTTP Kontrol & Info (`/info`, `/status`, `/pair`, `/play`, `/volume`, `/capture`)
- **Port 81**: MJPEG video stream (`/stream`) dengan mode webcam lokal atau sintetis siswa belajar
- **Port 82**: REST API Voice alert (`/api/alert`)
- **Port 4210**: UDP broadcast responder (`NARA_DISCOVER`)
- **mDNS**: Penyiaran otomatis `_nara._tcp.local.`

Setelah worker dijalankan di terminal terpisah, kamera **NARA Cam Simulator** akan otomatis terdeteksi di dashboard NARA.

## Pipeline Machine Learning & Data Collection

### 1. Perekaman Dataset Real-Time (`dataset_collector.py`)
Script ini merekam koordinat wajah secara temporal (*Sliding Window* 3 detik, step 1 detik) dan menghitung nilai statistik *mean* dan *standard deviation* untuk 9 indikator visual + durasi mata terpejam, menghasilkan **20 fitur numerik**.

- **Rekam Sampel Siswa FOKUS (Label 0)**:
  ```bash
  python dataset_collector.py --source 0 --label 0 --output raw_focus_dataset.csv
  ```
- **Rekam Sampel Siswa TIDAK FOKUS (Label 1)**:
  ```bash
  python dataset_collector.py --source 0 --label 1 --output raw_focus_dataset.csv
  ```
> *Tekan tombol `q` pada jendela preview video untuk menyelesaikan perekaman.*

### 2. Pembersihan & Penyeimbangan Data (`data_cleaner.py`)
Membersihkan missing value, menghapus duplikasi, mengeliminasi outlier ekstrem ($Z\text{-score} > 3.5$), serta menyeimbangkan proporsi kelas fokus dan tidak fokus secara seimbang (50:50).
```bash
python data_cleaner.py
```
*Output: `clean_focus_dataset.csv`*

### 3. Pelatihan Model & Evaluasi (`train_svm.py`)
Melatih model SVM dengan kernel Radial Basis Function (RBF), melakukan pencarian hyperparameter optimal ($C, \gamma$) via 5-Fold Stratified Cross Validation, dan menyimpan model serta grafik evaluasi ke folder `models/`.
```bash
python train_svm.py
```

---

## Endpoint API & WebSocket

| Endpoint | Metode | Deskripsi |
| :--- | :---: | :--- |
| `/` | `GET` | Halaman antarmuka utama dashboard |
| `/video_feed` | `GET` | MJPEG video streaming dengan overlay CV |
| `/ws` | `WebSocket` | Stream telemetry real-time (EAR, MAR, Pose, Gaze, Prediksi ML, Timer Pomodoro) |
| `/api/pomodoro/start` | `POST` | Memulai sesi Pomodoro (menerima query `mode` dan `subject`) |
| `/api/pomodoro/pause` | `POST` | Menjeda timer sesi aktif |
| `/api/pomodoro/resume` | `POST` | Melanjutkan timer sesi |
| `/api/pomodoro/skip` | `POST` | Melompati fase saat ini ke fase berikutnya |
| `/api/pomodoro/end` | `POST` | Mengakhiri sesi (user) dan menyimpan log |
| `/api/pomodoro/reset` | `POST` | Mereset timer Pomodoro ke status siap (*idle*) |
| `/api/pomodoro/configure` | `POST` | Mengonfigurasi durasi belajar dan istirahat |
| `/api/pomodoro/accept_break`| `POST` | Menyetujui saran jeda adaptif sistem |
| `/api/pomodoro/dismiss_alert`| `POST` | Menutup notifikasi alert intervensi |
| `/api/cameras` | `GET` | Webcam lokal 0–3 dan ESP yang ditemukan |
| `/api/camera/select` | `POST` | Memilih kamera (`webcam:N` atau `esp:<id>`) |
| `/api/devices/rescan` | `POST` | Siaran ulang pencarian UDP |
| `/api/devices/manual` | `POST` | Menambah ESP lewat alamat IP |
| `/api/audio/config` | `POST` | Bahasa (`id`/`en`), volume 0–30, suara hidup/mati |
| `/api/audio/test` | `POST` | Memutar pengumuman percobaan ke ESP yang dipilih |
| `/api/history` | `GET` | Mengambil daftar riwayat sesi belajar & istirahat dari database SQLite |
| `/api/config` | `GET` | Mengambil konfigurasi aktif server (sumber kamera, audio, interval) |
