/*
 * NARA — firmware ESP32-S3-CAM + modul MP3 (DFPlayer Mini) via UART.
 *
 * Kontrak (detail di README.md):
 *   AP portal : NARA-Setup-XXXX
 *   mDNS      : nara-cam-XXXX.local  service _nara._tcp port 80
 *               TXT id, stream_port, fw
 *   UDP 4210  : datagram "NARA_DISCOVER" -> JSON info
 *   HTTP :81  : GET /stream  (MJPEG, satu klien)
 *   HTTP :80  : GET /info  POST /pair  POST /play  POST /volume  GET /status
 *
 * Audio ada di kartu SD modul MP3, bukan di slot SD ESP.
 * Jika modul MP3 tidak ACK, kamera tetap streaming.
 */

#include <WiFi.h>
#include <WiFiUdp.h>
#include <WebServer.h>
#include <ESPmDNS.h>
#include <WiFiManager.h>
#include <Preferences.h>
#include <HardwareSerial.h>
#include <DFRobotDFPlayerMini.h>
#include <ArduinoJson.h>
#include "esp_camera.h"
#include "esp_system.h"
#include "esp_arduino_version.h"

//own libraries
#include "config.h"
#include "User_Interface.h"
#include "nara_stream.h"

// Ukuran frame sensor. QVGA (320x240) paling stabil untuk satu klien MJPEG.
// Naikkan ke FRAMESIZE_VGA (640x480) kalau laptop masih lancar; jpeg_quality
// 0 = paling tajam / besar, 63 = paling kecil. 12 adalah kompromi yang aman.
#define NARA_FRAME_SIZE FRAMESIZE_QVGA
#define NARA_JPEG_QUALITY 12

#if defined(ARDUINOJSON_VERSION_MAJOR) && ARDUINOJSON_VERSION_MAJOR >= 7
using NaraDoc = JsonDocument;
#else
using NaraDoc = StaticJsonDocument<512>;
#endif

static WebServer server(NARA_HTTP_PORT);
static WebServer alertServer(NARA_ALERT_PORT);
static WiFiUDP udp;
static WiFiManager wm;
static Preferences prefs;
static HardwareSerial mp3Serial(1);
static DFRobotDFPlayerMini player;

static char deviceId[20];
static char hostname[20];
static char apName[24];
static char deviceName[24];
static char token[40];
static char lang[4];
static char lastEvent[24];

static uint8_t volume = NARA_VOLUME_DEFAULT;
static bool cameraReady = false;
static bool mp3Ready = false;
static bool servicesStarted = false;
static uint32_t servicesStartedMs = 0;

static bool jsonVariantMissing(JsonVariant v) {
#if defined(ARDUINOJSON_VERSION_MAJOR) && ARDUINOJSON_VERSION_MAJOR >= 7
  return v.isUnbound() || v.isNull();
#else
  return v.isNull();
#endif
}

static bool jsonIsNumber(JsonVariant v) {
  return v.is<int>() || v.is<long>() || v.is<float>() || v.is<double>();
}

static void loadPrefs() {
  memset(token, 0, sizeof(token));
  memset(lang, 0, sizeof(lang));
  memset(lastEvent, 0, sizeof(lastEvent));
  prefs.begin("nara", true);
  prefs.getString("token", token, sizeof(token));
  prefs.getString("lang", lang, sizeof(lang));
  volume = prefs.getUChar("vol", NARA_VOLUME_DEFAULT);
  prefs.end();
  if (strcmp(lang, "en") != 0) {
    snprintf(lang, sizeof(lang), "id");
  }
  if (volume > NARA_VOLUME_MAX) {
    volume = NARA_VOLUME_DEFAULT;
  }
}

static void saveToken() {
  prefs.begin("nara", false);
  prefs.putString("token", token);
  prefs.end();
}

static void saveLang() {
  prefs.begin("nara", false);
  prefs.putString("lang", lang);
  prefs.end();
}

static void saveVolume() {
  prefs.begin("nara", false);
  prefs.putUChar("vol", volume);
  prefs.end();
}

static bool isPaired() { return token[0] != '\0'; }

