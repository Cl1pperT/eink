#pragma once

// The one frame checked after boot or a button wake. Override with a -D flag
// if the board should follow another concrete server frame.
#ifndef EINK_FRAME_MODE
#define EINK_FRAME_MODE "uploaded-photo"
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
