#pragma once

#include <Arduino.h>
#include <cstddef>

namespace eink {

constexpr size_t kBackingWidth = 1200;
constexpr size_t kBackingHeight = 1600;
constexpr size_t kFrameBytes = kBackingWidth * kBackingHeight / 2;

constexpr char kWireFormat[] = "seeed-ee02-t133a01-4bpp-v1";
constexpr char kContentType[] = "application/vnd.seeed.ee02-4bpp";

inline bool isConcreteMode(const String &mode) {
  return mode == "weather" || mode == "birds" || mode == "star-map" ||
         mode == "uploaded-photo" || mode == "test-pattern";
}

inline bool isAllowedColorCode(uint8_t value) {
  // Setup510 sprite nibbles: white, green, red, yellow, blue, black.
  return value == 0x0 || value == 0x2 || value == 0x6 || value == 0xB ||
         value == 0xD || value == 0xF;
}

inline bool hasOnlySupportedColors(const uint8_t *frame, size_t length) {
  for (size_t index = 0; index < length; ++index) {
    if (!isAllowedColorCode(frame[index] >> 4) ||
        !isAllowedColorCode(frame[index] & 0x0F)) {
      return false;
    }
  }
  return true;
}

static_assert(kFrameBytes == 960000, "EE02 payload size changed unexpectedly");

}  // namespace eink

