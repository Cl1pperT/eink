#include <Arduino.h>
#include <ESPmDNS.h>
#include <HTTPClient.h>
#include <Preferences.h>
#include <TFT_eSPI.h>
#include <WiFi.h>
#include <esp_heap_caps.h>
#include <esp_sleep.h>
#include <esp_sntp.h>
#include <mbedtls/sha256.h>
#include <mbedtls/version.h>

#include <cstring>
#include <ctime>

#include "battery_monitor.h"
#include "device_config.h"
#include "frame_contract.h"
#include "wake_schedule.h"

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
static_assert(EINK_CHECK_INTERVAL_SECONDS > 0,
              "EINK_CHECK_INTERVAL_SECONDS must be positive");
static_assert(EINK_BATTERY_SAMPLE_COUNT >= 3 &&
                  EINK_BATTERY_SAMPLE_COUNT % 2 == 1,
              "EINK_BATTERY_SAMPLE_COUNT must be odd and at least three");
static_assert(EINK_BATTERY_DIVIDER_MULTIPLIER > 0,
              "EINK_BATTERY_DIVIDER_MULTIPLIER must be positive");
static_assert(EINK_NTP_RETRY_WAKES > 0,
              "EINK_NTP_RETRY_WAKES must be positive");
static_assert(EINK_MAX_SCHEDULE_SLEEP_SECONDS >=
                  EINK_CHECK_INTERVAL_SECONDS,
              "schedule sleep bound must permit the fallback interval");
static_assert(EINK_MAX_MANIFEST_BYTES > 0,
              "manifest size bound must be positive");

