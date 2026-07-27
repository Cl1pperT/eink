# Headless Display Runtime

`display_runtime` is the Raspberry Pi-facing renderer for the frame. It uses
the same weather, AvianVisitors, inkystarmap, sizing, and Spectra 6 conversion
code as the desktop simulator, but it never imports Tkinter or opens a window.

The runtime produces both a validated six-color PNG and the exact raw Seeed
EE02/T133A01 4bpp backing buffer, then atomically maintains one last-known-good
frame per mode. Its authenticated HTTP pull service exposes only committed
manifests and verified EE02 payloads; it never renders in an HTTP request.

## Raspberry Pi installation

The supported production target is a Raspberry Pi 5 running 64-bit Raspberry
Pi OS Bookworm (or newer) with systemd and Python 3.11 or newer. From a checkout
that is not `/opt/eink-display`, run:

```bash
sudo ./deploy/install-raspberry-pi.sh
```

The installer creates a locked-down `eink-display` system account, builds a
fresh virtual environment, installs live integrations and shared headless
Chromium, verifies the package, renders an isolated 960,000-byte test frame,
and installs the authenticated server and daily render timers. Managed paths
are:

```text
/opt/eink-display/                  application, venv, browser
/etc/eink-display/runtime.toml      operator configuration
/etc/eink-display/frame-server.token
/etc/eink-display/control-panel.token
/var/lib/eink-display/              atomic frames, cache, uploads, phone settings
```

Configuration, token, frame state, and external repositories are preserved on
a normal reinstall. Use `--rotate-token` or `--force-config` only deliberately;
the latter first creates a timestamped backup. Useful constrained installs are
`--core-only`, `--skip-browser`, `--no-start`, and `--no-enable`. Run
`./deploy/install-raspberry-pi.sh --help` for layout and staging options.
Upgrades activate a fresh venv only after smoke tests, then roll back both the
previous venv and unit files if final verification or service startup fails.

Edit the installed configuration and point it at separately managed source
checkouts:

```bash
sudoedit /etc/eink-display/runtime.toml
sudo systemctl restart eink-display-server.service
sudo systemctl start eink-display-render@test-pattern.service
```

Use absolute repository paths that the service account can read.
`repositories.avian_weather` is shared by weather and birds. It must point
directly at the checkout containing both `weather_frame/renderer.py` and
`frame/shoot.py`; regional bird rendering also uses
`frame/birdweather.py`. `repositories.inkystarmap` must point directly at the
checkout containing `src/inkystarmap/inkystarmap.py`. If either external repo
adds Python requirements beyond this package's `integrations` extra, install
them into `/opt/eink-display/.venv` and rerun `eink-display check` as the
service account.

The activity editor also loads the sibling `season` catalog. Keep that checkout
beside AvianVisitors—for example `/opt/eink/AvianVisitors` and
`/opt/eink/season`—so the control service can expose the complete activity
list. It fails clearly at startup if the catalog is unavailable rather than
showing an incomplete editor.

Running the CLI directly from the full development checkout is more convenient:
blank repository fields automatically discover `peacock/AvianVisitors` and
`stars/integrations/inkystarmap` relative to the source tree. Explicit TOML
paths override discovery; when a field is blank, `WEATHER_FRAME_REPO`,
`AVIANVISITORS_REPO`, or `INKYSTARMAP_REPO` can override the matching source.
For strict runtime checks, a nonempty environment override is authoritative and
must point directly to a checkout containing the expected marker; an invalid
value fails instead of silently selecting another checkout.
The root repository locally ignores `peacock/` and `stars/`, and the Pi
installer packages neither directory. Do not depend on development discovery
for the installed systemd service: maintain its checkouts separately and keep
their readable absolute paths in `/etc/eink-display/runtime.toml`.

Leave `sources.bird` blank for the default keyless BirdWeather integration.
The validated phone settings provide its postal code, country, lookback, and
artwork labels, while the real AvianVisitors frontend composes the local bird
illustrations. The result represents nearby regional reports, not detections at
the frame. An explicit PNG or AvianVisitors collage URL remains supported for a
separately operated microphone/BirdNET installation.

Check services, logs, and timers with:

