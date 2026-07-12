# Desktop Display Simulator

This package previews planned content for a 13.3-inch, 1600×1200 Seeed Studio
EE02 Spectra 6 frame. Its scheduled sources are adapters for three existing
projects:

| Period | Project | Simulator path |
| --- | --- | --- |
| Morning | [Cl1pperT/AvianVisitors](https://github.com/Cl1pperT/AvianVisitors) | `weather_frame.renderer` generated-scene/procedural artwork |
| Day | [Twarner491/AvianVisitors](https://github.com/Twarner491/AvianVisitors) | the real `frame/shoot.py` web-collage capture |
| Night | [Marcel-Jan/inkystarmap](https://github.com/Marcel-Jan/inkystarmap) | its Starplot horizon-map recipe, without hardware initialization |
| Manual | Built-in LAN upload page | uploaded Pillow-readable images |

The upstream bird repository is named **AvianVisitors** (plural). It is a
desktop-only tool: **it never imports display drivers, changes physical refresh
state, or updates physical hardware.**

## macOS installation and launch

Use Python 3.11 or newer with Tk support. The installer from
[python.org](https://www.python.org/downloads/macos/) includes Tk; Homebrew users
may need a Python/Tk combination appropriate to their Homebrew version.

```bash
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install -r requirements-simulator.txt
python3 -m display_simulator
```

If `_tkinter` cannot be imported, the launcher prints a focused error instead of
a traceback. Install a Python distribution that includes Tk and retry.

## Architecture

- `sources/` contains adapters implementing `render(context) -> RGB Image`.
- `schedule.py` chooses Weather, Birds, or Star Map for Automatic mode.
- `pipeline.py` owns sizing, fit/crop, Spectra 6 conversion, validation, and
  stable checksums. Sources never quantize independently.
- `controller.py` runs rendering in one worker and rejects stale results.
- `widgets/` and `app.py` implement the responsive Tk interface.
- `defaults.toml` contains launch defaults. A user config is optional.

The repository paths are intentionally configurable rather than vendored. This
avoids duplicating hundreds of bird illustrations, weather scenes, or
inkystarmap and lets each project update independently.

## Connect the real projects

Clone the projects wherever you keep source checkouts:

```bash
git clone --branch avian-visitors https://github.com/Cl1pperT/AvianVisitors.git AvianVisitors-weather
git clone https://github.com/Marcel-Jan/inkystarmap.git
```

Because the weather fork contains `avian/`, `frame/`, and `weather_frame/`, the
simulator uses one **AvianVisitors + Weather** repository selector for both the
morning and daytime modes (including the Twarner491 bird-viewer code it is based
on). Choose the Cl1pperT checkout there, then choose the
separate inkystarmap checkout. Paths can alternatively be provided through
`AVIANVISITORS_REPO`/`WEATHER_FRAME_REPO` and `INKYSTARMAP_REPO` environment
variables.

For live bird-page capture and Starplot rendering, install the optional desktop
dependencies:

```bash
python3 -m pip install -r requirements-integrations.txt
python3 -m playwright install chromium
```

Do not install inkystarmap's full Raspberry Pi package on the Mac solely for the
simulator: its module initializes `inky.auto()` at import time. The adapter uses
the same `HorizonPlot`, style, constellation, magnitude, planet, moon, and field-
of-view settings while keeping that hardware module out of the process.

The central conversion is ported from the weather fork's `weather_frame/eink.py`
and mirrors the Pimoroni EL133UF1 driver's saturation-interpolated matching
palette, selective blue bias, and Floyd–Steinberg assignment. It then maps the
indices to the weather project's monitor approximation of the physical inks.

The pipeline retains two native-resolution images. The **RGB source** is the
normalized continuous-colour input; the **native e-ink PNG** contains exactly
black, white, red, yellow, blue, and green pixels. The **monitor preview** is a
separately scaled copy. Its optional warmth, softness, and reduced saturation do
not modify either native image or saved output.

## Using the simulator

Choose a display mode and simulated time, then select **Generate / Refresh
Preview**. Automatic mode uses the configurable schedule (Weather 06:00,
Birds 10:00, Star Map 20:00 by default). Moving the time slider only updates the
mode label unless auto-render is enabled; auto-render is debounced and only
occurs across mode boundaries.

Landscape output is always 1600×1200 and portrait output 1200×1600. Photo
imports apply EXIF orientation and support crop-to-fill, fit-with-border, and
stretch. PNG, JPEG, WebP, and other Pillow-readable formats are supported. The
test pattern needs no external assets.

The simulated buttons default to Weather, Birds, and Star Map. Their mappings
can also select an uploaded image, Automatic mode, or refresh the current mode.
Diagnostics report the final checksum, timing, estimated RGB transfer size,
wake reason, and whether identical pixel data would justify a panel refresh.

## Weather, birds, and stars

With the weather checkout configured, fixture weather constructs a deterministic
`DailyForecast` and sends it through the real renderer and its local generated
scene/procedural fallback. Turning fixture weather off uses that project's
Open-Meteo provider. Without the checkout, fixture mode uses the small built-in
Pillow fallback and live mode reports a clear error.

Bird mode accepts either a completed PNG or an AvianVisitors page URL. When its
checkout is configured, URL capture invokes the project's actual
`frame/shoot.py` with a native-aspect viewport. Landscape capture restores the
frontend's wide packing bias and uses a denser bird-area budget so the collage
fills the 4:3 panel; portrait capture keeps the upstream frame defaults. Demo Birds uses illustrations
directly from `avian/assets/illustrations` and renders them through the original
local AvianVisitors frontend. If the default `birdnet.local` hostname is not
reachable, the simulator automatically uses that real-frontend fixture and
labels the result as demo data. Only when the checkout or browser is unavailable
does it use the lightweight synthetic fallback.

Bird captures default to the last seven days. Tile area is ranked by
`seven-day calls × local rarity weight`, where rarity is the inverse of the
species' lifetime calls-per-day since its first local detection. The weight is
bounded from 1× through 12×; this promotes repeatedly heard rare visitors while
preventing a single historical detection from overwhelming the collage. Hover
diagnostics on the browser viewer show calls, rarity weight, and final score.

Star Map accepts a pre-rendered PNG. With the checkout and Starplot dependencies
available, it applies the simulated location, date, time, and viewing direction
to inkystarmap's plotting recipe. Otherwise it generates a deterministic Pillow
chart. A repository selector may point either directly at inkystarmap or at a
parent such as `stars/` containing `integrations/inkystarmap`; the nested
checkout is detected automatically. When the checkout exists but Starplot is
missing, its checked-in sample image is shown with an explicit diagnostic. All
slow paths run in the rendering worker. Starplot downloads its astronomy
catalogs on the first live render and caches them for subsequent frames.

## LAN photo uploads

The Uploaded Photo section can start a tiny standard-library web server. It
listens at the configured host and port (`0.0.0.0:8765` by default), shows a
mobile-friendly upload form, limits request size, verifies the image with
Pillow, removes EXIF orientation, and atomically stores an RGB PNG. A successful
upload selects Uploaded Photo, records `Manual upload` as the wake reason, and
generates a preview automatically.

You can also run the page independently:

```bash
python3 -m display_simulator.upload_server --host 0.0.0.0 --port 8765
```

The page has no authentication and is intended only for a trusted home LAN.
Firewall or reverse-proxy authentication should be added before exposing it
beyond that network. In the future physical system, the Pi can watch the saved
`latest-upload.png` and publish the converted frame for the ESP32; this desktop
simulator intentionally stops before any hardware update.

## Configuration

Project defaults live in `display_simulator/defaults.toml`: repository paths,
coordinates, upload listener, orientation, location, schedule boundaries,
output directory, conversion settings, source paths, physical preview, button
mappings, and window size. The application launches without a user
configuration. `load_config(path)` can merge an optional TOML file over the
project defaults.

The last weather location, shared AvianVisitors/weather checkout, inkystarmap
checkout, bird URL or PNG, star-map PNG, and uploaded-photo path are remembered
automatically. On macOS they are stored in:

```text
~/Library/Application Support/EInk Display Simulator/preferences.json
```

Preferences are saved when a path is selected, when a render starts, and when
the application closes. **Reset Defaults** also replaces the remembered values.

## Testing

```bash
python3 -m unittest discover -s tests -v
```

Tests are headless and require no Tk window, API, browser, BirdNET-Pi, or panel.
The localhost upload test skips under sandboxes that prohibit binding sockets.

The AvianVisitors repositories inherit a CC-BY-NC-SA-4.0 non-commercial
license. Check each linked project's current licensing and attribution terms
before redistributing a combined appliance or generated assets.

## Simulation limitations

A backlit, emissive Mac display cannot reproduce reflective pigment, ambient
light response, refresh artifacts, viewing angle, or the exact panel colour
gamut. The physical treatment is intentionally subtle and is useful only as a
visual cue. Native palette pixels are the authoritative simulated output.
