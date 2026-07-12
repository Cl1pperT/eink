# Headless Display Runtime

`display_runtime` is the Raspberry Pi-facing renderer for the frame. It uses
the same weather, AvianVisitors, inkystarmap, sizing, and Spectra 6 conversion
code as the desktop simulator, but it never imports Tkinter or opens a window.

The first milestone produces validated six-color PNG artifacts and atomically
maintains one last-known-good frame per mode. It does not yet expose an HTTP
endpoint or encode the Seeed EE02 4bpp wire buffer.

## Raspberry Pi installation

Use 64-bit Raspberry Pi OS and Python 3.11 or newer:

```bash
git clone <this-repository> /opt/eink/frame-runtime
cd /opt/eink/frame-runtime
python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -e '.[integrations]'
sudo .venv/bin/playwright install-deps chromium
.venv/bin/playwright install chromium
```

The optional integration dependencies are needed for live bird-page and star
map rendering. Test-pattern, uploaded-photo, and core conversion only require
Pillow.

Copy and edit the example configuration:

```bash
mkdir -p ~/.config/eink-display
cp display_runtime/config.example.toml ~/.config/eink-display/runtime.toml
```

Use absolute repository paths on the Pi. `repositories.avian_weather` is shared
by weather and birds. It must point directly at the checkout containing both
`weather_frame/renderer.py` and `frame/shoot.py`. `repositories.inkystarmap`
must point directly at the checkout containing
`src/inkystarmap/inkystarmap.py`.

`sources.bird` must be the actual AvianVisitors collage page—the page containing
the `.gtile` bird tiles—not merely the stock BirdNET-Pi dashboard. If the
BirdNET host only serves its standard dashboard, host/install the AvianVisitors
frontend there or provide a pre-rendered bird PNG until that integration is
available.

## CLI

The module command works from a checkout:

```bash
python3 -m display_runtime check
python3 -m display_runtime mode --at 2026-07-11T21:00:00-06:00
python3 -m display_runtime render test-pattern
python3 -m display_runtime render weather
python3 -m display_runtime render automatic
python3 -m display_runtime status
```

After installing the package, the equivalent console command is
`eink-display`:

```bash
eink-display --config ~/.config/eink-display/runtime.toml render birds --json
```

The `render` command accepts:

- `automatic`, `weather`, `birds`, `star-map`, `uploaded-photo`, or
  `test-pattern`.
- `--at`/`--when` with an ISO-8601 time. A time without an offset is interpreted
  in `location.timezone`.
- `--orientation landscape|portrait` and `--fit crop|fit|stretch` overrides.
- `--output-dir`, `--no-rgb`, and `--force` artifact overrides.
- `--json` for a stable machine-readable result.
- `--allow-demo` for a manual development render only.

Automatic mode uses the configured local timezone and schedule and never
allows fixture, demo, sample, or synthetic fallbacks. That prevents a failed
weather request, BirdNET host, browser, or astronomy dependency from replacing
a valid physical frame with convincing-looking fake data.

## Artifacts and last-known-good behavior

Each mode owns an independent directory:

```text
frames/
└── weather/
    ├── .render.lock
    ├── current.json
    └── frames/
        ├── <pixel-checksum>.png
        └── <pixel-checksum>.rgb.png
```

The native PNG is exactly 1600×1200 in landscape or 1200×1600 in portrait and
contains only the six supported RGB values. `current.json` records dimensions,
palette, pixel checksum, PNG file hashes, provenance, render time, and timings.

Frame files are immutable and named by their pixel checksum. A same-directory
temporary file is flushed and atomically renamed before `current.json` becomes
the new commit point. A render, validation, or disk-write failure therefore
leaves the previous manifest and frame readable. Per-mode file locking prevents
two service invocations from committing the same mode concurrently.

The optional RGB sidecar is the normalized continuous-color source before
Spectra conversion. It is useful for diagnostics but is not sent to hardware.

## Configuration and source policy

Packaged defaults are sufficient for a test-pattern render. The CLI otherwise
loads `$DISPLAY_RUNTIME_CONFIG`, or
`~/.config/eink-display/runtime.toml` when it exists. Relative paths in a user
configuration are resolved from that configuration file's directory. Unknown
keys, invalid coordinates, timezones, schedule ordering, and conversion values
are rejected instead of silently ignored.

`runtime.strict_sources = true` is the production default:

- Weather requires the configured AvianVisitors checkout and real provider.
- A bird URL requires the configured AvianVisitors checkout and never falls
  back to fixture species after a capture failure.
- A live star map requires the configured inkystarmap checkout and Starplot.
- Explicit bird, star-map, and uploaded-photo files are allowed and recorded
  with `file` provenance.
- Test pattern is always an explicit manual mode and is never scheduled.

Run `check` to see configuration and dependency readiness without making a
weather request or rendering a frame.

## Exit codes

- `0`: successful render, including an unchanged frame.
- `2`: invalid or unreadable configuration.
- `3`: missing/rejected source or render failure.
- `4`: runtime artifact I/O failure.
- `1`: unexpected CLI failure.

## Next production layer

The runtime deliberately stops at a validated native PNG. The next package
layer will rotate and encode that image into the EE02's 960,000-byte 4bpp
buffer, publish manifest/frame HTTP endpoints, and provide systemd units. The
ESP32 downloader can then validate the payload hash and refresh only when the
wire checksum changes.