```bash
systemctl status eink-display-server.service
systemctl status eink-display-control.service
systemctl list-timers 'eink-display-*'
journalctl -u eink-display-server.service
journalctl -u 'eink-display-render@star-map.service'
```

The installer also starts a phone-friendly configuration site on port 8765.
Read its short access code with
`sudo cat /etc/eink-display/control-panel.token`, then open
`http://<pi-hostname-or-address>:8765/?token=<code>` on a phone on the same
trusted LAN. The page removes the token from its address after storing it in
that browser. It writes settings, optimized uploads, caches, and committed
frames only below `/var/lib/eink-display`; it cannot rewrite the root-owned
TOML or application. Authenticated render-now actions and photo uploads use the
same shared scheduler lock as timer jobs, and the page reports completion or
failure without blocking an HTTP request. Render jobs reload the overlay each
time. Do not expose port 8765 to the public internet.

The installed timers render weather at 05:55, birds at 09:55, and the star map
at 19:55 in the Pi's local timezone, which should match `location.timezone`.
Each render starts five minutes before the ESP32's 06:00, 10:00, and 20:00
display boundary so a newly committed frame is normally ready before the mode
changes, avoiding an unnecessary refresh of yesterday's frame.
They are persistent across outages and share a lock—with phone-triggered jobs
using that same lock—so missed or concurrent renders cannot exhaust Pi memory.
Failed source renders retry at a bounded interval while the previous committed
frame remains available to the server.
These `OnCalendar` values are independent of the TOML automatic-mode schedule;
use `systemctl edit eink-display-weather.timer` (and the corresponding bird or
star timer) if you change production render times.

Provision the ESP32 with the complete token stored at
`/etc/eink-display/frame-server.token`; the installer never prints it. Treat it
like a password. `sudo /opt/eink-display/install-raspberry-pi.sh --uninstall`
removes the application and units while preserving configuration and frames;
add `--purge` only when those retained files should also be deleted.

For development or an unsupported host, the manual equivalent is still:

```bash
python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -e '.[integrations]'
PLAYWRIGHT_BROWSERS_PATH="$PWD/playwright" .venv/bin/playwright install chromium
cp display_runtime/config.example.toml runtime.toml
```

The optional integration dependencies are needed for live bird-page and star
map rendering. Test-pattern, uploaded-photo, and core conversion only require
Pillow.

## CLI

The module command works from a checkout:

```bash
python3 -m display_runtime check
python3 -m display_runtime mode --at 2026-07-11T21:00:00-06:00
python3 -m display_runtime render test-pattern
python3 -m display_runtime render weather
python3 -m display_runtime render automatic
python3 -m display_runtime status
DISPLAY_RUNTIME_AUTH_TOKEN='<a-long-random-token>' \
  python3 -m display_runtime serve
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
- `--landscape-rotation clockwise|counter-clockwise` to match the display's
  physical mounting.
- `--output-dir`, `--no-rgb`, and `--force` artifact overrides.
- `--json` for a stable machine-readable result.
- `--allow-demo` for a manual development render only.

Automatic mode uses the configured local timezone and schedule and never
allows fixture, demo, sample, or synthetic fallbacks. That prevents a failed
weather request, BirdWeather request, browser, or astronomy dependency from
replacing a valid physical frame with convincing-looking fake data.

## HTTP frame server

By default, an empty `server.auth_token` provides public read-only access to
frames on the LAN. To require authentication, create a bearer token and
provide the same value to the Pi service and ESP32:

```bash
export DISPLAY_RUNTIME_AUTH_TOKEN='<a-long-random-token>'
python3 -m display_runtime serve
```

`serve` reads `server.host`, `server.port`, `server.chunk_size`,
`server.max_connections`, and `server.request_timeout` from the configuration.
The bounded connection pool and timeout keep slow clients from exhausting the
Pi. `--host`, `--port`, and `--output-dir` override their matching values.
For a service manager, a service-account-readable token file is also supported:

```bash
python3 -m display_runtime serve \
  --token-file /etc/eink-display/frame-server.token
