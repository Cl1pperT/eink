from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any

try:
    import tomllib
except ImportError:  # pragma: no cover - Python 3.11+ is required
    tomllib = None


DEFAULTS: dict[str, Any] = {
    "display": {"orientation": "landscape", "physical_treatment": True},
    "location": {"name": "Denver, Colorado"},
    "schedule": {"weather_start": "06:00", "birds_start": "09:00", "star_start": "20:00"},
    "conversion": {"dithering": True, "method": "floyd-steinberg", "saturation": 0.6, "blue_bias": 0.5},
    "photo": {"saturation": 0.35, "blue_bias": 0.0},
    "repositories": {"avian_weather": "", "inkystarmap": ""},
    "sources": {"bird": "", "starmap": ""},
    "coordinates": {"latitude": 39.7392, "longitude": -104.9903, "direction": 180, "timezone": "America/Denver"},
    "upload": {"host": "0.0.0.0", "port": 8765, "file": "simulator_output/latest-upload.png", "max_megabytes": 20},
    "output": {"directory": "simulator_output"},
    "buttons": {"button1": "Weather", "button2": "Birds", "button3": "Star Map"},
    "window": {"width": 1280, "height": 820},
}


def _merge(base: dict[str, Any], updates: dict[str, Any]) -> dict[str, Any]:
    for key, value in updates.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _merge(base[key], value)
        else:
            base[key] = value
    return base


def load_config(path: Path | None = None) -> dict[str, Any]:
    data = deepcopy(DEFAULTS)
    default_path = Path(__file__).with_name("defaults.toml")
    for candidate in (default_path, path):
        if candidate and candidate.exists() and tomllib:
            with candidate.open("rb") as handle:
                _merge(data, tomllib.load(handle))
    return data
