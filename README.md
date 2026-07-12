# E-Ink Frame

## Headless Raspberry Pi Runtime

The Tk-free `display_runtime` package renders weather, birds, star maps,
uploaded photos, automatic schedules, and test patterns from a terminal or
service. It validates native dimensions and the six-color palette, writes
checksum-named immutable frames, and atomically maintains a last-known-good
manifest for each mode.

```bash
python3 -m venv .venv
.venv/bin/pip install -e '.[integrations]'
python3 -m display_runtime check
python3 -m display_runtime render test-pattern
```

Production source rendering fails closed by default: live-source failures never
publish demo content over an existing valid frame. See
[display_runtime/README.md](display_runtime/README.md) for Raspberry Pi setup,
TOML configuration, CLI commands, output layout, and current ESP32 integration
boundary.

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
