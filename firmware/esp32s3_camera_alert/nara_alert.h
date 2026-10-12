#ifndef NARA_ALERT_H
#define NARA_ALERT_H
#pragma once

#include <Arduino.h>

// DFPlayer Mini on UART1. Track layout on the DFPlayer microSD:
//   folder 01 (language=id) / folder 02 (language=en)
//   001.mp3 NOT_FOCUSED
//   002.mp3 SLEEPING
//   003.mp3 YAWNING
//   004.mp3 FACE_ABSENT
// HTTP control API (separate from camera ports 80/81):
//   POST http://nara-camera.local:82/api/alert
//   GET  http://nara-camera.local:82/api/alert

void initNaraVoiceAlert();
void startNaraAlertServer();
bool naraAlertDfplayerReady();
uint32_t naraAlertQueuedCount();

#endif  // NARA_ALERT_H
