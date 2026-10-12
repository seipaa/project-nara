#include "esp_camera.h"
#include <WiFi.h>
#include <WiFiManager.h>
#include <ESPmDNS.h>
#include "FS.h"      // Tambahan library SD Card
#include "SD_MMC.h"  // Tambahan library SD Card
#include <array>
#include <string.h>


// own libraries from tab
#include "User_Interface.h"
#include "config.h"
#include "nara_alert.h"

//
// WARNING!!! PSRAM IC required for UXGA resolution and high JPEG quality
//            Ensure ESP32 Wrover Module or other board with PSRAM is selected
//            Partial images will be transmitted if image exceeds buffer size
//
//            You must select partition scheme from the board menu that has at least 3MB APP space.
//            Face Recognition is DISABLED for ESP32 and ESP32-S2, because it takes up from 15
//            seconds to process single frame. Face Detection is ENABLED if PSRAM is enabled as well

#if __DEBUG_IP_STATIC 
  IPAddress local_IP    (192, 168, 1, 100);
  IPAddress gateway     (192, 168, 1, 1);
  IPAddress subnet      (255, 255, 255, 0);    // Subnet Mask
  IPAddress primaryDNS  (8, 8, 8, 8);
  IPAddress SecondaryDNS(8, 8, 4, 4);
#endif 

#include "camera_pins.h"

// defined boot pin as reset WiFi Manager, mendefinisikan boot pin pada pin 0
#if __DEBUG_IP_STATIC
  WiFiServer myCameraServer(8080);
#endif 


#if defined (__USE_WIFI_NORMALLY_)
// ===========================
// Enter your WiFi credentials
// ===========================
const char *ssid = "Darnan@dev";
const char *password = "IoTdeveloper";
#endif

void startCameraServer();
void setupLedFlash(int pin);

WiFiManager wm;

