# ESP32 EE02 image and battery updater

This PlatformIO/Arduino project targets a Seeed Studio XIAO ESP32S3 with PSRAM,
the XIAO ePaper Display Board EE02, and the 13.3-inch T133A01 Spectra 6 panel.
Every five minutes it checks the headless runtime's virtual `active` frame
endpoint. The Pi resolves that channel from the validated phone-control
selection and automatic weather, bird, and star-map schedule. The three user
buttons wake the board immediately and directly request weather, birds, or the
star map respectively.

The EE02's built-in battery divider is sampled once per local day at 06:00.
The estimate is cached in NVS and written on every physical mode as a small
handwritten `91/100`-style signature in the bottom-right corner. The firmware
aligns the last timer wake before 06:00 to that deadline and NTP-corrects its
retained clock daily. A failed daily NTP request is not repeated every five
minutes; a clockless first boot backs off for 12 wake cycles. On first
installation it takes one useful reading immediately; if that happens before
06:00 it still performs the scheduled reading later that morning.
Implausible or disconnected-battery readings count as that day's check, so a
missing pack does not wake the ADC repeatedly; the last valid estimate remains
cached.

The Pi remains responsible for rendering, rotating, resizing, dithering, and
encoding the base artwork. Firmware downloads into a second PSRAM buffer and
verifies HTTP metadata, exact length, SHA-256, and every color nibble. It then
copies the verified base into `epaper.getPointer()`, adds only the exact-palette
battery signature, validates the finished buffer again, and calls
`epaper.update()`. Any network, authentication, allocation, or validation
failure leaves the physical e-paper image unchanged.

## Configure and build

Install PlatformIO Core or use the PlatformIO IDE extension, then provision a
local secrets file:

```bash
cd firmware/esp32-ee02
cp include/secrets.example.h include/secrets.h
```

Edit `include/secrets.h` with the Wi-Fi credentials and Pi URL. A bearer token
is optional: omit `EINK_FRAME_AUTH_TOKEN` for a public, read-only frame endpoint
on a trusted LAN, or set it to the Pi's token when authentication is enabled.
Do not commit that file. The Pi URL must use plain HTTP because the built-in
runtime server does not terminate TLS. Numeric addresses, router DNS names,
and `.local` mDNS names are supported.

Build, upload, and monitor (replace the port if macOS assigned another one):

```bash
pio run
pio run --target upload --upload-port /dev/cu.usbmodem101
pio device monitor --port /dev/cu.usbmodem101
```

The project pins PlatformIO's ESP32 platform and Seeed_GFX V3.1.0 by immutable
commit. The global build flags select Seeed Setup510 and the EE02 pin mapping.
Do not change to a generic ESP32-S3 target: the 960,000-byte Seeed sprite plus
the validation staging frame require the XIAO board's OPI PSRAM configuration.
The repository's `ESP32 EE02 firmware` workflow performs a clean build with
PlatformIO Core 6.1.19 for this exact production environment on every firmware
pull request. Compilation and the network contract are verified; a physical
EE02 refresh still needs to be checked when the hardware is available.

## Battery connection

The EE02 v1 already contains a switched 1:1 battery divider. `BAT_ADC` is
GPIO1/D0/A0 and its low-leakage enable is GPIO6/D5; no external voltage divider
or fuel-gauge board is required. Do not read the XIAO Plus `ADC_BAT` signal on
GPIO10 because the EE02 uses GPIO10 for the display data/command line.