static void buildIdentity() {
  uint8_t mac[6] = {0};
  WiFi.macAddress(mac);
  char suffix[5];
  char suffixUp[5];
  snprintf(suffix, sizeof(suffix), "%02x%02x", mac[4], mac[5]);
  snprintf(suffixUp, sizeof(suffixUp), "%02X%02X", mac[4], mac[5]);
  snprintf(deviceId, sizeof(deviceId), "nara-cam-%s", suffix);
#if NARA_USE_FIXED_HOSTNAME
  snprintf(hostname, sizeof(hostname), "%s", NARA_DEFAULT_HOSTNAME);
#else
  snprintf(hostname, sizeof(hostname), "nara-cam-%s", suffix);
#endif
  snprintf(apName, sizeof(apName), "NARA-Setup-%s", suffixUp);
  snprintf(deviceName, sizeof(deviceName), "NARA Device");
}

static void makeToken() {
  static const char *hex = "0123456789abcdef";
  for (int i = 0; i < 32; i += 2) {
    uint8_t b = (uint8_t)(esp_random() & 0xFF);
    token[i] = hex[b >> 4];
    token[i + 1] = hex[b & 0x0F];
  }
  token[32] = '\0';
}

static void sendDoc(int code, NaraDoc &doc) {
  char buf[512];
  size_t n = serializeJson(doc, buf, sizeof(buf));
  if (n == 0 || n >= sizeof(buf)) {
    server.send(500, "application/json", "{\"error\":\"response_too_big\"}");
    return;
  }
  server.send(code, "application/json", buf);
}

static void sendError(int code, const char *error) {
  NaraDoc doc;
  doc["error"] = error;
  sendDoc(code, doc);
}

static bool readBody(NaraDoc &doc) {
  DeserializationError err = deserializeJson(doc, server.arg("plain"));
  return !err;
}

static bool authorized() {
  if (!isPaired()) return false;
  if (!server.hasHeader(NARA_TOKEN_HEADER)) return false;
  return server.header(NARA_TOKEN_HEADER) == token;
}

static bool pairingOpen() {
#if NARA_ALWAYS_ALLOW_PAIR
  return true;
#else
  if (isPaired() || !servicesStarted) return false;
  return (millis() - servicesStartedMs) < NARA_PAIR_WINDOW_MS;
#endif
}

static int folderForLang(const char *value) {
  if (strcmp(value, "en") == 0) return 1;
  if (strcmp(value, "id") == 0) return 2;
  return 0;
}

static int trackForEvent(const char *event) {
  if (strcmp(event, "UNFOCUSED") == 0 || strcmp(event, "NOT_FOCUSED") == 0 ||
      strcmp(event, "SLEEPING") == 0 || strcmp(event, "YAWNING") == 0 ||
      strcmp(event, "FACE_ABSENT") == 0) return 1;
  if (strcmp(event, "SESSION_END") == 0 || strcmp(event, "WAKTU_HABIS") == 0 ||
      strcmp(event, "TIME_UP") == 0) return 2;
  if (strcmp(event, "BREAK_END") == 0 || strcmp(event, "BREAK") == 0 ||
      strcmp(event, "ISTIRAHAT") == 0 || strcmp(event, "BREAK_START") == 0) return 3;
  if (strcmp(event, "SESSION_START") == 0 || strcmp(event, "MULAI") == 0 ||
      strcmp(event, "START") == 0) return 4;
  return 0;
}

// begin() false = modul diam atau kartu SD-nya tidak online.
// Selama belum siap, /play dan /volume tidak memanggil perintah UART
// (supaya tidak menunggu ACK). Keduanya boleh mencoba begin() lagi.
static bool ensureMp3(bool doReset) {
  if (mp3Ready) return true;
  Serial.println("[mp3] menunggu ACK modul");
  if (!player.begin(mp3Serial, true, doReset)) {
    Serial.println("[mp3] tidak ACK, stream kamera tetap jalan");
    mp3Ready = false;
    return false;
  }
  player.setTimeOut(400);
  player.volume(volume);
  mp3Ready = true;
  Serial.println("[mp3] siap");
  return true;
}

