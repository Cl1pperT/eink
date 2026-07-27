"""Cached, read-only BirdWeather data for the phone control panel."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path
import re
import sys
import tempfile
import threading
import time
from typing import Any, Callable, Mapping, Sequence


MAX_CACHE_BYTES = 1024 * 1024
SOURCE_LABEL = "Nearby BirdWeather reports"


def bird_slug(scientific_name: str) -> str:
    """Return the illustration filename stem used by AvianVisitors."""
    return re.sub(r"[^a-z0-9]+", "-", scientific_name.casefold()).strip("-")


@dataclass(frozen=True, slots=True)
class BirdWeatherQuery:
    postal_code: str
    country: str
    lookback_days: int

    @classmethod
    def from_settings(cls, settings: Mapping[str, Any]) -> "BirdWeatherQuery":
        birds = settings.get("birds")
        if not isinstance(birds, Mapping):
            raise ValueError("validated settings do not contain birds")
        return cls(
            postal_code=str(birds["postal_code"]),
            country=str(birds["country"]),
            lookback_days=int(birds["lookback_days"]),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "postal_code": self.postal_code,
            "country": self.country,
            "lookback_days": self.lookback_days,
        }


BirdFetcher = Callable[[Path, BirdWeatherQuery, float], Sequence[Mapping[str, Any]]]


def _load_birdweather(repository: Path):
    path = repository / "frame" / "birdweather.py"
    if not path.is_file():
        raise FileNotFoundError(f"BirdWeather adapter not found under {repository}")
    digest = hashlib.sha256(str(path.resolve()).encode("utf-8")).hexdigest()[:16]
    module_name = f"_eink_control_birdweather_{digest}"
    existing = sys.modules.get(module_name)
    if existing is not None:
        return existing
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load BirdWeather adapter from {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(module_name, None)
        raise
    return module


def fetch_nearby_species(
    repository: Path,
    query: BirdWeatherQuery,
    timeout: float,
) -> Sequence[Mapping[str, Any]]:
    """Use AvianVisitors' keyless BirdWeather adapter with per-call timeouts."""
    module = _load_birdweather(repository)
    return module.species_for_zip(
        query.postal_code,
        country=query.country,
        target=24,
        days=query.lookback_days,
        timeout=timeout,
    )


