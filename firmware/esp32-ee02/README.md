# ESP32 EE02 frame client

This PlatformIO/Arduino project targets a Seeed Studio XIAO ESP32S3 with PSRAM,
the XIAO ePaper Display Board EE02, and the 13.3-inch T133A01 Spectra 6 panel.
It pulls the headless runtime's authenticated frame endpoint and gives
Seeed_GFX the exact 960,000-byte 4bpp sprite buffer produced by the Pi.

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

Edit `include/secrets.h` with the Wi-Fi credentials, Pi URL, and the complete
token from `/etc/eink-display/frame-server.token`. Do not commit that file. The
Pi URL must be plain `http://<pi-address>:8787` because the built-in runtime
server does not terminate TLS.

Build, upload, and monitor:

```bash
pio run
pio run --target upload
pio device monitor
```

The project pins PlatformIO's ESP32 platform and Seeed_GFX V3.1.0 by immutable
commit. The global build flags select Seeed Setup510 and the EE02 pin mapping.
Do not change to a generic ESP32-S3 target: the 960,000-byte Seeed sprite plus
the validation staging frame require the XIAO board's OPI PSRAM configuration.

## Operation

The default mode is `weather`; `device_config.h` controls the default and poll
interval. On each poll the client sends `Authorization: Bearer ...` and, when
it has a known displayed checksum, `If-None-Match`. An HTTP 304 skips the slow
panel refresh. Successful ETag, mode, and SHA-256 state are stored in ESP32 NVS
and survive reboot; the e-paper itself preserves the corresponding image
without power.

Serial commands make the skeleton useful before physical buttons are wired:

- `w`: weather
- `b`: birds
- `s`: star map
- `p`: uploaded photo
- `t`: test pattern
- `r`: force a full download and refresh

The Pi must have already rendered the requested concrete mode. Scheduled
`automatic` selection is a Pi concern and is not an HTTP mode. A mode with no
committed frame returns an error and leaves the display alone.

Because the current Pi server is HTTP, the bearer credential is not encrypted
on the wire. Keep both devices on a trusted LAN, restrict port 8787 to the
ESP32 where practical, and never expose it to the public internet.

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
