#pragma once

// Non-secret defaults. Override these with -D flags if desired.
#ifndef EINK_DEFAULT_MODE
#define EINK_DEFAULT_MODE "weather"
#endif

#ifndef EINK_POLL_INTERVAL_MS
#define EINK_POLL_INTERVAL_MS 300000UL
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