void setup() {

  Serial.begin(115200);
  delay(1000);
  Serial.setDebugOutput(true);
  Serial.println();
  Serial.println("========================================");
  Serial.println("[NARA-CAMERA] Firmware mDNS starting");
  Serial.println("[NARA-CAMERA] Build: NARA-VOICE-ALERT-2026-10-12");
  Serial.println("[NARA-CAMERA] Stream: single-client guard, WiFi sleep off");
  Serial.println("[NARA-CAMERA] Alert API: :82/api/alert  DFPlayer UART1 GPIO21/47");
  Serial.println("[NARA-CAMERA] Serial baud: 115200");
  Serial.println("========================================");

    pinMode(__BOOT_PIN, INPUT_PULLUP);

  camera_config_t config;
  config.ledc_channel = LEDC_CHANNEL_0;
  config.ledc_timer = LEDC_TIMER_0;
  config.pin_d0 = Y2_GPIO_NUM;
  config.pin_d1 = Y3_GPIO_NUM;
  config.pin_d2 = Y4_GPIO_NUM;
  config.pin_d3 = Y5_GPIO_NUM;
  config.pin_d4 = Y6_GPIO_NUM;
  config.pin_d5 = Y7_GPIO_NUM;
  config.pin_d6 = Y8_GPIO_NUM;
  config.pin_d7 = Y9_GPIO_NUM;
  config.pin_xclk = XCLK_GPIO_NUM;
  config.pin_pclk = PCLK_GPIO_NUM;
  config.pin_vsync = VSYNC_GPIO_NUM;
  config.pin_href = HREF_GPIO_NUM;
  config.pin_sccb_sda = SIOD_GPIO_NUM;
  config.pin_sccb_scl = SIOC_GPIO_NUM;
  config.pin_pwdn = PWDN_GPIO_NUM;
  config.pin_reset = RESET_GPIO_NUM;
  config.xclk_freq_hz = 20000000;
  config.frame_size = FRAMESIZE_UXGA;
  config.pixel_format = PIXFORMAT_JPEG;  // for streaming
  //config.pixel_format = PIXFORMAT_RGB565; // for face detection/recognition
  config.grab_mode = CAMERA_GRAB_WHEN_EMPTY;
  config.fb_location = CAMERA_FB_IN_PSRAM;
  config.jpeg_quality = 12;
  config.fb_count = 1;

  // if PSRAM IC present, init with UXGA resolution and higher JPEG quality
  //                      for larger pre-allocated frame buffer.
  if (config.pixel_format == PIXFORMAT_JPEG) {
    if (psramFound()) {
      config.jpeg_quality = 10;
      config.fb_count = 2;
      config.grab_mode = CAMERA_GRAB_LATEST;
    } else {
      // Limit the frame size when PSRAM is not available
      config.frame_size = FRAMESIZE_SVGA;
      config.fb_location = CAMERA_FB_IN_DRAM;
    }
  } else {
    // Best option for face detection/recognition
    config.frame_size = FRAMESIZE_240X240;
#if CONFIG_IDF_TARGET_ESP32S3
    config.fb_count = 2;
#endif
  }

#if defined(CAMERA_MODEL_ESP_EYE)
  pinMode(13, INPUT_PULLUP);
  pinMode(14, INPUT_PULLUP);
#endif

  // camera init
  esp_err_t err = esp_camera_init(&config);
  if (err != ESP_OK) {
    Serial.printf("Camera init failed with error 0x%x", err);
    return;
  }

  sensor_t *s = esp_camera_sensor_get();
  // initial sensors are flipped vertically and colors are a bit saturated
  if (s->id.PID == OV3660_PID) {
    s->set_vflip(s, 1);        // flip it back
    s->set_brightness(s, 1);   // up the brightness just a bit
    s->set_saturation(s, -2);  // lower the saturation
  }
  // drop down frame size for higher initial frame rate
  if (config.pixel_format == PIXFORMAT_JPEG) {
    s->set_framesize(s, FRAMESIZE_QVGA);
  }

#if defined(CAMERA_MODEL_M5STACK_WIDE) || defined(CAMERA_MODEL_M5STACK_ESP32CAM)
  s->set_vflip(s, 1);
  s->set_hmirror(s, 1);
#endif

#if defined(CAMERA_MODEL_ESP32S3_EYE)
  s->set_vflip(s, 1);
#endif

// Setup LED FLash if LED pin is defined in camera_pins.h
#if defined(LED_GPIO_NUM)
  setupLedFlash(LED_GPIO_NUM);
#endif

#if __DEBUG_IP_STATIC
  if (!WiFi.config(local_IP, gateway, subnet, primaryDNS, SecondaryDNS)) {
    Serial.println("Gagal konfigurasi Static IP!");
  } else {
    Serial.println("\t✅ Berhasil Konfigurasi IP Static anjiengg !!\n");
  }
#endif 

#if defined (__USE_WIFI_NORMALLY_)

  WiFi.setHostname("nara-camera");
  WiFi.begin(ssid, password);
  WiFi.setSleep(false);

  Serial.print("WiFi connecting");
  while (WiFi.status() != WL_CONNECTED) {
    vTaskDelay(pdMS_TO_TICKS(500));
    Serial.print(".");
  }
  Serial.println("");
  Serial.println("WiFi connected");
#endif

#if defined (__USE_WIFI_MANAGER_)
  WiFi.setHostname("nara-camera");
  Serial.println("Menyambungkan ke WiFi atau membuka Access Point...");

    #if defined (IF_USING_BOOT_AS_RESET_WM)

      // Cek apakah tombol BOOT (GPIO 0) ditekan saat ESP32 dinyalakan (selama ~2 detik)
      pinMode(__BOOT_PIN, INPUT_PULLUP);
      if (digitalRead(__BOOT_PIN) == LOW) {
        Serial.println("Tombol BOOT terdeteksi, menahan reset settings...");
        unsigned long startTime = millis();
        while (digitalRead(__BOOT_PIN) == LOW) {
          if (millis() - startTime > 3000) { // Jika ditekan > 3 detik
            Serial.println("Resetting WiFi Settings...");
            wm.resetSettings();
            break;
          }
          vTaskDelay(pdMS_TO_TICKS(100));
        }
      }
      
    #endif
  
#if __DEBUG_UI_PARAMETER
  setParameterUI(wm);
#endif 
  WiFi.mode(WIFI_AP_STA);
  // Jalankan AutoConnect WiFiManager
  bool res {wm.autoConnect(NameOfSSID, NameOfPASS)};
  
  if(!res) {
    Serial.println("[NARA-WIFI] Gagal konek atau timeout! ESP bakal restart...");
    vTaskDelay(pdMS_TO_TICKS(3000));
    ESP.restart(); 
  } else { 
    Serial.println("[NARA-WIFI] WiFi tersambung.");
    WiFi.mode(WIFI_STA);
    WiFi.setSleep(false);
  }
#endif

  if (WiFi.status() == WL_CONNECTED) {
    WiFi.setSleep(false);
    Serial.print("[NARA-WIFI] IP ESP32: ");
    Serial.println(WiFi.localIP());
    Serial.println("[NARA-MDNS] Memulai hostname nara-camera.local...");
    if (MDNS.begin("nara-camera")) {
      MDNS.addService("http", "tcp", 80);
      MDNS.addService("http", "tcp", 81);
      MDNS.addService("http", "tcp", NARA_ALERT_HTTP_PORT);
      Serial.println("[NARA-MDNS] BERHASIL: http://nara-camera.local:81/stream");
      Serial.println("[NARA-MDNS] Snapshot: http://nara-camera.local/capture");
      Serial.println("[NARA-MDNS] Alert: http://nara-camera.local:82/api/alert");
    } else {
      Serial.println("[NARA-MDNS] GAGAL memulai nara-camera.local");
    }
  } else {
    Serial.print("[NARA-WIFI] Koneksi belum siap. Status WiFi: ");
    Serial.println(WiFi.status());
    Serial.println("[NARA-MDNS] Tidak dimulai karena Wi-Fi belum tersambung.");
  }

#if __DEBUG_IP_STATIC
  myCameraServer.begin();
#endif 

  // ==========================================
  // INI TAMBAHAN INISIALISASI SD CARD (Mode 1-bit)
  // ==========================================
  Serial.println("Memulai inisialisasi SD Card...");
  if(!SD_MMC.begin("/sdcard", true)){
    Serial.println("GAGAL: SD Card tidak terdeteksi!");
  } else {
    Serial.println("MANTAP: SD Card berhasil dipasang!");
    // // uint64_t cardSize = SD_MMC.cardSize() / (1024 * 1024);
    // Serial.printf("Ukuran SD Card: %llu MB\n", cardSize);
  }
  // ========================================== 

  startCameraServer();
  initNaraVoiceAlert();
  startNaraAlertServer();

  Serial.print("[NARA-CAMERA] Camera Ready: http://");
  Serial.print(WiFi.localIP());
  Serial.println("/");
  Serial.println("[NARA-CAMERA] Stream hostname: http://nara-camera.local:81/stream");
  Serial.println("[NARA-CAMERA] Voice alert: http://nara-camera.local:82/api/alert");
}

void loop() {
  // Do nothing. Everything is done in another task by the web server
  vTaskDelay(pdMS_TO_TICKS(5000));
}