static void initCamera() {
  camera_config_t config;
  memset(&config, 0, sizeof(config));
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
  // Core 2.x menamai field ini "sscb" (typo lama). Core 3.x memperbaiki jadi "sccb".
#if ESP_ARDUINO_VERSION >= ESP_ARDUINO_VERSION_VAL(3, 0, 0)
  config.pin_sccb_sda = SIOD_GPIO_NUM;
  config.pin_sccb_scl = SIOC_GPIO_NUM;
#else
  config.pin_sscb_sda = SIOD_GPIO_NUM;
  config.pin_sscb_scl = SIOC_GPIO_NUM;
#endif
  config.pin_pwdn = (gpio_num_t)PWDN_GPIO_NUM;
  config.pin_reset = (gpio_num_t)RESET_GPIO_NUM;
  config.xclk_freq_hz = 20000000;
  // Inisialisasi awal sensor dengan UXGA wajib untuk probe OV3660/OV2640
  config.frame_size = FRAMESIZE_UXGA;
  config.pixel_format = PIXFORMAT_JPEG;
  config.grab_mode = CAMERA_GRAB_WHEN_EMPTY;
  config.fb_location = CAMERA_FB_IN_PSRAM;
  config.jpeg_quality = 12;
  config.fb_count = 1;

  if (psramFound()) {
    config.jpeg_quality = 10;
    config.fb_count = 2;
    config.grab_mode = CAMERA_GRAB_LATEST;
    Serial.printf("[cam] PSRAM %u byte terdeteksi\n", (unsigned)ESP.getPsramSize());
  } else {
    // Tanpa PSRAM, fallback ke SVGA internal DRAM
    config.frame_size = FRAMESIZE_SVGA;
    config.fb_location = CAMERA_FB_IN_DRAM;
    Serial.println("[cam] PSRAM tidak ketemu. Pastikan opsi 'PSRAM: OPI PSRAM' aktif di Arduino IDE!");
  }

  // Berikan waktu sensor kamera untuk stabilisasi daya (power rail settling)
  delay(200);

  esp_err_t err = esp_camera_init(&config);
  if (err != ESP_OK) {
    // Coba fallback dengan frekuensi XCLK 10MHz (sangat membantu modul OV3660 / kabel pita panjang)
    Serial.printf("[cam] Init 20MHz gagal (0x%x). Mencoba frekuensi XCLK 10MHz...\n", err);
    esp_camera_deinit();
    delay(100);
    config.xclk_freq_hz = 10000000;
    err = esp_camera_init(&config);
  }

  if (err != ESP_OK) {
    cameraReady = false;
    Serial.printf("[cam] init gagal: 0x%x (%s)\n", err, esp_err_to_name(err));
    return;
  }

  cameraReady = true;

  sensor_t *s = esp_camera_sensor_get();
  if (s != NULL) {
    // Optimasi khusus sensor OV3660 (flip vertikal & saturasi)
    if (s->id.PID == OV3660_PID) {
      s->set_vflip(s, 1);
      s->set_brightness(s, 1);
      s->set_saturation(s, -2);
      Serial.println("[cam] Sensor OV3660 terdeteksi & terkonfigurasi!");
    }
    // Turunkan resolusi ke NARA_FRAME_SIZE (QVGA) untuk streaming stabil
    s->set_framesize(s, NARA_FRAME_SIZE);
    Serial.printf("[cam] Resolusi streaming diset ke frame_size=%d\n", (int)NARA_FRAME_SIZE);
  }
}

static void handleInfo() {
  NaraDoc doc;
  doc["id"] = deviceId;
  doc["name"] = deviceName;
  doc["fw"] = NARA_FW_VERSION;
  doc["paired"] = isPaired();
  doc["stream_port"] = NARA_STREAM_PORT;
  sendDoc(200, doc);
}

static void handleStatus() {
  NaraDoc doc;
  doc["wifi"] = WiFi.status() == WL_CONNECTED;
  doc["ip"] = WiFi.localIP().toString();
  doc["paired"] = isPaired();
  doc["volume"] = volume;
  doc["last_event"] = lastEvent;
  doc["lang"] = lang;
  doc["mp3_ok"] = mp3Ready;
  sendDoc(200, doc);
}

