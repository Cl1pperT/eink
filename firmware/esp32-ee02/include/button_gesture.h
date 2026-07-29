#pragma once

#include <cstdint>

namespace eink {

constexpr uint8_t kWeatherButtonMask = 1U << 0;
constexpr uint8_t kBirdsButtonMask = 1U << 1;
constexpr uint8_t kStarsButtonMask = 1U << 2;
constexpr uint8_t kAllButtonMask =
    kWeatherButtonMask | kBirdsButtonMask | kStarsButtonMask;
constexpr uint32_t kButtonGestureWindowMilliseconds = 350;

enum class ButtonGestureAction : uint8_t {
  kNone,
  kWeather,
  kBirds,
  kStars,
  kImageCheck,
};

struct ButtonGestureLatch {
  uint8_t mask = 0;
  uint32_t startedAtMilliseconds = 0;
};

#if defined(__GNUC__)
#define EINK_BUTTON_ALWAYS_INLINE inline __attribute__((always_inline))
#else
#define EINK_BUTTON_ALWAYS_INLINE inline
#endif

EINK_BUTTON_ALWAYS_INLINE uint8_t validButtonMask(uint8_t mask) {
  return mask & kAllButtonMask;
}

EINK_BUTTON_ALWAYS_INLINE bool isButtonChord(uint8_t mask) {
  const uint8_t valid = validButtonMask(mask);
  return valid != 0 && (valid & static_cast<uint8_t>(valid - 1U)) != 0;
}

EINK_BUTTON_ALWAYS_INLINE uint32_t buttonGestureAge(
    const ButtonGestureLatch &latch, uint32_t nowMilliseconds) {
  // Unsigned subtraction intentionally handles millis() wraparound.
  return nowMilliseconds - latch.startedAtMilliseconds;
}

EINK_BUTTON_ALWAYS_INLINE ButtonGestureAction buttonGestureAction(
    uint8_t mask) {
  const uint8_t valid = validButtonMask(mask);
  if (isButtonChord(valid)) {
    return ButtonGestureAction::kImageCheck;
  }
  if (valid == kWeatherButtonMask) {
    return ButtonGestureAction::kWeather;
  }
  if (valid == kBirdsButtonMask) {
    return ButtonGestureAction::kBirds;
  }
  if (valid == kStarsButtonMask) {
    return ButtonGestureAction::kStars;
  }
  return ButtonGestureAction::kNone;
}

EINK_BUTTON_ALWAYS_INLINE void latchButtonGesture(
    ButtonGestureLatch &latch, uint8_t observedMask,
    uint32_t nowMilliseconds) {
  const uint8_t observed = validButtonMask(observedMask);
  if (observed == 0 || isButtonChord(latch.mask)) {
    return;
  }

  if (latch.mask == 0 ||
      buttonGestureAge(latch, nowMilliseconds) >
          kButtonGestureWindowMilliseconds) {
    // A distinct late press starts a new one-shot gesture. Repeated contact
    // bounce from one physical button can never create a chord.
    latch.mask = observed;
    latch.startedAtMilliseconds = nowMilliseconds;
    return;
  }
  latch.mask = validButtonMask(latch.mask | observed);
}

EINK_BUTTON_ALWAYS_INLINE bool buttonGestureIsReady(
    const ButtonGestureLatch &latch, uint32_t nowMilliseconds) {
  return latch.mask != 0 &&
         (isButtonChord(latch.mask) ||
          buttonGestureAge(latch, nowMilliseconds) >
              kButtonGestureWindowMilliseconds);
}

EINK_BUTTON_ALWAYS_INLINE uint8_t takeReadyButtonGesture(
    ButtonGestureLatch &latch, uint32_t nowMilliseconds) {
  if (!buttonGestureIsReady(latch, nowMilliseconds)) {
    return 0;
  }
  const uint8_t mask = validButtonMask(latch.mask);
  latch = {};
  return mask;
}

EINK_BUTTON_ALWAYS_INLINE void discardButtonGestureMask(
    ButtonGestureLatch &latch, uint8_t consumedMask,
    uint32_t nowMilliseconds) {
  latch.mask =
      validButtonMask(latch.mask & static_cast<uint8_t>(~consumedMask));
  if (latch.mask == 0) {
    latch = {};
    return;
  }
  // Any different button preserved across wake-button bounce starts its own
  // gesture window instead of inheriting the consumed press's timestamp.
  latch.startedAtMilliseconds = nowMilliseconds;
}

#undef EINK_BUTTON_ALWAYS_INLINE

}  // namespace eink