def _clean_species(value: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    cleaned: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in value[:60]:
        if not isinstance(raw, Mapping):
            continue
        scientific = raw.get("sci") or raw.get("scientific_name")
        common = raw.get("com") or raw.get("common_name")
        count = raw.get("n") if raw.get("n") is not None else raw.get("count")
        if not isinstance(scientific, str) or not scientific.strip():
            continue
        scientific = scientific.strip()[:160]
        common = common.strip()[:160] if isinstance(common, str) and common.strip() else scientific
        if isinstance(count, bool) or not isinstance(count, (int, float)):
            continue
        numeric = float(count)
        if not math.isfinite(numeric) or numeric <= 0:
            continue
        slug = bird_slug(scientific)
        if not slug or slug in seen:
            continue
        seen.add(slug)
        cleaned.append(
            {
                "scientific_name": scientific,
                "common_name": common,
                "count": int(round(numeric)),
                "slug": slug,
                "art_url": f"/bird-art/{slug}.png",
            }
        )
    cleaned.sort(key=lambda item: (-item["count"], item["common_name"].casefold()))
    return cleaned[:24]


class BirdWeatherCache:
    """Keep the web UI fast while a daemon refreshes bounded upstream calls."""

    def __init__(
        self,
        repository: Path,
        cache_path: Path,
        *,
        fetcher: BirdFetcher = fetch_nearby_species,
        ttl_seconds: float = 15 * 60,
        retry_seconds: float = 60,
        upstream_timeout: float = 4,
        clock: Callable[[], float] = time.time,
    ):
        if ttl_seconds <= 0 or retry_seconds <= 0 or upstream_timeout <= 0:
            raise ValueError("BirdWeather cache timeouts must be positive")
        self.repository = Path(repository).expanduser().resolve(strict=False)
        self.cache_path = Path(cache_path).expanduser()
        self.fetcher = fetcher
        self.ttl_seconds = float(ttl_seconds)
        self.retry_seconds = float(retry_seconds)
        self.upstream_timeout = float(upstream_timeout)
        self.clock = clock
        self._lock = threading.RLock()
        self._cached: dict[str, Any] | None = None
        self._loaded = False
        self._refreshing = False
        self._last_attempt = -math.inf
        self._last_error = ""

    def _load(self) -> dict[str, Any] | None:
        if self._loaded:
            return self._cached
        self._loaded = True
        try:
            if self.cache_path.is_symlink():
                return None
            size = self.cache_path.stat().st_size
            if size <= 0 or size > MAX_CACHE_BYTES:
                return None
            value = json.loads(self.cache_path.read_text(encoding="utf-8"))
            if not isinstance(value, dict):
                return None
            query = value.get("query")
            species = value.get("species")
            fetched_epoch = value.get("fetched_epoch")
            if (
                not isinstance(query, dict)
                or not isinstance(species, list)
                or isinstance(fetched_epoch, bool)
                or not isinstance(fetched_epoch, (int, float))
            ):
                return None
            value["species"] = _clean_species(species)
            self._cached = value
        except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError):
            self._cached = None
        return self._cached

    def _write(self, value: Mapping[str, Any]) -> None:
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=self.cache_path.name + ".",
            suffix=".tmp",
            dir=self.cache_path.parent,
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump(value, stream, ensure_ascii=False, separators=(",", ":"))
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            temporary.replace(self.cache_path)
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass

    @staticmethod
    def _same_query(cached: Mapping[str, Any] | None, query: BirdWeatherQuery) -> bool:
        return bool(cached and cached.get("query") == query.as_dict())

    def _summary(
        self,
        query: BirdWeatherQuery,
        cached: Mapping[str, Any] | None,
        *,
        refreshing: bool,
    ) -> dict[str, Any]:
        now = self.clock()
        same = self._same_query(cached, query)
        fetched_epoch = float(cached.get("fetched_epoch", 0)) if same and cached else 0.0
        age = max(0, int(now - fetched_epoch)) if fetched_epoch else None
        fresh = age is not None and age <= self.ttl_seconds
        if fresh:
            freshness = "fresh"
        elif same:
            freshness = "stale"
        elif refreshing:
            freshness = "loading"
        else:
            freshness = "unavailable"
        return {
            "provider": "birdweather",
            "source_label": SOURCE_LABEL,
            **query.as_dict(),
            "freshness": freshness,
            "stale": freshness == "stale",
            "refreshing": refreshing,
            "fetched_at": cached.get("fetched_at") if same and cached else None,
            "age_seconds": age,
            "species": list(cached.get("species", ())) if same and cached else [],
            "error": self._last_error or None,
            "disclaimer": (
                "Regional reports from nearby BirdWeather stations; "
                "these are not detections from a microphone at this frame."
            ),
        }

    def refresh(self, query: BirdWeatherQuery) -> dict[str, Any]:
        """Refresh synchronously; callers normally use ``get``'s daemon path."""
        now = self.clock()
        raw = self.fetcher(self.repository, query, self.upstream_timeout)
        species = _clean_species(raw)
        if not species:
            raise RuntimeError("BirdWeather returned no illustrated nearby species")
        value = {
            "schema_version": 1,
            "query": query.as_dict(),
            "fetched_epoch": now,
            "fetched_at": datetime.fromtimestamp(now, timezone.utc).isoformat(),
            "species": species,
        }
        self._write(value)
        with self._lock:
            self._cached = value
            self._loaded = True
            self._last_error = ""
        return self._summary(query, value, refreshing=False)

    def _refresh_daemon(self, query: BirdWeatherQuery) -> None:
        try:
            self.refresh(query)
        except Exception:
            with self._lock:
                self._last_error = "BirdWeather is temporarily unavailable; showing last saved reports."
        finally:
            with self._lock:
                self._refreshing = False

    def get(self, settings: Mapping[str, Any], *, background: bool = True) -> dict[str, Any]:
        query = BirdWeatherQuery.from_settings(settings)
        now = self.clock()
        with self._lock:
            cached = self._load()
            same = self._same_query(cached, query)
            age = now - float(cached.get("fetched_epoch", 0)) if same and cached else math.inf
            should_refresh = (
                age > self.ttl_seconds
                and not self._refreshing
                and now - self._last_attempt >= self.retry_seconds
            )
            if should_refresh:
                self._last_attempt = now
                if background:
                    self._refreshing = True
                    threading.Thread(
                        target=self._refresh_daemon,
                        args=(query,),
                        name="birdweather-refresh",
                        daemon=True,
                    ).start()
                else:
                    try:
                        return self.refresh(query)
                    except Exception:
                        self._last_error = (
                            "BirdWeather is temporarily unavailable; showing last saved reports."
                        )
            return self._summary(query, cached, refreshing=self._refreshing)
