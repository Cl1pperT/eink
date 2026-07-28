#include <cassert>
#include <cstdint>

#include "battery_monitor.h"

int main() {
  using eink::BatterySampleDecision;

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

  const uint32_t today = 2026210;
  BatterySampleDecision decision = eink::decideBatterySample(
      true, 5, today, today - 1, true, true, 6);
  assert(!decision.due && !decision.daily);  // 05:59

  decision = eink::decideBatterySample(
      true, 6, today, today - 1, true, true, 6);
  assert(decision.due && decision.daily);  // 06:00

  decision = eink::decideBatterySample(
      true, 23, today, today, true, true, 6);
  assert(!decision.due && !decision.daily);  // already attempted today

  decision = eink::decideBatterySample(
      true, 6, today + 1, today, true, true, 6);
  assert(decision.due && decision.daily);  // next local date

  decision = eink::decideBatterySample(
      true, 4, today, 0, false, false, 6);
  assert(decision.due && !decision.daily);  // first useful estimate

  decision = eink::decideBatterySample(
      true, 4, today, 0, false, true, 6);
  assert(!decision.due && !decision.daily);  // failed initial read deduped

  decision = eink::decideBatterySample(
      false, 0, 0, 0, false, false, 6);
  assert(decision.due && !decision.daily);  // clockless first boot

  decision = eink::decideBatterySample(
      false, 0, 0, 0, false, true, 6);
  assert(!decision.due && !decision.daily);  // clockless wake deduped

  decision = eink::decideBatterySample(
      true, 7, today, today - 1, false, true, 6);
  assert(decision.due && decision.daily);  // daily check after failed initial

  assert(eink::clockwiseBackingPixelIndex(0, 0, 1200) == 1199);
  assert(eink::clockwiseBackingPixelIndex(1599, 0, 1200) ==
         1599U * 1200U + 1199U);
  assert(eink::clockwiseBackingPixelIndex(0, 1199, 1200) == 0);
  assert(eink::clockwiseBackingPixelIndex(1599, 1199, 1200) ==
         1599U * 1200U);
}
