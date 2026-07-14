#pragma once

// Non-secret defaults. Override these with -D flags if desired.
#ifndef EINK_DEFAULT_MODE
#define EINK_DEFAULT_MODE "automatic"
#endif

#ifndef EINK_POLL_INTERVAL_MS
#define EINK_POLL_INTERVAL_MS 300000UL
#endif

#ifndef EINK_FORCE_RETRY_INTERVAL_MS
#define EINK_FORCE_RETRY_INTERVAL_MS 30000UL
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

// POSIX timezone and display boundaries should match the Pi's
// location.timezone and schedule. This default is America/Denver.
#ifndef EINK_POSIX_TIMEZONE
#define EINK_POSIX_TIMEZONE "MST7MDT,M3.2.0,M11.1.0"
#endif

#ifndef EINK_WEATHER_START_MINUTES
#define EINK_WEATHER_START_MINUTES 360
#endif

#ifndef EINK_BIRDS_START_MINUTES
#define EINK_BIRDS_START_MINUTES 600
#endif

#ifndef EINK_STAR_MAP_START_MINUTES
#define EINK_STAR_MAP_START_MINUTES 1200
#endif

#ifndef EINK_NTP_SYNC_WAIT_MS
#define EINK_NTP_SYNC_WAIT_MS 10000UL
#endif

#ifndef EINK_NTP_SERVER_1
#define EINK_NTP_SERVER_1 "pool.ntp.org"
#endif

#ifndef EINK_NTP_SERVER_2
#define EINK_NTP_SERVER_2 "time.nist.gov"
#endif
