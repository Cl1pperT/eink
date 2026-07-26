# ESP32 EE02 button image updater

This PlatformIO/Arduino project targets a Seeed Studio XIAO ESP32S3 with PSRAM,
the XIAO ePaper Display Board EE02, and the 13.3-inch T133A01 Spectra 6 panel.
On power-up or a press of any of the three user buttons, it checks the
headless runtime's authenticated `uploaded-photo` frame endpoint. It refreshes
the panel only when that frame has changed, then returns to deep sleep.

The firmware deliberately does not render, rotate, resize, dither, or
re-encode. It downloads into a second PSRAM buffer, verifies HTTP metadata,
exact length, SHA-256, and every color nibble, and only then copies into
`epaper.getPointer()` and calls `epaper.update()`. Any network, authentication,
allocation, or validation failure leaves the physical e-paper image unchanged.

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
Do not commit that file. The Pi URL must be plain
`http://<pi-address>:8787` because the built-in runtime server does not
terminate TLS.

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

## Operation

The flow is intentionally small:

1. Power-up, reset, firmware upload, or any user button wakes the ESP32.
2. The board joins Wi-Fi and checks the configured frame.
3. HTTP 304 or a matching SHA-256 skips the slow panel refresh.
4. A verified new frame refreshes the display.
5. The ESP32 turns Wi-Fi off and deep-sleeps until the next button press.

The default frame is `uploaded-photo`. Change `EINK_FRAME_MODE` in
`include/device_config.h` if another single concrete server frame is desired.
GPIO 2, 3, and 5 are the EE02's active-low user buttons. All three currently
perform the same check.

Successful ETag, mode, and SHA-256 state are stored in ESP32 NVS and survive
deep sleep; the e-paper preserves its image without power. Before touching the
panel, firmware commits an invalid NVS marker and only marks the new checksum
valid after the refresh and state writes complete. A reset during an update
therefore causes a safe unconditional download on the next wake.

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
- No firmware rotation. A landscape render is already rotated by the Pi.

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
- `HTTP 404`: render that concrete mode on the Pi at least once.
- A replacement or externally cleared panel: erase the board's NVS or change
  the frame once so the stored ETag cannot suppress the first refresh.
