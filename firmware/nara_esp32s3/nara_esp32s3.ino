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

#include "config.h"
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
static WiFiUDP udp;
static WiFiManager wifiManager;
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
  snprintf(hostname, sizeof(hostname), "nara-cam-%s", suffix);
  snprintf(apName, sizeof(apName), "NARA-Setup-%s", suffixUp);
  snprintf(deviceName, sizeof(deviceName), "NARA Cam %s", suffixUp);
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
  if (isPaired() || !servicesStarted) return false;
  return (millis() - servicesStartedMs) < NARA_PAIR_WINDOW_MS;
}

static int folderForLang(const char *value) {
  if (strcmp(value, "id") == 0) return 1;
  if (strcmp(value, "en") == 0) return 2;
  return 0;
}

static int trackForEvent(const char *event) {
  if (strcmp(event, "SESSION_START") == 0) return 1;
  if (strcmp(event, "UNFOCUSED") == 0) return 2;
  if (strcmp(event, "BREAK_END") == 0) return 3;
  if (strcmp(event, "SESSION_END") == 0) return 4;
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
  config.pixel_format = PIXFORMAT_JPEG;
  config.grab_mode = CAMERA_GRAB_LATEST;

  if (psramFound()) {
    config.frame_size = NARA_FRAME_SIZE;
    config.jpeg_quality = NARA_JPEG_QUALITY;
    config.fb_count = 2;
    config.fb_location = CAMERA_FB_IN_PSRAM;
    Serial.printf("[cam] PSRAM %u byte, frame %d\n", (unsigned)ESP.getPsramSize(), (int)NARA_FRAME_SIZE);
  } else {
    // Tanpa PSRAM, VGA mudah gagal alokasi. Paksa QVGA satu buffer di DRAM.
    config.frame_size = FRAMESIZE_QVGA;
    config.jpeg_quality = 16;
    config.fb_count = 1;
    config.fb_location = CAMERA_FB_IN_DRAM;
    config.grab_mode = CAMERA_GRAB_WHEN_EMPTY;
    Serial.println("[cam] PSRAM tidak ketemu. Aktifkan OPI PSRAM di Arduino IDE, atau coba mode QSPI.");
  }

  esp_err_t err = esp_camera_init(&config);
  if (err != ESP_OK) {
    cameraReady = false;
    Serial.printf("[cam] init gagal: %s\n", esp_err_to_name(err));
    return;
  }
  cameraReady = true;
  // Gambar terbalik? Aktifkan salah satu baris ini, jangan keduanya sekaligus
  // kecuali hasil uji memang butuh keduanya.
  // sensor_t *sensor = esp_camera_sensor_get();
  // sensor->set_vflip(sensor, 1);
  // sensor->set_hmirror(sensor, 1);
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
    sendError(503, "mp3_unavailable");
    return;
  }
  // folder 1 = /01 (Indonesia), folder 2 = /02 (English)
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
    sendError(503, "mp3_unavailable");
    return;
  }
  player.volume(volume);
  NaraDoc out;
  out["ok"] = true;
  out["volume"] = volume;
  sendDoc(200, out);
}

static void handleNotFound() { sendError(404, "not_found"); }

static void factoryReset() {
  Serial.println("[reset] hapus WiFi + token, restart ke portal setup");
  prefs.begin("nara", false);
  prefs.remove("token");
  prefs.end();
  token[0] = '\0';
  wifiManager.resetSettings();
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

  char buf[32];
  int len = udp.read(buf, sizeof(buf) - 1);
  if (len < 0) len = 0;
  buf[len] = '\0';
  while (udp.available() > 0) udp.read();
  while (len > 0 && (buf[len - 1] == '\n' || buf[len - 1] == '\r' || buf[len - 1] == ' ' ||
                     buf[len - 1] == '\0')) {
    buf[--len] = '\0';
  }
  if (strcmp(buf, NARA_DISCOVERY_MAGIC) != 0) return;

  NaraDoc doc;
  doc["id"] = deviceId;
  doc["name"] = deviceName;
  doc["ip"] = WiFi.localIP().toString();
  doc["stream_port"] = NARA_STREAM_PORT;
  doc["paired"] = isPaired();

  char out[256];
  size_t n = serializeJson(doc, out, sizeof(out));
  if (n == 0 || n >= sizeof(out)) return;
  udp.beginPacket(udp.remoteIP(), udp.remotePort());
  udp.write((const uint8_t *)out, n);
  udp.endPacket();
}

