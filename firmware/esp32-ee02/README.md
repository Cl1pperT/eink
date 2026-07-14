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
server does not terminate TLS. Prefer a DHCP-reserved numeric address or a
hostname supplied by your router; this skeleton does not assume `.local` mDNS.

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
The repository's `ESP32 EE02 firmware` workflow performs a clean build with
PlatformIO Core 6.1.19 for this exact production environment on every firmware
pull request. Compilation and the network contract are verified; a physical
EE02 refresh still needs to be checked when the hardware is available.

## Operation

The default is unattended `automatic` mode. The ESP32 synchronizes time over
NTP and selects weather from 06:00, birds from 10:00, and the star map from
20:00 through the following morning. `device_config.h` controls the POSIX
timezone, boundaries, default, and poll interval; keep them aligned with the
Pi's TOML timezone/schedule and systemd timers. If NTP is temporarily
unavailable, the client retains the last displayed concrete mode instead of
guessing a new one. The supplied Pi timers render each target five minutes
before these boundaries so the new atomic frame is normally committed first.

On each poll the client sends `Authorization: Bearer ...` and, when
it has a known displayed checksum, `If-None-Match`. An HTTP 304 skips the slow
panel refresh. Successful ETag, mode, and SHA-256 state are stored in ESP32 NVS
and survive reboot; the e-paper itself preserves the corresponding image
without power. Before touching the panel, firmware commits an invalid NVS
marker; it marks the new checksum valid only after the refresh and all state
writes complete. A reset at any intermediate point therefore forces a safe
unconditional download on the next boot. If the panel is replaced or cleared outside this firmware, send
`r` once so a persisted ETag cannot suppress its first refresh. A forced
refresh remains pending across network or server failures and retries every 30
seconds until a verified frame reaches the panel or another mode is selected.

Serial commands provide manual control before physical buttons are wired:

- `a`: resume the automatic schedule
- `w`: weather
- `b`: birds
- `s`: star map
- `p`: uploaded photo
- `t`: test pattern
- `r`: force a full download and refresh

The Pi must have already rendered each concrete mode. `automatic` is local
firmware scheduling rather than an HTTP mode. A mode with no committed frame
returns an error and leaves the display alone.

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
- A replacement or externally cleared panel: send `r`; the request remains
  forced across temporary failures until a verified refresh succeeds.
