"""Validated, versioned settings for the phone-friendly control panel."""

from __future__ import annotations

import importlib
import importlib.util
import hashlib
import json
import math
import os
import platform
import re
import sys
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping


SCHEMA_VERSION = 2
DISPLAY_MODES = (
    "automatic",
    "weather",
    "birds",
    "star-map",
    "uploaded-photo",
)
RANGE_FIELDS = (
    "tolerable_min",
    "ideal_min",
    "ideal_max",
    "tolerable_max",
    "weight",
    "required",
)
BOUNDS_FIELDS = RANGE_FIELDS[:4]
ARTWORK_ALIASES = {
    "downhill_skiing": "skiing",
    "rock_climbing": "rock_climbing",
    "stand_up_paddleboarding": "paddleboarding",
    "hammocking": "hammocking",
    "cross_country_skiing": "skiing",
}
# Schema-v1 deployments used a Provo-specific scene ID. The current catalog
# represents that same local Wasatch setting with its Mount Timpanogos scene.
LEGACY_LOCATION_IDS = {
    "provo_utah": "mount_timpanogos",
}


class SettingsValidationError(ValueError):
    """Raised when a control-panel setting cannot be safely applied."""


def stable_id(name: str) -> str:
    """Return the stable ASCII identifier used in persisted settings."""
    return re.sub(r"[^a-z0-9]+", "_", name.casefold()).strip("_")


@dataclass(frozen=True)
class LocationDefinition:
    id: str
    name: str
    description: str
    has_artwork: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "has_artwork": self.has_artwork,
        }


@dataclass(frozen=True)
class ActivityDefinition:
    id: str
    name: str
    estimated_great_days: int
    toddler_friendly: bool
    conditions: dict[str, dict[str, float | bool]]
    has_artwork: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "estimated_great_days": self.estimated_great_days,
            "toddler_friendly": self.toddler_friendly,
            "conditions": self.conditions,
            "has_artwork": self.has_artwork,
        }


@dataclass(frozen=True)
class Catalog:
    weather_repo: Path
    locations: tuple[LocationDefinition, ...]
    activities: tuple[ActivityDefinition, ...]
    metrics: tuple[str, ...]

    @property
    def location_ids(self) -> tuple[str, ...]:
        return tuple(item.id for item in self.locations)

    @property
    def activity_ids(self) -> tuple[str, ...]:
        return tuple(item.id for item in self.activities)

    @property
    def activities_by_id(self) -> dict[str, ActivityDefinition]:
        return {item.id: item for item in self.activities}

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "locations": [item.as_dict() for item in self.locations],
            "activities": [item.as_dict() for item in self.activities],
            "metrics": list(self.metrics),
            "defaults": default_settings(self),
        }


def default_settings_path() -> Path:
    """Return a user-writable settings path on macOS and Linux/Raspberry Pi."""
    override = os.environ.get("EINK_CONTROL_SETTINGS", "").strip()
    if override:
        return Path(override).expanduser()
    if platform.system() == "Darwin":
        return (
            Path.home()
            / "Library"
            / "Application Support"
            / "EInk Display Simulator"
            / "control-panel.json"
        )
    config_home = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return config_home / "eink-display" / "control-panel.json"


def default_photo_path(settings_path: Path | None = None) -> Path:
    override = os.environ.get("EINK_CONTROL_PHOTO", "").strip()
    if override:
        return Path(override).expanduser()
    return (settings_path or default_settings_path()).parent / "latest-upload.png"


def _looks_like_weather_repo(path: Path) -> bool:
    return (path / "weather_frame" / "scene_catalog.py").is_file()


def discover_weather_repo(explicit: Path | str | None = None) -> Path:
    """Find AvianVisitors without relying on the process working directory."""
    candidates: list[Path] = []
    if explicit:
        candidates.append(Path(explicit).expanduser())
    for env_name in ("AVIAN_WEATHER_REPO", "WEATHER_FRAME_REPO", "AVIANVISITORS_REPO"):
        env_value = os.environ.get(env_name, "").strip()
        if env_value:
            candidates.append(Path(env_value).expanduser())
    package_root = Path(__file__).resolve().parent.parent
    for base in (Path.cwd(), package_root, package_root.parent):
        candidates.extend((base, base / "AvianVisitors", base / "peacock" / "AvianVisitors"))
    seen: set[Path] = set()
    for candidate in candidates:
        try:
            resolved = candidate.resolve()
        except OSError:
            continue
        if resolved in seen:
            continue
        seen.add(resolved)
        if _looks_like_weather_repo(resolved):
            return resolved
    requested = f" at {explicit}" if explicit else ""
    raise FileNotFoundError(
        "Could not find the AvianVisitors weather repository"
        f"{requested}. Pass --weather-repo /path/to/AvianVisitors."
    )