namespace {

constexpr char kPreferencesNamespace[] = "eink-frame";
constexpr char kPreferenceMode[] = "mode";
constexpr char kPreferenceSha[] = "sha256";
constexpr char kPreferenceEtag[] = "etag";
constexpr char kPreferenceValid[] = "state-valid";
constexpr char kPreferenceBatteryValid[] = "bat-valid";
constexpr char kPreferenceBatteryMillivolts[] = "bat-mv";
constexpr char kPreferenceBatteryPercent[] = "bat-pct";
constexpr char kPreferenceClockDay[] = "clock-day";
constexpr char kPreferenceClockAttemptDay[] = "clock-try";
constexpr char kPreferenceMarkValid[] = "mark-valid";
constexpr char kPreferenceMarkPercent[] = "mark-pct";
constexpr char kPreferenceMarkVersion[] = "mark-ver";
constexpr uint8_t kBatteryMarkVersion = 1;
constexpr gpio_num_t kButton1 = GPIO_NUM_2;
constexpr gpio_num_t kButton2 = GPIO_NUM_3;
constexpr gpio_num_t kButton3 = GPIO_NUM_5;
constexpr char kButton1FrameMode[] = "weather";
constexpr char kButton2FrameMode[] = "birds";
constexpr char kButton3FrameMode[] = "star-map";
constexpr uint8_t kBatteryAdcPin = EINK_BATTERY_ADC_PIN;
constexpr uint8_t kBatteryEnablePin = EINK_BATTERY_ENABLE_PIN;
constexpr uint64_t kButtonWakeMask =
    (1ULL << kButton1) | (1ULL << kButton2) | (1ULL << kButton3);
constexpr time_t kMinimumValidClock = 1704067200;  // 2024-01-01 UTC
constexpr int16_t kBatteryMarkRightMargin = 18;
constexpr int16_t kBatteryMarkBottomMargin = 14;
constexpr int16_t kBatteryMarkSampleWidth = 92;
constexpr int16_t kBatteryMarkSampleHeight = 42;

EPaper epaper;
Preferences preferences;
bool preferencesReady = false;

String displayedMode;
String displayedSha;
String displayedEtag;
String resolvedServerBase;
bool panelInitialized = false;

struct BatteryState {
  bool valid = false;
  uint16_t millivolts = 0;
  uint8_t percent = 0;
};

BatteryState battery;
bool displayedBatteryMarkValid = false;
uint8_t displayedBatteryPercent = 0;
uint32_t clockSyncedLocalDay = 0;
uint32_t clockAttemptedLocalDay = 0;
bool wifiAttemptedThisWake = false;
RTC_DATA_ATTR uint16_t ntpRetryWakesRemaining = 0;
eink::WakeDeadline scheduledWake;
volatile uint8_t pendingButtonNumber = 0;

enum class SyncResult {
  kUpdated,
  kNotModified,
  kFailed,
};

bool elapsed(uint32_t since, uint32_t duration) {
  return static_cast<uint32_t>(millis() - since) >= duration;
}

bool readLocalClock(struct tm &localClock) {
  const time_t now = time(nullptr);
  if (now < kMinimumValidClock) {
    return false;
  }
  return localtime_r(&now, &localClock) != nullptr;
}

uint32_t localDayKey(const struct tm &localClock) {
  return static_cast<uint32_t>(localClock.tm_year + 1900) * 1000UL +
         static_cast<uint32_t>(localClock.tm_yday + 1);
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
  if (!eink::isFrameChannel(EINK_FRAME_MODE)) {
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
  if (!persistedStateIsValid || !eink::isFrameChannel(displayedMode) ||
      !isLowercaseSha256(displayedSha) ||
      displayedEtag != etagForSha(displayedSha)) {
    displayedMode = "";
    displayedSha = "";
    displayedEtag = "";
    displayedBatteryMarkValid = false;
    displayedBatteryPercent = 0;
    Serial.println("No valid persisted display checksum; a full pull is required");
    return;
  }

  const uint8_t persistedMarkPercent =
      preferences.getUChar(kPreferenceMarkPercent, 0);
  displayedBatteryMarkValid =
      preferences.getBool(kPreferenceMarkValid, false) &&
      preferences.getUChar(kPreferenceMarkVersion, 0) ==
          kBatteryMarkVersion &&
      persistedMarkPercent <= 100;
  displayedBatteryPercent =
      displayedBatteryMarkValid ? persistedMarkPercent : 0;
}

void loadBatteryState() {
  if (!preferencesReady) {
    return;
  }
  clockSyncedLocalDay =
      preferences.getUInt(kPreferenceClockDay, 0);
  clockAttemptedLocalDay =
      preferences.getUInt(kPreferenceClockAttemptDay, 0);
  const uint16_t persistedMillivolts =
      preferences.getUShort(kPreferenceBatteryMillivolts, 0);
  const uint8_t persistedPercent =
      preferences.getUChar(kPreferenceBatteryPercent, 0);
  const bool persistedBatteryIsValid =
      preferences.getBool(kPreferenceBatteryValid, false) &&
      eink::isPlausibleBatteryMillivolts(persistedMillivolts) &&
      persistedPercent <= 100;
  if (!persistedBatteryIsValid) {
    Serial.println("No valid cached battery estimate");
    return;
  }

  battery.valid = true;
  battery.millivolts = persistedMillivolts;
  battery.percent = persistedPercent;
  Serial.printf("Cached battery estimate: %u/100 (%u mV)\n",
                static_cast<unsigned>(battery.percent),
                static_cast<unsigned>(battery.millivolts));
}

bool persistBatteryState() {
  if (!preferencesReady || !battery.valid) {
    return false;
  }

  // Battery sampling and physical display commits are intentionally separate,
  // so a failed frame pull retains the fresh estimate for the next wake.
  const bool invalidated =
      preferences.putBool(kPreferenceBatteryValid, false) > 0 &&
      !preferences.getBool(kPreferenceBatteryValid, true);
  const bool voltageSaved =
      preferences.putUShort(kPreferenceBatteryMillivolts,
                            battery.millivolts) > 0;
  const bool percentSaved =
      preferences.putUChar(kPreferenceBatteryPercent, battery.percent) > 0;
  const bool fieldsMatch =
      voltageSaved && percentSaved &&
      preferences.getUShort(kPreferenceBatteryMillivolts, 0) ==
          battery.millivolts &&
      preferences.getUChar(kPreferenceBatteryPercent, 255) ==
          battery.percent;
  const bool committed =
      invalidated && fieldsMatch &&
      preferences.putBool(kPreferenceBatteryValid, true) > 0 &&
      preferences.getBool(kPreferenceBatteryValid, false);
  if (!committed) {
    preferences.putBool(kPreferenceBatteryValid, false);
    Serial.println(
        "Warning: battery estimate was not committed; it will be sampled again");
  }
  return committed;
}

bool readBatteryMillivolts(uint16_t &batteryMillivolts) {
  uint32_t samples[EINK_BATTERY_SAMPLE_COUNT];

  analogReadResolution(12);
  analogSetPinAttenuation(kBatteryAdcPin, ADC_11db);
  digitalWrite(kBatteryEnablePin, HIGH);
  delay(EINK_BATTERY_SETTLE_MS);
  // Discard the first conversion after switching on the divider.
  analogReadMilliVolts(kBatteryAdcPin);

  for (size_t index = 0; index < EINK_BATTERY_SAMPLE_COUNT; ++index) {
    samples[index] = analogReadMilliVolts(kBatteryAdcPin);
    delay(2);
  }
  digitalWrite(kBatteryEnablePin, LOW);

  // Insertion sort is small and deterministic for the default 25 readings.
  for (size_t index = 1; index < EINK_BATTERY_SAMPLE_COUNT; ++index) {
    const uint32_t value = samples[index];
    size_t position = index;
    while (position > 0 && samples[position - 1] > value) {
      samples[position] = samples[position - 1];
      --position;
    }
    samples[position] = value;
  }

  const uint32_t adcMillivolts =
      samples[EINK_BATTERY_SAMPLE_COUNT / 2];
  const uint32_t measuredBatteryMillivolts =
      adcMillivolts * EINK_BATTERY_DIVIDER_MULTIPLIER;
  if (!eink::isPlausibleBatteryMillivolts(measuredBatteryMillivolts)) {
    Serial.printf(
        "Battery ADC reading is not plausible (%lu mV); cached estimate kept\n",
        static_cast<unsigned long>(measuredBatteryMillivolts));
    return false;
  }

  batteryMillivolts =
      static_cast<uint16_t>(measuredBatteryMillivolts);
  return true;
}

void sampleBatteryEveryWake() {
  const bool hadEstimate = battery.valid;
  const uint8_t previousPercent = battery.percent;
  uint16_t measuredMillivolts = 0;
  if (!readBatteryMillivolts(measuredMillivolts)) {
    return;
  }

  battery.valid = true;
  battery.millivolts = measuredMillivolts;
  battery.percent = eink::stabilizeBatteryPercent(
      measuredMillivolts, hadEstimate, previousPercent);
  persistBatteryState();
  Serial.printf("Battery estimate sampled: %u/100 (%u mV)\n",
                static_cast<unsigned>(battery.percent),
                static_cast<unsigned>(battery.millivolts));
}

bool batteryMarkNeedsRefresh() {
  return battery.valid != displayedBatteryMarkValid ||
         (battery.valid &&
          displayedBatteryPercent != battery.percent);
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
  const bool markValidSaved =
      preferences.putBool(kPreferenceMarkValid, battery.valid) > 0;
  const bool markPercentSaved =
      preferences.putUChar(kPreferenceMarkPercent,
                           battery.valid ? battery.percent : 0) > 0;
  const bool markVersionSaved =
      preferences.putUChar(kPreferenceMarkVersion, kBatteryMarkVersion) > 0;
  const bool fieldsMatch = shaSaved && etagSaved && modeSaved &&
                           markValidSaved && markPercentSaved &&
                           markVersionSaved &&
                           preferences.getString(kPreferenceSha, "") == sha &&
                           preferences.getString(kPreferenceEtag, "") == etag &&
                           preferences.getString(kPreferenceMode, "") == mode &&
                           preferences.getBool(kPreferenceMarkValid, false) ==
                               battery.valid &&
                           preferences.getUChar(kPreferenceMarkPercent, 255) ==
                               (battery.valid ? battery.percent : 0) &&
                           preferences.getUChar(kPreferenceMarkVersion, 0) ==
                               kBatteryMarkVersion;
  const bool committed =
      fieldsMatch && preferences.putBool(kPreferenceValid, true) > 0 &&
      preferences.getBool(kPreferenceValid, false);
  if (!committed) {
    preferences.putBool(kPreferenceValid, false);
    Serial.println(
        "Warning: display checksum remains invalid; next boot will pull fully");
    return;
  }

  displayedBatteryMarkValid = battery.valid;
  displayedBatteryPercent = battery.valid ? battery.percent : 0;
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
  if (wifiAttemptedThisWake) {
    Serial.println("Wi-Fi already failed during this wake");
    return false;
  }
  wifiAttemptedThisWake = true;

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

bool synchronizeClockIfDue(struct tm &localClock) {
  const bool hadValidClock = readLocalClock(localClock);
  const uint32_t currentLocalDay =
      hadValidClock ? localDayKey(localClock) : 0;
  if (hadValidClock && clockSyncedLocalDay == currentLocalDay) {
    return true;
  }
  if (hadValidClock &&
      clockAttemptedLocalDay == currentLocalDay) {
    Serial.println(
        "Daily NTP attempt already completed; using the retained clock");
    return true;
  }
  if (!hadValidClock && ntpRetryWakesRemaining > 0) {
    --ntpRetryWakesRemaining;
    Serial.printf(
        "NTP retry backed off for %u more wake cycles\n",
        static_cast<unsigned>(ntpRetryWakesRemaining));
    return false;
  }

  if (hadValidClock) {
    clockAttemptedLocalDay = currentLocalDay;
    const bool attemptSaved =
        preferencesReady &&
        preferences.putUInt(kPreferenceClockAttemptDay,
                            clockAttemptedLocalDay) > 0 &&
        preferences.getUInt(kPreferenceClockAttemptDay, 0) ==
            clockAttemptedLocalDay;
    if (!attemptSaved) {
      Serial.println("Warning: NTP attempt date was not persisted");
    }
  }
  if (!connectWifi()) {
    if (!hadValidClock) {
      ntpRetryWakesRemaining = EINK_NTP_RETRY_WAKES;
    }
    Serial.println(
        "Clock sync unavailable; using the retained clock if present");
    return hadValidClock;
  }

  Serial.println("Synchronizing the local clock with NTP");
  configTzTime(EINK_TIMEZONE, EINK_NTP_SERVER_PRIMARY,
               EINK_NTP_SERVER_SECONDARY);
  const uint32_t started = millis();
  bool syncCompleted = false;
  while (!elapsed(started, EINK_NTP_SYNC_TIMEOUT_MS)) {
    if (sntp_get_sync_status() == SNTP_SYNC_STATUS_COMPLETED) {
      syncCompleted = true;
      break;
    }
    delay(100);
  }

  struct tm synchronizedClock {};
  const bool synchronized =
      syncCompleted && readLocalClock(synchronizedClock);
  if (!synchronized) {
    if (!hadValidClock) {
      ntpRetryWakesRemaining = EINK_NTP_RETRY_WAKES;
    }
    Serial.println(
        "NTP timed out; retained clock will keep the approximate schedule");
    return readLocalClock(localClock) || hadValidClock;
  }

  ntpRetryWakesRemaining = 0;
  localClock = synchronizedClock;
  clockSyncedLocalDay = localDayKey(localClock);
  clockAttemptedLocalDay = clockSyncedLocalDay;
  const bool saved =
      preferencesReady &&
      preferences.putUInt(kPreferenceClockDay, clockSyncedLocalDay) > 0 &&
      preferences.putUInt(kPreferenceClockAttemptDay,
                          clockAttemptedLocalDay) > 0 &&
      preferences.getUInt(kPreferenceClockDay, 0) ==
          clockSyncedLocalDay &&
      preferences.getUInt(kPreferenceClockAttemptDay, 0) ==
          clockAttemptedLocalDay;
  if (!saved) {
    Serial.println("Warning: NTP sync date was not persisted");
  }
  Serial.printf("Clock synchronized: %04d-%02d-%02d %02d:%02d:%02d\n",
                localClock.tm_year + 1900, localClock.tm_mon + 1,
                localClock.tm_mday, localClock.tm_hour,
                localClock.tm_min, localClock.tm_sec);
  return true;
}

String serverBaseUrl() {
  if (resolvedServerBase.length() > 0) {
    return resolvedServerBase;
  }
  String base(EINK_FRAME_SERVER_URL);
  while (base.endsWith("/")) {
    base.remove(base.length() - 1);
  }
  const String authority = base.substring(7);
  String hostname = authority;
  String port;
  const int colon = authority.lastIndexOf(':');
  if (colon >= 0) {
    hostname = authority.substring(0, colon);
    port = authority.substring(colon);
  }
  if (hostname.endsWith(".local")) {
    const String mdnsName = hostname.substring(0, hostname.length() - 6);
    if (!MDNS.begin("eink-ee02")) {
      Serial.println("Could not start mDNS");
      return String();
    }
    const IPAddress address = MDNS.queryHost(mdnsName, 5000);
    if (address == INADDR_NONE) {
      Serial.printf("Could not resolve %s with mDNS\n", hostname.c_str());
      return String();
    }
    base = String("http://") + address.toString() + port;
    Serial.printf("Resolved %s to %s\n", hostname.c_str(),
                  address.toString().c_str());
  }
  resolvedServerBase = base;
  return resolvedServerBase;
}

String manifestUrl(const String &mode) {
  const String base = serverBaseUrl();
  return base.length() > 0 ? base + "/v1/manifest/" + mode : String();
}

String immutableFrameUrl(const String &mode, const String &sha) {
  const String base = serverBaseUrl();
  return base.length() > 0
             ? base + "/v1/frame/" + mode + "/" + sha
             : String();
}

bool readExactPayload(HTTPClient &http, uint8_t *destination,
                      size_t expectedBytes) {
  auto *stream = http.getStreamPtr();
  if (stream == nullptr) {
    return false;
  }

  size_t offset = 0;
  uint32_t lastProgress = millis();
  while (offset < expectedBytes) {
    const int available = stream->available();
    if (available > 0) {
      const size_t wanted =
          min(static_cast<size_t>(available), expectedBytes - offset);
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
  return offset == expectedBytes;
}

uint8_t backingColorAtLogical(const uint8_t *frame, int16_t logicalX,
                              int16_t logicalY) {
  if (logicalX < 0 ||
      logicalX >= static_cast<int16_t>(eink::kBackingHeight) ||
      logicalY < 0 ||
      logicalY >= static_cast<int16_t>(eink::kBackingWidth)) {
    return TFT_WHITE;
  }

  // The Pi rotates its 1600x1200 landscape canvas clockwise into the native
  // 1200x1600 backing: logical (x,y) -> backing (1199-y,x).
  const size_t pixelIndex = eink::clockwiseBackingPixelIndex(
      static_cast<size_t>(logicalX), static_cast<size_t>(logicalY),
      eink::kBackingWidth);
  const uint8_t packed = frame[pixelIndex / 2];
  return pixelIndex % 2 == 0 ? packed >> 4 : packed & 0x0F;
}

uint16_t batteryMarkInk(const uint8_t *frame) {
  const int16_t logicalWidth =
      static_cast<int16_t>(eink::kBackingHeight);
  const int16_t logicalHeight =
      static_cast<int16_t>(eink::kBackingWidth);
  const int16_t right = logicalWidth - kBatteryMarkRightMargin;
  const int16_t bottom = logicalHeight - kBatteryMarkBottomMargin;
  size_t lightSamples = 0;
  size_t darkSamples = 0;

  for (int16_t y = bottom - kBatteryMarkSampleHeight; y <= bottom;
       y += 3) {
    for (int16_t x = right - kBatteryMarkSampleWidth; x <= right;
         x += 3) {
      const uint8_t color = backingColorAtLogical(frame, x, y);
      if (color == TFT_WHITE || color == TFT_YELLOW) {
        ++lightSamples;
      } else {
        ++darkSamples;
      }
    }
  }
  return lightSamples >= darkSamples ? TFT_BLACK : TFT_WHITE;
}

void applyBatteryMark(uint8_t *displayBuffer) {
  if (!battery.valid) {
    return;
  }

  char mark[8];
  snprintf(mark, sizeof(mark), "%u/100",
           static_cast<unsigned>(battery.percent));
  const uint16_t ink = batteryMarkInk(displayBuffer);

  // Satisfy's digit ink is about 19 pixels tall on this panel (roughly 9 pt).
  // Drawing through the 4bpp sprite writes exact black/white nibbles without
  // antialiasing, keeping the server frame palette contract intact.
  epaper.setRotation(1);
  epaper.setFreeFont(&Satisfy_24);
  epaper.setTextSize(1);
  epaper.setTextDatum(BR_DATUM);
  epaper.setTextColor(ink);
  epaper.drawString(mark, epaper.width() - kBatteryMarkRightMargin,
                    epaper.height() - kBatteryMarkBottomMargin);
  epaper.setTextDatum(TL_DATUM);
  epaper.setTextFont(1);
  epaper.setRotation(0);
}

bool refreshPanel(const uint8_t *verifiedFrame) {
  // The base frame remains byte-for-byte verified against the server. Only the
  // small device-owned battery signature is drawn after that verification.
  auto *displayBuffer = static_cast<uint8_t *>(epaper.getPointer());
  if (displayBuffer == nullptr) {
    Serial.println("Seeed_GFX framebuffer allocation failed");
    return false;
  }
  memcpy(displayBuffer, verifiedFrame, eink::kFrameBytes);
  applyBatteryMark(displayBuffer);
  if (!eink::hasOnlySupportedColors(displayBuffer, eink::kFrameBytes)) {
    Serial.println("Battery mark produced an invalid display color");
    return false;
  }
  if (!panelInitialized) {
    epaper.begin();
    panelInitialized = true;
  }
  epaper.update();
  return true;
}

struct ManifestInfo {
  String mode;
  String frameSha;
  String frameEtag;
  eink::WakeDeadline wake;
};

void configureHttp(HTTPClient &http) {
  http.setConnectTimeout(EINK_HTTP_CONNECT_TIMEOUT_MS);
  http.setTimeout(EINK_HTTP_READ_TIMEOUT_MS);
  http.useHTTP10(true);
  http.setFollowRedirects(HTTPC_DISABLE_FOLLOW_REDIRECTS);
}

void addAuthorization(HTTPClient &http) {
  if (EINK_FRAME_AUTH_TOKEN[0] != '\0') {
    http.addHeader("Authorization",
                   String("Bearer ") + EINK_FRAME_AUTH_TOKEN);
  }
}

bool jsonStringFieldEquals(const char *document, const char *key,
                           const String &expected) {
  if (document == nullptr || key == nullptr) {
    return false;
  }
  const String marker = String("\"") + key + "\"";
  const char *cursor = strstr(document, marker.c_str());
  if (cursor == nullptr) {
    return false;
  }
  cursor += marker.length();
  while (*cursor == ' ' || *cursor == '\t' || *cursor == '\r' ||
         *cursor == '\n') {
    ++cursor;
  }
  if (*cursor++ != ':') {
    return false;
  }
  while (*cursor == ' ' || *cursor == '\t' || *cursor == '\r' ||
         *cursor == '\n') {
    ++cursor;
  }
  if (*cursor++ != '"') {
    return false;
  }
  return strncmp(cursor, expected.c_str(), expected.length()) == 0 &&
         cursor[expected.length()] == '"';
}

bool fetchManifest(const String &requestedMode, ManifestInfo &manifest) {
  WiFiClient transport;
  HTTPClient http;
  configureHttp(http);
  const char *headerNames[] = {
      "ETag",              "Content-Type",      "X-Frame-ETag",
      "X-Frame-Format",    "X-Frame-SHA256",   "X-Content-SHA256",
      "X-Resolved-Mode",   "X-Server-Time",     "X-Next-Wake-At",
      "X-Server-Epoch",    "X-Next-Wake-Epoch",
  };
  http.collectHeaders(headerNames,
                      sizeof(headerNames) / sizeof(headerNames[0]));

  const String url = manifestUrl(requestedMode);
  if (url.length() == 0 || !http.begin(transport, url)) {
    Serial.println("Could not initialize the manifest request");
    return false;
  }
  addAuthorization(http);
  http.addHeader("Accept", "application/json");
  http.addHeader("Accept-Encoding", "identity");

  // This GET is deliberately unconditional on every timer and button wake.
  // The response is small and is the Pi's authoritative schedule decision.
  Serial.printf("Requesting updated %s manifest\n", requestedMode.c_str());
  const int status = http.GET();
  if (status != HTTP_CODE_OK) {
    Serial.printf("Manifest server returned HTTP %d\n", status);
    http.end();
    return false;
  }

  String contentType = http.header("Content-Type");
  String manifestEtag = http.header("ETag");
  String frameEtag = http.header("X-Frame-ETag");
  String wireFormat = http.header("X-Frame-Format");
  String frameSha = http.header("X-Frame-SHA256");
  String contentSha = http.header("X-Content-SHA256");
  String resolvedMode = http.header("X-Resolved-Mode");
  String serverTime = http.header("X-Server-Time");
  String nextWakeAt = http.header("X-Next-Wake-At");
  String serverEpochText = http.header("X-Server-Epoch");
  String nextWakeEpochText = http.header("X-Next-Wake-Epoch");
  for (String *value :
       {&contentType, &manifestEtag, &frameEtag, &wireFormat, &frameSha,
        &contentSha, &resolvedMode, &serverTime, &nextWakeAt,
        &serverEpochText, &nextWakeEpochText}) {
    value->trim();
  }
  if (requestedMode != "active") {
    resolvedMode = requestedMode;
  }

  const int contentLength = http.getSize();
  const bool validHeaders =
      contentLength > 0 &&
      contentLength <= static_cast<int>(EINK_MAX_MANIFEST_BYTES) &&
      contentType == "application/json; charset=utf-8" &&
      eink::isConcreteFrameMode(resolvedMode) &&
      isLowercaseSha256(frameSha) && contentSha == frameSha &&
      frameEtag == etagForSha(frameSha) &&
      wireFormat == eink::kWireFormat;
  if (!validHeaders) {
    Serial.println("Manifest response headers are invalid");
    http.end();
    return false;
  }

  auto *document = static_cast<uint8_t *>(heap_caps_malloc(
      static_cast<size_t>(contentLength) + 1,
      MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT));
  if (document == nullptr) {
    Serial.println("Could not allocate the manifest staging buffer");
    http.end();
    return false;
  }
  const bool complete =
      readExactPayload(http, document, static_cast<size_t>(contentLength));
  http.end();
  document[contentLength] = '\0';
  if (!complete) {
    Serial.println("Manifest response was truncated");
    heap_caps_free(document);
    return false;
  }

  const String actualManifestSha =
      sha256Hex(document, static_cast<size_t>(contentLength));
  const bool validDocument =
      manifestEtag == etagForSha(actualManifestSha) &&
      jsonStringFieldEquals(reinterpret_cast<const char *>(document), "mode",
                            resolvedMode);
  if (!validDocument) {
    Serial.println("Manifest body failed validation");
    heap_caps_free(document);
    return false;
  }

  uint64_t serverEpoch = 0;
  uint64_t nextWakeEpoch = 0;
  const bool completeSchedule =
      serverTime.length() > 0 && nextWakeAt.length() > 0 &&
      eink::parseUnsignedEpoch(serverEpochText.c_str(), serverEpoch) &&
      eink::parseUnsignedEpoch(nextWakeEpochText.c_str(), nextWakeEpoch) &&
      jsonStringFieldEquals(reinterpret_cast<const char *>(document),
                            "next_wake_at", nextWakeAt);
  heap_caps_free(document);

  manifest.mode = resolvedMode;
  manifest.frameSha = frameSha;
  manifest.frameEtag = frameEtag;
  if (completeSchedule) {
    manifest.wake = eink::makeWakeDeadline(
        serverEpoch, nextWakeEpoch, millis(),
        EINK_MAX_SCHEDULE_SLEEP_SECONDS);
  }
  if (manifest.wake.valid) {
    Serial.printf("Pi scheduled the next wake for %s\n", nextWakeAt.c_str());
  } else {
    Serial.println(
        "Manifest has no valid future deadline; using the short retry interval");
  }
  return true;
}

uint8_t *downloadFrame(const ManifestInfo &manifest) {
  WiFiClient transport;
  HTTPClient http;
  configureHttp(http);
  const char *headerNames[] = {
      "ETag", "Content-Type", "X-Frame-Format", "X-Frame-SHA256",
      "X-Content-SHA256",
  };
  http.collectHeaders(headerNames,
                      sizeof(headerNames) / sizeof(headerNames[0]));

  const String url = immutableFrameUrl(manifest.mode, manifest.frameSha);
  if (url.length() == 0 || !http.begin(transport, url)) {
    Serial.println("Could not initialize the frame request");
    return nullptr;
  }
  addAuthorization(http);
  http.addHeader("Accept", eink::kContentType);
  http.addHeader("Accept-Encoding", "identity");
  const int status = http.GET();
  if (status != HTTP_CODE_OK) {
    Serial.printf("Frame server returned HTTP %d; panel left unchanged\n",
                  status);
    http.end();
    return nullptr;
  }

  String contentType = http.header("Content-Type");
  String wireFormat = http.header("X-Frame-Format");
  String expectedSha = http.header("X-Frame-SHA256");
  String contentSha = http.header("X-Content-SHA256");
  String etag = http.header("ETag");
  for (String *value :
       {&contentType, &wireFormat, &expectedSha, &contentSha, &etag}) {
    value->trim();
  }
  const bool validHeaders =
      http.getSize() == static_cast<int>(eink::kFrameBytes) &&
      contentType == eink::kContentType &&
      wireFormat == eink::kWireFormat &&
      expectedSha == manifest.frameSha && contentSha == expectedSha &&
      etag == manifest.frameEtag;
  if (!validHeaders) {
    Serial.println("Frame response contract is invalid; panel left unchanged");
    http.end();
    return nullptr;
  }

  auto *staging = static_cast<uint8_t *>(heap_caps_malloc(
      eink::kFrameBytes, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT));
  if (staging == nullptr) {
    Serial.println("Could not allocate the 960,000-byte PSRAM staging buffer");
    http.end();
    return nullptr;
  }
  const bool complete =
      readExactPayload(http, staging, eink::kFrameBytes);
  http.end();
  if (!complete) {
    Serial.println("Frame response was truncated; panel left unchanged");
    heap_caps_free(staging);
    return nullptr;
  }
  const String actualSha = sha256Hex(staging, eink::kFrameBytes);
  if (actualSha != manifest.frameSha) {
    Serial.println("Frame SHA-256 mismatch; panel left unchanged");
    heap_caps_free(staging);
    return nullptr;
  }
  if (!eink::hasOnlySupportedColors(staging, eink::kFrameBytes)) {
    Serial.println("Frame contains unsupported EE02 color nibbles");
    heap_caps_free(staging);
    return nullptr;
  }
  return staging;
}

SyncResult syncFrame(const String &requestedMode) {
  scheduledWake = {};
  if (!eink::isFrameChannel(requestedMode)) {
    Serial.println("Refusing an unsupported frame mode");
    return SyncResult::kFailed;
  }
  if (!connectWifi()) {
    return SyncResult::kFailed;
  }

  ManifestInfo manifest;
  if (!fetchManifest(requestedMode, manifest)) {
    return SyncResult::kFailed;
  }
  const bool baseMatches =
      manifest.frameSha == displayedSha &&
      manifest.frameEtag == displayedEtag;
  if (baseMatches && !batteryMarkNeedsRefresh()) {
    if (displayedMode != manifest.mode) {
      persistModeForUnchangedFrame(manifest.mode);
    }
    scheduledWake = manifest.wake;
    Serial.println(
        "Manifest frame is unchanged; skipping the large download and refresh");
    return SyncResult::kNotModified;
  }

  uint8_t *staging = downloadFrame(manifest);
  if (staging == nullptr) {
    return SyncResult::kFailed;
  }
  if (manifest.frameSha == displayedSha &&
      manifest.frameEtag == displayedEtag &&
      !batteryMarkNeedsRefresh()) {
    heap_caps_free(staging);
    if (displayedMode != manifest.mode) {
      persistModeForUnchangedFrame(manifest.mode);
    }
    scheduledWake = manifest.wake;
    Serial.println("Downloaded frame matches the display; refresh skipped");
    return SyncResult::kNotModified;
  }

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

  persistDisplayState(
      manifest.mode, manifest.frameSha, manifest.frameEtag);
  scheduledWake = manifest.wake;
  Serial.printf("Displayed %s frame %s\n", manifest.mode.c_str(),
                manifest.frameSha.substring(0, 12).c_str());
  return SyncResult::kUpdated;
}

bool anyButtonIsPressed() {
  return digitalRead(kButton1) == LOW || digitalRead(kButton2) == LOW ||
         digitalRead(kButton3) == LOW;
}

const char *frameModeForButtonNumber(uint8_t buttonNumber) {
  if (buttonNumber == 1) {
    return kButton1FrameMode;
  }
  if (buttonNumber == 2) {
    return kButton2FrameMode;
  }
  if (buttonNumber == 3) {
    return kButton3FrameMode;
  }
  return nullptr;
}

uint8_t buttonNumberForWakeStatus(uint64_t wakeStatus) {
  // Give the numbered buttons deterministic priority if more than one is
  // pressed at the same time. EXT1 retains this mask across deep sleep, so the
  // selection remains reliable even when the user releases the button before
  // setup reaches this function.
  if ((wakeStatus & (1ULL << kButton1)) != 0) {
    return 1;
  }
  if ((wakeStatus & (1ULL << kButton2)) != 0) {
    return 2;
  }
  if ((wakeStatus & (1ULL << kButton3)) != 0) {
    return 3;
  }
  return 0;
}

const char *frameModeForButtonWake(uint64_t wakeStatus) {
  return frameModeForButtonNumber(buttonNumberForWakeStatus(wakeStatus));
}

void discardWakeButtonLatch(uint8_t wakeButtonNumber) {
  if (wakeButtonNumber == 0) {
    return;
  }
  noInterrupts();
  if (pendingButtonNumber == wakeButtonNumber) {
    pendingButtonNumber = 0;
  }
  interrupts();
}

void IRAM_ATTR onButton1Pressed() {
  pendingButtonNumber = 1;
}

void IRAM_ATTR onButton2Pressed() {
  pendingButtonNumber = 2;
}

void IRAM_ATTR onButton3Pressed() {
  pendingButtonNumber = 3;
}

uint8_t takePendingButton() {
  noInterrupts();
  const uint8_t buttonNumber = pendingButtonNumber;
  pendingButtonNumber = 0;
  interrupts();
  return buttonNumber;
}

void attachButtonInterrupts() {
  // Deep-sleep EXT1 handles the usual idle case. Attach these latches as soon
  // as the pins are configured so a short press made during boot, Wi-Fi,
  // download, panel refresh, or shutdown work is preserved. HTTP work never
  // runs inside the ISR.
  attachInterrupt(digitalPinToInterrupt(kButton1), onButton1Pressed, FALLING);
  attachInterrupt(digitalPinToInterrupt(kButton2), onButton2Pressed, FALLING);
  attachInterrupt(digitalPinToInterrupt(kButton3), onButton3Pressed, FALLING);
}

uint64_t nextTimerWakeSeconds() {
  return eink::remainingWakeSeconds(
      scheduledWake, millis(), EINK_CHECK_INTERVAL_SECONDS);
}

void sleepUntilButton() {
  while (true) {
    while (anyButtonIsPressed()) {
      delay(10);
    }
    const uint8_t pending = takePendingButton();
    const char *pendingMode = frameModeForButtonNumber(pending);
    if (pendingMode != nullptr) {
      Serial.printf(
          "Button %u was pressed while awake; requesting %s manifest\n",
          static_cast<unsigned>(pending), pendingMode);
      sampleBatteryEveryWake();
      wifiAttemptedThisWake = false;
      syncFrame(pendingMode);
      continue;
    }

    const uint64_t timerWakeSeconds = nextTimerWakeSeconds();
    MDNS.end();
    WiFi.disconnect(true);
    WiFi.mode(WIFI_OFF);
    digitalWrite(kBatteryEnablePin, LOW);

    esp_sleep_enable_ext1_wakeup(kButtonWakeMask, ESP_EXT1_WAKEUP_ANY_LOW);
    esp_sleep_enable_timer_wakeup(timerWakeSeconds * 1000000ULL);
    Serial.printf(
        "Sleeping; checking again in %llu seconds or on any user button\n",
        timerWakeSeconds);
    Serial.flush();
    delay(20);

    // Check once more after radio shutdown and serial flushing. A press held
    // here is released before servicing so it cannot immediately retrigger
    // EXT1; a short released press remains latched by the ISR.
    while (anyButtonIsPressed()) {
      delay(10);
    }
    const uint8_t finalPending = takePendingButton();
    const char *finalMode = frameModeForButtonNumber(finalPending);
    if (finalMode != nullptr) {
      Serial.printf(
          "Button %u was pressed before sleep; requesting %s manifest\n",
          static_cast<unsigned>(finalPending), finalMode);
      sampleBatteryEveryWake();
      wifiAttemptedThisWake = false;
      syncFrame(finalMode);
      continue;
    }

    // NVS writes above are synchronous. Leaving the Preferences handle open
    // until reset avoids creating another button-loss window before sleep.
    esp_deep_sleep_start();
  }
}

}  // namespace

void setup() {
  Serial.begin(115200);
  pinMode(kButton1, INPUT_PULLUP);
  pinMode(kButton2, INPUT_PULLUP);
  pinMode(kButton3, INPUT_PULLUP);
  pinMode(kBatteryEnablePin, OUTPUT);
  digitalWrite(kBatteryEnablePin, LOW);
  attachButtonInterrupts();
  setenv("TZ", EINK_TIMEZONE, 1);
  tzset();
  delay(750);
  Serial.println("EE02 scheduled image and per-wake battery updater starting");
  const esp_sleep_wakeup_cause_t wakeCause = esp_sleep_get_wakeup_cause();
  const char *requestedFrameMode = EINK_FRAME_MODE;
  uint8_t wakeButtonNumber = 0;
  if (wakeCause == ESP_SLEEP_WAKEUP_EXT1) {
    const uint64_t wakeStatus =
        esp_sleep_get_ext1_wakeup_status() & kButtonWakeMask;
    wakeButtonNumber = buttonNumberForWakeStatus(wakeStatus);
    const char *buttonFrameMode = frameModeForButtonWake(wakeStatus);
    if (buttonFrameMode != nullptr) {
      requestedFrameMode = buttonFrameMode;
      Serial.printf("Wake reason: user button; requesting %s\n",
                    requestedFrameMode);
    } else {
      Serial.println(
          "Wake reason: user button without a valid pin; requesting active");
    }
  } else if (wakeCause == ESP_SLEEP_WAKEUP_TIMER) {
    Serial.println("Wake reason: scheduled timer");
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
  loadBatteryState();
  loadDisplayState();
  sampleBatteryEveryWake();

  if (!configurationIsUsable()) {
    sleepUntilButton();
    return;
  }
  while (anyButtonIsPressed()) {
    delay(10);
  }
  // The falling-edge ISR may see contact bounce from the same press that EXT1
  // already consumed. Remove that one duplicate after release while preserving
  // a different button pressed during boot.
  discardWakeButtonLatch(wakeButtonNumber);

  struct tm localClock {};
  synchronizeClockIfDue(localClock);

  syncFrame(requestedFrameMode);
  sleepUntilButton();
}

void loop() {
  delay(1000);
}
