from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path
import os
from typing import Any, Mapping
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import tomllib

from display_simulator.models import ConversionSettings, FitMode, Orientation
from display_simulator.schedule import ScheduleConfig, parse_clock

from .ee02 import EE02EncodingError, LandscapeRotation, parse_landscape_rotation


class ConfigError(ValueError):
    """Raised when a runtime configuration cannot be loaded safely."""


@dataclass(frozen=True, slots=True)
class RuntimeConfig:
    config_path: Path | None
    strict_sources: bool
    orientation: Orientation
    fit_mode: FitMode
    location: str
    latitude: float
    longitude: float
    direction: int
    timezone: str
    schedule: ScheduleConfig
    conversion: ConversionSettings
    landscape_rotation: LandscapeRotation
    output_directory: Path
    write_rgb: bool
    server_host: str
    server_port: int
    server_auth_token: str = field(repr=False)
    server_chunk_size: int
    server_max_connections: int
    server_request_timeout: float
    esp_server_url: str
    esp_state_directory: Path
    esp_timeout: float
    esp_chunk_size: int
    avian_weather_repo: Path | None
    inkystarmap_repo: Path | None
    bird_source: str
    bird_demo: bool
    starmap_source: Path | None
    photo_path: Path | None
    avian_python: Path | None
    use_inkystarmap: bool
    dark_starmap: bool
    weather_offline: bool
    weather_style: str
    weather_scene_source: str
    weather_environment: str
    weather_caption: bool
    weather_units: str
    weather_country_code: str
    weather_timeout: float
    weather_condition: str
    photo_rotation: int
    photo_caption: str


def default_user_config_path() -> Path:
    override = os.environ.get("DISPLAY_RUNTIME_CONFIG", "").strip()
    if override:
        return Path(override).expanduser()
    config_home = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return config_home / "eink-display" / "runtime.toml"


def _merge(base: dict[str, Any], updates: Mapping[str, Any]) -> dict[str, Any]:
    for key, value in updates.items():
        if isinstance(value, Mapping) and isinstance(base.get(key), dict):
            _merge(base[key], value)
        else:
            base[key] = value
    return base


def _reject_unknown_keys(updates: Mapping[str, Any], defaults: Mapping[str, Any], prefix: str = "") -> None:
    for key, value in updates.items():
        name = f"{prefix}.{key}" if prefix else str(key)
        if key not in defaults:
            raise ConfigError(f"unknown configuration key {name!r}")
        default_value = defaults[key]
        if isinstance(default_value, Mapping) and not isinstance(value, Mapping):
            raise ConfigError(f"configuration key {name!r} must be a table")
        if isinstance(value, Mapping):
            if not isinstance(default_value, Mapping):
                raise ConfigError(f"configuration key {name!r} must not be a table")
            _reject_unknown_keys(value, default_value, name)


