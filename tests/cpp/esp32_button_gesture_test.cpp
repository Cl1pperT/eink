#include <cassert>
#include <cstdint>
#include <limits>

#include "button_gesture.h"

int main() {
  using eink::ButtonGestureAction;

  assert(eink::buttonGestureAction(0) == ButtonGestureAction::kNone);
  assert(eink::buttonGestureAction(eink::kWeatherButtonMask) ==
         ButtonGestureAction::kWeather);
  assert(eink::buttonGestureAction(eink::kBirdsButtonMask) ==
         ButtonGestureAction::kBirds);
  assert(eink::buttonGestureAction(eink::kStarsButtonMask) ==
         ButtonGestureAction::kStars);
  assert(eink::buttonGestureAction(eink::kWeatherButtonMask |
                                   eink::kBirdsButtonMask) ==
         ButtonGestureAction::kImageCheck);
  assert(eink::buttonGestureAction(eink::kWeatherButtonMask |
                                   eink::kStarsButtonMask) ==
         ButtonGestureAction::kImageCheck);
  assert(eink::buttonGestureAction(eink::kBirdsButtonMask |
                                   eink::kStarsButtonMask) ==
         ButtonGestureAction::kImageCheck);
  assert(eink::buttonGestureAction(eink::kAllButtonMask) ==
         ButtonGestureAction::kImageCheck);

  eink::ButtonGestureLatch bounce;
  eink::latchButtonGesture(bounce, eink::kWeatherButtonMask, 1000);
  eink::latchButtonGesture(bounce, eink::kWeatherButtonMask, 1005);
  eink::latchButtonGesture(bounce, eink::kWeatherButtonMask, 1017);
  assert(bounce.mask == eink::kWeatherButtonMask);
  assert(!eink::buttonGestureIsReady(bounce, 1350));
  assert(eink::buttonGestureIsReady(bounce, 1351));
  assert(eink::takeReadyButtonGesture(bounce, 1351) ==
         eink::kWeatherButtonMask);
  assert(bounce.mask == 0);

  eink::ButtonGestureLatch chord;
  eink::latchButtonGesture(chord, eink::kWeatherButtonMask, 2000);
  eink::latchButtonGesture(chord, eink::kStarsButtonMask, 2350);
  assert(eink::isButtonChord(chord.mask));
  assert(eink::buttonGestureIsReady(chord, 2350));
  // Once recognized, later edges cannot replace the image gesture.
  eink::latchButtonGesture(chord, eink::kBirdsButtonMask, 4000);
  assert(chord.mask ==
         (eink::kWeatherButtonMask | eink::kStarsButtonMask));

  eink::ButtonGestureLatch late;
  eink::latchButtonGesture(late, eink::kWeatherButtonMask, 5000);
  eink::latchButtonGesture(late, eink::kBirdsButtonMask, 5351);
  assert(late.mask == eink::kBirdsButtonMask);
  assert(late.startedAtMilliseconds == 5351);
  assert(!eink::isButtonChord(late.mask));

  eink::ButtonGestureLatch simultaneous;
  eink::latchButtonGesture(
      simultaneous,
      eink::kWeatherButtonMask | eink::kBirdsButtonMask, 6000);
  assert(eink::buttonGestureAction(simultaneous.mask) ==
         ButtonGestureAction::kImageCheck);

  eink::ButtonGestureLatch wrapped;
  const uint32_t nearWrap = std::numeric_limits<uint32_t>::max() - 99U;
  eink::latchButtonGesture(wrapped, eink::kBirdsButtonMask, nearWrap);
  eink::latchButtonGesture(wrapped, eink::kStarsButtonMask, 200);
  assert(eink::isButtonChord(wrapped.mask));

  eink::ButtonGestureLatch discarded;
  eink::latchButtonGesture(
      discarded, eink::kWeatherButtonMask | eink::kBirdsButtonMask, 7000);
  eink::discardButtonGestureMask(
      discarded, eink::kWeatherButtonMask, 7100);
  assert(discarded.mask == eink::kBirdsButtonMask);
  assert(discarded.startedAtMilliseconds == 7100);
  eink::discardButtonGestureMask(discarded, eink::kBirdsButtonMask, 7200);
  assert(discarded.mask == 0);

  eink::ButtonGestureLatch invalid;
  eink::latchButtonGesture(invalid, 0x80, 8000);
  assert(invalid.mask == 0);
}
