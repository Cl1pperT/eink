from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timezone
import hashlib
import importlib
import importlib.util
import inspect
import json
import math
import os
from pathlib import Path
import tempfile
from typing import Any, Callable, Iterator, Mapping
from zoneinfo import ZoneInfo

# This module is a service/CLI entry point. Force an off-screen plotting backend
# before importing source adapters that may import Starplot or Matplotlib.
os.environ.setdefault("MPLBACKEND", "Agg")

from PIL import Image

from display_simulator.models import RenderContext
from display_simulator.pipeline import ImagePipeline, SPECTRA_PALETTE, checksum_image, validate_palette
from display_simulator.repositories import find_repository
from display_simulator.sources import (
    BirdsSource,
    StarMapSource,
    TestPatternSource,
    UploadedPhotoSource,
    WeatherSource,
)
from display_simulator.sources.uploaded_photo import (
    normalized_photo_crop,
    photo_recipe_digest,
)

from .config import RuntimeConfig
from .ee02 import (
    EE02_BUFFER_HEIGHT,
    EE02_BUFFER_WIDTH,
    EE02_NAMED_COLOR_CODES,
    EE02_PAYLOAD_BYTES,
    EE02_WIRE_FORMAT,
    EncodedEE02Frame,
    encode_ee02,
)
from .frame_server import FrameSelection
from .schedule import ScheduleState, schedule_state


SCHEMA_VERSION = 2
CANONICAL_MODES = (
    "automatic",
    "weather",
    "birds",
    "star-map",
    "uploaded-photo",
    "test-pattern",
)
SCHEDULED_MODES = ("weather", "birds", "star-map")
STAR_DIRECTION_DEGREES = {
    "north": 0,
    "east": 90,
    "south": 180,
    "west": 270,
}
_DIRECTION_AWARE_STAR_SOURCE = "live inkystarmap/Starplot render"
_RARE_EVENT_KINDS = frozenset(
    ("meteor", "aurora", "eclipse", "satellite", "conjunction")
)
_RARE_EVENT_CONFIDENCE = frozenset(("high", "medium", "low"))
_RARE_EVENT_DIRECTIONS = frozenset(
    ("N", "NE", "E", "SE", "S", "SW", "W", "NW")
)
_RARE_EVENT_REQUIRED_FIELDS = frozenset(
    (
        "id",
        "kind",
        "title",
        "timing",
        "detail",
        "priority",
        "confidence",
        "source",
        "is_tonight",
    )
)
_RARE_EVENT_OPTIONAL_FIELDS = frozenset(
    (
        "starts_at",
        "peaks_at",
        "ends_at",
        "direction",
        "altitude_degrees",
        "azimuth_degrees",
        "separation_degrees",
    )
)
_RARE_EVENT_STRING_LIMITS = {
    "id": 100,
    "title": 100,
    "timing": 120,
    "detail": 240,
    "source": 100,
}
_LOWERCASE_HEXADECIMAL = frozenset("0123456789abcdef")

_ALIASES = {
    "automatic": "automatic",
    "auto": "automatic",
    "weather": "weather",
    "birds": "birds",
    "bird": "birds",
    "star-map": "star-map",
    "star map": "star-map",
    "starmap": "star-map",
    "stars": "star-map",
    "uploaded-photo": "uploaded-photo",
    "uploaded photo": "uploaded-photo",
    "photo": "uploaded-photo",
    "test-pattern": "test-pattern",
    "test pattern": "test-pattern",
    "test": "test-pattern",
}

_SOURCE_FACTORIES: Mapping[str, Callable[[], Any]] = {
    "weather": WeatherSource,
    "birds": BirdsSource,
    "star-map": StarMapSource,
    "uploaded-photo": UploadedPhotoSource,
    "test-pattern": TestPatternSource,
}


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _first_value(*values: Any, default: Any = None) -> Any:
    for value in values:
        if value is not None:
            return value
    return default


def _aware_iso_timestamp(value: Any) -> str | None:
    if (
        not isinstance(value, str)
        or value.strip() != value
        or not value
        or not value.isprintable()
    ):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.isoformat()


def _bounded_event_string(value: Any, maximum: int) -> str | None:
    if (
        not isinstance(value, str)
        or value.strip() != value
        or not 1 <= len(value) <= maximum
        or not value.isprintable()
    ):
        return None
    return value


def _bounded_event_number(
    value: Any,
    minimum: float,
    maximum: float,
    *,
    maximum_inclusive: bool = True,
) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if not math.isfinite(number) or number < minimum:
        return None
    if number > maximum or (not maximum_inclusive and number == maximum):
        return None
    return number


def _normalized_rare_events(value: Any) -> list[dict[str, Any]] | None:
    if not isinstance(value, list) or len(value) > 8:
        return None
    allowed = _RARE_EVENT_REQUIRED_FIELDS | _RARE_EVENT_OPTIONAL_FIELDS
    normalized: list[dict[str, Any]] = []
    for raw in value:
        if not isinstance(raw, Mapping) or set(raw) - allowed:
            return None
        if not _RARE_EVENT_REQUIRED_FIELDS.issubset(raw):
            return None

        event: dict[str, Any] = {}
        for key, maximum in _RARE_EVENT_STRING_LIMITS.items():
            text = _bounded_event_string(raw.get(key), maximum)
            if text is None:
                return None
            event[key] = text

        kind = raw.get("kind")
        confidence = raw.get("confidence")
        priority = raw.get("priority")
        is_tonight = raw.get("is_tonight")
        if not isinstance(kind, str) or kind not in _RARE_EVENT_KINDS:
            return None
        if (
            not isinstance(confidence, str)
            or confidence not in _RARE_EVENT_CONFIDENCE
        ):
            return None
        if (
            isinstance(priority, bool)
            or not isinstance(priority, int)
            or not 0 <= priority <= 100
        ):
            return None
        if not isinstance(is_tonight, bool):
            return None
        event.update(
            {
                "kind": kind,
                "priority": priority,
                "confidence": confidence,
                "is_tonight": is_tonight,
            }
        )

        for key in ("starts_at", "peaks_at", "ends_at"):
            if key not in raw:
                continue
            if raw[key] is None:
                event[key] = None
                continue
            timestamp = _aware_iso_timestamp(raw[key])
            if timestamp is None:
                return None
            event[key] = timestamp

        if "direction" in raw:
            direction = raw["direction"]
            if (
                not isinstance(direction, str)
                or direction not in _RARE_EVENT_DIRECTIONS
            ):
                return None
            event["direction"] = direction

        number_fields = (
            ("altitude_degrees", -90.0, 90.0, True),
            ("azimuth_degrees", 0.0, 360.0, False),
            ("separation_degrees", 0.0, 180.0, True),
        )
        for key, minimum, maximum, inclusive in number_fields:
            if key not in raw:
                continue
            number = _bounded_event_number(
                raw[key],
                minimum,
                maximum,
                maximum_inclusive=inclusive,
            )
            if number is None:
                return None
            event[key] = number
        normalized.append(event)
    return normalized


