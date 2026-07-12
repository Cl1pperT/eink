# E-Ink Frame

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