static void handlePair() {
  if (!pairingOpen()) {
    sendError(403, isPaired() ? "already_paired" : "pairing_window_closed");
    return;
  }
  makeToken();
  saveToken();
  NaraDoc doc;
  doc["token"] = token;
  sendDoc(200, doc);
  Serial.println("[pair] token baru disimpan");
}

static void handlePlay() {
  if (!authorized()) {
    sendError(403, "unauthorized");
    return;
  }
  NaraDoc doc;
  if (!readBody(doc)) {
    sendError(400, "bad_json");
    return;
  }
  const char *langIn = doc["lang"] | "";
  const char *eventIn = doc["event"] | "";
  int folder = folderForLang(langIn);
  int track = trackForEvent(eventIn);
  if (folder == 0) {
    sendError(400, "bad_lang");
    return;
  }
  if (track == 0) {
    sendError(400, "bad_event");
    return;
  }

  snprintf(lang, sizeof(lang), "%s", langIn);
  saveLang();

  if (!ensureMp3(false)) {
#if NARA_TOLERANT_MP3
    snprintf(lastEvent, sizeof(lastEvent), "%s", eventIn);
    Serial.printf("[mp3] MOCK PLAY (DFPlayer offline/simulasi): folder %d track %d (%s)\n", folder, track, lastEvent);
    NaraDoc out;
    out["ok"] = true;
    out["lang"] = lang;
    out["event"] = lastEvent;
    out["folder"] = folder;
    out["track"] = track;
    out["dfplayer"] = false;
    sendDoc(200, out);
    return;
#else
    sendError(503, "mp3_unavailable");
    return;
#endif
  }
  // folder 1 = /01 (English), folder 2 = /02 (Indonesia)
  // track 1..4 = 001.mp3 .. 004.mp3
  player.playFolder((uint8_t)folder, (uint8_t)track);
  snprintf(lastEvent, sizeof(lastEvent), "%s", eventIn);
  Serial.printf("[mp3] play folder %d track %d (%s)\n", folder, track, lastEvent);

  NaraDoc out;
  out["ok"] = true;
  out["lang"] = lang;
  out["event"] = lastEvent;
  out["folder"] = folder;
  out["track"] = track;
  out["dfplayer"] = true;
  sendDoc(200, out);
}

static void handleVolume() {
  if (!authorized()) {
    sendError(403, "unauthorized");
    return;
  }
  NaraDoc doc;
  if (!readBody(doc)) {
    sendError(400, "bad_json");
    return;
  }
  JsonVariant volVar = doc["volume"];
  if (jsonVariantMissing(volVar) || !jsonIsNumber(volVar)) {
    sendError(400, "bad_volume");
    return;
  }
  int requested = volVar.as<int>();
  if (requested < 0 || requested > NARA_VOLUME_MAX) {
    sendError(400, "bad_volume");
    return;
  }
  volume = (uint8_t)requested;
  saveVolume();
  if (!ensureMp3(false)) {
#if NARA_TOLERANT_MP3
    Serial.printf("[mp3] MOCK VOLUME (DFPlayer offline/simulasi): vol=%d\n", volume);
    NaraDoc out;
    out["ok"] = true;
    out["volume"] = volume;
    out["dfplayer"] = false;
    sendDoc(200, out);
    return;
#else
    sendError(503, "mp3_unavailable");
    return;
#endif
  }
  player.volume(volume);
  NaraDoc out;
  out["ok"] = true;
  out["volume"] = volume;
  out["dfplayer"] = true;
  sendDoc(200, out);
}

static void handleCapture() {
  if (!cameraReady) {
    server.send(503, "text/plain", "camera_not_ready");
    return;
  }
  camera_fb_t *fb = esp_camera_fb_get();
  if (!fb) {
    server.send(500, "text/plain", "capture_failed");
    return;
  }
  server.sendHeader("Access-Control-Allow-Origin", "*");
  WiFiClient client = server.client();
  server.setContentLength(fb->len);
  server.send(200, "image/jpeg", "");
  client.write(fb->buf, fb->len);
  esp_camera_fb_return(fb);
}

