#include <Arduino.h>
#include <HTTPClient.h>
#include <Preferences.h>
#include <TFT_eSPI.h>
#include <WiFi.h>
#include <esp_heap_caps.h>
#include <mbedtls/sha256.h>
#include <mbedtls/version.h>

#include <cstring>

#include "device_config.h"
#include "frame_contract.h"

#if __has_include("secrets.h")
#include "secrets.h"
#else
#include "secrets.example.h"
#warning "Using placeholder credentials; copy secrets.example.h to secrets.h"
#endif

static_assert(EPD_WIDTH == eink::kBackingWidth,
              "Seeed_GFX is not configured for Setup510 width");
static_assert(EPD_HEIGHT == eink::kBackingHeight,
              "Seeed_GFX is not configured for Setup510 height");
static_assert(EPD_COLOR_DEPTH == 4,
              "Seeed_GFX is not configured for the EE02 4bpp sprite");

namespace {

constexpr char kPreferencesNamespace[] = "eink-frame";
constexpr char kPreferenceMode[] = "mode";
constexpr char kPreferenceSha[] = "sha256";
constexpr char kPreferenceEtag[] = "etag";

EPaper epaper;
Preferences preferences;
bool preferencesReady = false;

String requestedMode = EINK_DEFAULT_MODE;
String displayedMode;
String displayedSha;
String displayedEtag;
uint32_t lastPollStarted = 0;
bool pollImmediately = true;
bool forceNextDownload = false;

enum class SyncResult {
  kUpdated,
  kNotModified,
  kFailed,
};

bool elapsed(uint32_t since, uint32_t duration) {
  return static_cast<uint32_t>(millis() - since) >= duration;
}

bool isPlaceholder(const char *value) {
  return value == nullptr || value[0] == '\0' ||
         String(value).startsWith("replace-with-");
}

bool configurationIsUsable() {
  if (isPlaceholder(EINK_WIFI_SSID) || isPlaceholder(EINK_FRAME_AUTH_TOKEN)) {
    Serial.println("Configuration error: provision include/secrets.h");
    return false;
  }
  const String server(EINK_FRAME_SERVER_URL);
  if (!server.startsWith("http://")) {
    Serial.println("Configuration error: server URL must use http://");
    return false;
  }
  if (!eink::isConcreteMode(requestedMode)) {
    Serial.println("Configuration error: EINK_DEFAULT_MODE is invalid");
    return false;
  }
  return true;
}

String etagForSha(const String &sha) {
  return String("\"sha256-") + sha + "\"";
}

bool isLowercaseSha256(const String &value) {
  if (value.length() != 64) {
    return false;
  }
  for (size_t index = 0; index < value.length(); ++index) {
    const char character = value[index];
    if (!((character >= '0' && character <= '9') ||
          (character >= 'a' && character <= 'f'))) {
      return false;
    }
  }
  return true;
}

String sha256Hex(const uint8_t *payload, size_t length) {
  uint8_t digest[32];
#if MBEDTLS_VERSION_MAJOR >= 3
  const int result = mbedtls_sha256(payload, length, digest, 0);
#else
  const int result = mbedtls_sha256_ret(payload, length, digest, 0);
#endif
  if (result != 0) {
    return String();
  }

  static constexpr char kHex[] = "0123456789abcdef";
  char encoded[65];
  for (size_t index = 0; index < sizeof(digest); ++index) {
    encoded[index * 2] = kHex[digest[index] >> 4];
    encoded[index * 2 + 1] = kHex[digest[index] & 0x0F];
  }
  encoded[64] = '\0';
  return String(encoded);
}

void loadDisplayState() {
  if (!preferencesReady) {
    return;
  }
  displayedMode = preferences.getString(kPreferenceMode, "");
  displayedSha = preferences.getString(kPreferenceSha, "");
  displayedEtag = preferences.getString(kPreferenceEtag, "");
  if (!eink::isConcreteMode(displayedMode) ||
      !isLowercaseSha256(displayedSha) ||
      displayedEtag != etagForSha(displayedSha)) {
    displayedMode = "";
    displayedSha = "";
    displayedEtag = "";
    Serial.println("No valid persisted display checksum; a full pull is required");
  }
}

void persistDisplayState(const String &mode, const String &sha,
                         const String &etag) {
  displayedMode = mode;
  displayedSha = sha;
  displayedEtag = etag;
  if (!preferencesReady) {
    return;
  }

  // The values cross-check on load. An interrupted series is rejected and
  // causes a safe full download rather than a false 304 decision.
  const bool saved = preferences.putString(kPreferenceSha, sha) > 0 &&
                     preferences.putString(kPreferenceEtag, etag) > 0 &&
                     preferences.putString(kPreferenceMode, mode) > 0;
  if (!saved) {
    Serial.println("Warning: display checksum could not be persisted");
  }
}

bool connectWifi() {
  if (WiFi.status() == WL_CONNECTED) {
    return true;
  }

  Serial.printf("Connecting to Wi-Fi SSID %s", EINK_WIFI_SSID);
  WiFi.mode(WIFI_STA);
  WiFi.setAutoReconnect(true);
  WiFi.persistent(false);
  WiFi.begin(EINK_WIFI_SSID, EINK_WIFI_PASSWORD);

  const uint32_t started = millis();
  while (WiFi.status() != WL_CONNECTED &&
         !elapsed(started, EINK_WIFI_CONNECT_TIMEOUT_MS)) {
    Serial.print('.');
    delay(250);
  }
  Serial.println();
  if (WiFi.status() != WL_CONNECTED) {
    Serial.println("Wi-Fi connection timed out; panel left unchanged");
    return false;
  }
  Serial.print("Wi-Fi connected at ");
  Serial.println(WiFi.localIP());
  return true;
}

String frameUrl(const String &mode) {
  String base(EINK_FRAME_SERVER_URL);
  while (base.endsWith("/")) {
    base.remove(base.length() - 1);
  }
  return base + "/v1/frame/" + mode;
}

bool readExactFrame(HTTPClient &http, uint8_t *destination) {
  auto *stream = http.getStreamPtr();
  if (stream == nullptr) {
    return false;
  }

  size_t offset = 0;
  uint32_t lastProgress = millis();
  while (offset < eink::kFrameBytes) {
    const int available = stream->available();
    if (available > 0) {
      const size_t wanted =
          min(static_cast<size_t>(available), eink::kFrameBytes - offset);
      const size_t received = stream->readBytes(destination + offset, wanted);
      if (received == 0) {
        return false;
      }
      offset += received;
      lastProgress = millis();
      delay(0);
      continue;
    }
    if (!http.connected()) {
      break;
    }
    if (elapsed(lastProgress, EINK_HTTP_READ_TIMEOUT_MS)) {
      Serial.println("Frame download stalled");
      return false;
    }
    delay(1);
  }
  return offset == eink::kFrameBytes;
}

bool refreshPanel(const uint8_t *verifiedFrame) {
  // The Pi has already rotated landscape pixels into the native 1200x1600
  // Setup510 backing order. Do not call setRotation(), pushImage(), or convert
  // nibbles here.
  epaper.begin();
  auto *displayBuffer = static_cast<uint8_t *>(epaper.getPointer());
  if (displayBuffer == nullptr) {
    Serial.println("Seeed_GFX framebuffer allocation failed");
    return false;
  }
  memcpy(displayBuffer, verifiedFrame, eink::kFrameBytes);
  epaper.update();
  return true;
}

SyncResult syncFrame(const String &mode, bool force) {
  if (!eink::isConcreteMode(mode)) {
    Serial.println("Refusing an unsupported frame mode");
    return SyncResult::kFailed;
  }
  if (!connectWifi()) {
    return SyncResult::kFailed;
  }

  auto *staging = static_cast<uint8_t *>(heap_caps_malloc(
      eink::kFrameBytes, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT));
  if (staging == nullptr) {
    Serial.println("Could not allocate the 960,000-byte PSRAM staging buffer");
    return SyncResult::kFailed;
  }

  WiFiClient transport;
  HTTPClient http;
  http.setConnectTimeout(EINK_HTTP_CONNECT_TIMEOUT_MS);
  http.setTimeout(EINK_HTTP_READ_TIMEOUT_MS);
  http.useHTTP10(true);

  const char *headerNames[] = {
      "ETag",          "Content-Type",       "X-Frame-Format",
      "X-Frame-SHA256", "X-Content-SHA256",
  };
  http.collectHeaders(headerNames,
                      sizeof(headerNames) / sizeof(headerNames[0]));

  const String url = frameUrl(mode);
  if (!http.begin(transport, url)) {
    Serial.println("Could not initialize the HTTP request");
    heap_caps_free(staging);
    return SyncResult::kFailed;
  }
  http.addHeader("Authorization",
                 String("Bearer ") + EINK_FRAME_AUTH_TOKEN);
  if (!force && displayedMode == mode && displayedEtag.length() > 0) {
    http.addHeader("If-None-Match", displayedEtag);
  }

  Serial.printf("Checking %s frame\n", mode.c_str());
  const int status = http.GET();
  if (status == HTTP_CODE_NOT_MODIFIED) {
    http.end();
    heap_caps_free(staging);
    Serial.println("Frame is unchanged; skipping the slow e-paper refresh");
    return SyncResult::kNotModified;
  }
  if (status != HTTP_CODE_OK) {
    Serial.printf("Frame server returned HTTP %d; panel left unchanged\n",
                  status);
    http.end();
    heap_caps_free(staging);
    return SyncResult::kFailed;
  }

  String contentType = http.header("Content-Type");
  String wireFormat = http.header("X-Frame-Format");
  String expectedSha = http.header("X-Frame-SHA256");
  String contentSha = http.header("X-Content-SHA256");
  String etag = http.header("ETag");
  contentType.trim();
  wireFormat.trim();
  expectedSha.trim();
  contentSha.trim();
  etag.trim();

  const bool validHeaders =
      http.getSize() == static_cast<int>(eink::kFrameBytes) &&
      contentType == eink::kContentType && wireFormat == eink::kWireFormat &&
      isLowercaseSha256(expectedSha) && contentSha == expectedSha &&
      etag == etagForSha(expectedSha);
  if (!validHeaders) {
    Serial.println("Frame response contract is invalid; panel left unchanged");
    http.end();
    heap_caps_free(staging);
    return SyncResult::kFailed;
  }

  const bool complete = readExactFrame(http, staging);
  http.end();
  if (!complete) {
    Serial.println("Frame response was truncated; panel left unchanged");
    heap_caps_free(staging);
    return SyncResult::kFailed;
  }

  const String actualSha = sha256Hex(staging, eink::kFrameBytes);
  if (actualSha != expectedSha) {
    Serial.println("Frame SHA-256 mismatch; panel left unchanged");
    heap_caps_free(staging);
    return SyncResult::kFailed;
  }
  if (!eink::hasOnlySupportedColors(staging, eink::kFrameBytes)) {
    Serial.println("Frame contains unsupported EE02 color nibbles");
    heap_caps_free(staging);
    return SyncResult::kFailed;
  }

  Serial.println("Frame verified; starting the e-paper refresh");
  const bool refreshed = refreshPanel(staging);
  heap_caps_free(staging);
  if (!refreshed) {
    Serial.println("Panel refresh could not start; previous image remains");
    return SyncResult::kFailed;
  }

  persistDisplayState(mode, actualSha, etag);
  Serial.printf("Displayed %s frame %s\n", mode.c_str(),
                actualSha.substring(0, 12).c_str());
  return SyncResult::kUpdated;
}

void requestMode(const char *mode) {
  requestedMode = mode;
  forceNextDownload = false;
  pollImmediately = true;
  Serial.printf("Selected mode: %s\n", mode);
}

void handleSerialCommand() {
  while (Serial.available() > 0) {
    const char command = static_cast<char>(Serial.read());
    switch (command) {
      case 'w':
        requestMode("weather");
        break;
      case 'b':
        requestMode("birds");
        break;
      case 's':
        requestMode("star-map");
        break;
      case 'p':
        requestMode("uploaded-photo");
        break;
      case 't':
        requestMode("test-pattern");
        break;
      case 'r':
        forceNextDownload = true;
        pollImmediately = true;
        Serial.println("A full frame download was requested");
        break;
      default:
        break;
    }
  }
}

}  // namespace

void setup() {
  Serial.begin(115200);
  delay(1000);
  Serial.println("EE02 network frame client starting");
  Serial.println("Commands: w=weather b=birds s=star-map p=photo t=test r=refresh");

  if (!psramFound()) {
    Serial.println("Fatal: EE02 firmware requires the XIAO ESP32S3 PSRAM target");
    return;
  }
  Serial.printf("Free PSRAM: %u bytes\n",
                static_cast<unsigned>(ESP.getFreePsram()));

  preferencesReady = preferences.begin(kPreferencesNamespace, false);
  if (!preferencesReady) {
    Serial.println("Warning: NVS display checksum storage is unavailable");
  }
  loadDisplayState();

  if (!configurationIsUsable()) {
    pollImmediately = false;
    return;
  }
  connectWifi();
}

void loop() {
  handleSerialCommand();
  if ((pollImmediately || elapsed(lastPollStarted, EINK_POLL_INTERVAL_MS)) &&
      configurationIsUsable() && psramFound()) {
    pollImmediately = false;
    lastPollStarted = millis();
    syncFrame(requestedMode, forceNextDownload);
    forceNextDownload = false;
  }
  delay(10);
}