static void tryStartServices() {
  if (servicesStarted) return;

  // Portal WiFiManager juga memakai port 80. Tunggu sebentar setelah STA up
  // dan portal sudah tutup, supaya bind port 80 tidak bentrok.
  static bool settling = false;
  static uint32_t readySince = 0;
  bool ready = WiFi.status() == WL_CONNECTED && !wifiManager.getConfigPortalActive();
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
    snprintf(portTxt, sizeof(portTxt), "%d", NARA_STREAM_PORT);
    MDNS.addService("nara", "tcp", NARA_HTTP_PORT);
    MDNS.addServiceTxt("nara", "tcp", "id", deviceId);
    MDNS.addServiceTxt("nara", "tcp", "stream_port", portTxt);
    MDNS.addServiceTxt("nara", "tcp", "fw", NARA_FW_VERSION);
    Serial.printf("[mdns] %s.local _nara._tcp port %d\n", hostname, NARA_HTTP_PORT);
  } else {
    Serial.println("[mdns] gagal, UDP 4210 tetap dipakai");
  }

  if (!udp.begin(NARA_DISCOVERY_PORT)) {
    Serial.println("[udp] bind 4210 gagal");
  }

  if (naraStartMjpegServer(NARA_STREAM_PORT, cameraReady)) {
    Serial.printf("[cam] http://%s:%d/stream\n", WiFi.localIP().toString().c_str(), NARA_STREAM_PORT);
  }

  static const char *headerKeys[] = {NARA_TOKEN_HEADER};
  server.collectHeaders(headerKeys, 1);
  server.on("/info", HTTP_GET, handleInfo);
  server.on("/status", HTTP_GET, handleStatus);
  server.on("/pair", HTTP_POST, handlePair);
  server.on("/play", HTTP_POST, handlePlay);
  server.on("/volume", HTTP_POST, handleVolume);
  server.onNotFound(handleNotFound);
  server.begin();

  servicesStarted = true;
  servicesStartedMs = millis();
  Serial.printf("[http] kontrol :%d  id=%s  paired=%s  jendela pair=%lus\n", NARA_HTTP_PORT, deviceId,
                isPaired() ? "ya" : "tidak", (unsigned long)(NARA_PAIR_WINDOW_MS / 1000));
}

void setup() {
  Serial.begin(115200);
  delay(200);
  Serial.println();
  Serial.println("NARA ESP32-S3 firmware " NARA_FW_VERSION);

  pinMode(RESET_BUTTON_PIN, INPUT_PULLUP);
  loadPrefs();

  WiFi.mode(WIFI_STA);
  buildIdentity();
  Serial.printf("[pin] board=%d  UART TX=%d RX=%d  tombol=%d\n", NARA_CAMERA_BOARD, MP3_UART_TX,
                MP3_UART_RX, RESET_BUTTON_PIN);
  Serial.printf("[id] %s  AP %s\n", deviceId, apName);

  initCamera();

  mp3Serial.begin(MP3_BAUD, SERIAL_8N1, MP3_UART_RX, MP3_UART_TX);
  delay(100);
  ensureMp3(true);

  wifiManager.setDebugOutput(true);
  wifiManager.setTitle("NARA");
  wifiManager.setHostname(hostname);
  wifiManager.setConfigPortalBlocking(false);
  wifiManager.setConnectTimeout(20);
  wifiManager.setConfigPortalTimeout(0);
  wifiManager.setWiFiAutoReconnect(true);
  // Portal non-blocking supaya tombol reset tetap dipolling di loop().
  wifiManager.autoConnect(apName);
  tryStartServices();
}

void loop() {
  wifiManager.process();
  tryStartServices();
  if (servicesStarted) {
    server.handleClient();
    serviceDiscover();
  }
  serviceButton();
  delay(2);
}
