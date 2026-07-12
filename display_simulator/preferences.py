from __future__ import annotations

import json
import os
import platform
from pathlib import Path
from typing import Any, Mapping


def preferences_path() -> Path:
    override = os.environ.get("DISPLAY_SIMULATOR_PREFERENCES", "").strip()
    if override:
        return Path(override).expanduser()
    if platform.system() == "Darwin":
        return Path.home() / "Library" / "Application Support" / "EInk Display Simulator" / "preferences.json"
    config_home = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return config_home / "display_simulator" / "preferences.json"


def load_preferences(path: Path | None = None) -> dict[str, Any]:
    path = path or preferences_path()
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        return {}
    return value if isinstance(value, dict) else {}


def save_preferences(data: Mapping[str, Any], path: Path | None = None) -> Path:
    path = path or preferences_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(dict(data), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)
    return path
