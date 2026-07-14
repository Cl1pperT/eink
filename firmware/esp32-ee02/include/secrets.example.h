#pragma once

// Copy this file to include/secrets.h and replace every placeholder. The real
// secrets.h is ignored by Git. The Pi installer creates the 64-character token
// at /etc/eink-display/frame-server.token.
#define EINK_WIFI_SSID "replace-with-wifi-name"
#define EINK_WIFI_PASSWORD "replace-with-wifi-password"
#define EINK_FRAME_SERVER_URL "http://replace-with-pi-address:8787"
#define EINK_FRAME_AUTH_TOKEN "replace-with-pi-frame-server-token"
