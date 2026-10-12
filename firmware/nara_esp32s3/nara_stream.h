#pragma once

#include <stdint.h>
#include <stdbool.h>

// Dipisah dari sketch utama karena WiFiManager (WebServer.h) dan
// esp_http_server sama-sama mendefinisikan HTTP_GET. Satu file yang
// meng-include keduanya gagal kompilasi.
// Mengembalikan false kalau httpd tidak jadi start. Satu klien; koneksi
// baru menendang klien lama (lru purge).
bool naraStartMjpegServer(uint16_t port, bool cameraReady);
