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
without transforming the base art. It then adds a discreet, contrast-aware
handwritten battery estimate in the logical bottom-right corner. The EE02's
built-in battery divider is sampled on every wake; no external divider is
needed. The ESP32 first requests the Pi's virtual `active` manifest, downloads a
full frame only when artwork or the displayed charge changes, and otherwise
deep-sleeps until the Pi-advertised boundary or a physical button. Automatic
mode shows weather at 06:00, birds at 09:00, and the star map from local sunset
plus 30 minutes; the map itself remains charted for sunset plus 90 minutes.
Failed or invalid schedule responses retry after five minutes.

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

The live star display is a circular full-sky atlas calculated for 90 minutes
after local sunset. The selected north, east, south, or west direction is
rotated to the bottom of the chart. Most stars, grid lines, and constellation
geometry remain clean white; only the brightest stars receive catalogued B-V
blue, white, gold, or red color. One prominent constellation is selected
automatically and traced in gold. Palette-safe illustrated planets appear at
their live positions, while a right-hand planetarium guide shows the Moon,
sunset and sunrise, visible planets, a suggested object, compass, and color key.

When those projects are checked out at `peacock/AvianVisitors` and
`stars/integrations/inkystarmap`, the simulator and checkout-based headless CLI
discover them relative to this source tree. No repository selection in the UI
is required. A valid remembered UI/configuration path takes precedence; when it
is blank or stale, the corresponding repository environment variables override
co-located discovery. The two copied trees are locally ignored by this
repository, so they retain their own Git histories and are not included in
parent-repository commits or clones.

```bash
python3 -m pip install -r requirements-simulator.txt
python3 -m display_simulator
```

## Phone control panel

The same responsive control panel runs on macOS and Raspberry Pi. It selects
automatic/weather/birds/star-map/photo display modes, queues explicit renders,
manages locations and activity recommendations, and turns phone photo uploads
into color-managed, photo-tuned committed frames. Its Birds tab includes a
compact current-frame preview and a full responsive `/birds` gallery backed by
cached, regional BirdWeather reports and the local AvianVisitors illustrations.
Its Stars tab safely previews the latest committed sky frame and persists a
North, East, South, or West center direction for scheduled and manual renders.
**Save & render tonight's sky** saves that choice and creates a new committed
star frame. The read-only `/api/stars/summary` and `/api/stars/preview` routes
expose only manifest-validated committed metadata and artwork.
From the development checkout, start it with:

```bash
python3 -m display_control
```

Open the printed `http://<lan-address>:8765/` URL on a phone connected to the
same trusted network. The simulator's **Start Phone Control Panel** button runs
the same service and automatically previews a successful upload. A Raspberry
Pi installation enables `eink-display-control.service` on port 8765. No
pairing code, login, or control-panel token is required.

Selections are a validated, atomic overlay rather than source-code edits. On
the Pi they persist in `/var/lib/eink-display/control/settings.json`, separate
from the root-owned runtime configuration. Any device on the trusted LAN can
view the site and use its control API to change settings, upload images, start
demos, or queue renders. If multiple phones edit at the same time, the last
valid save wins, so refresh before making another change. Do not forward port
8765 from your router; it is intended for a trusted home LAN.

The Overview page and Stars tab also provide a diagnostic **Five-minute demo**
for Weather, Birds, Stars, or the uploaded Image. It selects the latest
committed artwork without rendering or changing the saved display mode. Press
the frame's physical button to fetch it immediately. At expiry, the next button
press or advertised demo-expiry wake resumes the saved manual mode or automatic
schedule. This override never enables fixture/demo artwork; it only selects an
already committed production frame.

BirdWeather is a regional view of reports from nearby stations. Without a
microphone attached to this frame it intentionally does not describe those
reports as visitors to the property or claim to provide local recordings.

The desktop simulator generates 1600×1200 landscape or 1200×1600 portrait
previews. On the Pi, control-panel actions commit hardware-ready frames that the
ESP32 picks up on its next scheduled or physical-button wake. See
[display_simulator/README.md](display_simulator/README.md) for desktop setup,
controls, offline mode, integrations, and limitations.

This source-tree discovery is a development convenience. The Raspberry Pi
installer does not copy these locally ignored checkouts; an installed service
continues to use separately managed repositories at readable absolute paths in
`/etc/eink-display/runtime.toml`.
