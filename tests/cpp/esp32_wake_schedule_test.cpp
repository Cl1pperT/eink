#include <cassert>
#include <cstdint>
#include <limits>

#include "wake_schedule.h"

int main() {
  uint64_t parsed = 0;
  assert(eink::parseUnsignedEpoch("1785348000", parsed));
  assert(parsed == 1785348000ULL);
  assert(!eink::parseUnsignedEpoch("", parsed));
  assert(!eink::parseUnsignedEpoch("-1", parsed));
  assert(!eink::parseUnsignedEpoch("12.3", parsed));
  assert(!eink::parseUnsignedEpoch(
      "18446744073709551616", parsed));

  const eink::WakeDeadline valid = eink::makeWakeDeadline(
      1000, 4600, 500, 86400);
  assert(valid.valid);
  assert(eink::remainingWakeSeconds(valid, 500, 300) == 3600);
  assert(eink::remainingWakeSeconds(valid, 12500, 300) == 3588);
  assert(eink::remainingWakeSeconds(valid, 3600500, 300) == 1);

  const eink::WakeDeadline stale = eink::makeWakeDeadline(
      1000, 1000, 0, 86400);
  assert(!stale.valid);
  assert(eink::remainingWakeSeconds(stale, 100, 300) == 300);

  const eink::WakeDeadline tooFar = eink::makeWakeDeadline(
      1000, 87401, 0, 86400);
  assert(!tooFar.valid);

  // millis() wrap is intentional and handled with uint32_t subtraction.
  const eink::WakeDeadline wrapped = eink::makeWakeDeadline(
      1000, 1060, std::numeric_limits<uint32_t>::max() - 499, 86400);
  assert(wrapped.valid);
  assert(eink::remainingWakeSeconds(wrapped, 500, 300) == 59);
}