def _load_package_submodule(package_dir: Path, alias: str, submodule: str):
    """Load a repository package under a private name to avoid import collisions."""
    path_key = hashlib.sha1(str(package_dir.resolve()).encode("utf-8")).hexdigest()[:10]
    package_name = f"_eink_control_{alias}_{path_key}"
    module_name = f"{package_name}.{submodule}"
    existing = sys.modules.get(module_name)
    if existing is not None:
        return existing
    init_path = package_dir / "__init__.py"
    spec = importlib.util.spec_from_file_location(
        package_name,
        init_path,
        submodule_search_locations=[str(package_dir)],
    )
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load package from {package_dir}")
    package = importlib.util.module_from_spec(spec)
    sys.modules[package_name] = package
    try:
        spec.loader.exec_module(package)
        return importlib.import_module(module_name)
    except Exception:
        for key in tuple(sys.modules):
            if key == package_name or key.startswith(package_name + "."):
                sys.modules.pop(key, None)
        raise


def _discover_season_repo(weather_repo: Path) -> Path:
    candidates = (
        weather_repo / "season",
        weather_repo.parent / "season",
        weather_repo.parent.parent / "season",
        Path.cwd() / "season",
        Path.cwd() / "peacock" / "season",
    )
    for path in candidates:
        if (path / "activity_catalog.py").is_file() and (path / "__init__.py").is_file():
            return path.resolve()
    raise FileNotFoundError(
        "Could not find the season activity catalog beside AvianVisitors. "
        "Expected a season/activity_catalog.py repository."
    )


def _json_number(value: Any) -> float | int:
    number = float(value)
    return int(number) if number.is_integer() else number


def discover_catalog(weather_repo: Path | str | None = None) -> Catalog:
    """Load the live location and activity catalogs from the copied repositories."""
    repository = discover_weather_repo(weather_repo)
    scene_catalog = _load_package_submodule(
        repository / "weather_frame", "weather_frame", "scene_catalog"
    )
    season_repo = _discover_season_repo(repository)
    activity_catalog = _load_package_submodule(season_repo, "season", "activity_catalog")

    scene_root = repository / "weather_frame" / "assets" / "scenes"
    activity_root = repository / "weather_frame" / "assets" / "activity_scenes"
    locations = tuple(
        LocationDefinition(
            id=location_id,
            name=value.name,
            description=value.description,
            has_artwork=any((scene_root / location_id).glob("*.png")),
        )
        for location_id, value in scene_catalog.ENVIRONMENTS.items()
    )

    activities: list[ActivityDefinition] = []
    metric_order: list[str] = []
    used_ids: set[str] = set()
    for profile in activity_catalog.UTAH_OUTDOOR_ACTIVITIES:
        activity_id = stable_id(profile.name)
        if not activity_id or activity_id in used_ids:
            raise ValueError(f"Activity names do not produce unique IDs: {profile.name!r}")
        used_ids.add(activity_id)
        conditions: dict[str, dict[str, float | bool]] = {}
        for metric, value in profile.conditions.items():
            if metric not in metric_order:
                metric_order.append(metric)
            conditions[metric] = {
                "tolerable_min": _json_number(value.tolerable_min),
                "ideal_min": _json_number(value.ideal_min),
                "ideal_max": _json_number(value.ideal_max),
                "tolerable_max": _json_number(value.tolerable_max),
                "weight": _json_number(value.weight),
                "required": bool(value.required),
            }
        artwork_slug = ARTWORK_ALIASES.get(activity_id, activity_id)
        has_artwork = any((activity_root / artwork_slug).glob("**/*.png"))
        activities.append(
            ActivityDefinition(
                id=activity_id,
                name=profile.name,
                estimated_great_days=int(profile.estimated_great_days),
                toddler_friendly=bool(profile.toddler_friendly),
                conditions=conditions,
                has_artwork=has_artwork,
            )
        )
    if not locations:
        raise ValueError("The weather repository has no configured locations")
    return Catalog(repository, locations, tuple(activities), tuple(metric_order))


