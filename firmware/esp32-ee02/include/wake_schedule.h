#pragma once

#include <cstdint>
#include <limits>

namespace eink {

struct WakeDeadline {
  bool valid = false;
  uint64_t serverEpoch = 0;
  uint64_t nextWakeEpoch = 0;
  uint32_t receivedAtMillis = 0;
};

inline bool parseUnsignedEpoch(const char *value, uint64_t &result) {
  if (value == nullptr || value[0] == '\0') {
    return false;
  }
  uint64_t parsed = 0;
  for (const char *cursor = value; *cursor != '\0'; ++cursor) {
    if (*cursor < '0' || *cursor > '9') {
      return false;
    }
    const uint8_t digit = static_cast<uint8_t>(*cursor - '0');
    if (parsed >
        (std::numeric_limits<uint64_t>::max() - digit) / 10ULL) {
      return false;
    }
    parsed = parsed * 10ULL + digit;
  }
  result = parsed;
  return true;
}

inline WakeDeadline makeWakeDeadline(uint64_t serverEpoch,
                                     uint64_t nextWakeEpoch,
                                     uint32_t receivedAtMillis,
                                     uint64_t maximumSleepSeconds) {
  WakeDeadline deadline;
  if (nextWakeEpoch <= serverEpoch ||
      nextWakeEpoch - serverEpoch > maximumSleepSeconds) {
    return deadline;
  }
  deadline.valid = true;
  deadline.serverEpoch = serverEpoch;
  deadline.nextWakeEpoch = nextWakeEpoch;
  deadline.receivedAtMillis = receivedAtMillis;
  return deadline;
}

inline uint64_t remainingWakeSeconds(const WakeDeadline &deadline,
                                     uint32_t currentMillis,
                                     uint64_t fallbackSeconds) {
  if (!deadline.valid) {
    return fallbackSeconds;
  }
  const uint64_t advertised =
      deadline.nextWakeEpoch - deadline.serverEpoch;
  const uint64_t elapsedSeconds =
      static_cast<uint32_t>(currentMillis - deadline.receivedAtMillis) / 1000ULL;
  return elapsedSeconds >= advertised ? 1ULL : advertised - elapsedSeconds;
}

}  // namespace eink
