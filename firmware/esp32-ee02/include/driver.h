#pragma once

// Seeed_GFX also discovers this project header from User_Setup_Select.h. The
// same values are global PlatformIO build flags so the library's .cpp files
// cannot accidentally compile with a different display setup.
#ifndef BOARD_SCREEN_COMBO
#define BOARD_SCREEN_COMBO 510
#endif

#ifndef USE_XIAO_EPAPER_DISPLAY_BOARD_EE02
#define USE_XIAO_EPAPER_DISPLAY_BOARD_EE02
#endif
