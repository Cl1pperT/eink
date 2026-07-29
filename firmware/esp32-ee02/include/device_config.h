#pragma once

// "active" follows the Pi's validated automatic/manual display selection.
// Override with a -D flag only if this board should pin one concrete frame.
#ifndef EINK_FRAME_MODE
#define EINK_FRAME_MODE "active"
#endif

// Wake from deep sleep this often to check for a changed frame. Button presses
// remain an additional immediate wake source.
#ifndef EINK_CHECK_INTERVAL_SECONDS
#define EINK_CHECK_INTERVAL_SECONDS 300ULL
#endif

#ifndef EINK_WIFI_CONNECT_TIMEOUT_MS
#define EINK_WIFI_CONNECT_TIMEOUT_MS 30000UL
#endif

#ifndef EINK_HTTP_CONNECT_TIMEOUT_MS
#define EINK_HTTP_CONNECT_TIMEOUT_MS 10000UL
#endif

#ifndef EINK_HTTP_READ_TIMEOUT_MS
#define EINK_HTTP_READ_TIMEOUT_MS 30000UL
#endif

// EE02 v1 provides a gated 1:1 battery divider: BAT_ADC is GPIO1 (D0/A0)
// and ADC_EN is GPIO6 (D5). Do not use the XIAO Plus ADC_BAT/GPIO10 signal;
// EE02 assigns GPIO10 to the display data/command line.
#ifndef EINK_BATTERY_ADC_PIN
#define EINK_BATTERY_ADC_PIN 1
#endif

#ifndef EINK_BATTERY_ENABLE_PIN
#define EINK_BATTERY_ENABLE_PIN 6
#endif

#ifndef EINK_BATTERY_DIVIDER_MULTIPLIER
#define EINK_BATTERY_DIVIDER_MULTIPLIER 2UL
#endif

#ifndef EINK_BATTERY_SETTLE_MS
#define EINK_BATTERY_SETTLE_MS 25UL
#endif

// Keep this odd so the daily reading can use a noise-resistant median.
#ifndef EINK_BATTERY_SAMPLE_COUNT
#define EINK_BATTERY_SAMPLE_COUNT 25
#endif

// POSIX timezone for America/Denver, including current US DST transitions.
// Override this build flag if the display moves to another timezone.
#ifndef EINK_TIMEZONE
#define EINK_TIMEZONE "MST7MDT,M3.2.0,M11.1.0"
#endif

#ifndef EINK_NTP_SERVER_PRIMARY
#define EINK_NTP_SERVER_PRIMARY "pool.ntp.org"
#endif

#ifndef EINK_NTP_SERVER_SECONDARY
#define EINK_NTP_SERVER_SECONDARY "time.cloudflare.com"
#endif

#ifndef EINK_NTP_SYNC_TIMEOUT_MS
#define EINK_NTP_SYNC_TIMEOUT_MS 15000UL
#endif

// When no trustworthy clock exists and public NTP is unreachable, back off
// this many ordinary deep-sleep wakes before another NTP request.
#ifndef EINK_NTP_RETRY_WAKES
#define EINK_NTP_RETRY_WAKES 12
#endif

// A missing, stale, or malformed Pi deadline falls back to the short check
// interval. Valid absolute schedule deadlines must remain within this bound.
#ifndef EINK_MAX_SCHEDULE_SLEEP_SECONDS
#define EINK_MAX_SCHEDULE_SLEEP_SECONDS 86400ULL
#endif

#ifndef EINK_MAX_MANIFEST_BYTES
#define EINK_MAX_MANIFEST_BYTES 65536UL
#endif
