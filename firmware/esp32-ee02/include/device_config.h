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
