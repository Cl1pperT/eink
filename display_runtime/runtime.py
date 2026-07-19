from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
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
from display_simulator.schedule import mode_for_time
from display_simulator.sources import (
    BirdsSource,
    StarMapSource,
    TestPatternSource,
    UploadedPhotoSource,
    WeatherSource,
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
        return canonical_mode(mode_for_time(when, self.config.schedule))

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

    def _preflight(self, mode: str, *, strict: bool) -> None:
        cfg = self.config
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
            if strict and (cfg.bird_demo or not cfg.bird_source):
                raise SourcePolicyError("production bird rendering requires sources.bird")
            if cfg.bird_source and not cfg.bird_source.startswith(("http://", "https://")):
                if not Path(cfg.bird_source).is_file():
                    raise FileNotFoundError(f"bird frame not found: {cfg.bird_source}")
            elif strict:
                self._repository(
                    cfg.avian_weather_repo,
                    "frame/shoot.py",
                    "AvianVisitors",
                    "AVIANVISITORS_REPO",
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
            if cfg.photo_path is None or not cfg.photo_path.is_file():
                raise FileNotFoundError("sources.photo must point to an existing image")
            return
        raise RuntimeRenderError(f"no source adapter for mode {mode!r}")

    def _context(self, when: datetime, *, allow_demo: bool) -> RenderContext:
        cfg = self.config
        settings_label = (
            f"dither={cfg.conversion.dither} · saturation={cfg.conversion.saturation:.2f} · "
            f"blue bias={cfg.conversion.blue_bias:.2f}"
        )
        return RenderContext(
            orientation=cfg.orientation,
            when=when,
            location=cfg.location,
            config_path=cfg.config_path,
            offline=cfg.weather_offline,
            options={
                "bird_source": cfg.bird_source,
                "demo_birds": cfg.bird_demo,
                "avian_repo": str(cfg.avian_weather_repo or ""),
                "weather_repo": str(cfg.avian_weather_repo or ""),
                "inkystarmap_repo": str(cfg.inkystarmap_repo or ""),
                "starmap_source": str(cfg.starmap_source or ""),
                "photo_path": str(cfg.photo_path or ""),
                "avian_python": str(cfg.avian_python or ""),
                "allow_demo_fallback": allow_demo,
                "use_inkystarmap": cfg.use_inkystarmap,
                "dark_starmap": cfg.dark_starmap,
                "latitude": cfg.latitude,
                "longitude": cfg.longitude,
                "direction": cfg.direction,
                "timezone": cfg.timezone,
                "weather_style": cfg.weather_style,
                "weather_scene_source": cfg.weather_scene_source,
                "weather_environment": cfg.weather_environment,
                "weather_caption": cfg.weather_caption,
                "weather_units": cfg.weather_units,
                "country_code": cfg.weather_country_code,
                "weather_timeout": cfg.weather_timeout,
                "weather_condition": cfg.weather_condition,
                "rotation": cfg.photo_rotation,
                "caption": cfg.photo_caption,
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
        return {
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
        self._preflight(mode, strict=strict)

        mode_directory = cfg.output_directory / mode
        frames_directory = mode_directory / "frames"
        current_path = mode_directory / "current.json"
        with _mode_lock(mode_directory):
            source = self.source_factories[mode]()
            result = ImagePipeline().render(source, self._context(when, allow_demo=not strict), cfg.conversion, cfg.fit_mode)
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
            frame_valid = _native_file_is_valid(frame_path, checksum, result.eink_image.size)
            wire_valid = _binary_file_is_valid(
                wire_path, EE02_PAYLOAD_BYTES, encoded.sha256
            )
            rgb_valid = rgb_path is None or rgb_path.is_file()
            changed = current_wire_checksum != encoded.sha256

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
        for mode in (*SCHEDULED_MODES, "uploaded-photo", "test-pattern"):
            try:
                self._preflight(mode, strict=True)
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
