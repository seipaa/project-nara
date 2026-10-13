#include "nara_alert.h"
#include <cstdint>
#include "config.h"

#include <ArduinoJson.h>
#include <DFRobotDFPlayerMini.h>
#include "esp_http_server.h"
#include "freertos/FreeRTOS.h"
#include "freertos/queue.h"
#include "freertos/semphr.h"
#include "freertos/task.h"

static HardwareSerial dfSerial(1);
static DFRobotDFPlayerMini dfPlayer;
static httpd_handle_t alert_httpd = NULL;
static QueueHandle_t alertQueue = NULL;
static SemaphoreHandle_t alertMutex = NULL;
static volatile bool dfplayerReady = false;
static volatile uint32_t queuedCount = 0;

typedef struct {
  uint8_t folder;
  uint8_t file;
  char event_id[NARA_ALERT_EVENT_ID_MAX + 1];
  char code[24];
} AlertJob;

static char seenEventIds[NARA_ALERT_DEDUP_SLOTS][NARA_ALERT_EVENT_ID_MAX + 1];
static uint8_t seenEventHead = 0;
static uint8_t seenEventCount = 0;

static void jsonResponse(httpd_req_t *req, int status, const char *body) {
  switch (status) {
    case 202:
      httpd_resp_set_status(req, "202 Accepted");
      break;
    case 400:
      httpd_resp_set_status(req, "400 Bad Request");
      break;
    case 409:
      httpd_resp_set_status(req, "409 Conflict");
      break;
    case 503:
      httpd_resp_set_status(req, "503 Service Unavailable");
      break;
    case 405:
      httpd_resp_set_status(req, "405 Method Not Allowed");
      break;
    default:
      httpd_resp_set_status(req, "200 OK");
      break;
  }
  httpd_resp_set_type(req, "application/json");
  httpd_resp_set_hdr(req, "Access-Control-Allow-Origin", "*");
  httpd_resp_set_hdr(req, "Access-Control-Allow-Methods", "GET, POST, OPTIONS");
  httpd_resp_set_hdr(req, "Access-Control-Allow-Headers", "Content-Type");
  httpd_resp_send(req, body, HTTPD_RESP_USE_STRLEN);
}

static void toUpperInPlace(char *s) {
  for (; s && *s; ++s) {
    if (*s >= 'a' && *s <= 'z') {
      *s = static_cast<char>(*s - 32);
    }
  }
}

static void toLowerInPlace(char *s) {
  for (; s && *s; ++s) {
    if (*s >= 'A' && *s <= 'Z') {
      *s = static_cast<char>(*s + 32);
    }
  }
}

static bool mapAlertTrack(const char *code, const char *language, uint8_t *folder, uint8_t *file) {
  if (strcmp(language, "id") == 0) {
    *folder = 1;
  } else if (strcmp(language, "en") == 0) {
    *folder = 2;
  } else {
    return false;
  }

  // Track 1: 001_not_focus.mp3 (Semua kondisi atensi hilang / mengantuk)
  if (strcmp(code, "NOT_FOCUSED") == 0 ||
      strcmp(code, "SLEEPING") == 0 ||
      strcmp(code, "YAWNING") == 0 ||
      strcmp(code, "FACE_ABSENT") == 0 ||
      strcmp(code, "UNFOCUSED") == 0) {
    *file = 1;
  }
  // Track 2: 002_waktu_habis.mp3 (Sesi Pomodoro Belajar Selesai)
  else if (strcmp(code, "SESSION_END") == 0 ||
           strcmp(code, "WAKTU_HABIS") == 0 ||
           strcmp(code, "TIME_UP") == 0) {
    *file = 2;
  }
  // Track 3: 003_istirahat.mp3 (Sesi Istirahat Pomodoro)
  else if (strcmp(code, "BREAK_START") == 0 ||
           strcmp(code, "BREAK_END") == 0 ||
           strcmp(code, "ISTIRAHAT") == 0 ||
           strcmp(code, "BREAK") == 0) {
    *file = 3;
  }
  // Track 4: 004_mulai.mp3 (Memulai Sesi Pomodoro Belajar)
  else if (strcmp(code, "SESSION_START") == 0 ||
           strcmp(code, "MULAI") == 0 ||
           strcmp(code, "START") == 0) {
    *file = 4;
  } else {
    return false;
  }
  return true;
}

static bool eventIdSeen(const char *event_id) {
  for (uint8_t i = 0; i < seenEventCount; ++i) {
    if (strcmp(seenEventIds[i], event_id) == 0) {
      return true;
    }
  }
  return false;
}

