#include "nara_stream.h"

#include <Arduino.h>
#include <string.h>
#include <stdio.h>

#include "esp_camera.h"
#include "esp_http_server.h"
#include "img_converters.h"

static const char *STREAM_CONTENT_TYPE =
    "multipart/x-mixed-replace;boundary=naraframe";
static const char *STREAM_BOUNDARY = "\r\n--naraframe\r\n";
static const char *STREAM_PART = "Content-Type: image/jpeg\r\nContent-Length: %u\r\n\r\n";

static bool streamCameraReady = false;

static esp_err_t handleStream(httpd_req_t *req) {
  if (!streamCameraReady) {
    httpd_resp_send_err(req, HTTPD_500_INTERNAL_SERVER_ERROR, "camera init failed");
    return ESP_FAIL;
  }

  esp_err_t res = httpd_resp_set_type(req, STREAM_CONTENT_TYPE);
  if (res != ESP_OK) return res;
  httpd_resp_set_hdr(req, "Access-Control-Allow-Origin", "*");

  while (true) {
    camera_fb_t *fb = esp_camera_fb_get();
    if (!fb) {
      Serial.println("[cam] capture gagal");
      break;
    }

    const uint8_t *jpg = fb->buf;
    size_t len = fb->len;
    uint8_t *owned = NULL;
    if (fb->format != PIXFORMAT_JPEG) {
      if (!frame2jpg(fb, 80, &owned, &len)) {
        esp_camera_fb_return(fb);
        break;
      }
      jpg = owned;
    }

    char part[96];
    int hlen = snprintf(part, sizeof(part), STREAM_PART, (unsigned)len);
    res = ESP_OK;
    if (hlen <= 0 || (size_t)hlen >= sizeof(part)) {
      res = ESP_FAIL;
    } else {
      res = httpd_resp_send_chunk(req, part, (size_t)hlen);
    }
    if (res == ESP_OK) {
      res = httpd_resp_send_chunk(req, (const char *)jpg, len);
    }
    if (res == ESP_OK) {
      res = httpd_resp_send_chunk(req, STREAM_BOUNDARY, strlen(STREAM_BOUNDARY));
    }

    if (owned) free(owned);
    esp_camera_fb_return(fb);
    if (res != ESP_OK) break;
  }
  return res;
}

bool naraStartMjpegServer(uint16_t port, bool cameraReady) {
  streamCameraReady = cameraReady;

  httpd_config_t config = HTTPD_DEFAULT_CONFIG();
  config.server_port = port;
  config.ctrl_port = 32769;
  config.max_open_sockets = 1;
  config.lru_purge_enable = true;
  config.stack_size = 8192;
  config.send_wait_timeout = 10;

  httpd_uri_t streamUri = {};
  streamUri.uri = "/stream";
  streamUri.method = HTTP_GET;
  streamUri.handler = handleStream;
  streamUri.user_ctx = NULL;

  httpd_handle_t handle = NULL;
  esp_err_t err = httpd_start(&handle, &config);
  if (err != ESP_OK) {
    Serial.printf("[cam] server stream gagal: %s\n", esp_err_to_name(err));
    return false;
  }
  httpd_register_uri_handler(handle, &streamUri);

  httpd_uri_t rootUri = streamUri;
  rootUri.uri = "/";
  httpd_register_uri_handler(handle, &rootUri);

  httpd_uri_t mjpegUri = streamUri;
  mjpegUri.uri = "/stream.mjpeg";
  httpd_register_uri_handler(handle, &mjpegUri);

  Serial.printf("[cam] MJPEG port %u path /stream dan /\n", (unsigned)port);
  return true;
}
