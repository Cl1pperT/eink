#include <Arduino.h>
#include <HTTPClient.h>
#include <Preferences.h>
#include <TFT_eSPI.h>
#include <WiFi.h>
#include <esp_heap_caps.h>
#include <esp_sleep.h>
#include <mbedtls/sha256.h>
#include <mbedtls/version.h>

#include <cstring>

#include "device_config.h"
#include "frame_contract.h"

#if __has_include("secrets.h")
#include "secrets.h"
#else
#include "secrets.example.h"
#endif

#ifndef EINK_FRAME_AUTH_TOKEN
#define EINK_FRAME_AUTH_TOKEN ""
#endif

#ifndef BOARD_HAS_PSRAM
#error "The EE02 target must provide external PSRAM"
#endif

static_assert(EPD_WIDTH == eink::kBackingWidth,
              "Seeed_GFX is not configured for Setup510 width");
static_assert(EPD_HEIGHT == eink::kBackingHeight,
              "Seeed_GFX is not configured for Setup510 height");
static_assert(EPD_COLOR_DEPTH == 4,
              "Seeed_GFX is not configured for the EE02 4bpp sprite");
static_assert(TFT_WHITE == 0x0 && TFT_GREEN == 0x2 && TFT_RED == 0x6 &&
                  TFT_YELLOW == 0xB && TFT_BLUE == 0xD && TFT_BLACK == 0xF,
              "Seeed_GFX Setup510 color codes do not match the wire contract");
static_assert(EINK_HTTP_READ_TIMEOUT_MS > 0 &&
                  EINK_HTTP_READ_TIMEOUT_MS <= 65535UL,
              "HTTPClient read timeout must fit its 16-bit millisecond API");