def _rare_event_view_metadata(options: Mapping[str, Any]) -> dict[str, Any]:
    """Validate and bind one renderer event list to its canonical identity."""
    events = _normalized_rare_events(options.get("star_rare_events"))
    generated_at = _aware_iso_timestamp(
        options.get("star_rare_events_generated_at")
    )
    claimed_digest = options.get("star_rare_events_digest")
    if (
        events is None
        or generated_at is None
        or not isinstance(claimed_digest, str)
        or len(claimed_digest) != 64
        or any(
            character not in _LOWERCASE_HEXADECIMAL
            for character in claimed_digest
        )
    ):
        return {}
    canonical = json.dumps(
        events,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    computed_digest = hashlib.sha256(canonical).hexdigest()
    if claimed_digest != computed_digest:
        return {}
    return {
        "rare_events": events,
        "rare_events_generated_at": generated_at,
        "rare_events_digest": computed_digest,
    }


def _star_view_metadata(options: Mapping[str, Any]) -> dict[str, Any]:
    """Validate astronomy metadata emitted by the live star source."""
    result: dict[str, Any] = {}
    observation = options.get("star_observation_time")
    if isinstance(observation, str):
        try:
            parsed = datetime.fromisoformat(observation.replace("Z", "+00:00"))
        except ValueError:
            pass
        else:
            if parsed.tzinfo is not None and parsed.utcoffset() is not None:
                result["observation_time"] = parsed.isoformat()
    sunrise = options.get("star_sunrise_time")
    if isinstance(sunrise, str):
        try:
            parsed = datetime.fromisoformat(sunrise.replace("Z", "+00:00"))
        except ValueError:
            pass
        else:
            if parsed.tzinfo is not None and parsed.utcoffset() is not None:
                result["sunrise_time"] = parsed.isoformat()
    night_date = options.get("star_night_date")
    if isinstance(night_date, str):
        try:
            result["night_date"] = date.fromisoformat(night_date).isoformat()
        except ValueError:
            pass
    featured = options.get("star_featured_constellation")
    if (
        isinstance(featured, str)
        and featured.strip() == featured
        and 1 <= len(featured) <= 80
        and featured.isprintable()
    ):
        result["featured_constellation"] = featured
    result.update(_rare_event_view_metadata(options))
    return result


def _photo_manifest_metadata(context: RenderContext) -> dict[str, Any]:
    """Return the exact input recipe used to prepare an uploaded photo."""
    crop = normalized_photo_crop(context.options.get("photo_crop"))
    return {
        "recipe_sha256": photo_recipe_digest(
            context.options["photo_path"],
            int(context.options.get("rotation", 0)),
            str(context.options.get("caption", "")),
            crop,
            target_size=context.orientation.dimensions,
        ),
        "rotation": int(context.options.get("rotation", 0)),
        "caption": str(context.options.get("caption", "")).strip(),
        "crop": crop,
    }


def _load_control_overlay(
    path: Path | None,
    weather_repository: Path | None,
    *,
    fail_closed: bool = False,
) -> dict[str, Any]:
    """Read the mutable control-panel settings without coupling the renderer to it.

    The control panel is an optional companion service. A missing state file or
    package must not prevent an otherwise valid TOML-only installation from
    rendering. Invalid files are likewise ignored here; the control server owns
    validation and writes replacements atomically.
    """

    if path is None or not path.is_file():
        return {}
    try:
        settings_module = importlib.import_module("display_control.settings")
        load_settings = settings_module.load_settings
        catalog = None
        load_catalog = getattr(
            settings_module,
            "discover_catalog",
            getattr(settings_module, "load_catalog", None),
        )
        if callable(load_catalog):
            try:
                catalog = load_catalog(weather_repository)
            except (OSError, RuntimeError, TypeError, ValueError):
                catalog = None
        parameters = inspect.signature(load_settings).parameters
        kwargs: dict[str, Any] = {}
        if "catalog" in parameters:
            kwargs["catalog"] = catalog
        if "strict" in parameters:
            kwargs["strict"] = True
        if "strict" not in parameters and catalog is not None:
            # Older control packages had a forgiving loader that substituted
            # defaults for malformed JSON. Validate the raw document directly
            # so corrupt mutable state never overrides the trusted TOML config.
            validate_settings = getattr(settings_module, "validate_settings", None)
            if callable(validate_settings):
                raw = json.loads(path.read_text(encoding="utf-8"))
                loaded = validate_settings(raw, catalog)
            else:
                loaded = load_settings(path, **kwargs)
        else:
            loaded = load_settings(path, **kwargs)
    except (
        ImportError,
        AttributeError,
        OSError,
        RuntimeError,
        TypeError,
        UnicodeError,
        ValueError,
    ) as exc:
        if fail_closed:
            raise RuntimeError("control settings could not be validated") from exc
        return {}
    if not isinstance(loaded, Mapping):
        if fail_closed:
            raise RuntimeError("validated control settings are not an object")
        return {}
    nested = loaded.get("control_panel")
    return dict(nested) if isinstance(nested, Mapping) else dict(loaded)


def _active_demo_override(
    path: Path | None, when: datetime
) -> tuple[str, datetime] | None:
    """Read an optional short-lived control override without making it required."""
    if path is None:
        return None
    try:
        demo_module = importlib.import_module("display_control.demo")
        read_demo_override = demo_module.read_demo_override
        value = read_demo_override(path, now=when)
    except (
        ImportError,
        AttributeError,
        OSError,
        RuntimeError,
        TypeError,
        UnicodeError,
        ValueError,
    ):
        return None
    if not isinstance(value, Mapping):
        return None
    mode = value.get("mode")
    expires_at = value.get("expires_at")
    allowed = (*SCHEDULED_MODES, "uploaded-photo")
    if not isinstance(mode, str) or mode not in allowed:
        return None
    if not isinstance(expires_at, str):
        return None
    try:
        expires = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
    except ValueError:
        return None
    if expires.tzinfo is None or expires.utcoffset() is None:
        return None
    return mode, expires.astimezone(timezone.utc)


def _control_values(settings: Mapping[str, Any]) -> dict[str, Any]:
    """Normalize supported settings-schema spellings into render options."""

    locations = _mapping(settings.get("locations") or settings.get("location"))
    activities = _mapping(settings.get("activities"))
    weather = _mapping(settings.get("weather"))
    display = _mapping(settings.get("display"))
    birds = _mapping(settings.get("birds"))
    stars = _mapping(settings.get("stars"))
    photo = _mapping(settings.get("photo"))

    enabled_environments = _first_value(
        settings.get("enabled_locations"),
        settings.get("enabled_environments"),
        locations.get("enabled_environments"),
        locations.get("enabled"),
        locations.get("selected"),
    )
    enabled_activity_ids = _first_value(
        settings.get("enabled_activities"),
        settings.get("enabled_activity_ids"),
        activities.get("enabled_activity_ids"),
        activities.get("enabled"),
        activities.get("selected"),
    )
    overrides = _first_value(
        settings.get("activity_overrides"),
        activities.get("activity_overrides"),
        activities.get("overrides"),
        default={},
    )
    return {
        "display_mode": _first_value(
            settings.get("display_mode"),
            settings.get("mode"),
            display.get("mode"),
        ),
        "location": _first_value(
            settings.get("location_name"),
            display.get("location_name"),
            locations.get("location_name"),
            locations.get("forecast_location"),
            locations.get("name"),
        ),
        "enabled_environments": enabled_environments,
        "weather_environment": _first_value(
            settings.get("weather_environment"),
            weather.get("environment"),
            locations.get("environment"),
        ),
        "enabled_activity_ids": enabled_activity_ids,
        "activity_overrides": overrides if isinstance(overrides, Mapping) else {},
        "recommendation_count": _first_value(
            settings.get("recommendation_count"),
            activities.get("recommendation_count"),
            activities.get("limit"),
            activities.get("count"),
        ),
        "minimum_suitability": _first_value(
            settings.get("minimum_suitability"),
            activities.get("minimum_suitability"),
            activities.get("min_suitability"),
        ),
        "weather_caption": _first_value(
            settings.get("weather_caption"),
            weather.get("caption"),
            display.get("weather_caption"),
            display.get("caption"),
        ),
        "weather_units": _first_value(
            settings.get("weather_units"),
            weather.get("units"),
            display.get("weather_units"),
            display.get("units"),
        ),
        "bird_provider": _first_value(
            settings.get("bird_provider"),
            birds.get("provider"),
        ),
        "bird_postal_code": _first_value(
            settings.get("bird_postal_code"),
            birds.get("postal_code"),
        ),
        "bird_country": _first_value(
            settings.get("bird_country"),
            birds.get("country"),
        ),
        "bird_lookback_days": _first_value(
            settings.get("bird_lookback_days"),
            birds.get("lookback_days"),
        ),
        "bird_title": _first_value(
            settings.get("bird_title"),
            birds.get("title"),
        ),
        "bird_subtitle": _first_value(
            settings.get("bird_subtitle"),
            birds.get("subtitle"),
        ),
        "star_direction": _first_value(
            settings.get("star_direction"),
            stars.get("direction"),
        ),
        "photo_path": _first_value(settings.get("photo_path"), photo.get("path")),
        "photo_caption": _first_value(settings.get("photo_caption"), photo.get("caption")),
        "photo_rotation": _first_value(settings.get("photo_rotation"), photo.get("rotation")),
        "photo_crop": _first_value(settings.get("photo_crop"), photo.get("crop")),
        "photo_enabled": _first_value(
            settings.get("photo_enabled"),
            photo.get("enabled"),
            default=True,
        ),
    }


def _overlay_path(value: Any, settings_path: Path | None) -> Path | None:
    if not isinstance(value, (str, os.PathLike)) or not str(value).strip():
        return None
    path = Path(value).expanduser()
    if not path.is_absolute() and settings_path is not None:
        path = settings_path.parent / path
    return path.resolve(strict=False)


def _string_tuple(value: Any) -> tuple[str, ...] | None:
    if value is None:
        return None
    if isinstance(value, str):
        value = (value,)
    if not isinstance(value, (list, tuple, set, frozenset)):
        return None
    result = tuple(str(item).strip() for item in value if str(item).strip())
    return result


class RuntimeRenderError(RuntimeError):
    """Base error for a failed headless render."""


class SourcePolicyError(RuntimeRenderError):
    """A source would violate the production fail-closed policy."""


@dataclass(frozen=True, slots=True)
class RuntimeArtifact:
    requested_mode: str
    mode: str
    changed: bool
    written: bool
    checksum: str
    wire_checksum: str
    frame_path: Path
    wire_path: Path
    wire_rotation: str
    seeed_sprite_rotation: int
    rgb_path: Path | None
    manifest_path: Path
    width: int
    height: int
    source_name: str
    provenance: str
    rendered_for: datetime
    source_seconds: float
    conversion_seconds: float

    def as_dict(self) -> dict[str, Any]:
        return {
            "requested_mode": self.requested_mode,
            "mode": self.mode,
            "changed": self.changed,
            "written": self.written,
            "checksum": self.checksum,
            "wire": {
                "format": EE02_WIRE_FORMAT,
                "checksum": self.wire_checksum,
                "path": str(self.wire_path),
                "bytes": EE02_PAYLOAD_BYTES,
                "buffer_dimensions": {
                    "width": EE02_BUFFER_WIDTH,
                    "height": EE02_BUFFER_HEIGHT,
                },
                "rotation": self.wire_rotation,
                "seeed_sprite_rotation": self.seeed_sprite_rotation,
            },
            "frame_path": str(self.frame_path),
            "rgb_path": str(self.rgb_path) if self.rgb_path else None,
            "manifest_path": str(self.manifest_path),
            "dimensions": {"width": self.width, "height": self.height},
            "source": {"name": self.source_name, "provenance": self.provenance},
            "rendered_for": self.rendered_for.isoformat(),
            "timings": {
                "source_seconds": round(self.source_seconds, 6),
                "conversion_seconds": round(self.conversion_seconds, 6),
            },
        }


def canonical_mode(value: str) -> str:
    key = str(value).strip().lower().replace("_", "-")
    try:
        return _ALIASES[key]
    except KeyError as exc:
        choices = ", ".join(CANONICAL_MODES)
        raise ValueError(f"unknown display mode {value!r}; choose one of: {choices}") from exc


def parse_render_time(value: str | None, timezone_name: str) -> datetime:
    zone = ZoneInfo(timezone_name)
    if value is None:
        return datetime.now(zone)
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        when = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"invalid ISO-8601 date/time {value!r}") from exc
    if when.tzinfo is None:
        return when.replace(tzinfo=zone)
    return when.astimezone(zone)


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _fsync_directory(path: Path) -> None:
    flags = getattr(os, "O_DIRECTORY", 0) | os.O_RDONLY
    try:
        descriptor = os.open(path, flags)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _atomic_save_png(image: Image.Image, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w+b", prefix=f".{path.name}.", suffix=".tmp", dir=path.parent, delete=False
        ) as handle:
            temporary = Path(handle.name)
            image.save(handle, format="PNG")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    except Exception:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        raise


