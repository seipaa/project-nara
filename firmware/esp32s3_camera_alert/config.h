#ifndef CONFIG_H
#define CONFIG_H
#pragma once

//==============================
// basic config
//==============================
#define __USE_WIFI_NORMALLY__DISABLE
#define IF_USING_BOOT_AS_RESET_WM__DISABLE

#define __USE_WIFI_MANAGER_ true
#define __DEBUG_IP_STATIC false
#define __DEBUG_UI_PARAMETER true

//==============================
// Camera Model Select
//==============================

//#define CAMERA_MODEL_WROVER_KIT // Has PSRAM
//#define CAMERA_MODEL_ESP_EYE  // Has PSRAM
#define CAMERA_MODEL_ESP32S3_EYE // Has PSRAM
//#define CAMERA_MODEL_M5STACK_PSRAM // Has PSRAM
//#define CAMERA_MODEL_M5STACK_V2_PSRAM // M5Camera version B Has PSRAM
//#define CAMERA_MODEL_M5STACK_WIDE // Has PSRAM
//#define CAMERA_MODEL_M5STACK_ESP32CAM // No PSRAM
//#define CAMERA_MODEL_M5STACK_UNITCAM // No PSRAM
//#define CAMERA_MODEL_M5STACK_CAMS3_UNIT  // Has PSRAM
//#define CAMERA_MODEL_AI_THINKER // Has PSRAM
//#define CAMERA_MODEL_TTGO_T_JOURNAL // No PSRAM
//#define CAMERA_MODEL_XIAO_ESP32S3 // Has PSRAM

// ** Espressif Internal Boards **
//#define CAMERA_MODEL_ESP32_CAM_BOARD
//#define CAMERA_MODEL_ESP32S2_CAM_BOARD
//#define CAMERA_MODEL_ESP32S3_CAM_LCD
//#define CAMERA_MODEL_DFRobot_FireBeetle2_ESP32S3 // Has PSRAM
//#define CAMERA_MODEL_DFRobot_Romeo_ESP32S3 // Has PSRAM


//==============================
// WM CONFIGURATION
//==============================

#define __BOOT_PIN 0 
constexpr std::uint_fast16_t LIMITED_TIME  {5000};
constexpr std::uint_fast16_t PREVIOUS_TIME {5000};

const char* NameOfSSID {"NARA - Assistant Learning 📖"};
const char* NameOfPASS {"DevelopedByArdev"};

//==============================
// NARA voice alert + DFPlayer Mini (UART1)
//==============================
// Pin occupancy on CAMERA_MODEL_ESP32S3_EYE (this sketch):
//   Camera: 4,5,6,7,8,9,10,11,12,13,15,16,17,18
//   LED PWM: 48
//   BOOT / WM reset: 0
//   USB D-/D+: 19,20
//   UART0 Serial debug: 43 TX, 44 RX
//   SD_MMC 1-bit on ESP32-S3-EYE hardware: CMD 38, CLK 39, D0 40
//   I2S mic on official ESP32-S3-EYE: 2, 41, 42
//   Octal PSRAM (do not use): 33-37
// Free GPIOs chosen for DFPlayer UART (not in the lists above):
//   GPIO 21 = ESP32 TX  -> DFPlayer RX (wajib, perintah 9600 8N1)
//   GPIO 47 = ESP32 RX  <- DFPlayer TX (ACK pustaka DFRobot)
#define NARA_ALERT_HTTP_PORT 82
#define DFPLAYER_UART_BAUD 9600
#define DFPLAYER_RX_PIN 47
#define DFPLAYER_TX_PIN 21
#define DFPLAYER_VOLUME 20
#define NARA_ALERT_QUEUE_LEN 8
#define NARA_ALERT_EVENT_ID_MAX 64
#define NARA_ALERT_DEDUP_SLOTS 16

#endif // CONFIG_H