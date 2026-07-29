#include <cassert>
#include <cstdint>

#include "battery_monitor.h"

int main() {
  assert(!eink::isPlausibleBatteryMillivolts(2499));
  assert(eink::isPlausibleBatteryMillivolts(2500));
  assert(eink::isPlausibleBatteryMillivolts(4350));
  assert(!eink::isPlausibleBatteryMillivolts(4351));

  assert(eink::estimateBatteryPercent(2500) == 0);
  assert(eink::estimateBatteryPercent(3300) == 0);
  assert(eink::estimateBatteryPercent(3750) == 25);
  assert(eink::estimateBatteryPercent(4000) == 80);
  assert(eink::estimateBatteryPercent(4200) == 100);
  assert(eink::estimateBatteryPercent(4350) == 100);
  uint8_t prior = 0;
  for (uint16_t millivolts = 2500; millivolts <= 4350; ++millivolts) {
    const uint8_t percent = eink::estimateBatteryPercent(millivolts);
    assert(percent >= prior);
    assert(percent <= 100);
    prior = percent;
  }

  assert(eink::stabilizeBatteryPercent(4000, false, 0) == 80);
  assert(eink::stabilizeBatteryPercent(4000, true, 79) == 79);
  assert(eink::stabilizeBatteryPercent(4020, true, 80) == 82);
  assert(eink::stabilizeBatteryPercent(3990, true, 80) == 80);
  assert(eink::stabilizeBatteryPercent(3980, true, 80) == 77);

  assert(eink::clockwiseBackingPixelIndex(0, 0, 1200) == 1199);
  assert(eink::clockwiseBackingPixelIndex(1599, 0, 1200) ==
         1599U * 1200U + 1199U);
  assert(eink::clockwiseBackingPixelIndex(0, 1199, 1200) == 0);
  assert(eink::clockwiseBackingPixelIndex(1599, 1199, 1200) ==
         1599U * 1200U);
}