def default_settings(catalog: Catalog) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "enabled_locations": list(catalog.location_ids),
        "enabled_activities": list(catalog.activity_ids),
        "recommendation_count": 5,
        "minimum_suitability": 0.5,
        "activity_overrides": {},
        "display": {
            "location_name": "Provo, Utah",
            "units": "imperial",
            "caption": False,
            "mode": "automatic",
        },
        "birds": {
            "provider": "birdweather",
            "postal_code": "84601",
            "country": "us",
            "lookback_days": 7,
            "title": "Avian Visitors",
            "subtitle": "Nearby This Week",
        },
        "photo": {
            "caption": "",
            "rotation": 0,
            "enabled": False,
        },
    }


def _expect_mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise SettingsValidationError(f"{label} must be an object")
    return value


def _reject_unknown(value: Mapping[str, Any], allowed: set[str], label: str) -> None:
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise SettingsValidationError(f"Unknown {label} field: {unknown[0]}")


def _finite_number(value: Any, label: str) -> float | int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SettingsValidationError(f"{label} must be a number")
    number = float(value)
    if not math.isfinite(number):
        raise SettingsValidationError(f"{label} must be finite")
    return int(number) if number.is_integer() else number


def _bounded_integer(value: Any, minimum: int, maximum: int, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise SettingsValidationError(f"{label} must be an integer")
    if not minimum <= value <= maximum:
        raise SettingsValidationError(f"{label} must be from {minimum} to {maximum}")
    return value


def _id_list(value: Any, known: set[str], label: str, *, nonempty: bool) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise SettingsValidationError(f"{label} must be a list of IDs")
    if nonempty and not value:
        raise SettingsValidationError(f"{label} must contain at least one location")
    if len(value) != len(set(value)):
        raise SettingsValidationError(f"{label} cannot contain duplicate IDs")
    unknown = [item for item in value if item not in known]
    if unknown:
        raise SettingsValidationError(f"Unknown ID in {label}: {unknown[0]}")
    return list(value)


def _short_string(value: Any, label: str, maximum: int, *, allow_empty: bool = True) -> str:
    if not isinstance(value, str):
        raise SettingsValidationError(f"{label} must be text")
    value = value.strip()
    if not allow_empty and not value:
        raise SettingsValidationError(f"{label} cannot be empty")
    if len(value) > maximum:
        raise SettingsValidationError(f"{label} must be at most {maximum} characters")
    return value


def _validated_overrides(value: Any, catalog: Catalog) -> dict[str, Any]:
    overrides = _expect_mapping(value, "activity_overrides")
    defaults_by_id = catalog.activities_by_id
    result: dict[str, Any] = {}
    for activity_id, raw_activity in overrides.items():
        if activity_id not in defaults_by_id:
            raise SettingsValidationError(f"Unknown activity override: {activity_id}")
        activity = _expect_mapping(raw_activity, f"override for {activity_id}")
        _reject_unknown(activity, {"estimated_great_days", "conditions"}, "activity override")
        cleaned: dict[str, Any] = {}
        default_activity = defaults_by_id[activity_id]
        if "estimated_great_days" in activity:
            days = _bounded_integer(
                activity["estimated_great_days"], 0, 365, f"{activity_id}.estimated_great_days"
            )
            if days != default_activity.estimated_great_days:
                cleaned["estimated_great_days"] = days
        if "conditions" in activity:
            raw_conditions = _expect_mapping(activity["conditions"], f"{activity_id}.conditions")
            condition_result: dict[str, Any] = {}
            for metric, raw_range in raw_conditions.items():
                if metric not in default_activity.conditions:
                    raise SettingsValidationError(f"Unknown metric for {activity_id}: {metric}")
                range_value = _expect_mapping(raw_range, f"{activity_id}.{metric}")
                _reject_unknown(range_value, set(RANGE_FIELDS), "condition range")
                if not range_value:
                    continue
                baseline = default_activity.conditions[metric]
                resolved = dict(baseline)
                cleaned_range: dict[str, Any] = {}
                for field, raw in range_value.items():
                    label = f"{activity_id}.{metric}.{field}"
                    if field == "required":
                        if not isinstance(raw, bool):
                            raise SettingsValidationError(f"{label} must be true or false")
                        normalized: float | int | bool = raw
                    else:
                        normalized = _finite_number(raw, label)
                        if field == "weight" and normalized <= 0:
                            raise SettingsValidationError(f"{label} must be positive")
                    resolved[field] = normalized
                    if normalized != baseline[field]:
                        cleaned_range[field] = normalized
                bounds = [float(resolved[field]) for field in BOUNDS_FIELDS]
                if bounds != sorted(bounds):
                    raise SettingsValidationError(
                        f"{activity_id}.{metric} bounds must be ascending: "
                        "tolerable min ≤ ideal min ≤ ideal max ≤ tolerable max"
                    )
                if float(resolved["weight"]) <= 0:
                    raise SettingsValidationError(f"{activity_id}.{metric}.weight must be positive")
                if cleaned_range:
                    condition_result[metric] = cleaned_range
            if condition_result:
                cleaned["conditions"] = condition_result
        if cleaned:
            result[activity_id] = cleaned
    return result


def validate_settings(value: Any, catalog: Catalog) -> dict[str, Any]:
    """Normalize settings and reject unknown IDs, fields, and unsafe values."""
    data = _expect_mapping(value, "settings")
    allowed = {
        "schema_version",
        "enabled_locations",
        "enabled_activities",
        "recommendation_count",
        "minimum_suitability",
        "activity_overrides",
        "display",
        "birds",
        "photo",
    }
    _reject_unknown(data, allowed, "settings")
    defaults = default_settings(catalog)
    # Version 1 is migrated in memory by filling the v2 display-mode and
    # BirdWeather fields from defaults. The next successful save persists v2.
    version = data.get("schema_version", 1)
    if isinstance(version, bool) or version not in (1, SCHEMA_VERSION):
        raise SettingsValidationError(
            f"Unsupported schema_version {version!r}; expected 1 or {SCHEMA_VERSION}"
        )
    location_value = data.get("enabled_locations", defaults["enabled_locations"])
    if version == 1 and isinstance(location_value, list):
        location_value = [
            LEGACY_LOCATION_IDS.get(item, item) if isinstance(item, str) else item
            for item in location_value
        ]
    locations = _id_list(
        location_value,
        set(catalog.location_ids),
        "enabled_locations",
        nonempty=True,
    )
    activities = _id_list(
        data.get("enabled_activities", defaults["enabled_activities"]),
        set(catalog.activity_ids),
        "enabled_activities",
        nonempty=False,
    )
    count = _bounded_integer(
        data.get("recommendation_count", defaults["recommendation_count"]),
        1,
        10,
        "recommendation_count",
    )
    suitability = _finite_number(
        data.get("minimum_suitability", defaults["minimum_suitability"]),
        "minimum_suitability",
    )
    if not 0 <= suitability <= 1:
        raise SettingsValidationError("minimum_suitability must be from 0 to 1")
    overrides = _validated_overrides(data.get("activity_overrides", {}), catalog)

    display = _expect_mapping(data.get("display", defaults["display"]), "display")
    _reject_unknown(display, {"location_name", "units", "caption", "mode"}, "display")
    location_name = _short_string(
        display.get("location_name", defaults["display"]["location_name"]),
        "display.location_name",
        120,
        allow_empty=False,
    )
    units = display.get("units", defaults["display"]["units"])
    if units not in ("imperial", "metric"):
        raise SettingsValidationError("display.units must be imperial or metric")
    caption = display.get("caption", defaults["display"]["caption"])
    if not isinstance(caption, bool):
        raise SettingsValidationError("display.caption must be true or false")
    display_mode = display.get("mode", defaults["display"]["mode"])
    if display_mode not in DISPLAY_MODES:
        raise SettingsValidationError(
            "display.mode must be automatic, weather, birds, star-map, or uploaded-photo"
        )

    birds = _expect_mapping(data.get("birds", defaults["birds"]), "birds")
    _reject_unknown(
        birds,
        {"provider", "postal_code", "country", "lookback_days", "title", "subtitle"},
        "birds",
    )
    provider = birds.get("provider", defaults["birds"]["provider"])
    if provider != "birdweather":
        raise SettingsValidationError("birds.provider must be birdweather")
    postal_code = _short_string(
        birds.get("postal_code", defaults["birds"]["postal_code"]),
        "birds.postal_code",
        10,
        allow_empty=False,
    )
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 -]{1,9}", postal_code) is None:
        raise SettingsValidationError("birds.postal_code must be a valid postal code")
    country = _short_string(
        birds.get("country", defaults["birds"]["country"]),
        "birds.country",
        2,
        allow_empty=False,
    ).casefold()
    if re.fullmatch(r"[a-z]{2}", country) is None:
        raise SettingsValidationError("birds.country must be a two-letter country code")
    lookback_days = _bounded_integer(
        birds.get("lookback_days", defaults["birds"]["lookback_days"]),
        1,
        30,
        "birds.lookback_days",
    )
    bird_title = _short_string(
        birds.get("title", defaults["birds"]["title"]),
        "birds.title",
        80,
        allow_empty=False,
    )
    bird_subtitle = _short_string(
        birds.get("subtitle", defaults["birds"]["subtitle"]),
        "birds.subtitle",
        120,
        allow_empty=False,
    )

    photo = _expect_mapping(data.get("photo", defaults["photo"]), "photo")
    _reject_unknown(photo, {"caption", "rotation", "enabled"}, "photo")
    photo_caption = _short_string(
        photo.get("caption", defaults["photo"]["caption"]), "photo.caption", 200
    )
    rotation = photo.get("rotation", defaults["photo"]["rotation"])
    if isinstance(rotation, bool) or rotation not in (0, 90, 180, 270):
        raise SettingsValidationError("photo.rotation must be 0, 90, 180, or 270")
    photo_enabled = photo.get("enabled", defaults["photo"]["enabled"])
    if not isinstance(photo_enabled, bool):
        raise SettingsValidationError("photo.enabled must be true or false")

    return {
        "schema_version": SCHEMA_VERSION,
        "enabled_locations": locations,
        "enabled_activities": activities,
        "recommendation_count": count,
        "minimum_suitability": suitability,
        "activity_overrides": overrides,
        "display": {
            "location_name": location_name,
            "units": units,
            "caption": caption,
            "mode": display_mode,
        },
        "birds": {
            "provider": provider,
            "postal_code": postal_code,
            "country": country,
            "lookback_days": lookback_days,
            "title": bird_title,
            "subtitle": bird_subtitle,
        },
        "photo": {
            "caption": photo_caption,
            "rotation": int(rotation),
            "enabled": photo_enabled,
        },
    }


