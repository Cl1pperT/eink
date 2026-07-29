#pragma once

#include <cstddef>
#include <cstdint>

namespace eink {

struct BatteryCurvePoint {
  uint16_t millivolts;
  uint8_t percent;
};

// Generic unloaded 1-cell Li-ion/LiPo voltage curve. Voltage-only state of
// charge is necessarily approximate; these anchors are intentionally easy to
// replace with values from the fitted cell's datasheet.
constexpr BatteryCurvePoint kBatteryCurve[] = {
    {3300, 0},  {3500, 3},  {3600, 7},  {3700, 15},
    {3750, 25}, {3800, 40}, {3850, 55}, {3900, 65},
    {4000, 80}, {4100, 92}, {4200, 100},
};

constexpr uint16_t kMinimumPlausibleBatteryMillivolts = 2500;
constexpr uint16_t kMaximumPlausibleBatteryMillivolts = 4350;

inline bool isPlausibleBatteryMillivolts(uint32_t millivolts) {
  return millivolts >= kMinimumPlausibleBatteryMillivolts &&
         millivolts <= kMaximumPlausibleBatteryMillivolts;
}

inline uint8_t estimateBatteryPercent(uint16_t millivolts) {
  if (millivolts <= kBatteryCurve[0].millivolts) {
    return kBatteryCurve[0].percent;
  }

  constexpr size_t pointCount =
      sizeof(kBatteryCurve) / sizeof(kBatteryCurve[0]);
  for (size_t index = 1; index < pointCount; ++index) {
    const BatteryCurvePoint upper = kBatteryCurve[index];
    if (millivolts > upper.millivolts) {
      continue;
    }
    const BatteryCurvePoint lower = kBatteryCurve[index - 1];
    const uint32_t voltageOffset = millivolts - lower.millivolts;
    const uint32_t voltageSpan = upper.millivolts - lower.millivolts;
    const uint32_t percentSpan = upper.percent - lower.percent;
    return static_cast<uint8_t>(
        lower.percent +
        (voltageOffset * percentSpan + voltageSpan / 2) / voltageSpan);
  }
  return 100;
}

inline uint8_t stabilizeBatteryPercent(uint16_t millivolts,
                                       bool hasPreviousEstimate,
                                       uint8_t previousPercent) {
  const uint8_t estimated = estimateBatteryPercent(millivolts);
  if (!hasPreviousEstimate || previousPercent > 100) {
    return estimated;
  }
  const int difference =
      static_cast<int>(estimated) - static_cast<int>(previousPercent);
  // A one-point display change is usually ADC/load noise. Waiting for a
  // two-point move prevents needless 13.3-inch refreshes while every wake
  // still records a fresh voltage measurement.
  return difference >= -1 && difference <= 1 ? previousPercent : estimated;
}

inline size_t clockwiseBackingPixelIndex(size_t logicalX,
                                         size_t logicalY,
                                         size_t backingWidth) {
  return logicalX * backingWidth + (backingWidth - logicalY - 1);
}

}  // namespace eink