static char seenAlertIds[16][48];
static uint8_t seenAlertHead = 0;
static uint8_t seenAlertCount = 0;

static bool alertIdSeen(const char *event_id) {
  if (!event_id || event_id[0] == '\0') return false;
  for (uint8_t i = 0; i < seenAlertCount; ++i) {
    if (strcmp(seenAlertIds[i], event_id) == 0) return true;
  }
  return false;
}

static void rememberAlertId(const char *event_id) {
  if (!event_id || event_id[0] == '\0') return;
  strncpy(seenAlertIds[seenAlertHead], event_id, sizeof(seenAlertIds[0]) - 1);
  seenAlertIds[seenAlertHead][sizeof(seenAlertIds[0]) - 1] = '\0';
  seenAlertHead = (seenAlertHead + 1) % 16;
  if (seenAlertCount < 16) seenAlertCount++;
}

static void handleApiAlertOptions() {
  server.sendHeader("Access-Control-Allow-Origin", "*");
  server.sendHeader("Access-Control-Allow-Methods", "GET, POST, OPTIONS");
  server.sendHeader("Access-Control-Allow-Headers", "Content-Type");
  server.send(200, "application/json", "{\"ok\":true}");
}

static void handleApiAlertGet() {
  server.sendHeader("Access-Control-Allow-Origin", "*");
  NaraDoc doc;
  doc["ok"] = true;
  doc["dfplayer"] = mp3Ready;
  doc["queue"] = 0;
  doc["volume"] = volume;
  doc["last_event"] = lastEvent;
  sendDoc(200, doc);
}

static void handleApiAlert() {
  server.sendHeader("Access-Control-Allow-Origin", "*");
  NaraDoc doc;
  if (!readBody(doc)) {
    sendError(400, "bad_json");
    return;
  }
  const char *eventId = doc["event_id"] | "";
  const char *code = doc["code"] | "";
  const char *language = doc["language"] | "";

  if (eventId[0] == '\0' || code[0] == '\0' || language[0] == '\0') {
    sendError(400, "missing_fields");
    return;
  }

  if (alertIdSeen(eventId)) {
    sendError(409, "duplicate_event_id");
    return;
  }
  rememberAlertId(eventId);

  int folder = folderForLang(language);
  int track = trackForEvent(code);
  if (folder == 0 || track == 0) {
    sendError(400, "unsupported_code_or_language");
    return;
  }

  snprintf(lang, sizeof(lang), "%s", language);
  saveLang();

  if (ensureMp3(false)) {
    player.playFolder((uint8_t)folder, (uint8_t)track);
    snprintf(lastEvent, sizeof(lastEvent), "%s", code);
    Serial.printf("[alert:80] play folder %d track %d (%s) id=%s\n", folder, track, code, eventId);
  } else {
#if NARA_TOLERANT_MP3
    snprintf(lastEvent, sizeof(lastEvent), "%s", code);
    Serial.printf("[alert:80] MOCK PLAY (DFPlayer offline/simulasi): folder %d track %d (%s) id=%s\n", folder, track, code, eventId);
#endif
  }

  NaraDoc out;
  out["ok"] = true;
  out["queued"] = true;
  out["folder"] = folder;
  out["file"] = track;
  out["dfplayer"] = mp3Ready;
  sendDoc(202, out);
}

static void handleNotFound() { sendError(404, "not_found"); }

// -----------------------------------------------------------------------------
// Handler Port 82 (Dedicated Alert Server untuk voice_alert_service.py)
// -----------------------------------------------------------------------------
static void sendAlertDoc(int code, NaraDoc &doc) {
  char buf[512];
  size_t n = serializeJson(doc, buf, sizeof(buf));
  if (n == 0 || n >= sizeof(buf)) {
    alertServer.send(500, "application/json", "{\"error\":\"response_too_big\"}");
    return;
  }
  alertServer.send(code, "application/json", buf);
}

static void sendAlertError(int code, const char *error) {
  NaraDoc doc;
  doc["error"] = error;
  sendAlertDoc(code, doc);
}