```

When used, the file must contain a single-line token. Restrict it to the service
account (the installer uses root/service-group mode `0640`). Credentials are
never accepted in a URL or query string.

The version 1 pull API supports `GET` and `HEAD`:

| Endpoint | Authentication | Result |
| --- | --- | --- |
| `/v1/health` | None | Minimal non-cached liveness response |
| `/v1/manifest/<mode>` | Bearer token | Atomically committed `current.json` |
| `/v1/frame/<mode>` | Bearer token | EE02 payload referenced by the current manifest |
| `/v1/frame/<mode>/<sha256>` | Bearer token | Immutable, content-addressed EE02 payload |

`active` is a virtual `<mode>` for the current manifest and frame endpoints.
It resolves on every request from validated phone settings: a manual selection
maps directly to its concrete artifact, while `automatic` uses the configured
timezone and schedule. Responses include `X-Resolved-Mode`. Invalid settings
or resolver failures receive `503`, and a selected mode with no committed
artifact receives `404`, so the ESP retains its current image.

Send the token as `Authorization: Bearer <token>`. Missing or incorrect
credentials receive `401`; an unknown mode or absent committed frame receives
`404`; and a malformed manifest or missing/corrupt current payload receives
`503`. The concrete modes are `weather`, `birds`, `star-map`,
`uploaded-photo`, and `test-pattern`; `active` is the virtual ESP channel.

Manifest ETags identify the exact JSON representation. Frame ETags identify
the EE02 SHA-256. Responses also carry the frame checksum and wire-format
headers. A client can send `If-None-Match` and receives `304 Not Modified` when
its entity is current. `Cache-Control: no-cache` means caches may retain a
response but must revalidate it. General clients can fetch the manifest first
and then request `/v1/frame/<mode>/<sha256>`. The constrained ESP32 firmware
uses `/v1/frame/<mode>` directly; that endpoint validates one current manifest
and opens its exact immutable payload before sending headers, so a concurrent
render cannot mix metadata and bytes.

Before any frame is served, the Pi verifies its exact length, SHA-256, and all
1,920,000 packed color nibbles. A checksum-valid artifact containing a color
outside Setup510's six codes is treated as unavailable and receives `503`.

The built-in server is plain HTTP. A bearer token prevents unauthenticated
access but does not encrypt the token or frame in transit. Use it only on a
trusted LAN, restrict the Pi firewall to the ESP32's address, and do not expose
port 8787 to the public internet. For an untrusted network, bind to
`127.0.0.1` and put a TLS reverse proxy or a VPN in front of it.

## Simulated ESP client

`esp-sync` exercises the same validation, caching, last-known-good, and refresh
decisions as the firmware. It deliberately uses the more inspectable
manifest-first form of the protocol, while the memory-constrained firmware
uses the equivalent current-frame endpoint. Start with a committed frame and
a running server, then use a second terminal:

```bash
export DISPLAY_RUNTIME_AUTH_TOKEN='<the-same-token>'
python3 -m display_runtime esp-sync weather
python3 -m display_runtime esp-sync weather --json
```

Use `--server-url http://<pi-address>:8787` when the simulator is not running
on the Pi. `--state-dir`, `--timeout`, and `--token-file` override the matching
configuration or credential source. `esp-sync` requires a concrete mode and
does not run a clock. The production firmware performs its own NTP-backed
`automatic` selection using the same configured boundaries.

The simulated client stores verified, checksum-named payloads under
`esp_client.state_directory`, plus atomic `state.json` and `display.ee02`
files. It revalidates the manifest ETag, verifies the declared format, exact
960,000-byte length, response headers, SHA-256, and every packed color nibble
while downloading to a temporary file, and only then atomically activates the frame. Switching back
to a previously verified mode activates its cache without downloading it
again. A timeout, authentication error, malformed response, truncation, or
checksum mismatch leaves `display.ee02` and persistent client state unchanged.
The result's `changed` value tells firmware whether a physical refresh is
needed. `status` separately reports whether the client received `not-modified`,
used a verified cache, or downloaded bytes; rebuilding a damaged local cache
can therefore report `downloaded` while correctly leaving `changed` false.

## Artifacts and last-known-good behavior

Each mode owns an independent directory:

```text
frames/
└── weather/
    ├── .render.lock
    ├── current.json
    └── frames/
        ├── <pixel-checksum>.png
        ├── <pixel-checksum>.rgb.png
        └── <wire-checksum>.ee02
```

