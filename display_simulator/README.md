# Desktop Display Simulator

This package previews planned content for a 13.3-inch, 1600×1200 Seeed Studio
EE02 Spectra 6 frame. Its scheduled sources are adapters for three existing
projects:

| Period | Project | Simulator path |
| --- | --- | --- |
| Morning | [Cl1pperT/AvianVisitors](https://github.com/Cl1pperT/AvianVisitors) | `weather_frame.renderer` generated-scene/procedural artwork |
| Day | [Twarner491/AvianVisitors](https://github.com/Twarner491/AvianVisitors) | the real `frame/shoot.py` web-collage capture |
| Night | [Marcel-Jan/inkystarmap](https://github.com/Marcel-Jan/inkystarmap) | its Starplot horizon-map recipe, without hardware initialization |
| Manual | Built-in phone control panel | uploaded Pillow-readable images |

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

The integrations remain independent checkouts rather than package data. Their
paths are automatically discovered in this source tree, while explicit paths
remain configurable for alternate layouts.

## Automatic checkout discovery and overrides

The simulator automatically discovers the copied repositories at:

```text
peacock/AvianVisitors
stars/integrations/inkystarmap
```

Discovery is anchored to the E-Ink Frame source directory, so it does not
depend on the shell or IDE launch directory. If either checkout is absent, it
can be populated with:

```bash
git clone --branch avian-visitors https://github.com/Cl1pperT/AvianVisitors.git peacock/AvianVisitors
git clone https://github.com/Marcel-Jan/inkystarmap.git stars/integrations/inkystarmap
```

Because the weather fork contains `avian/`, `frame/`, and `weather_frame/`, the
simulator uses one **AvianVisitors + Weather** repository selector for both the
morning and daytime modes (including the Twarner491 bird-viewer code it is based
on). The discovered paths appear in the repository controls; selecting folders
there is optional.

A valid remembered UI path or configured repository path takes precedence over
the co-located checkout. The desktop's shared Peacock resolver requires both
`weather_frame/renderer.py` and `frame/shoot.py`; when no valid saved path
exists, it checks `WEATHER_FRAME_REPO` and then `AVIANVISITORS_REPO` as aliases
for that shared checkout. Stars similarly uses `INKYSTARMAP_REPO`. A selector
may point directly to a checkout or to a supported parent collection such as
`stars/`.

The root `.gitignore` excludes `peacock/` and `stars/`. These are local working
copies with their own Git histories, not content committed or distributed by
the E-Ink Frame repository. A fresh parent checkout therefore needs the two
integration repositories cloned into the paths above (or supplied through an
override).

The Raspberry Pi installer deliberately does not copy these local trees. Its
system service uses separately managed AvianVisitors and inkystarmap checkouts
at the absolute paths configured in `/etc/eink-display/runtime.toml`; see the
[headless runtime documentation](../display_runtime/README.md) for that
deployment layout.

For live bird-page capture and Starplot rendering, install the optional desktop
dependencies:

```bash
python3 -m pip install -r requirements-integrations.txt
python3 -m playwright install chromium
```

Do not install inkystarmap's full Raspberry Pi package on the Mac solely for the
simulator: its module initializes `inky.auto()` at import time. The adapter uses
Starplot's circular `ZenithPlot` while keeping that hardware module out of the
process. The atlas shows the full visible sky 90 minutes after local sunset and
rotates the selected cardinal direction to the bottom. Its grid, ordinary
constellations, and faint stars stay white; only the brightest stars receive
their catalogued B-V colors. A prominent constellation is traced in gold,
palette-safe miniature planets retain details such as Saturn's ring and
Jupiter's bands, and a right-side planetarium guide summarizes the observing
night. This side-by-side planetarium layout targets the frame's 1600×1200
landscape orientation.

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
Preview**. Automatic mode uses the configurable desktop schedule (Weather
06:00, Birds 09:00, Star Map 20:00 by default). Moving the time slider only
updates the
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

With the weather checkout discovered or configured, fixture weather constructs
a deterministic `DailyForecast` and sends it through the real renderer and its
local generated scene/procedural fallback. Turning fixture weather off uses
that project's Open-Meteo provider. Without the checkout, fixture mode uses the
small built-in Pillow fallback and live mode reports a clear error.

Bird mode accepts either a completed PNG or an AvianVisitors page URL. When its
checkout is discovered or configured, URL capture invokes the project's actual
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
slow paths run in the rendering worker. A source checkout uses the astronomy
catalogs stored at the repository root even when launched elsewhere.

## Phone control panel and photo uploads

The Uploaded Photo section can start the same responsive control panel used by
the Raspberry Pi. It listens at the configured host and port
(`0.0.0.0:8765` by default) and lets a phone on the same LAN:

- enable the Utah locations that may rotate through weather scenes;
- choose which outdoor activities may be recommended;
- edit each activity's ideal/tolerable weather ranges, weights, required
  conditions, and estimated great days per year;
- adjust recommendation count, suitability threshold, caption, and units; and
- upload and preview a PNG, JPEG, or WebP photo.

The annual-day value affects rarity priority; it is an estimate, not a quota
that stops an activity after that many displays. Settings and uploads are
validated and atomically replaced. A successful simulator upload selects
Uploaded Photo, records `Manual upload` as the wake reason, and generates a
preview automatically. Embedded profiles such as iPhone Display P3 are
converted to sRGB before palette matching. Uploaded photos use the separate
neutral `[photo]` saturation and blue-bias settings; generated Weather, Birds,
and Stars artwork continues to use `[conversion]`.

You can also run the panel independently:

```bash
python3 -m display_control
```

The command prints a LAN URL. The Mac may ask for permission to accept incoming
connections the first time. The default Mac settings file is stored beside the
simulator's application data. No pairing code, login, or control-panel token is
required: any device on the trusted LAN can use the website and its control API
to change settings, upload images, start demos, or queue renders. If multiple
phones edit concurrently, the last valid save wins, so refresh before making
another change. Keep the site on a trusted home LAN and do not expose port 8765
through router forwarding.

## Configuration

Project defaults live in `display_simulator/defaults.toml`: repository overrides,
coordinates, upload listener, orientation, location, schedule boundaries,
output directory, conversion settings, source paths, physical preview, button
mappings, and window size. The application launches without a user
configuration. `load_config(path)` can merge an optional TOML file over the
project defaults. Blank repository overrides allow the source-tree discovery
described above; **Reset Defaults** runs that discovery again.

The last weather location, explicitly selected or typed repository overrides,
bird URL or PNG, star-map PNG, and uploaded-photo path are remembered
automatically. Untouched auto-discovered repository paths remain discovery
results rather than becoming saved overrides, so a later environment override
still takes effect. On macOS preferences are stored in:

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
