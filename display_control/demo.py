"""Short-lived display overrides for the diagnostic control panel.

The override deliberately lives beside, rather than inside, the persistent
settings document.  A stale browser saving ordinary settings therefore cannot
extend, delete, or resurrect another phone's five-minute demo.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import math
import os
from pathlib import Path
import tempfile
import threading
from typing import Any, Callable, Mapping


DEMO_SCHEMA_VERSION = 1
DEMO_DURATION_SECONDS = 5 * 60
DEMO_MODES = frozenset(("weather", "birds", "star-map", "uploaded-photo"))
MAX_DEMO_STATE_BYTES = 4096
_DEMO_FIELDS = frozenset(("schema_version", "mode", "started_at", "expires_at"))


class DemoOverrideError(ValueError):
    """Raised when a demo override cannot be safely created."""


def demo_path_for_settings(settings_path: Path | str) -> Path:
    """Return the sidecar used by both the control and frame-server processes."""
    override = os.environ.get("EINK_CONTROL_DEMO", "").strip()
    if override:
        return Path(override).expanduser()
    return Path(settings_path).expanduser().with_name("demo-override.json")


def _aware_utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise DemoOverrideError("demo timestamps must include a timezone")
    return value.astimezone(timezone.utc)


def _timestamp(value: datetime) -> str:
    return _aware_utc(value).isoformat().replace("+00:00", "Z")


def _parse_timestamp(value: Any, label: str) -> datetime:
    if not isinstance(value, str) or not value:
        raise DemoOverrideError(f"{label} must be an ISO-8601 timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise DemoOverrideError(f"{label} must be an ISO-8601 timestamp") from exc
    return _aware_utc(parsed)


def _load_document(path: Path) -> dict[str, Any]:
    try:
        stat = path.lstat()
    except OSError as exc:
        raise DemoOverrideError("demo override is unavailable") from exc
    if path.is_symlink() or not path.is_file():
        raise DemoOverrideError("demo override must be a regular file")
    if stat.st_size <= 0 or stat.st_size > MAX_DEMO_STATE_BYTES:
        raise DemoOverrideError("demo override has an invalid size")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise DemoOverrideError("demo override is not valid JSON") from exc
    if not isinstance(value, Mapping):
        raise DemoOverrideError("demo override must be an object")
    if set(value) != _DEMO_FIELDS:
        raise DemoOverrideError("demo override has unknown or missing fields")
    if value["schema_version"] != DEMO_SCHEMA_VERSION:
        raise DemoOverrideError("demo override schema is unsupported")
    mode = value["mode"]
    if mode not in DEMO_MODES:
        raise DemoOverrideError("demo override mode is unsupported")
    started_at = _parse_timestamp(value["started_at"], "started_at")
    expires_at = _parse_timestamp(value["expires_at"], "expires_at")
    if expires_at - started_at != timedelta(seconds=DEMO_DURATION_SECONDS):
        raise DemoOverrideError("demo override must last exactly five minutes")
    return {
        "mode": mode,
        "started_at": started_at,
        "expires_at": expires_at,
    }


def read_demo_override(
    settings_path: Path | str,
    *,
    now: datetime | None = None,
) -> dict[str, Any] | None:
    """Return the active validated override, ignoring absent/corrupt/expired state."""
    current = _aware_utc(now or datetime.now(timezone.utc))
    try:
        value = _load_document(demo_path_for_settings(settings_path))
    except DemoOverrideError:
        return None
    if current >= value["expires_at"]:
        return None
    return {
        "mode": value["mode"],
        "started_at": _timestamp(value["started_at"]),
        "expires_at": _timestamp(value["expires_at"]),
        "remaining_seconds": max(
            1,
            math.ceil((value["expires_at"] - current).total_seconds()),
        ),
    }


def _inactive_status() -> dict[str, Any]:
    return {
        "active": False,
        "mode": None,
        "started_at": None,
        "expires_at": None,
        "remaining_seconds": 0,
        "duration_seconds": DEMO_DURATION_SECONDS,
    }


class DemoOverrideStore:
    """Thread-safe atomic writer and status facade for the control server."""

    def __init__(
        self,
        settings_path: Path | str,
        *,
        clock: Callable[[], datetime] | None = None,
    ):
        self.settings_path = Path(settings_path).expanduser()
        self.path = demo_path_for_settings(self.settings_path)
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._lock = threading.RLock()

    def status(self, *, now: datetime | None = None) -> dict[str, Any]:
        with self._lock:
            value = read_demo_override(
                self.settings_path,
                now=now or self._clock(),
            )
            if value is None:
                return _inactive_status()
            return {
                "active": True,
                **value,
                "duration_seconds": DEMO_DURATION_SECONDS,
            }

    def activate(self, mode: str, *, now: datetime | None = None) -> dict[str, Any]:
        if mode not in DEMO_MODES:
            raise DemoOverrideError(
                "mode must be weather, birds, star-map, or uploaded-photo"
            )
        with self._lock:
            started_at = _aware_utc(now or self._clock())
            expires_at = started_at + timedelta(seconds=DEMO_DURATION_SECONDS)
            document = {
                "schema_version": DEMO_SCHEMA_VERSION,
                "mode": mode,
                "started_at": _timestamp(started_at),
                "expires_at": _timestamp(expires_at),
            }
            self.path.parent.mkdir(parents=True, exist_ok=True)
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=self.path.name + ".",
                suffix=".tmp",
                dir=self.path.parent,
            )
            temporary = Path(temporary_name)
            try:
                with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                    json.dump(document, stream, indent=2, sort_keys=True)
                    stream.write("\n")
                    stream.flush()
                    os.fsync(stream.fileno())
                temporary.replace(self.path)
            finally:
                try:
                    temporary.unlink()
                except FileNotFoundError:
                    pass
            return self.status(now=started_at)

    def cancel(self) -> dict[str, Any]:
        with self._lock:
            try:
                self.path.unlink()
            except FileNotFoundError:
                pass
            return _inactive_status()