static void handleApiAlertOptions82() {
  alertServer.sendHeader("Access-Control-Allow-Origin", "*");
  alertServer.sendHeader("Access-Control-Allow-Methods", "GET, POST, OPTIONS");
  alertServer.sendHeader("Access-Control-Allow-Headers", "Content-Type");
  alertServer.send(200, "application/json", "{\"ok\":true}");
}

static void handleApiAlertGet82() {
  alertServer.sendHeader("Access-Control-Allow-Origin", "*");
  NaraDoc doc;
  doc["ok"] = true;
  doc["dfplayer"] = mp3Ready;
  doc["queue"] = 0;
  doc["volume"] = volume;
  doc["last_event"] = lastEvent;
  sendAlertDoc(200, doc);
}

static void handleApiAlert82() {
  alertServer.sendHeader("Access-Control-Allow-Origin", "*");
  NaraDoc doc;
  DeserializationError err = deserializeJson(doc, alertServer.arg("plain"));
  if (err) {
    sendAlertError(400, "bad_json");
    return;
  }
  const char *eventId = doc["event_id"] | "";
  const char *code = doc["code"] | "";
  const char *language = doc["language"] | "";

  if (eventId[0] == '\0' || code[0] == '\0' || language[0] == '\0') {
    sendAlertError(400, "missing_fields");
    return;
  }

  if (alertIdSeen(eventId)) {
    sendAlertError(409, "duplicate_event_id");
    return;
  }
  rememberAlertId(eventId);

  int folder = folderForLang(language);
  int track = trackForEvent(code);
  if (folder == 0 || track == 0) {
    sendAlertError(400, "unsupported_code_or_language");
    return;
  }

  snprintf(lang, sizeof(lang), "%s", language);
  saveLang();

  if (ensureMp3(false)) {
    player.playFolder((uint8_t)folder, (uint8_t)track);
    snprintf(lastEvent, sizeof(lastEvent), "%s", code);
    Serial.printf("[alert:82] play folder %d track %d (%s) id=%s\n", folder, track, code, eventId);
  } else {
#if NARA_TOLERANT_MP3
    snprintf(lastEvent, sizeof(lastEvent), "%s", code);
    Serial.printf("[alert:82] MOCK PLAY (DFPlayer offline/simulasi): folder %d track %d (%s) id=%s\n", folder, track, code, eventId);
#endif
  }

  NaraDoc out;
  out["ok"] = true;
  out["queued"] = true;
  out["folder"] = folder;
  out["file"] = track;
  out["dfplayer"] = mp3Ready;
  sendAlertDoc(202, out);
}

static void handleNotFoundAlert() { sendAlertError(404, "not_found"); }

static void factoryReset() {
  Serial.println("[reset] hapus WiFi + token, restart ke portal setup");
  prefs.begin("nara", false);
  prefs.remove("token");
  prefs.end();
  token[0] = '\0';
  wm.resetSettings();
  WiFi.disconnect(true, true);
  delay(300);
  ESP.restart();
}

static void serviceButton() {
  static bool down = false;
  static bool announced = false;
  static uint32_t pressedAt = 0;
  bool pressed = digitalRead(RESET_BUTTON_PIN) == LOW;
  if (!pressed) {
    down = false;
    announced = false;
    return;
  }
  if (!down) {
    down = true;
    announced = false;
    pressedAt = millis();
    return;
  }
  if (!announced && millis() - pressedAt >= 400) {
    announced = true;
    Serial.println("[reset] tombol ditahan, lepas sebelum 5 detik kalau tidak ingin reset");
  }
  if (millis() - pressedAt >= RESET_HOLD_MS) {
    factoryReset();
  }
}