def _atomic_write_bytes(payload: bytes, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w+b", prefix=f".{path.name}.", suffix=".tmp", dir=path.parent, delete=False
        ) as handle:
            temporary = Path(handle.name)
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    except Exception:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        raise


def _atomic_write_json(value: Mapping[str, Any], path: Path) -> None:
    payload = (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")
    _atomic_write_bytes(payload, path)


def _load_manifest(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        return None
    if not isinstance(value, dict) or value.get("schema_version") != SCHEMA_VERSION:
        return None
    return value


def _native_file_is_valid(path: Path, expected_checksum: str, size: tuple[int, int]) -> bool:
    try:
        with Image.open(path) as opened:
            image = opened.convert("RGB")
            image.load()
    except (OSError, ValueError):
        return False
    return image.size == size and validate_palette(image) and checksum_image(image) == expected_checksum


def _binary_file_is_valid(path: Path, expected_bytes: int, expected_sha256: str) -> bool:
    try:
        return path.stat().st_size == expected_bytes and _file_sha256(path) == expected_sha256
    except OSError:
        return False


@contextmanager
def _mode_lock(mode_directory: Path) -> Iterator[None]:
    mode_directory.mkdir(parents=True, exist_ok=True)
    lock_path = mode_directory / ".render.lock"
    with lock_path.open("a+b") as handle:
        try:
            import fcntl
        except ImportError:  # pragma: no cover - the runtime targets macOS/Linux
            yield
            return
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


class FrameRuntime:
    """Synchronous, Tk-free renderer with atomic last-known-good commits."""

    def __init__(
        self,
        config: RuntimeConfig,
        *,
        source_factories: Mapping[str, Callable[[], Any]] | None = None,
    ) -> None:
        self.config = config
        self.source_factories = dict(_SOURCE_FACTORIES)
        if source_factories:
            self.source_factories.update(source_factories)

    def resolve_mode(self, requested_mode: str, when: datetime) -> str:
        requested = canonical_mode(requested_mode)
        if requested != "automatic":
            return requested
        return self.resolve_schedule(when).mode

    def resolve_schedule(self, when: datetime) -> ScheduleState:
        return schedule_state(
            when,
            self.config.schedule,
            latitude=self.config.latitude,
            longitude=self.config.longitude,
            timezone_name=self.config.timezone,
        )

    def _control_settings(self, *, fail_closed: bool = False) -> dict[str, Any]:
        loaded = _load_control_overlay(
            self.config.control_settings_path,
            self.config.avian_weather_repo,
            fail_closed=fail_closed,
        )
        return _control_values(loaded) if loaded else {}

    def resolve_active_state(
        self, when: datetime | None = None
    ) -> FrameSelection:
        """Resolve the phone-selected display channel to one concrete mode.

        A missing settings file means the safe default, ``automatic``. When a
        settings file exists it must pass the control package's validation, and
        its selected mode must be canonical. A validated, unexpired five-minute
        demo sidecar temporarily wins without changing that saved preference.
        Invalid persistent state fails closed so the frame server returns an
        availability error and the ESP retains its current physical image;
        invalid transient demo state is simply ignored.
        """

        zone = ZoneInfo(self.config.timezone)
        evaluated = when or datetime.now(timezone.utc)
        local = (
            evaluated.replace(tzinfo=zone)
            if evaluated.tzinfo is None or evaluated.utcoffset() is None
            else evaluated.astimezone(zone)
        )
        automatic = self.resolve_schedule(local)
        control = self._control_settings(fail_closed=True)
        demo = _active_demo_override(self.config.control_settings_path, local)
        selected = demo[0] if demo is not None else None
        if selected is None:
            selected = control.get("display_mode")
        if selected is None:
            selected = "automatic"
        if not isinstance(selected, str) or selected not in CANONICAL_MODES:
            raise SourcePolicyError("control display.mode is not a valid canonical mode")
        mode = automatic.mode if selected == "automatic" else selected
        # A transient override owns the panel until its exact expiry. Automatic
        # boundaries inside that lease would only wake the radio and reselect
        # the same image, wasting battery.
        next_wake = demo[1] if demo is not None else automatic.next_wake_at
        return FrameSelection(
            mode=mode,
            evaluated_at=local.astimezone(timezone.utc),
            next_wake_at=next_wake,
        )

    def resolve_active_mode(self, when: datetime | None = None) -> str:
        return self.resolve_active_state(when).mode

    def _photo_path(self, control: Mapping[str, Any]) -> Path | None:
        overlay = _overlay_path(
            control.get("photo_path"),
            self.config.control_settings_path,
        )
        if overlay is not None and overlay.is_file():
            return overlay
        if self.config.photo_path is not None:
            return self.config.photo_path
        # A standalone macOS control server normally writes beside its settings
        # file. Pi installs configure sources.photo explicitly, so that path
        # remains authoritative instead of deriving control/latest-upload.png.
        if control and self.config.control_settings_path is not None:
            try:
                settings_module = importlib.import_module("display_control.settings")
                default_photo_path = settings_module.default_photo_path
                derived = Path(default_photo_path(self.config.control_settings_path)).expanduser()
            except (ImportError, AttributeError, OSError, TypeError, ValueError):
                derived = None
            if derived is not None:
                return derived.resolve(strict=False)
        return overlay

    def _repository(
        self,
        path: Path | None,
        marker: str,
        label: str,
        env_name: str,
    ) -> Path:
        if path is None:
            environment_value = os.environ.get(env_name, "").strip()
            if environment_value:
                path = Path(environment_value).expanduser().resolve(strict=False)
            else:
                path = find_repository("", marker, env_name)
        if path is None:
            raise SourcePolicyError(
                f"{label} repository path is not configured and no co-located checkout was found"
            )
        if not (path / marker).exists():
            raise SourcePolicyError(f"{label} repository is invalid; expected {path / marker}")
        return path

    def _preflight(
        self,
        mode: str,
        *,
        strict: bool,
        control: Mapping[str, Any] | None = None,
    ) -> None:
        cfg = self.config
        control = control or {}
        if mode == "test-pattern":
            return
        if mode == "weather":
            if strict and cfg.weather_offline:
                raise SourcePolicyError("fixture weather is disabled for production renders")
            if strict:
                self._repository(
                    cfg.avian_weather_repo,
                    "weather_frame/renderer.py",
                    "AvianVisitors/weather",
                    "WEATHER_FRAME_REPO",
                )
            return
        if mode == "birds":
            if strict and cfg.bird_demo:
                raise SourcePolicyError("fixture bird rendering is disabled in production")
            if cfg.bird_source and not cfg.bird_source.startswith(("http://", "https://")):
                if not Path(cfg.bird_source).is_file():
                    raise FileNotFoundError(f"bird frame not found: {cfg.bird_source}")
            elif cfg.bird_source:
                if strict:
                    self._repository(
                        cfg.avian_weather_repo,
                        "frame/shoot.py",
                        "AvianVisitors",
                        "AVIANVISITORS_REPO",
                    )
            elif strict:
                repository = self._repository(
                    cfg.avian_weather_repo,
                    "frame/shoot.py",
                    "AvianVisitors",
                    "AVIANVISITORS_REPO",
                )
                provider = control.get("bird_provider", "birdweather")
                if provider != "birdweather":
                    raise SourcePolicyError(
                        "production birds require the validated BirdWeather provider"
                    )
                adapter = repository / "frame" / "birdweather.py"
                if not adapter.is_file():
                    raise SourcePolicyError(
                        f"AvianVisitors repository is invalid; expected {adapter}"
                    )
            if strict and cfg.avian_python is not None and not cfg.avian_python.is_file():
                raise SourcePolicyError(f"sources.avian_python does not exist: {cfg.avian_python}")
            return
        if mode == "star-map":
            if cfg.starmap_source is not None:
                if not cfg.starmap_source.is_file():
                    raise FileNotFoundError(f"star-map image not found: {cfg.starmap_source}")
                return
            if strict:
                self._repository(
                    cfg.inkystarmap_repo,
                    "src/inkystarmap/inkystarmap.py",
                    "inkystarmap",
                    "INKYSTARMAP_REPO",
                )
                if not cfg.use_inkystarmap:
                    raise SourcePolicyError("sources.use_inkystarmap must be true for production star maps")
                if importlib.util.find_spec("starplot") is None:
                    raise SourcePolicyError("Starplot is not installed; install the integrations dependencies")
            return
        if mode == "uploaded-photo":
            if control.get("photo_enabled") is False:
                raise SourcePolicyError("uploaded-photo is disabled in the control panel")
            photo_path = self._photo_path(control)
            if photo_path is None or not photo_path.is_file():
                raise FileNotFoundError("sources.photo must point to an existing image")
            return
        raise RuntimeRenderError(f"no source adapter for mode {mode!r}")

    def _context(
        self,
        when: datetime,
        *,
        allow_demo: bool,
        control: Mapping[str, Any] | None = None,
    ) -> RenderContext:
        cfg = self.config
        control = control or {}
        location = control.get("location")
        if not isinstance(location, str) or not location.strip():
            location = cfg.location
        photo_path = self._photo_path(control)
        weather_environment = control.get("weather_environment")
        if not isinstance(weather_environment, str) or not weather_environment.strip():
            weather_environment = cfg.weather_environment
        enabled_environments = _string_tuple(control.get("enabled_environments"))
        if (
            enabled_environments
            and weather_environment != "auto"
            and weather_environment not in enabled_environments
        ):
            weather_environment = "auto"
        weather_caption = control.get("weather_caption")
        if not isinstance(weather_caption, bool):
            weather_caption = cfg.weather_caption
        weather_units = control.get("weather_units")
        if weather_units not in ("imperial", "metric"):
            weather_units = cfg.weather_units
        photo_caption = control.get("photo_caption")
        if not isinstance(photo_caption, str):
            photo_caption = cfg.photo_caption
        photo_rotation = control.get("photo_rotation")
        if photo_rotation not in (0, 90, 180, 270):
            photo_rotation = cfg.photo_rotation
        photo_crop = normalized_photo_crop(control.get("photo_crop"))
        recommendation_count = control.get("recommendation_count")
        if not isinstance(recommendation_count, int) or isinstance(recommendation_count, bool):
            recommendation_count = 5
        minimum_suitability = control.get("minimum_suitability")
        if isinstance(minimum_suitability, bool) or not isinstance(minimum_suitability, (int, float)):
            minimum_suitability = 0.0
        settings_label = (
            f"dither={cfg.conversion.dither} · saturation={cfg.conversion.saturation:.2f} · "
            f"blue bias={cfg.conversion.blue_bias:.2f}"
        )
        # An explicit page/file source remains authoritative. With no explicit
        # source, the validated control overlay selects the keyless regional
        # BirdWeather integration and supplies its bounded query settings.
        bird_provider = ""
        if not cfg.bird_source:
            candidate = control.get("bird_provider", "birdweather")
            bird_provider = candidate if candidate == "birdweather" else ""
        bird_postal_code = control.get("bird_postal_code", "84601")
        if not isinstance(bird_postal_code, str) or not bird_postal_code.strip():
            bird_postal_code = "84601"
        bird_country = control.get("bird_country", "us")
        if not isinstance(bird_country, str) or len(bird_country.strip()) != 2:
            bird_country = "us"
        bird_lookback_days = control.get("bird_lookback_days", 7)
        if (
            isinstance(bird_lookback_days, bool)
            or not isinstance(bird_lookback_days, int)
            or not 1 <= bird_lookback_days <= 30
        ):
            bird_lookback_days = 7
        bird_title = control.get("bird_title", "Avian Visitors")
        if not isinstance(bird_title, str) or not bird_title.strip():
            bird_title = "Avian Visitors"
        bird_subtitle = control.get("bird_subtitle", "Nearby This Week")
        if not isinstance(bird_subtitle, str) or not bird_subtitle.strip():
            bird_subtitle = "Nearby This Week"
        direction = STAR_DIRECTION_DEGREES.get(
            control.get("star_direction"),
            cfg.direction,
        )
        return RenderContext(
            orientation=cfg.orientation,
            when=when,
            location=location.strip(),
            config_path=cfg.config_path,
            offline=cfg.weather_offline,
            options={
                "bird_source": cfg.bird_source,
                "bird_provider": bird_provider,
                "bird_postal_code": bird_postal_code.strip(),
                "bird_country": bird_country.strip().casefold(),
                "bird_lookback_days": bird_lookback_days,
                "bird_title": bird_title.strip(),
                "bird_subtitle": bird_subtitle.strip(),
                "demo_birds": cfg.bird_demo,
                "avian_repo": str(cfg.avian_weather_repo or ""),
                "weather_repo": str(cfg.avian_weather_repo or ""),
                "inkystarmap_repo": str(cfg.inkystarmap_repo or ""),
                "starmap_source": str(cfg.starmap_source or ""),
                "photo_path": str(photo_path or ""),
                "avian_python": str(cfg.avian_python or ""),
                "allow_demo_fallback": allow_demo,
                "use_inkystarmap": cfg.use_inkystarmap,
                "dark_starmap": cfg.dark_starmap,
                "latitude": cfg.latitude,
                "longitude": cfg.longitude,
                "direction": direction,
                "timezone": cfg.timezone,
                "sky_event_cache_path": str(
                    (
                        Path(os.environ["XDG_CACHE_HOME"]).expanduser()
                        if os.environ.get("XDG_CACHE_HOME")
                        else cfg.output_directory / ".cache"
                    )
                    / "sky-events-v1.json"
                ),
                "weather_style": cfg.weather_style,
                "weather_scene_source": cfg.weather_scene_source,
                "weather_environment": weather_environment.strip(),
                "enabled_environments": enabled_environments,
                "enabled_activity_ids": _string_tuple(control.get("enabled_activity_ids")),
                "activity_overrides": dict(_mapping(control.get("activity_overrides"))),
                "recommendation_count": recommendation_count,
                "minimum_suitability": float(minimum_suitability),
                "weather_caption": weather_caption,
                "weather_units": weather_units,
                "country_code": cfg.weather_country_code,
                "weather_timeout": cfg.weather_timeout,
                "weather_condition": cfg.weather_condition,
                "rotation": photo_rotation,
                "caption": photo_caption,
                "photo_crop": photo_crop,
                "photo_enabled": control.get("photo_enabled", True) is not False,
                "settings_label": settings_label,
            },
        )

    def _provenance(self, mode: str, source_name: str) -> str:
        cfg = self.config
        lowered = source_name.lower()
        if mode in ("uploaded-photo", "test-pattern"):
            return "file" if mode == "uploaded-photo" else "synthetic"
        if mode == "weather" and cfg.weather_offline:
            return "fixture"
        if mode == "birds" and cfg.bird_source and not cfg.bird_source.startswith(("http://", "https://")):
            return "file"
        if mode == "star-map" and cfg.starmap_source is not None:
            return "file"
        if "repository sample" in lowered or " sample" in lowered:
            return "sample"
        if "synthetic" in lowered:
            return "synthetic"
        if any(marker in lowered for marker in ("demo", "fallback", "unavailable", "fixture")):
            return "fixture"
        return "live"

    def _manifest(
        self,
        *,
        mode: str,
        result,
        provenance: str,
        when: datetime,
        frame_path: Path,
        wire_path: Path,
        encoded: EncodedEE02Frame,
        rgb_path: Path | None,
        mode_directory: Path,
        context: RenderContext,
        photo_metadata: Mapping[str, Any] | None,
    ) -> dict[str, Any]:
        files: dict[str, Any] = {
            "eink_png": {
                "path": str(frame_path.relative_to(mode_directory)),
                "bytes": frame_path.stat().st_size,
                "sha256": _file_sha256(frame_path),
            },
            "ee02_4bpp": {
                "path": str(wire_path.relative_to(mode_directory)),
                "bytes": wire_path.stat().st_size,
                "sha256": encoded.sha256,
            },
        }
        if rgb_path is not None:
            files["rgb_png"] = {
                "path": str(rgb_path.relative_to(mode_directory)),
                "bytes": rgb_path.stat().st_size,
                "sha256": _file_sha256(rgb_path),
            }
        manifest = {
            "schema_version": SCHEMA_VERSION,
            "format": "eink-frame-artifacts-v2",
            "mode": mode,
            "source": {"name": result.source_name, "provenance": provenance},
            "rendered_for": when.isoformat(),
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "orientation": self.config.orientation.value,
            "dimensions": {"width": result.eink_image.width, "height": result.eink_image.height},
            "palette": {"name": "spectra6-monitor-rgb-v1", "colors": [list(color) for color in SPECTRA_PALETTE]},
            "pixel_checksum": {"algorithm": "sha256-dimensions-rgb-v1", "value": result.checksum},
            "wire": {
                "format": EE02_WIRE_FORMAT,
                "bits_per_pixel": 4,
                "buffer_dimensions": {
                    "width": encoded.buffer_width,
                    "height": encoded.buffer_height,
                },
                "logical_dimensions": {
                    "width": encoded.logical_width,
                    "height": encoded.logical_height,
                },
                "rotation": encoded.rotation,
                "seeed_sprite_rotation": encoded.seeed_sprite_rotation,
                "pixel_order": "row-major",
                "nibble_order": "even-x-high-odd-x-low",
                "color_codes": {
                    name: f"0x{code:X}" for name, code in EE02_NAMED_COLOR_CODES.items()
                },
                "bytes": encoded.payload_bytes,
                "sha256": encoded.sha256,
            },
            "files": files,
            "timings": {
                "source_seconds": round(result.source_seconds, 6),
                "conversion_seconds": round(result.conversion_seconds, 6),
            },
        }
        if (
            mode == "star-map"
            and _DIRECTION_AWARE_STAR_SOURCE in result.source_name
        ):
            direction_degrees = context.options.get("direction")
            if (
                isinstance(direction_degrees, bool)
                or not isinstance(direction_degrees, int)
                or not 0 <= direction_degrees < 360
            ):
                return manifest
            cardinal = {
                0: "N",
                90: "E",
                180: "S",
                270: "W",
            }.get(direction_degrees)
            manifest["view"] = {"direction_degrees": direction_degrees}
            if cardinal is not None:
                manifest["view"]["direction_cardinal"] = cardinal
            manifest["view"].update(_star_view_metadata(context.options))
        if mode == "uploaded-photo":
            if photo_metadata is None:
                raise RuntimeRenderError("uploaded-photo recipe metadata is unavailable")
            manifest["photo"] = dict(photo_metadata)
        return manifest

    def render(
        self,
        requested_mode: str,
        *,
        when: datetime | None = None,
        allow_demo: bool = False,
        force: bool = False,
    ) -> RuntimeArtifact:
        cfg = self.config
        zone = ZoneInfo(cfg.timezone)
        when = when or datetime.now(zone)
        when = when.replace(tzinfo=zone) if when.tzinfo is None else when.astimezone(zone)
        requested = canonical_mode(requested_mode)
        mode = self.resolve_mode(requested, when)

        effective_allow_demo = allow_demo or not cfg.strict_sources
        if requested == "automatic" and effective_allow_demo:
            raise SourcePolicyError("automatic mode never permits demo, fixture, sample, or fallback sources")
        strict = requested == "automatic" or not effective_allow_demo
        control = self._control_settings()
        self._preflight(mode, strict=strict, control=control)

        mode_directory = cfg.output_directory / mode
        frames_directory = mode_directory / "frames"
        current_path = mode_directory / "current.json"
        with _mode_lock(mode_directory):
            source = self.source_factories[mode]()
            context = self._context(
                when,
                allow_demo=not strict,
                control=control,
            )
            conversion = (
                cfg.photo_conversion
                if mode == "uploaded-photo"
                else cfg.conversion
            )
            result = ImagePipeline().render(
                source,
                context,
                conversion,
                cfg.fit_mode,
            )
            provenance = self._provenance(mode, result.source_name)
            if strict and provenance not in ("live", "file") and mode != "test-pattern":
                raise SourcePolicyError(
                    f"{mode} produced {provenance} content ({result.source_name}); last-known-good frame retained"
                )

            checksum = result.checksum
            encoded = encode_ee02(result.eink_image, cfg.landscape_rotation)
            frame_path = frames_directory / f"{checksum}.png"
            wire_path = frames_directory / f"{encoded.sha256}.ee02"
            rgb_path = frames_directory / f"{checksum}.rgb.png" if cfg.write_rgb else None
            current = _load_manifest(current_path)
            current_wire_checksum = ((current or {}).get("wire") or {}).get("sha256")
            target_photo_metadata = (
                _photo_manifest_metadata(context)
                if mode == "uploaded-photo"
                else None
            )
            current_photo = _mapping((current or {}).get("photo"))
            photo_recipe_changed = (
                mode == "uploaded-photo"
                and target_photo_metadata is not None
                and current_photo.get("recipe_sha256")
                != target_photo_metadata["recipe_sha256"]
            )
            current_view = (current or {}).get("view") or {}
            current_direction = current_view.get("direction_degrees")
            source_applies_direction = (
                _DIRECTION_AWARE_STAR_SOURCE in result.source_name
            )
            target_star_metadata = _star_view_metadata(context.options)
            star_view_changed = (
                mode == "star-map"
                and (
                    current_direction != context.options.get("direction")
                    if source_applies_direction
                    else current_direction is not None
                )
            )
            if mode == "star-map" and source_applies_direction:
                star_view_changed = star_view_changed or any(
                    current_view.get(key) != target_star_metadata.get(key)
                    for key in (
                        "observation_time",
                        "sunrise_time",
                        "night_date",
                        "featured_constellation",
                        "rare_events_digest",
                        "rare_events_generated_at",
                    )
                )
            frame_valid = _native_file_is_valid(frame_path, checksum, result.eink_image.size)
            wire_valid = _binary_file_is_valid(
                wire_path, EE02_PAYLOAD_BYTES, encoded.sha256
            )
            rgb_valid = rgb_path is None or rgb_path.is_file()
            changed = (
                current_wire_checksum != encoded.sha256
                or star_view_changed
                or photo_recipe_changed
            )

            if not force and not changed and frame_valid and wire_valid and rgb_valid:
                return RuntimeArtifact(
                    requested_mode=requested,
                    mode=mode,
                    changed=False,
                    written=False,
                    checksum=checksum,
                    wire_checksum=encoded.sha256,
                    frame_path=frame_path,
                    wire_path=wire_path,
                    wire_rotation=encoded.rotation,
                    seeed_sprite_rotation=encoded.seeed_sprite_rotation,
                    rgb_path=rgb_path,
                    manifest_path=current_path,
                    width=result.eink_image.width,
                    height=result.eink_image.height,
                    source_name=result.source_name,
                    provenance=provenance,
                    rendered_for=when,
                    source_seconds=result.source_seconds,
                    conversion_seconds=result.conversion_seconds,
                )

            frames_directory.mkdir(parents=True, exist_ok=True)
            if force or not frame_valid:
                _atomic_save_png(result.eink_image, frame_path)
            if rgb_path is not None and (force or not rgb_valid):
                _atomic_save_png(result.rgb_image, rgb_path)
            if force or not wire_valid:
                _atomic_write_bytes(encoded.payload, wire_path)
            manifest = self._manifest(
                mode=mode,
                result=result,
                provenance=provenance,
                when=when,
                frame_path=frame_path,
                wire_path=wire_path,
                encoded=encoded,
                rgb_path=rgb_path,
                mode_directory=mode_directory,
                context=context,
                photo_metadata=target_photo_metadata,
            )
            _atomic_write_json(manifest, current_path)
            return RuntimeArtifact(
                requested_mode=requested,
                mode=mode,
                changed=changed,
                written=True,
                checksum=checksum,
                wire_checksum=encoded.sha256,
                frame_path=frame_path,
                wire_path=wire_path,
                wire_rotation=encoded.rotation,
                seeed_sprite_rotation=encoded.seeed_sprite_rotation,
                rgb_path=rgb_path,
                manifest_path=current_path,
                width=result.eink_image.width,
                height=result.eink_image.height,
                source_name=result.source_name,
                provenance=provenance,
                rendered_for=when,
                source_seconds=result.source_seconds,
                conversion_seconds=result.conversion_seconds,
            )

    def status(self, mode: str | None = None) -> dict[str, dict[str, Any]]:
        modes = (canonical_mode(mode),) if mode else (*SCHEDULED_MODES, "uploaded-photo", "test-pattern")
        if "automatic" in modes:
            raise ValueError("status needs a concrete mode, not automatic")
        result: dict[str, dict[str, Any]] = {}
        for item in modes:
            manifest = _load_manifest(self.config.output_directory / item / "current.json")
            if manifest is not None:
                result[item] = manifest
        return result

    def check(self) -> dict[str, Any]:
        readiness: dict[str, dict[str, Any]] = {}
        control = self._control_settings()
        for mode in (*SCHEDULED_MODES, "uploaded-photo", "test-pattern"):
            try:
                self._preflight(mode, strict=True, control=control)
            except Exception as exc:
                readiness[mode] = {"ready": False, "reason": str(exc)}
            else:
                readiness[mode] = {"ready": True, "reason": "ready"}
        return {
            "config_path": str(self.config.config_path) if self.config.config_path else None,
            "output_directory": str(self.config.output_directory),
            "orientation": self.config.orientation.value,
            "timezone": self.config.timezone,
            "strict_sources": self.config.strict_sources,
            "control_settings": {
                "path": str(self.config.control_settings_path)
                if self.config.control_settings_path
                else None,
                "loaded": bool(control),
            },
            "headless": True,
            "ee02": {
                "format": EE02_WIRE_FORMAT,
                "buffer_dimensions": {
                    "width": EE02_BUFFER_WIDTH,
                    "height": EE02_BUFFER_HEIGHT,
                },
                "bytes": EE02_PAYLOAD_BYTES,
                "landscape_rotation": self.config.landscape_rotation.value,
                "seeed_sprite_rotation": self.config.landscape_rotation.seeed_sprite_rotation,
            },
            "server": {
                "host": self.config.server_host,
                "port": self.config.server_port,
                "authentication_configured": bool(self.config.server_auth_token),
                "max_connections": self.config.server_max_connections,
                "request_timeout": self.config.server_request_timeout,
            },
            "esp_client": {
                "server_url": self.config.esp_server_url,
                "state_directory": str(self.config.esp_state_directory),
            },
            "modes": readiness,
        }
