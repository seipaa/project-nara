#pragma once

// =============================================================================
// NARA ESP32-S3-CAM — pin & parameter yang sering diubah
//
// WAJIB dicocokkan dengan skema board sebelum flash. Pin kamera di bawah
// adalah map yang paling umum untuk modul "ESP32-S3-CAM" dan Freenove
// ESP32-S3-CAM. ESP32-S3-EYE (Espressif) memakai pin kamera yang sama,
// tetapi LCD-nya sering memakai GPIO21 dan GPIO47 — jangan pakai default
// UART/tombol di bawah pada board EYE tanpa cek skema.
//
// Seeed XIAO ESP32S3 Sense pin kameranya BERBEDA. Ubah NARA_CAMERA_BOARD
// menjadi 3, lalu pindahkan UART dan tombol (lihat komentar di bawah).
// static_assert di file ini akan menolak kombinasi yang bentrok dengan kamera.
// =============================================================================

// 1 = ESP32-S3-CAM / Freenove ESP32-S3-CAM (DEFAULT; pin kamera = ESP32-S3-EYE)
// 3 = Seeed XIAO ESP32S3 Sense
#ifndef NARA_CAMERA_BOARD
#define NARA_CAMERA_BOARD 1
#endif

// -----------------------------------------------------------------------------
// UART modul MP3 (DFPlayer Mini). Audio TIDAK lewat slot SD ESP.
// ESP TX -> RX modul, ESP RX <- TX modul.
//
// Default GPIO21 (TX) dan GPIO14 (RX) kosong pada map kamera Freenove/S3-CAM.
// Jangan memakai pin kamera: 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 15, 16, 17, 18.
// Hindari juga:
//   GPIO0, GPIO3, GPIO45, GPIO46  (strapping; GPIO0 = BOOT)
//   GPIO19, GPIO20                 (USB D-/D+ pada banyak board S3)
//   GPIO43, GPIO44                 (UART0 / USB-serial monitor)
//   GPIO26-GPIO32                  (flash/PSRAM internal modul WROOM, sering tidak di-breakout)
//   GPIO38, GPIO39, GPIO40         (slot microSD onboard Freenove, mode 1-bit)
// Alternatif lain yang biasanya kosong di header S3-CAM: GPIO1, GPIO2, GPIO41, GPIO42.
//
// XIAO ESP32S3 Sense: GPIO14 = data kamera Y6, GPIO47 = HREF.
//   Pakai MP3_UART_TX 1, MP3_UART_RX 2, RESET_BUTTON_PIN 4.
// ESP32-S3-EYE: pin kamera sama dengan default, tetapi GPIO21/GPIO47 sering LCD.
//   Pindahkan UART/tombol jika board-nya EYE, bukan ESP32-S3-CAM polos.
// -----------------------------------------------------------------------------
#ifndef MP3_UART_TX
#define MP3_UART_TX 21
#endif
#ifndef MP3_UART_RX
#define MP3_UART_RX 14
#endif
#define MP3_BAUD 9600

// Tombol reset pabrik, active LOW ke GND. Tahan 5 detik.
// Bukan pin EN/RST board (itu reset keras, firmware tidak sempat menghitung).
// Bukan GPIO0/BOOT: ditahan saat boot akan masuk mode download.
#ifndef RESET_BUTTON_PIN
#define RESET_BUTTON_PIN 47
#endif
#define RESET_HOLD_MS 5000

// Jendela pairing, dihitung sejak server kontrol hidup (WiFi sudah terhubung),
// bukan selama captive portal masih terbuka.
#define NARA_PAIR_WINDOW_MS 120000UL

#define NARA_HTTP_PORT 80
#define NARA_STREAM_PORT 81
#define NARA_DISCOVERY_PORT 4210
#define NARA_DISCOVERY_MAGIC "NARA_DISCOVER"
#define NARA_TOKEN_HEADER "X-NARA-Token"
#define NARA_FW_VERSION "1.0.0"

// Volume DFPlayer 0-30. Disimpan di NVS dan dipakai ulang setelah boot.
#define NARA_VOLUME_DEFAULT 18
#define NARA_VOLUME_MAX 30