static void serviceDiscover() {
  int packetSize = udp.parsePacket();
  if (packetSize <= 0) return;

  char buf[64];
  int len = udp.read(buf, sizeof(buf) - 1);
  if (len < 0) len = 0;
  buf[len] = '\0';
  while (udp.available() > 0) udp.read();

  // Bersihkan newline/spasi di ujung paket
  while (len > 0 && (buf[len - 1] == '\n' || buf[len - 1] == '\r' || buf[len - 1] == ' ' || buf[len - 1] == '\0')) {
    buf[--len] = '\0';
  }
  if (strstr(buf, NARA_DISCOVERY_MAGIC) == NULL) return;

  NaraDoc doc;
  doc["id"] = deviceId;
  doc["name"] = deviceName;
  doc["ip"] = WiFi.localIP().toString();
  doc["stream_port"] = NARA_STREAM_PORT;
  doc["alert_port"] = NARA_ALERT_PORT;
  doc["http_port"] = NARA_HTTP_PORT;
  doc["paired"] = isPaired();
  doc["fw"] = NARA_FW_VERSION;

  char out[320];
  size_t n = serializeJson(doc, out, sizeof(out));
  if (n == 0 || n >= sizeof(out)) return;
  udp.beginPacket(udp.remoteIP(), udp.remotePort());
  udp.write((const uint8_t *)out, n);
  udp.endPacket();
  Serial.printf("[udp] Discovery probe dijawab ke %s:%d\n", udp.remoteIP().toString().c_str(), udp.remotePort());
}

static void tryStartServices() {
  if (servicesStarted) return;

  static bool settling = false;
  static uint32_t readySince = 0;
  bool ready = (WiFi.status() == WL_CONNECTED) && !wm.getConfigPortalActive();
  if (!ready) {
    settling = false;
    return;
  }
  if (!settling) {
    settling = true;
    readySince = millis();
    return;
  }
  if (millis() - readySince < 300) return;

  WiFi.setSleep(false);
  WiFi.setHostname(hostname);

  if (MDNS.begin(hostname)) {
    char portTxt[8];
    char alertTxt[8];
    snprintf(portTxt, sizeof(portTxt), "%d", NARA_STREAM_PORT);
    snprintf(alertTxt, sizeof(alertTxt), "%d", NARA_ALERT_PORT);

    // Layanan discovery untuk device_discovery.py
    MDNS.addService("nara", "tcp", NARA_HTTP_PORT);
    auto addTxt = [](const char *name, const char *proto, const char *key, const char *value) {
      MDNS.addServiceTxt((char *)name, (char *)proto, (char *)key, (char *)value);
    };
    addTxt("nara", "tcp", "id", (const char *)deviceId);
    addTxt("nara", "tcp", "stream_port", (const char *)portTxt);
    addTxt("nara", "tcp", "alert_port", (const char *)alertTxt);
    addTxt("nara", "tcp", "fw", (const char *)NARA_FW_VERSION);
    addTxt("nara", "tcp", "paired", (const char *)(isPaired() ? "1" : "0"));

    // Standard HTTP service (Cukup satu agar tidak memicu error duplicate service ESP-IDF)
    MDNS.addService("http", "tcp", NARA_HTTP_PORT);
    Serial.printf("[mdns] %s.local _nara._tcp port %d (stream:%d alert:%d)\n",
                  hostname, NARA_HTTP_PORT, NARA_STREAM_PORT, NARA_ALERT_PORT);
  } else {
    Serial.println("[mdns] Gagal menginisialisasi mDNS, UDP 4210 tetap aktif");
  }

  if (!udp.begin(NARA_DISCOVERY_PORT)) {
    Serial.println("[udp] Bind port 4210 gagal");
  } else {
    Serial.printf("[udp] Discovery listener aktif di port %d\n", NARA_DISCOVERY_PORT);
  }

  if (naraStartMjpegServer(NARA_STREAM_PORT, cameraReady)) {
    Serial.printf("[cam] Stream MJPEG: http://%s:%d/stream\n", WiFi.localIP().toString().c_str(), NARA_STREAM_PORT);
  }

  // 1. Inisialisasi Server Port 80 (Kontrol & Pairing)
  static const char *headerKeys[] = {NARA_TOKEN_HEADER};
  server.collectHeaders(headerKeys, 1);
  server.on("/info", HTTP_GET, handleInfo);
  server.on("/status", HTTP_GET, handleStatus);
  server.on("/pair", HTTP_POST, handlePair);
  server.on("/play", HTTP_POST, handlePlay);
  server.on("/volume", HTTP_POST, handleVolume);
  server.on("/capture", HTTP_GET, handleCapture);
  server.on("/api/alert", HTTP_POST, handleApiAlert);
  server.on("/api/alert", HTTP_GET, handleApiAlertGet);
  server.on("/api/alert", HTTP_OPTIONS, handleApiAlertOptions);
  server.onNotFound(handleNotFound);
  server.begin();

  // 2. Inisialisasi Server Port 82 (Dedicated Voice Alert untuk voice_alert_service.py)
  alertServer.collectHeaders(headerKeys, 1);
  alertServer.on("/api/alert", HTTP_POST, handleApiAlert82);
  alertServer.on("/api/alert", HTTP_GET, handleApiAlertGet82);
  alertServer.on("/api/alert", HTTP_OPTIONS, handleApiAlertOptions82);
  alertServer.onNotFound(handleNotFoundAlert);
  alertServer.begin();

  servicesStarted = true;
  servicesStartedMs = millis();
  Serial.printf("[http] Port 80 (Kontrol) & Port 82 (Alert) aktif! id=%s  paired=%s\n",
                deviceId, isPaired() ? "ya" : "tidak");
}