def _read_toml(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as handle:
            value = tomllib.load(handle)
    except FileNotFoundError as exc:
        raise ConfigError(f"configuration file not found: {path}") from exc
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"could not read configuration {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ConfigError(f"configuration root must be a TOML table: {path}")
    return value


def _string(value: Any, name: str, *, allow_empty: bool = True) -> str:
    if not isinstance(value, str):
        raise ConfigError(f"{name} must be a string")
    value = value.strip()
    if not allow_empty and not value:
        raise ConfigError(f"{name} must not be empty")
    return value


def _boolean(value: Any, name: str) -> bool:
    if not isinstance(value, bool):
        raise ConfigError(f"{name} must be true or false")
    return value


def _number(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"{name} must be a number")
    return float(value)


def _integer(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"{name} must be an integer")
    return value


def _path(value: Any, name: str, base: Path) -> Path | None:
    text = _string(value, name)
    if not text:
        return None
    path = Path(text).expanduser()
    if not path.is_absolute():
        path = base / path
    return path.resolve(strict=False)


def _bird_source(value: Any, base: Path) -> str:
    text = _string(value, "sources.bird")
    if not text or text.startswith(("http://", "https://")):
        return text
    path = Path(text).expanduser()
    if not path.is_absolute():
        path = base / path
    return str(path.resolve(strict=False))


def _fit_mode(value: Any) -> FitMode:
    text = _string(value, "display.fit_mode", allow_empty=False).lower()
    aliases = {
        "crop": FitMode.CROP,
        "crop-to-fill": FitMode.CROP,
        FitMode.CROP.value.lower(): FitMode.CROP,
        "fit": FitMode.FIT,
        "contain": FitMode.FIT,
        "fit-with-border": FitMode.FIT,
        FitMode.FIT.value.lower(): FitMode.FIT,
        "stretch": FitMode.STRETCH,
        FitMode.STRETCH.value.lower(): FitMode.STRETCH,
    }
    try:
        return aliases[text]
    except KeyError as exc:
        raise ConfigError("display.fit_mode must be crop, fit, or stretch") from exc


def _landscape_rotation(value: Any) -> LandscapeRotation:
    text = _string(value, "ee02.landscape_rotation", allow_empty=False)
    try:
        return parse_landscape_rotation(text)
    except EE02EncodingError as exc:
        raise ConfigError(str(exc)) from exc


def _parse(data: Mapping[str, Any], path: Path | None) -> RuntimeConfig:
    base = path.parent if path else Path.cwd()
    runtime = data["runtime"]
    display = data["display"]
    location = data["location"]
    schedule_data = data["schedule"]
    conversion = data["conversion"]
    ee02 = data["ee02"]
    repositories = data["repositories"]
    sources = data["sources"]
    weather = data["weather"]
    photo = data["photo"]
    output = data["output"]
    server = data["server"]
    esp_client = data["esp_client"]

    try:
        orientation = Orientation(_string(display["orientation"], "display.orientation", allow_empty=False).lower())
    except ValueError as exc:
        raise ConfigError("display.orientation must be landscape or portrait") from exc

    latitude = _number(location["latitude"], "location.latitude")
    longitude = _number(location["longitude"], "location.longitude")
    direction = _integer(location["direction"], "location.direction")
    if not -90 <= latitude <= 90:
        raise ConfigError("location.latitude must be between -90 and 90")
    if not -180 <= longitude <= 180:
        raise ConfigError("location.longitude must be between -180 and 180")
    if not 0 <= direction <= 359:
        raise ConfigError("location.direction must be between 0 and 359")
    timezone = _string(location["timezone"], "location.timezone", allow_empty=False)
    try:
        ZoneInfo(timezone)
    except ZoneInfoNotFoundError as exc:
        raise ConfigError(f"unknown location.timezone {timezone!r}") from exc

    try:
        schedule = ScheduleConfig(
            parse_clock(_string(schedule_data["weather_start"], "schedule.weather_start")),
            parse_clock(_string(schedule_data["birds_start"], "schedule.birds_start")),
            parse_clock(_string(schedule_data["star_start"], "schedule.star_start")),
        )
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"schedule times must use 24-hour HH:MM values: {exc}") from exc
    if not schedule.weather_start < schedule.birds_start < schedule.star_start:
        raise ConfigError("schedule must satisfy weather_start < birds_start < star_start")

    saturation = _number(conversion["saturation"], "conversion.saturation")
    blue_bias = _number(conversion["blue_bias"], "conversion.blue_bias")
    if not 0 <= saturation <= 1:
        raise ConfigError("conversion.saturation must be between 0 and 1")
    if not 0 <= blue_bias <= 1:
        raise ConfigError("conversion.blue_bias must be between 0 and 1")
    method = _string(conversion["method"], "conversion.method", allow_empty=False).lower()
    if method not in ("floyd-steinberg", "none"):
        raise ConfigError("conversion.method must be floyd-steinberg or none")
    conversion_settings = ConversionSettings(
        dither=_boolean(conversion["dithering"], "conversion.dithering"),
        dither_method=method,
        saturation=saturation,
        blue_bias=blue_bias,
    )

    output_directory = _path(output["directory"], "output.directory", base)
    if output_directory is None:
        raise ConfigError("output.directory must not be empty")
    rotation = _integer(photo["rotation"], "photo.rotation")
    if rotation not in (0, 90, 180, 270):
        raise ConfigError("photo.rotation must be 0, 90, 180, or 270")
    timeout = _number(weather["timeout"], "weather.timeout")
    if timeout <= 0:
        raise ConfigError("weather.timeout must be greater than zero")
    weather_units = _string(weather["units"], "weather.units", allow_empty=False)
    if weather_units not in ("imperial", "metric"):
        raise ConfigError("weather.units must be imperial or metric")

    server_port = _integer(server["port"], "server.port")
    if not 1 <= server_port <= 65535:
        raise ConfigError("server.port must be between 1 and 65535")
    server_chunk_size = _integer(server["chunk_size"], "server.chunk_size")
    if server_chunk_size <= 0:
        raise ConfigError("server.chunk_size must be greater than zero")
    server_max_connections = _integer(
        server["max_connections"], "server.max_connections"
    )
    if server_max_connections <= 0:
        raise ConfigError("server.max_connections must be greater than zero")
    server_request_timeout = _number(
        server["request_timeout"], "server.request_timeout"
    )
    if server_request_timeout <= 0:
        raise ConfigError("server.request_timeout must be greater than zero")
    esp_chunk_size = _integer(esp_client["chunk_size"], "esp_client.chunk_size")
    if esp_chunk_size <= 0:
        raise ConfigError("esp_client.chunk_size must be greater than zero")
    esp_timeout = _number(esp_client["timeout"], "esp_client.timeout")
    if esp_timeout <= 0:
        raise ConfigError("esp_client.timeout must be greater than zero")
    esp_state_directory = _path(esp_client["state_directory"], "esp_client.state_directory", base)
    if esp_state_directory is None:
        raise ConfigError("esp_client.state_directory must not be empty")
    auth_token = os.environ.get("DISPLAY_RUNTIME_AUTH_TOKEN")
    if auth_token is None:
        auth_token = _string(server["auth_token"], "server.auth_token")
    elif not auth_token:
        raise ConfigError("DISPLAY_RUNTIME_AUTH_TOKEN must not be empty when it is set")

    return RuntimeConfig(
        config_path=path,
        strict_sources=_boolean(runtime["strict_sources"], "runtime.strict_sources"),
        orientation=orientation,
        fit_mode=_fit_mode(display["fit_mode"]),
        location=_string(location["name"], "location.name", allow_empty=False),
        latitude=latitude,
        longitude=longitude,
        direction=direction,
        timezone=timezone,
        schedule=schedule,
        conversion=conversion_settings,
        landscape_rotation=_landscape_rotation(ee02["landscape_rotation"]),
        output_directory=output_directory,
        write_rgb=_boolean(output["write_rgb"], "output.write_rgb"),
        server_host=_string(server["host"], "server.host", allow_empty=False),
        server_port=server_port,
        server_auth_token=auth_token,
        server_chunk_size=server_chunk_size,
        server_max_connections=server_max_connections,
        server_request_timeout=server_request_timeout,
        esp_server_url=_string(esp_client["server_url"], "esp_client.server_url", allow_empty=False),
        esp_state_directory=esp_state_directory,
        esp_timeout=esp_timeout,
        esp_chunk_size=esp_chunk_size,
        avian_weather_repo=_path(repositories["avian_weather"], "repositories.avian_weather", base),
        inkystarmap_repo=_path(repositories["inkystarmap"], "repositories.inkystarmap", base),
        bird_source=_bird_source(sources["bird"], base),
        bird_demo=_boolean(sources["bird_demo"], "sources.bird_demo"),
        starmap_source=_path(sources["starmap"], "sources.starmap", base),
        photo_path=_path(sources["photo"], "sources.photo", base),
        avian_python=_path(sources["avian_python"], "sources.avian_python", base),
        use_inkystarmap=_boolean(sources["use_inkystarmap"], "sources.use_inkystarmap"),
        dark_starmap=_boolean(sources["dark_starmap"], "sources.dark_starmap"),
        weather_offline=_boolean(weather["offline"], "weather.offline"),
        weather_style=_string(weather["style"], "weather.style", allow_empty=False),
        weather_scene_source=_string(weather["scene_source"], "weather.scene_source", allow_empty=False),
        weather_environment=_string(weather["environment"], "weather.environment", allow_empty=False),
        weather_caption=_boolean(weather["caption"], "weather.caption"),
        weather_units=weather_units,
        weather_country_code=_string(weather["country_code"], "weather.country_code"),
        weather_timeout=timeout,
        weather_condition=_string(weather["condition"], "weather.condition", allow_empty=False),
        photo_rotation=rotation,
        photo_caption=_string(photo["caption"], "photo.caption"),
    )


def load_runtime_config(path: str | Path | None = None) -> RuntimeConfig:
    defaults_path = Path(__file__).with_name("defaults.toml")
    defaults = _read_toml(defaults_path)
    data = deepcopy(defaults)

    config_path: Path | None
    if path is not None:
        config_path = Path(path).expanduser().resolve(strict=False)
    else:
        candidate = default_user_config_path()
        config_path = candidate.resolve(strict=False) if candidate.exists() else None
        if os.environ.get("DISPLAY_RUNTIME_CONFIG", "").strip() and config_path is None:
            config_path = candidate.resolve(strict=False)

    if config_path is not None:
        user = _read_toml(config_path)
        _reject_unknown_keys(user, defaults)
        _merge(data, user)
    return _parse(data, config_path)