namespace {

constexpr char kPreferencesNamespace[] = "eink-frame";
constexpr char kPreferenceMode[] = "mode";
constexpr char kPreferenceSha[] = "sha256";
constexpr char kPreferenceEtag[] = "etag";
constexpr char kPreferenceValid[] = "state-valid";
constexpr gpio_num_t kButton1 = GPIO_NUM_2;
constexpr gpio_num_t kButton2 = GPIO_NUM_3;
constexpr gpio_num_t kButton3 = GPIO_NUM_5;
constexpr uint64_t kButtonWakeMask =
    (1ULL << kButton1) | (1ULL << kButton2) | (1ULL << kButton3);

EPaper epaper;
Preferences preferences;
bool preferencesReady = false;

String displayedMode;
String displayedSha;
String displayedEtag;
bool panelInitialized = false;

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

bool containsHttpUnsafeCharacters(const char *value) {
  const String text(value == nullptr ? "" : value);
  return text.indexOf('\r') >= 0 || text.indexOf('\n') >= 0;
}

bool configurationIsUsable() {
  const String wifiPassword(EINK_WIFI_PASSWORD);
  if (isPlaceholder(EINK_WIFI_SSID) ||
      wifiPassword.startsWith("replace-with-")) {
    Serial.println("Configuration error: provision include/secrets.h");
    return false;
  }
  if (containsHttpUnsafeCharacters(EINK_FRAME_AUTH_TOKEN) ||
      containsHttpUnsafeCharacters(EINK_FRAME_SERVER_URL)) {
    Serial.println("Configuration error: token and server URL must be one line");
    return false;
  }
  String server(EINK_FRAME_SERVER_URL);
  while (server.endsWith("/")) {
    server.remove(server.length() - 1);
  }
  const String authority =
      server.startsWith("http://") ? server.substring(7) : String();
  if (authority.length() == 0 || authority.indexOf('/') >= 0 ||
      authority.indexOf('@') >= 0 || authority.indexOf('?') >= 0 ||
      authority.indexOf('#') >= 0 ||
      server.indexOf("replace-with-") >= 0) {
    Serial.println("Configuration error: provision the Pi http:// server URL");
    return false;
  }
  if (!eink::isConcreteMode(EINK_FRAME_MODE)) {
    Serial.println("Configuration error: EINK_FRAME_MODE is invalid");
    return false;
  }
  return true;
}

bool displayBufferIsReady() {
  return epaper.created() && epaper.getPointer() != nullptr &&
         epaper.getColorDepth() == 4 &&
         epaper.width() == static_cast<int16_t>(eink::kBackingWidth) &&
         epaper.height() == static_cast<int16_t>(eink::kBackingHeight);
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
  const bool persistedStateIsValid =
      preferences.getBool(kPreferenceValid, false);
  displayedMode = preferences.getString(kPreferenceMode, "");
  displayedSha = preferences.getString(kPreferenceSha, "");
  displayedEtag = preferences.getString(kPreferenceEtag, "");
  if (!persistedStateIsValid || !eink::isConcreteMode(displayedMode) ||
      !isLowercaseSha256(displayedSha) ||
      displayedEtag != etagForSha(displayedSha)) {
    displayedMode = "";
    displayedSha = "";
    displayedEtag = "";
    Serial.println("No valid persisted display checksum; a full pull is required");
  }
}

bool invalidatePersistedDisplayState() {
  if (!preferencesReady) {
    Serial.println(
        "Cannot establish a power-safe display transaction without NVS");
    return false;
  }
  const bool written = preferences.putBool(kPreferenceValid, false) > 0;
  const bool verified = !preferences.getBool(kPreferenceValid, true);
  if (!written || !verified) {
    Serial.println(
        "Could not invalidate the old display checksum; panel left unchanged");
    return false;
  }
  return true;
}

void persistDisplayState(const String &mode, const String &sha,
                         const String &etag) {
  displayedMode = mode;
  displayedSha = sha;
  displayedEtag = etag;
  if (!preferencesReady) {
    return;
  }

  // state-valid was committed false before the panel refresh. Save and verify
  // every field, then make the record visible with one final marker write.
  // Power loss anywhere before that final write forces an unconditional pull.
  const bool shaSaved = preferences.putString(kPreferenceSha, sha) > 0;
  const bool etagSaved = preferences.putString(kPreferenceEtag, etag) > 0;
  const bool modeSaved = preferences.putString(kPreferenceMode, mode) > 0;
  const bool fieldsMatch = shaSaved && etagSaved && modeSaved &&
                           preferences.getString(kPreferenceSha, "") == sha &&
                           preferences.getString(kPreferenceEtag, "") == etag &&
                           preferences.getString(kPreferenceMode, "") == mode;
  const bool committed =
      fieldsMatch && preferences.putBool(kPreferenceValid, true) > 0 &&
      preferences.getBool(kPreferenceValid, false);
  if (!committed) {
    preferences.putBool(kPreferenceValid, false);
    Serial.println(
        "Warning: display checksum remains invalid; next boot will pull fully");
  }
}

void persistModeForUnchangedFrame(const String &mode) {
  displayedMode = mode;
  if (!preferencesReady) {
    return;
  }
  // Only the descriptive mode changes here; the physical SHA and ETag remain
  // identical, so either the old or new concrete mode is power-loss safe.
  if (preferences.putString(kPreferenceMode, mode) == 0 ||
      preferences.getString(kPreferenceMode, "") != mode) {
    Serial.println("Warning: unchanged frame mode could not be persisted");
  }
}

bool connectWifi() {
  if (WiFi.status() == WL_CONNECTED) {
    return true;
  }

  Serial.print("Connecting to Wi-Fi");
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
  if (!panelInitialized) {
    epaper.begin();
    panelInitialized = true;
  }
  auto *displayBuffer = static_cast<uint8_t *>(epaper.getPointer());
  if (displayBuffer == nullptr) {
    Serial.println("Seeed_GFX framebuffer allocation failed");
    return false;
  }
  memcpy(displayBuffer, verifiedFrame, eink::kFrameBytes);
  epaper.update();
  return true;
}

SyncResult syncFrame(const String &mode, bool bypassValidator = false) {
  if (!eink::isConcreteMode(mode)) {
    Serial.println("Refusing an unsupported frame mode");
    return SyncResult::kFailed;
  }
  if (!connectWifi()) {
    return SyncResult::kFailed;
  }

  WiFiClient transport;
  HTTPClient http;
  http.setConnectTimeout(EINK_HTTP_CONNECT_TIMEOUT_MS);
  http.setTimeout(EINK_HTTP_READ_TIMEOUT_MS);
  http.useHTTP10(true);
  http.setFollowRedirects(HTTPC_DISABLE_FOLLOW_REDIRECTS);

  const char *headerNames[] = {
      "ETag",          "Content-Type",       "X-Frame-Format",
      "X-Frame-SHA256", "X-Content-SHA256",
  };
  http.collectHeaders(headerNames,
                      sizeof(headerNames) / sizeof(headerNames[0]));

  const String url = frameUrl(mode);
  if (!http.begin(transport, url)) {
    Serial.println("Could not initialize the HTTP request");
    return SyncResult::kFailed;
  }
  if (EINK_FRAME_AUTH_TOKEN[0] != '\0') {
    http.addHeader("Authorization",
                   String("Bearer ") + EINK_FRAME_AUTH_TOKEN);
  }
  http.addHeader("Accept", eink::kContentType);
  http.addHeader("Accept-Encoding", "identity");
  if (!bypassValidator && isLowercaseSha256(displayedSha) &&
      displayedEtag == etagForSha(displayedSha)) {
    http.addHeader("If-None-Match", displayedEtag);
  }

  Serial.printf("Checking %s frame\n", mode.c_str());
  const int status = http.GET();
  if (status == HTTP_CODE_NOT_MODIFIED) {
    String responseEtag = http.header("ETag");
    String responseSha = http.header("X-Frame-SHA256");
    String responseContentSha = http.header("X-Content-SHA256");
    String responseFormat = http.header("X-Frame-Format");
    responseEtag.trim();
    responseSha.trim();
    responseContentSha.trim();
    responseFormat.trim();
    const bool validNotModified = !bypassValidator &&
                                  isLowercaseSha256(displayedSha) &&
                                  responseEtag == displayedEtag &&
                                  responseSha == displayedSha &&
                                  responseContentSha == displayedSha &&
                                  responseFormat == eink::kWireFormat;
    http.end();
    if (!validNotModified) {
      Serial.println("Invalid 304 response; retrying once without a validator");
      if (!bypassValidator) {
        return syncFrame(mode, true);
      }
      return SyncResult::kFailed;
    }
    if (displayedMode != mode) {
      persistModeForUnchangedFrame(mode);
    }
    Serial.println("Frame is unchanged; skipping the slow e-paper refresh");
    return SyncResult::kNotModified;
  }
  if (status != HTTP_CODE_OK) {
    Serial.printf("Frame server returned HTTP %d; panel left unchanged\n",
                  status);
    http.end();
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
    return SyncResult::kFailed;
  }

  auto *staging = static_cast<uint8_t *>(heap_caps_malloc(
      eink::kFrameBytes, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT));
  if (staging == nullptr) {
    Serial.println("Could not allocate the 960,000-byte PSRAM staging buffer");
    http.end();
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

  // A compliant server returns 304 for this case. This second guard also
  // prevents a redundant physical refresh behind a proxy that returned 200.
  if (actualSha == displayedSha && etag == displayedEtag) {
    heap_caps_free(staging);
    if (displayedMode != mode) {
      persistModeForUnchangedFrame(mode);
    }
    Serial.println("Downloaded frame matches the display; refresh skipped");
    return SyncResult::kNotModified;
  }

  // Invalidate the old persisted checksum before touching physical pixels.
  // This closes the power-loss window between epaper.update() and NVS writes.
  if (!invalidatePersistedDisplayState()) {
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

bool anyButtonIsPressed() {
  return digitalRead(kButton1) == LOW || digitalRead(kButton2) == LOW ||
         digitalRead(kButton3) == LOW;
}

void sleepUntilButton() {
  WiFi.disconnect(true);
  WiFi.mode(WIFI_OFF);
  if (preferencesReady) {
    preferences.end();
    preferencesReady = false;
  }

  // A held active-low button would wake the ESP32 immediately. Wait for the
  // press that started this run to be released before arming the next wake.
  while (anyButtonIsPressed()) {
    delay(10);
  }

  esp_sleep_enable_ext1_wakeup(kButtonWakeMask, ESP_EXT1_WAKEUP_ANY_LOW);
  Serial.println("Sleeping; press any user button to check for a new image");
  Serial.flush();
  delay(20);
  esp_deep_sleep_start();
}

}  // namespace

void setup() {
  Serial.begin(115200);
  pinMode(kButton1, INPUT_PULLUP);
  pinMode(kButton2, INPUT_PULLUP);
  pinMode(kButton3, INPUT_PULLUP);
  delay(750);
  Serial.println("EE02 button image updater starting");
  if (esp_sleep_get_wakeup_cause() == ESP_SLEEP_WAKEUP_EXT1) {
    Serial.println("Wake reason: user button");
  } else {
    Serial.println("Wake reason: power-on, reset, or upload");
  }

  if (!psramFound()) {
    Serial.println("Fatal: EE02 firmware requires the XIAO ESP32S3 PSRAM target");
    sleepUntilButton();
    return;
  }
  Serial.printf("Free PSRAM: %u bytes\n",
                static_cast<unsigned>(ESP.getFreePsram()));
  if (!displayBufferIsReady()) {
    Serial.println("Fatal: Seeed_GFX did not allocate a 1200x1600 4bpp sprite");
    sleepUntilButton();
    return;
  }

  preferencesReady = preferences.begin(kPreferencesNamespace, false);
  if (!preferencesReady) {
    Serial.println(
        "Fatal: NVS is required for power-safe display checksum storage");
    sleepUntilButton();
    return;
  }
  loadDisplayState();

  if (!configurationIsUsable()) {
    sleepUntilButton();
    return;
  }
  syncFrame(EINK_FRAME_MODE);
  sleepUntilButton();
}

void loop() {
  delay(1000);
}