Use a protected one-cell 3.7 V rechargeable Li-ion/LiPo pack with the EE02's
2-pin JST 2.0 connector. Confirm connector polarity before plugging it in—the
same shell is sold with both wire orders. The board's BQ24070 charger targets a
4.2 V cell and is configured for roughly 297 mA, so the pack must be suitable
for that charge rate. Use the EE02 battery power switch for battery operation.
See Seeed's [EE02 hardware guide](https://wiki.seeedstudio.com/getting_started_with_ee02/)
and [official schematic](https://files.seeedstudio.com/wiki/Epaper/EE02/202000224_XIAO_ePaper_Display_Board_EE02_V1.pdf).

This is a voltage-based estimate, not coulomb counting. Chemistry, temperature,
cell age, load sag, and charging all affect it; USB power can make the estimate
look fuller while the cell is charging. The generic curve is isolated in
`include/battery_monitor.h` so it can be replaced with the selected cell's
datasheet curve.

## Operation

The flow is intentionally small:

1. Power-up, reset, firmware upload, the five-minute timer, or any user button
   wakes the ESP32. Button 1 selects weather, the middle button selects birds,
   and button 3 selects the star map.
2. On the first wake at 06:00, GPIO6 briefly enables the EE02 divider. The
   firmware takes a median of 25 calibrated ADC readings on GPIO1, doubles the
   divider voltage, estimates a percentage, and switches the divider off.
3. The board joins Wi-Fi, performs the daily NTP correction when due, and
   checks the configured frame.
4. HTTP 304 or a matching SHA-256 skips the slow panel refresh when both the
   base frame and physically shown battery percentage are unchanged.
5. A changed base or battery percentage triggers a verified full pull and
   refresh with the cached handwritten signature.
6. The ESP32 turns Wi-Fi off and deep-sleeps until the next timer or button
   wake.

The default timer/reset frame channel is `active`. Change `EINK_FRAME_MODE` in
`include/device_config.h` only if those non-button wakes should follow one
concrete server frame regardless of the phone-control selection. GPIO 2, 3,
and 5 are the EE02's active-low user buttons and directly request `weather`,
`birds`, and `star-map` in that order. A button selection is a one-shot request:
the next timer wake returns to `active` and the normal automatic/manual web
selection. Change `EINK_CHECK_INTERVAL_SECONDS` to adjust the default
300-second timer. The default POSIX timezone is America/Denver
(`MST7MDT,M3.2.0,M11.1.0`); override `EINK_TIMEZONE` if the frame moves.

Successful ETag, mode, and SHA-256 state are stored in ESP32 NVS and survive
deep sleep; the e-paper preserves its image without power. Before touching the
panel, firmware commits an invalid NVS marker and only marks the new checksum
valid after the refresh and state writes complete. A reset during an update
therefore causes a safe unconditional download on the next wake.

A separate NVS record stores the latest battery millivolts, percentage, and
local sample date. The physically shown percentage is part of the display
commit record. If the daily percentage changes while the Pi base checksum does
not, firmware deliberately omits `If-None-Match`, pulls the full base, and
rebuilds the signed physical frame. A failed pull retries that cached reading
on the next wake without sampling the ADC again.

A network, server, or validation error leaves the old image intact and returns
the board to sleep. Press a button to try again.

The server uses HTTP. Keep it on a trusted LAN and never expose port 8787 to
the public internet.

## Exact display contract

- 1200×1600 native backing order, even x in the high nibble.
- Exactly 960,000 bytes with no header.
- Setup510 values: white `0x0`, green `0x2`, red `0x6`, yellow `0xB`, blue
  `0xD`, black `0xF`.
- `seeed-ee02-t133a01-4bpp-v1` and
  `application/vnd.seeed.ee02-4bpp` response identifiers.
- The base artwork is already rotated by the Pi. Firmware uses Seeed sprite
  rotation 1 only while drawing the bottom-right battery mark, then restores
  native rotation 0 before the panel update.
- Satisfy handwriting, approximately 9 pt physically, transparent background,
  exact black over white/yellow or white over the darker four inks.

See the main [runtime documentation](../../display_runtime/README.md) for the
server endpoints, last-known-good guarantees, and Raspberry Pi service setup.
Seeed's [EE02 guide](https://wiki.seeedstudio.com/getting_started_with_ee02/),
[e-paper Arduino guide](https://wiki.seeedstudio.com/epaper_work_with_arduino/),
and PlatformIO's [XIAO ESP32S3 board
definition](https://docs.platformio.org/en/stable/boards/espressif32/seeed_xiao_esp32s3.html)
are the hardware references for this target.

## Troubleshooting without a display refresh

- `Configuration error`: finish every placeholder in `include/secrets.h`; an
  intentionally open Wi-Fi network may use an empty password.
- `PSRAM` or `framebuffer allocation failed`: confirm the XIAO ESP32S3 target
  and the `qio_opi` PSRAM configuration rather than changing boards.
- `NVS is required`: reboot once; if it repeats, erase/reflash the ESP32's NVS
  partition before provisioning the secrets again.
- `HTTP 401`: copy the complete current Pi token again after any token rotation.
- `HTTP 404`: render the selected concrete mode on the Pi at least once.
- `HTTP 503` from `active`: repair invalid control settings or the selected
  mode's committed artifact; the existing panel image remains unchanged.
- `Battery ADC reading is not plausible`: confirm a charged 1-cell pack,
  connector polarity, and the EE02 battery switch. The last valid estimate is
  retained; with no valid estimate the signature stays hidden.
- A noticeably inaccurate charge number while USB is connected: unplug USB,
  let the cell rest, then wait for the next 06:00 sample. Fit
  `battery_monitor.h` to the chosen cell if tighter accuracy is required.
- A replacement or externally cleared panel: erase the board's NVS or change
  the frame once so the stored ETag cannot suppress the first refresh.
