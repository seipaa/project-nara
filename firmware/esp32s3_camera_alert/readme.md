# Firmware NARA ESP32-S3-CAM + DFPlayer Mini Voice Alert

Firmware ini menggabungkan **ESP32 CameraWebServer** untuk streaming video MJPEG dengan **modul audio DFPlayer Mini** melalui UART1 untuk menyuarakan peringatan atensi siswa.

---

## 1. Pin Wiring Hardware (ESP32-S3-EYE / ESP32-S3-CAM)

| Komponen | Pin ESP32-S3 | Pin DFPlayer Mini | Keterangan |
| :--- | :--- | :--- | :--- |
| **UART TX** | `GPIO 21` | `RX` | Kirim perintah serial (9600 baud) |
| **UART RX** | `GPIO 47` | `TX` | Menerima status / ACK |
| **VCC** | `5V` (atau 3.3V) | `VCC` | Daya modul audio (disarankan 5V stabil) |
| **GND** | `GND` | `GND` | Ground bersama |
| **Speaker** | - | `SPK_1` & `SPK_2` | Ke speaker mini 3W 8Ω |

> [!NOTE]
> Jika speaker mengeluarkan suara mendengung/noise, tambahkan resistor 1kΩ antara `GPIO 21 (TX)` ESP32 dan `RX` DFPlayer Mini.

---

## 2. Struktur File Audio di Kartu MicroSD DFPlayer

Format MicroSD: **FAT32**. Buat folder bernama `01` dan `02` di root MicroSD:

```text
MicroSD Root
├── 01/                  <-- Bahasa Inggris (English)
│   ├── 001.mp3          -> Focus alert (001_not_focus.mp3)
│   ├── 002.mp3          -> Session ended (002_waktu_habis.mp3)
│   ├── 003.mp3          -> Break time (003_istirahat.mp3)
│   └── 004.mp3          -> Session start (004_mulai.mp3)
└── 02/                  <-- Bahasa Indonesia
    ├── 001.mp3          (atau 001_not_focus.mp3) -> Peringatan tidak fokus / mengantuk
    ├── 002.mp3          (atau 002_waktu_habis.mp3) -> Sesi belajar selesai
    ├── 003.mp3          (atau 003_istirahat.mp3) -> Waktu istirahat
    └── 004.mp3          (atau 004_mulai.mp3) -> Memulai sesi belajar Pomodoro
```

---

## 3. Port & Layanan Jaringan (mDNS)

Hostname mDNS: **`nara-camera.local`**

*   **Port 80**: Web Index kamera & snapshot JPEG (`http://nara-camera.local/capture`)
*   **Port 81**: Video stream MJPEG (`http://nara-camera.local:81/stream`)
*   **Port 82**: REST API Voice Alert (`http://nara-camera.local:82/api/alert`)

### Endpoint API Alert (Port 82)
*   `POST /api/alert`
    Payload:
    ```json
    {
      "code": "NOT_FOCUSED",
      "language": "id",
      "event_id": "unique-id-123"
    }
    ```
    Respon: `202 Accepted`
*   `GET /api/alert`
    Mengembalikan status hardware DFPlayer, antrean FreeRTOS, dan konfigurasi pin.