def resolve_activities(catalog: Catalog, settings: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Merge sparse persisted overrides into catalog defaults for API/runtime use."""
    overrides = settings.get("activity_overrides", {})
    resolved: list[dict[str, Any]] = []
    for activity in catalog.activities:
        value = activity.as_dict()
        value["conditions"] = {
            metric: dict(bounds) for metric, bounds in activity.conditions.items()
        }
        override = overrides.get(activity.id, {})
        value["estimated_great_days"] = override.get(
            "estimated_great_days", activity.estimated_great_days
        )
        for metric, fields in override.get("conditions", {}).items():
            value["conditions"][metric].update(fields)
        value["enabled"] = activity.id in settings.get("enabled_activities", ())
        value["overridden"] = bool(override)
        resolved.append(value)
    return resolved


def load_settings(
    path: Path | str | None = None,
    catalog: Catalog | None = None,
    weather_repo: Path | str | None = None,
    *,
    strict: bool = False,
) -> dict[str, Any]:
    """Load settings, optionally propagating missing, corrupt, or invalid data."""
    catalog = catalog or discover_catalog(weather_repo)
    settings_path = Path(path).expanduser() if path is not None else default_settings_path()
    if strict:
        raw = json.loads(settings_path.read_text(encoding="utf-8"))
        return validate_settings(raw, catalog)
    try:
        raw = json.loads(settings_path.read_text(encoding="utf-8"))
        return validate_settings(raw, catalog)
    except (OSError, json.JSONDecodeError, UnicodeError, SettingsValidationError, TypeError):
        return default_settings(catalog)


def save_settings(
    path: Path | str,
    value: Any,
    catalog: Catalog | None = None,
    weather_repo: Path | str | None = None,
) -> dict[str, Any]:
    """Validate and atomically persist settings; return their normalized form."""
    catalog = catalog or discover_catalog(weather_repo)
    normalized = validate_settings(value, catalog)
    settings_path = Path(path).expanduser()
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(normalized, indent=2, sort_keys=True) + "\n"
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=settings_path.name + ".", suffix=".tmp", dir=settings_path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(settings_path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
    return normalized


class SettingsStore:
    """Thread-safe facade used by HTTP handlers and render processes."""

    def __init__(self, path: Path | str, catalog: Catalog):
        self.path = Path(path).expanduser()
        self.catalog = catalog
        self._lock = threading.RLock()

    def load(self) -> dict[str, Any]:
        with self._lock:
            return load_settings(self.path, self.catalog)

    def save(self, value: Any) -> dict[str, Any]:
        with self._lock:
            return save_settings(self.path, value, self.catalog)

    def reset(self) -> dict[str, Any]:
        with self._lock:
            return save_settings(self.path, default_settings(self.catalog), self.catalog)