The native PNG is exactly 1600×1200 in landscape or 1200×1600 in portrait and
contains only the six supported RGB values. The `.ee02` file is always exactly
960,000 bytes: the 1200×1600 Setup510 backing sprite at four bits per pixel.
`current.json` records dimensions, palette, pixel and wire checksums, all file
hashes, provenance, rotation, render time, and timings.

Frame files are immutable and named by their pixel checksum. A same-directory
temporary file is flushed and atomically renamed before `current.json` becomes
the new commit point. A render, validation, or disk-write failure therefore
leaves the previous manifest and frame readable. Per-mode file locking prevents
two service invocations from committing the same mode concurrently.

Persistent change detection compares the final EE02 payload SHA-256, not merely
the PNG pixels. Restarting the process retains unchanged detection, and changing
the landscape rotation correctly creates a new physical frame even when the
logical PNG is unchanged. A missing or corrupt cached binary is rebuilt without
falsely reporting that the display pixels changed.

The optional RGB sidecar is the normalized continuous-color source before
Spectra conversion. It is useful for diagnostics but is not sent to hardware.

## Configuration and source policy

Packaged defaults are sufficient for a test-pattern render. The CLI otherwise
loads `$DISPLAY_RUNTIME_CONFIG`, or
`~/.config/eink-display/runtime.toml` when it exists. Relative paths in a user
configuration are resolved from that configuration file's directory. Unknown
keys, invalid coordinates, timezones, schedule ordering, and conversion values
are rejected instead of silently ignored.

In a full source checkout, leaving repository fields blank enables automatic
discovery of `peacock/AvianVisitors` and
`stars/integrations/inkystarmap`. Explicit configuration and matching
repository environment variables remain overrides. An installed wheel does not
contain those ignored local checkouts, so production configuration should use
absolute repository paths.

`runtime.strict_sources = true` is the production default:

- Weather requires a configured or discovered AvianVisitors checkout and real
  provider.
- Blank `sources.bird` requires AvianVisitors' BirdWeather and capture adapters;
  an explicit bird URL still requires AvianVisitors. Neither path falls back to
  fixture species after a production capture failure.
- A live star map requires a configured or discovered inkystarmap checkout and
  Starplot.
- Explicit bird, star-map, and uploaded-photo files are allowed and recorded
  with `file` provenance.
- Test pattern is always an explicit manual mode and is never scheduled.

Run `check` to see configuration and dependency readiness without making a
weather request or rendering a frame.

## Exact EE02 buffer contract

The encoder follows Seeed_GFX's EE02 Setup510 and 4bpp `TFT_eSprite` layout:

- Backing dimensions: 1200×1600, row-major.
- Buffer length: 960,000 bytes, with no header.
- Even backing `x`: high nibble; odd backing `x`: low nibble.
- Driver values: black `0xF`, white `0x0`, yellow `0xB`, red `0x6`, blue
  `0xD`, green `0x2`.
- Clockwise landscape maps logical `(x,y)` to backing `(1199-y,x)`, matching
  Seeed sprite rotation 1.
- Counter-clockwise maps `(x,y)` to `(y,1599-x)`, matching rotation 3.
- Portrait 1200×1600 is already in backing order and uses no rotation.

These bytes are sprite-buffer values. Firmware should copy a completely
downloaded and SHA-256-verified payload into `epaper.getPointer()` and then call
`epaper.update()`. It must not rotate the already prepared payload again. The
Seeed driver performs its own later conversion from sprite nibbles to panel
controller transfer codes.

The compiling PlatformIO/Arduino implementation is in
[`firmware/esp32-ee02`](../firmware/esp32-ee02/). It targets the XIAO ESP32S3,
EE02 board, and Setup510 panel, and implements bearer authentication, ETag
revalidation, PSRAM staging, exact response validation, and persistent display
checksums.

## Exit codes

- `0`: successful command, including an unchanged render or ESP sync.
- `2`: invalid or unreadable configuration.
- `3`: missing/rejected source or render failure.
- `4`: runtime artifact I/O failure.
- `5`: ESP sync network, authentication, or protocol failure.
- `1`: unexpected CLI failure.
