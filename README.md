# E-Ink Frame

## Headless Raspberry Pi Runtime

The Tk-free `display_runtime` package renders weather, birds, star maps,
uploaded photos, automatic schedules, and test patterns from a terminal or
service. It validates native dimensions and the six-color palette, writes
checksum-named immutable PNG and exact 960,000-byte EE02 4bpp frames, and
atomically maintains a last-known-good manifest for each mode. Persistent
change detection uses the final hardware payload checksum. An authenticated
HTTP server publishes only committed artifacts, and the simulated ESP client
verifies and atomically activates the exact bytes that firmware should pull.

```bash
sudo ./deploy/install-raspberry-pi.sh
sudoedit /etc/eink-display/runtime.toml
sudo systemctl start eink-display-render@test-pattern.service
systemctl status eink-display-server.service
```

Production source rendering fails closed by default: live-source failures never
publish demo content over an existing valid frame. See
[display_runtime/README.md](display_runtime/README.md) for Raspberry Pi setup,
TOML configuration, CLI commands, the authenticated frame API, ETag behavior,
and the ESP32 pull/verification contract.

## ESP32 display firmware

[`firmware/esp32-ee02`](firmware/esp32-ee02/) is a compiling
PlatformIO/Arduino client for the Seeed XIAO ESP32S3, EE02 driver board, and
13.3-inch T133A01 Spectra 6 panel. It authenticates to the Pi, revalidates with
ETags, downloads into PSRAM, verifies the exact 960,000-byte payload and
SHA-256, and refreshes through pinned Seeed_GFX Setup510 only after validation.
The Pi performs landscape rotation; the ESP32 copies native backing bytes
without another transform. NTP-backed automatic mode switches between the
morning weather, daytime birds, and nighttime star map schedules.

```bash
cd firmware/esp32-ee02
cp include/secrets.example.h include/secrets.h
pio run
```

## Desktop Display Simulator

The macOS-compatible Tkinter simulator integrates the real
[Twarner491/AvianVisitors](https://github.com/Twarner491/AvianVisitors) daytime
collage, the morning weather renderer from
[Cl1pperT/AvianVisitors](https://github.com/Cl1pperT/AvianVisitors), and the
[inkystarmap](https://github.com/Marcel-Jan/inkystarmap) Starplot recipe. It also
hosts an optional LAN photo-upload page. Every source passes through the weather
fork's EL133UF1-compatible Spectra 6 conversion pipeline.

```bash
python3 -m pip install -r requirements-simulator.txt
python3 -m display_simulator
```

It generates 1600×1200 landscape or 1200×1600 portrait frames and never updates
physical hardware. See [display_simulator/README.md](display_simulator/README.md)
for setup, controls, offline mode, integrations, and limitations.