static void rememberEventId(const char *event_id) {
  strncpy(seenEventIds[seenEventHead], event_id, NARA_ALERT_EVENT_ID_MAX);
  seenEventIds[seenEventHead][NARA_ALERT_EVENT_ID_MAX] = '\0';
  seenEventHead = (seenEventHead + 1) % NARA_ALERT_DEDUP_SLOTS;
  if (seenEventCount < NARA_ALERT_DEDUP_SLOTS) {
    seenEventCount++;
  }
}

static int recvBody(httpd_req_t *req, char *buf, size_t bufLen) {
  int total = req->content_len;
  if (total <= 0 || total >= static_cast<int>(bufLen)) {
    return -1;
  }
  int received = 0;
  while (received < total) {
    int ret = httpd_req_recv(req, buf + received, total - received);
    if (ret <= 0) {
      return -1;
    }
    received += ret;
  }
  buf[received] = '\0';
  return received;
}

static esp_err_t alert_options_handler(httpd_req_t *req) {
  jsonResponse(req, 200, "{\"ok\":true}");
  return ESP_OK;
}

static esp_err_t alert_get_handler(httpd_req_t *req) {
  char body[192];
  snprintf(
    body,
    sizeof(body),
    "{\"ok\":true,\"dfplayer\":%s,\"queue\":%lu,\"rx\":%d,\"tx\":%d,\"port\":%d}",
    dfplayerReady ? "true" : "false",
    static_cast<unsigned long>(queuedCount),
    DFPLAYER_RX_PIN,
    DFPLAYER_TX_PIN,
    NARA_ALERT_HTTP_PORT
  );
  jsonResponse(req, 200, body);
  return ESP_OK;
}

static esp_err_t alert_post_handler(httpd_req_t *req) {
  char raw[384];
  if (recvBody(req, raw, sizeof(raw)) < 0) {
    jsonResponse(req, 400, "{\"ok\":false,\"error\":\"invalid_body\"}");
    return ESP_OK;
  }

  StaticJsonDocument<384> doc;
  DeserializationError err = deserializeJson(doc, raw);
  if (err) {
    jsonResponse(req, 400, "{\"ok\":false,\"error\":\"invalid_json\"}");
    return ESP_OK;
  }

  const char *event_id_in = doc["event_id"] | "";
  const char *code_in = doc["code"] | "";
  const char *lang_in = doc["language"] | "";
  if (event_id_in[0] == '\0' || code_in[0] == '\0' || lang_in[0] == '\0') {
    jsonResponse(req, 400, "{\"ok\":false,\"error\":\"missing_fields\"}");
    return ESP_OK;
  }
  if (strlen(event_id_in) > NARA_ALERT_EVENT_ID_MAX) {
    jsonResponse(req, 400, "{\"ok\":false,\"error\":\"event_id_too_long\"}");
    return ESP_OK;
  }

  char event_id[NARA_ALERT_EVENT_ID_MAX + 1];
  char code[24];
  char language[8];
  strncpy(event_id, event_id_in, NARA_ALERT_EVENT_ID_MAX);
  event_id[NARA_ALERT_EVENT_ID_MAX] = '\0';
  strncpy(code, code_in, sizeof(code) - 1);
  code[sizeof(code) - 1] = '\0';
  strncpy(language, lang_in, sizeof(language) - 1);
  language[sizeof(language) - 1] = '\0';
  toUpperInPlace(code);
  toLowerInPlace(language);

  uint8_t folder = 0;
  uint8_t file = 0;
  if (!mapAlertTrack(code, language, &folder, &file)) {
    jsonResponse(req, 400, "{\"ok\":false,\"error\":\"unsupported_code_or_language\"}");
    return ESP_OK;
  }

  if (alertMutex) {
    xSemaphoreTake(alertMutex, portMAX_DELAY);
  }
  bool duplicate = eventIdSeen(event_id);
  if (alertMutex) {
    xSemaphoreGive(alertMutex);
  }

  if (duplicate) {
    jsonResponse(req, 409, "{\"ok\":false,\"error\":\"duplicate_event_id\"}");
    return ESP_OK;
  }

  AlertJob job = {};
  job.folder = folder;
  job.file = file;
  strncpy(job.event_id, event_id, NARA_ALERT_EVENT_ID_MAX);
  strncpy(job.code, code, sizeof(job.code) - 1);

  if (alertQueue == NULL || xQueueSend(alertQueue, &job, 0) != pdTRUE) {
    jsonResponse(req, 503, "{\"ok\":false,\"error\":\"queue_full\"}");
    return ESP_OK;
  }

  if (alertMutex) {
    xSemaphoreTake(alertMutex, portMAX_DELAY);
  }
  rememberEventId(event_id);
  if (alertMutex) {
    xSemaphoreGive(alertMutex);
  }
  queuedCount++;

  char body[192];
  snprintf(
    body,
    sizeof(body),
    "{\"ok\":true,\"queued\":true,\"folder\":%u,\"file\":%u,\"dfplayer\":%s}",
    folder,
    file,
    dfplayerReady ? "true" : "false"
  );
  jsonResponse(req, 202, body);
  Serial.printf("[NARA-ALERT] queued %s lang=%s event=%s folder=%u file=%03u\n", code, language, event_id, folder, file);
  return ESP_OK;
}