// -----------------------------------------------------------------------------
// Pin kamera. Y2 = D0 (LSB) ... Y9 = D7 (MSB), sesuai esp_camera.
// -----------------------------------------------------------------------------
#if NARA_CAMERA_BOARD == 3
// Seeed XIAO ESP32S3 Sense (kamera di konektor B2B, bukan header D0-D10).
#define PWDN_GPIO_NUM -1
#define RESET_GPIO_NUM -1
#define XCLK_GPIO_NUM 10
#define SIOD_GPIO_NUM 40
#define SIOC_GPIO_NUM 39
#define Y9_GPIO_NUM 48
#define Y8_GPIO_NUM 11
#define Y7_GPIO_NUM 12
#define Y6_GPIO_NUM 14
#define Y5_GPIO_NUM 16
#define Y4_GPIO_NUM 18
#define Y3_GPIO_NUM 17
#define Y2_GPIO_NUM 15
#define VSYNC_GPIO_NUM 38
#define HREF_GPIO_NUM 47
#define PCLK_GPIO_NUM 13

#define NARA_CAM_CONFLICT(p)                                                                 \
  ((p) == 10 || (p) == 11 || (p) == 12 || (p) == 13 || (p) == 14 || (p) == 15 || (p) == 16 || \
   (p) == 17 || (p) == 18 || (p) == 38 || (p) == 39 || (p) == 40 || (p) == 47 || (p) == 48)

#elif NARA_CAMERA_BOARD == 1
// ESP32-S3-CAM / Freenove ESP32-S3-CAM / ESP32-S3-EYE (pin kamera identik).
// Bukan map "ESP32-S3-CAM-LCD" (XCLK di GPIO40). Kalau skema board Anda beda,
// jangan dipaksa: salin pin dari skema ke blok ini.
#define PWDN_GPIO_NUM -1
#define RESET_GPIO_NUM -1
#define XCLK_GPIO_NUM 15
#define SIOD_GPIO_NUM 4
#define SIOC_GPIO_NUM 5
#define Y9_GPIO_NUM 16
#define Y8_GPIO_NUM 17
#define Y7_GPIO_NUM 18
#define Y6_GPIO_NUM 12
#define Y5_GPIO_NUM 10
#define Y4_GPIO_NUM 8
#define Y3_GPIO_NUM 9
#define Y2_GPIO_NUM 11
#define VSYNC_GPIO_NUM 6
#define HREF_GPIO_NUM 7
#define PCLK_GPIO_NUM 13

#define NARA_CAM_CONFLICT(p)                                                                 \
  ((p) == 4 || (p) == 5 || (p) == 6 || (p) == 7 || (p) == 8 || (p) == 9 || (p) == 10 ||      \
   (p) == 11 || (p) == 12 || (p) == 13 || (p) == 15 || (p) == 16 || (p) == 17 || (p) == 18)

#else
#error "NARA_CAMERA_BOARD tidak dikenal. Pakai 1 (ESP32-S3-CAM/Freenove) atau 3 (XIAO)."
#endif

static_assert(MP3_UART_TX >= 0 && MP3_UART_TX <= 48, "MP3_UART_TX di luar GPIO ESP32-S3");
static_assert(MP3_UART_RX >= 0 && MP3_UART_RX <= 48, "MP3_UART_RX di luar GPIO ESP32-S3");
static_assert(RESET_BUTTON_PIN >= 0 && RESET_BUTTON_PIN <= 48, "RESET_BUTTON_PIN di luar GPIO");
static_assert(MP3_UART_TX != MP3_UART_RX, "UART TX dan RX harus pin berbeda");
static_assert(MP3_UART_TX != RESET_BUTTON_PIN, "UART TX bentrok dengan tombol reset");
static_assert(MP3_UART_RX != RESET_BUTTON_PIN, "UART RX bentrok dengan tombol reset");
static_assert(!NARA_CAM_CONFLICT(MP3_UART_TX),
              "MP3_UART_TX bentrok dengan pin kamera. XIAO: TX=1 RX=2 tombol=4.");
static_assert(!NARA_CAM_CONFLICT(MP3_UART_RX),
              "MP3_UART_RX bentrok dengan pin kamera. XIAO: TX=1 RX=2 tombol=4.");
static_assert(!NARA_CAM_CONFLICT(RESET_BUTTON_PIN),
              "RESET_BUTTON_PIN bentrok dengan pin kamera. XIAO: tombol GPIO4.");