void setup() {
  Serial.begin(115200);
  delay(200);
  Serial.println();
  Serial.println("==================================================");
  Serial.println("  NARA ESP32-S3 Firmware v" NARA_FW_VERSION);
  Serial.println("  ESP32-S3-CAM + DFPlayer Mini Voice Alert");
  Serial.println("==================================================");

  pinMode(RESET_BUTTON_PIN, INPUT_PULLUP);
  loadPrefs();

  WiFi.mode(WIFI_STA);
  WiFi.setSleep(false);
  buildIdentity();
  Serial.printf("[pin] Board=%d  UART TX=%d RX=%d  Tombol=%d\n", NARA_CAMERA_BOARD, MP3_UART_TX,
                MP3_UART_RX, RESET_BUTTON_PIN);
  Serial.printf("[id] Device ID: %s  |  Hostname: %s.local\n", deviceId, hostname);

  initCamera();

#if NARA_ENABLE_HARDWARE_MP3
  mp3Serial.begin(MP3_BAUD, SERIAL_8N1, MP3_UART_RX, MP3_UART_TX);
  delay(100);
  ensureMp3(true);
#else
  mp3Ready = false;
  Serial.println("[mp3] Hardware MP3 dinonaktifkan (audio ditangani laptop)");
#endif

#if USE_DIRECT_WIFI
  Serial.printf("[wifi] Menghubungkan langsung ke SSID '%s'...\n", WIFI_SSID);
  WiFi.begin(WIFI_SSID, WIFI_PASS);
  uint32_t startAttempt = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - startAttempt < 12000) {
    delay(400);
    Serial.print(".");
  }
  Serial.println();
  if (WiFi.status() == WL_CONNECTED) {
    Serial.printf("[wifi] TERHUBUNG! IP ESP: %s\n", WiFi.localIP().toString().c_str());
    tryStartServices();
  } else {
    Serial.println("[wifi] Gagal konek langsung ke SSID. Mengaktifkan wm AP...");
    wm.setDebugOutput(true);
    wm.setHostname(hostname);
    wm.setConfigPortalBlocking(false);
    wm.setConnectTimeout(20);
    wm.setConfigPortalTimeout(0);
    wm.setWiFiAutoReconnect(true);
    wm.autoConnect(apName);
  }
#else
  setParameterUI(wm);
  wm.autoConnect( WM_SSID, WM_PASS);
  wm.setDebugOutput(true);
  wm.setHostname(hostname);
  wm.setConfigPortalBlocking(false);
  wm.setConnectTimeout(180);
  wm.setConfigPortalTimeout(0);
  wm.setWiFiAutoReconnect(true);
#endif
}

void loop() {
#if !USE_DIRECT_WIFI
  wm.process();
#else
  if (WiFi.status() != WL_CONNECTED) {
    wm.process();
  }
#endif
  tryStartServices();
  if (servicesStarted) {
    server.handleClient();
    alertServer.handleClient();
    serviceDiscover();
  }
  serviceButton();
  delay(2);
}