static void naraAlertPlaybackTask(void *param) {
  (void)param;
  AlertJob job;
  for (;;) {
    if (xQueueReceive(alertQueue, &job, portMAX_DELAY) != pdTRUE) {
      continue;
    }
    if (queuedCount > 0) {
      queuedCount--;
    }

    if (!dfplayerReady) {
      Serial.printf("[NARA-ALERT] drop playback %s (DFPlayer belum siap)\n", job.event_id);
      continue;
    }

    Serial.printf("[NARA-ALERT] play folder=%u file=%03u code=%s id=%s\n", job.folder, job.file, job.code, job.event_id);
    dfPlayer.playFolder(job.folder, job.file);

    const unsigned long timeoutMs = 15000;
    unsigned long started = millis();
    vTaskDelay(pdMS_TO_TICKS(250));
    while (millis() - started < timeoutMs) {
      if (dfPlayer.available()) {
        uint8_t type = dfPlayer.readType();
        if (type == DFPlayerPlayFinished || type == DFPlayerError) {
          break;
        }
      }
      vTaskDelay(pdMS_TO_TICKS(120));
    }
  }
}

void initNaraVoiceAlert() {
  alertMutex = xSemaphoreCreateMutex();
  alertQueue = xQueueCreate(NARA_ALERT_QUEUE_LEN, sizeof(AlertJob));
  memset(seenEventIds, 0, sizeof(seenEventIds));

  Serial.printf("[NARA-ALERT] UART1 RX=%d TX=%d baud=%d\n", DFPLAYER_RX_PIN, DFPLAYER_TX_PIN, DFPLAYER_UART_BAUD);
  dfSerial.begin(DFPLAYER_UART_BAUD, SERIAL_8N1, DFPLAYER_RX_PIN, DFPLAYER_TX_PIN);
  vTaskDelay(pdMS_TO_TICKS(800));

  if (dfPlayer.begin(dfSerial, true, true)) {
    dfPlayer.setTimeOut(500);
    dfPlayer.volume(DFPLAYER_VOLUME);
    dfplayerReady = true;
    Serial.println("[NARA-ALERT] DFPlayer Mini terhubung.");
  } else {
    dfplayerReady = false;
    Serial.println("[NARA-ALERT] DFPlayer Mini TIDAK terdeteksi. HTTP /api/alert tetap aktif; playback ditahan.");
  }

  xTaskCreatePinnedToCore(naraAlertPlaybackTask, "naraAlert", 4096, NULL, 2, NULL, 1);
}

void startNaraAlertServer() {
  httpd_config_t config = HTTPD_DEFAULT_CONFIG();
  config.server_port = NARA_ALERT_HTTP_PORT;
  config.ctrl_port = 32780;
  config.max_uri_handlers = 4;
  config.lru_purge_enable = true;
  config.stack_size = 8192;

  httpd_uri_t alert_post = {
    .uri = "/api/alert",
    .method = HTTP_POST,
    .handler = alert_post_handler,
    .user_ctx = NULL
  };
  httpd_uri_t alert_get = {
    .uri = "/api/alert",
    .method = HTTP_GET,
    .handler = alert_get_handler,
    .user_ctx = NULL
  };
  httpd_uri_t alert_opt = {
    .uri = "/api/alert",
    .method = HTTP_OPTIONS,
    .handler = alert_options_handler,
    .user_ctx = NULL
  };

  Serial.printf("[NARA-ALERT] Starting control server on port %d\n", config.server_port);
  if (httpd_start(&alert_httpd, &config) == ESP_OK) {
    httpd_register_uri_handler(alert_httpd, &alert_post);
    httpd_register_uri_handler(alert_httpd, &alert_get);
    httpd_register_uri_handler(alert_httpd, &alert_opt);
    Serial.println("[NARA-ALERT] POST http://nara-camera.local:82/api/alert");
  } else {
    Serial.println("[NARA-ALERT] GAGAL start HTTP port 82");
  }
}

bool naraAlertDfplayerReady() {
  return dfplayerReady;
}

uint32_t naraAlertQueuedCount() {
  return queuedCount;
}
