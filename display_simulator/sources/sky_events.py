"""Battery-neutral rare-sky event forecasts for the planetarium guide.

The deterministic astronomy stays useful offline. Short-lived NOAA aurora
nowcasts and CelesTrak orbital elements are bounded, cached atomically, and
silently omitted when they are too old to support an honest notification.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import tempfile
from typing import Any, Callable, Iterable, Mapping, Sequence
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo


_AURORA_URL = "https://services.swpc.noaa.gov/json/ovation_aurora_latest.json"
_VISUAL_TLE_URL = (
    "https://celestrak.org/NORAD/elements/gp.php?GROUP=VISUAL&FORMAT=TLE"
)
_RECENT_TLE_URL = (
    "https://celestrak.org/NORAD/elements/gp.php?GROUP=LAST-30-DAYS&FORMAT=TLE"
)
_CACHE_SCHEMA = 1
_CACHE_MAX_BYTES = 1024 * 1024
_AURORA_MAX_BYTES = 2 * 1024 * 1024
_TLE_MAX_BYTES = 512 * 1024
_NETWORK_TIMEOUT_SECONDS = 5.0
_AURORA_CACHE_TTL = timedelta(minutes=10)
_SATELLITE_CACHE_TTL = timedelta(hours=6)
_SATELLITE_STALE_LIMIT = timedelta(days=7)
_EVENT_KINDS = frozenset(
    ("meteor", "aurora", "eclipse", "satellite", "conjunction")
)


@dataclass(frozen=True, slots=True)
class MeteorShower:
    slug: str
    name: str
    peak_month: int
    peak_day: int
    active_start: tuple[int, int]
    active_end: tuple[int, int]
    radiant_ra_degrees: float
    radiant_dec_degrees: float
    ideal_zhr: int
    note: str


# Factual recurring fields distilled from the IMO major-shower calendar. ZHR
# is deliberately described as an ideal reference rate, never a personal
# expected count.
_METEOR_SHOWERS = (
    MeteorShower(
        "quadrantids",
        "Quadrantids",
        1,
        3,
        (12, 28),
        (1, 12),
        230,
        49,
        80,
        "A short, sharp peak with many faint meteors",
    ),
    MeteorShower(
        "lyrids",
        "Lyrids",
        4,
        22,
        (4, 14),
        (4, 30),
        271,
        34,
        18,
        "Occasional bright meteors and persistent trains",
    ),
    MeteorShower(
        "eta-aquariids",
        "Eta Aquariids",
        5,
        6,
        (4, 19),
        (5, 28),
        338,
        -1,
        50,
        "Fast meteors best viewed before dawn",
    ),
    MeteorShower(
        "southern-delta-aquariids",
        "Southern Delta Aquariids",
        7,
        30,
        (7, 12),
        (8, 23),
        340,
        -16,
        20,
        "A broad peak that improves after midnight",
    ),
    MeteorShower(
        "alpha-capricornids",
        "Alpha Capricornids",
        7,
        31,
        (7, 3),
        (8, 15),
        307,
        -10,
        5,
        "A modest shower known for slow, bright fireballs",
    ),
    MeteorShower(
        "perseids",
        "Perseids",
        8,
        13,
        (7, 17),
        (8, 24),
        48,
        58,
        100,
        "A rich northern shower with many bright meteors",
    ),
    MeteorShower(
        "orionids",
        "Orionids",
        10,
        21,
        (10, 2),
        (11, 7),
        95,
        16,
        20,
        "Fast meteors from debris left by Halley's Comet",
    ),
    MeteorShower(
        "southern-taurids",
        "Southern Taurids",
        11,
        5,
        (9, 10),
        (11, 20),
        52,
        15,
        5,
        "A low-rate shower prized for bright fireballs",
    ),
    MeteorShower(
        "northern-taurids",
        "Northern Taurids",
        11,
        12,
        (10, 20),
        (12, 10),
        58,
        22,
        5,
        "A low-rate companion shower with occasional fireballs",
    ),
    MeteorShower(
        "leonids",
        "Leonids",
        11,
        17,
        (11, 6),
        (11, 30),
        152,
        22,
        15,
        "Very fast meteors that can leave persistent trains",
    ),
    MeteorShower(
        "geminids",
        "Geminids",
        12,
        14,
        (11, 19),
        (12, 24),
        112,
        33,
        120,
        "A strong shower with bright, often colorful meteors",
    ),
    MeteorShower(
        "ursids",
        "Ursids",
        12,
        22,
        (12, 13),
        (12, 24),
        217,
        76,
        10,
        "A compact northern shower near the December solstice",
    ),
)


@dataclass(frozen=True, slots=True)
class SkyEvent:
    id: str
    kind: str
    title: str
    timing: str
    detail: str
    priority: int
    confidence: str
    source: str
    is_tonight: bool
    starts_at: datetime | None = None
    peaks_at: datetime | None = None
    ends_at: datetime | None = None
    direction: str | None = None
    altitude_degrees: float | None = None
    azimuth_degrees: float | None = None
    separation_degrees: float | None = None
    marker_radec: tuple[float, float] | None = field(
        default=None, compare=False, repr=False
    )
    secondary_marker_radec: tuple[float, float] | None = field(
        default=None, compare=False, repr=False
    )
    track_radec: tuple[tuple[float, float], ...] = field(
        default=(), compare=False, repr=False
    )

    def __post_init__(self) -> None:
        if self.kind not in _EVENT_KINDS:
            raise ValueError(f"Unsupported sky event kind {self.kind!r}")

    def as_manifest(self) -> dict[str, Any]:
        value: dict[str, Any] = {
            "id": self.id,
            "kind": self.kind,
            "title": self.title,
            "timing": self.timing,
            "detail": self.detail,
            "priority": int(self.priority),
            "confidence": self.confidence,
            "source": self.source,
            "is_tonight": bool(self.is_tonight),
        }
        for key in ("starts_at", "peaks_at", "ends_at"):
            timestamp = getattr(self, key)
            if timestamp is not None:
                value[key] = _aware(timestamp).isoformat()
        if self.direction is not None:
            value["direction"] = self.direction
        for key in (
            "altitude_degrees",
            "azimuth_degrees",
            "separation_degrees",
        ):
            number = getattr(self, key)
            if number is not None and math.isfinite(number):
                value[key] = round(float(number), 2)
        return value


@dataclass(frozen=True, slots=True)
class SkyEventReport:
    generated_at: datetime
    events: tuple[SkyEvent, ...]

    @property
    def manifest_events(self) -> list[dict[str, Any]]:
        return [event.as_manifest() for event in self.events[:8]]

    @property
    def digest(self) -> str:
        payload = json.dumps(
            self.manifest_events,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()


FetchBytes = Callable[[str, float, int], bytes]
CoordinateConverter = Callable[[float, float], tuple[float, float]]


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Sky-event timestamps must include a timezone")
    return value


def _utc(value: datetime) -> datetime:
    return _aware(value).astimezone(timezone.utc)


def _cardinal(azimuth: float) -> str:
    labels = ("N", "NE", "E", "SE", "S", "SW", "W", "NW")
    return labels[int((float(azimuth) + 22.5) // 45) % len(labels)]


def _clock(value: datetime) -> str:
    return value.strftime("%-I:%M %p")


def _day_clock(
    value: datetime,
    reference: date,
    *,
    same_day_label: str = "Tonight",
) -> str:
    if value.date() == reference:
        prefix = same_day_label
    elif value.date() == reference + timedelta(days=1):
        prefix = "Tomorrow"
    else:
        prefix = value.strftime("%a · %b %-d")
    return f"{prefix} at {_clock(value)}"


def _night_contains(night: Any, value: datetime) -> bool:
    moment = value.astimezone(night.sunset.tzinfo)
    return night.sunset <= moment <= night.sunrise


def _network_fetch(url: str, timeout: float, maximum: int) -> bytes:
    request = Request(
        url,
        headers={
            "Accept": "application/json,text/plain;q=0.9,*/*;q=0.1",
            "User-Agent": "eink-sky-events/1.0 (+local planetarium display)",
        },
        method="GET",
    )
    with urlopen(request, timeout=timeout) as response:
        payload = response.read(maximum + 1)
    if not payload or len(payload) > maximum:
        raise ValueError("Rare-event provider returned an invalid payload size")
    return payload


class _EventCache:
    def __init__(self, path: Path) -> None:
        self.path = Path(path).expanduser()
        self.value = self._load()

    def _load(self) -> dict[str, Any]:
        try:
            if self.path.is_symlink():
                return {"schema_version": _CACHE_SCHEMA, "entries": {}}
            size = self.path.stat().st_size
            if size <= 0 or size > _CACHE_MAX_BYTES:
                return {"schema_version": _CACHE_SCHEMA, "entries": {}}
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return {"schema_version": _CACHE_SCHEMA, "entries": {}}
        if (
            not isinstance(raw, dict)
            or raw.get("schema_version") != _CACHE_SCHEMA
            or not isinstance(raw.get("entries"), dict)
        ):
            return {"schema_version": _CACHE_SCHEMA, "entries": {}}
        return raw

    def get(
        self,
        key: str,
        now: datetime,
        maximum_age: timedelta,
    ) -> Mapping[str, Any] | None:
        raw = self.value["entries"].get(key)
        if not isinstance(raw, Mapping):
            return None
        fetched_at = _parse_timestamp(raw.get("fetched_at"))
        payload = raw.get("payload")
        if (
            fetched_at is None
            or _utc(now) - fetched_at > maximum_age
            or not isinstance(payload, Mapping)
        ):
            return None
        return payload

    def put(self, key: str, now: datetime, payload: Mapping[str, Any]) -> None:
        entries = self.value.setdefault("entries", {})
        entries[key] = {
            "fetched_at": _utc(now).isoformat(),
            "payload": dict(payload),
        }
        self._save()

    def _save(self) -> None:
        if self.path.is_symlink():
            return
        encoded = json.dumps(
            self.value,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        if len(encoded) > _CACHE_MAX_BYTES:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=self.path.name + ".",
                suffix=".tmp",
                dir=self.path.parent,
            )
            temporary = Path(temporary_name)
            try:
                with os.fdopen(descriptor, "wb") as handle:
                    handle.write(encoded)
                    handle.flush()
                    os.fsync(handle.fileno())
                temporary.replace(self.path)
            finally:
                temporary.unlink(missing_ok=True)
        except OSError:
            return


def _parse_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(timezone.utc)


def _window_date(
    year: int,
    month_day: tuple[int, int],
    *,
    peak: date,
    start: bool,
) -> date:
    candidate = date(year, *month_day)
    if start and candidate > peak:
        candidate = date(year - 1, *month_day)
    elif not start and candidate < peak:
        candidate = date(year + 1, *month_day)
    return candidate


def meteor_events(
    night: Any,
    moon: Any,
    coordinate_converter: CoordinateConverter,
) -> list[SkyEvent]:
    zone = night.observation_time.tzinfo
    events: list[SkyEvent] = []
    for shower in _METEOR_SHOWERS:
        candidates: list[tuple[datetime, date, date]] = []
        for year in range(night.night_date.year - 1, night.night_date.year + 2):
            peak_date = date(year, shower.peak_month, shower.peak_day)
            peak = datetime.combine(peak_date, time(2, 0), tzinfo=zone)
            start_date = _window_date(
                year, shower.active_start, peak=peak_date, start=True
            )
            end_date = _window_date(
                year, shower.active_end, peak=peak_date, start=False
            )
            candidates.append((peak, start_date, end_date))
        peak, start_date, end_date = min(
            candidates,
            key=lambda item: abs(
                (item[0] - night.observation_time).total_seconds()
            ),
        )
        days_until = (peak.date() - night.night_date).days
        active = start_date <= night.night_date <= end_date
        if not active or not -1 <= days_until <= 7:
            continue

        is_tonight = _night_contains(night, peak) or days_until == 0
        if is_tonight:
            timing = "Peaks overnight"
            priority = 92
        elif days_until == 1:
            timing = "Peaks tomorrow night"
            priority = 82
        else:
            timing = f"Active now · peaks in {days_until} nights"
            priority = max(68, 80 - days_until)
        altitude, azimuth = coordinate_converter(
            shower.radiant_ra_degrees,
            shower.radiant_dec_degrees,
        )
        moon_note = ""
        if (
            float(getattr(moon, "illumination", 0)) >= 0.55
            and float(getattr(moon, "altitude", -90)) > 0
        ):
            moon_note = " · Moonlight may hide fainter streaks"
        detail = (
            f"Ideal ZHR {shower.ideal_zhr}; your count will be lower"
            f" · {shower.note}{moon_note}"
        )
        direction = _cardinal(azimuth) if altitude > 0 else None
        events.append(
            SkyEvent(
                id=f"meteor:{shower.slug}:{peak.year}",
                kind="meteor",
                title=shower.name,
                timing=timing,
                detail=detail,
                priority=priority,
                confidence="high",
                source="IMO annual meteor calendar",
                is_tonight=is_tonight,
                starts_at=datetime.combine(start_date, time(), tzinfo=zone),
                peaks_at=peak,
                ends_at=datetime.combine(
                    end_date + timedelta(days=1), time(), tzinfo=zone
                ),
                direction=direction,
                altitude_degrees=altitude if altitude > 0 else None,
                azimuth_degrees=azimuth if altitude > 0 else None,
                marker_radec=(
                    shower.radiant_ra_degrees,
                    shower.radiant_dec_degrees,
                ),
            )
        )
    return events


def _aurora_snapshot(
    latitude: float,
    longitude: float,
    payload: bytes,
) -> dict[str, Any]:
    raw = json.loads(payload.decode("utf-8"))
    if not isinstance(raw, Mapping):
        raise ValueError("NOAA aurora response is not an object")
    observation = _parse_timestamp(raw.get("Observation Time"))
    forecast = _parse_timestamp(raw.get("Forecast Time"))
    coordinates = raw.get("coordinates")
    if observation is None or forecast is None or not isinstance(coordinates, list):
        raise ValueError("NOAA aurora response is missing forecast metadata")
    target_lon = longitude % 360
    best: tuple[float, float, float] | None = None
    best_distance = math.inf
    for point in coordinates:
        if (
            not isinstance(point, list)
            or len(point) != 3
            or any(isinstance(value, bool) for value in point)
            or not all(isinstance(value, (int, float)) for value in point)
        ):
            continue
        lon, lat, intensity = (float(value) for value in point)
        distance = abs((lon - target_lon + 180) % 360 - 180) + abs(
            lat - latitude
        )
        if distance < best_distance:
            best = (lon, lat, intensity)
            best_distance = distance
    if best is None:
        raise ValueError("NOAA aurora response contains no usable grid point")
    return {
        "latitude": round(latitude, 3),
        "longitude": round(longitude, 3),
        "observation_time": observation.isoformat(),
        "forecast_time": forecast.isoformat(),
        "intensity": max(0, min(100, round(best[2]))),
    }


def aurora_events(
    night: Any,
    latitude: float,
    longitude: float,
    now: datetime,
    cache: _EventCache,
    *,
    offline: bool,
    fetcher: FetchBytes,
) -> list[SkyEvent]:
    snapshot = cache.get("aurora", now, _AURORA_CACHE_TTL)
    if (
        snapshot is None
        or abs(float(snapshot.get("latitude", 999)) - latitude) > 0.02
        or abs(float(snapshot.get("longitude", 999)) - longitude) > 0.02
    ):
        snapshot = None
        if not offline:
            try:
                snapshot = _aurora_snapshot(
                    latitude,
                    longitude,
                    fetcher(
                        _AURORA_URL,
                        _NETWORK_TIMEOUT_SECONDS,
                        _AURORA_MAX_BYTES,
                    ),
                )
                cache.put("aurora", now, snapshot)
            except (OSError, TimeoutError, UnicodeError, ValueError, json.JSONDecodeError):
                snapshot = None
    if not isinstance(snapshot, Mapping):
        return []
    forecast = _parse_timestamp(snapshot.get("forecast_time"))
    observation = _parse_timestamp(snapshot.get("observation_time"))
    intensity = snapshot.get("intensity")
    if (
        forecast is None
        or observation is None
        or isinstance(intensity, bool)
        or not isinstance(intensity, (int, float))
        or not 0 <= float(intensity) <= 100
    ):
        return []
    # OVATION is a short nowcast. A last-known-good file can remain useful for
    # diagnostics, but it must never create a stale notification.
    if abs((_utc(now) - forecast).total_seconds()) > 60 * 60:
        return []
    local_forecast = forecast.astimezone(night.sunset.tzinfo)
    if not (
        night.sunset - timedelta(minutes=45)
        <= local_forecast
        <= night.sunrise
    ):
        return []
    estimate = round(float(intensity))
    if estimate < 10:
        return []
    if estimate >= 40:
        confidence = "high"
        label = "Strong aurora potential"
        priority = 98
    elif estimate >= 20:
        confidence = "medium"
        label = "Aurora may be visible"
        priority = 91
    else:
        confidence = "low"
        label = "Possible aurora glow"
        priority = 84
    return [
        SkyEvent(
            id=f"aurora:{forecast.strftime('%Y%m%dT%H%M')}",
            kind="aurora",
            title=label,
            timing=f"NOAA forecast near {_clock(local_forecast)}",
            detail=(
                f"Modeled local viewing estimate {estimate}% · face north "
                "from a clear, dark location"
            ),
            priority=priority,
            confidence=confidence,
            source="NOAA SWPC OVATION",
            is_tonight=True,
            starts_at=observation,
            peaks_at=forecast,
            ends_at=forecast + timedelta(minutes=30),
            direction="N",
        )
    ]


def _planet_target(ephemeris: Any, name: str) -> Any:
    for key in (f"{name} barycenter", name):
        try:
            return ephemeris[key]
        except KeyError:
            continue
    raise KeyError(name)


def conjunction_events(
    night: Any,
    ephemeris: Any,
    observer_vector: Any,
    timescale: Any,
) -> list[SkyEvent]:
    names = ("mercury", "venus", "mars", "jupiter", "saturn")
    targets: dict[str, Any] = {}
    for name in names:
        try:
            targets[name] = _planet_target(ephemeris, name)
        except KeyError:
            continue
    candidates: list[SkyEvent] = []
    for day_offset in range(0, 8):
        local_time = night.observation_time + timedelta(days=day_offset)
        sky_time = timescale.from_datetime(_utc(local_time))
        observer = observer_vector.at(sky_time)
        apparent: dict[str, Any] = {}
        coordinates: dict[str, tuple[float, float, float, float]] = {}
        for name, target in targets.items():
            body = observer.observe(target).apparent()
            altitude, azimuth, _distance = body.altaz()
            if float(altitude.degrees) < 10:
                continue
            ra, dec, _distance = body.radec()
            apparent[name] = body
            coordinates[name] = (
                float(ra.hours) * 15,
                float(dec.degrees),
                float(altitude.degrees),
                float(azimuth.degrees) % 360,
            )
        available = tuple(apparent)
        for first_index, first in enumerate(available):
            for second in available[first_index + 1 :]:
                separation = float(
                    apparent[first].separation_from(apparent[second]).degrees
                )
                if not math.isfinite(separation) or separation > 5:
                    continue
                first_data = coordinates[first]
                second_data = coordinates[second]
                x = math.sin(math.radians(first_data[3])) + math.sin(
                    math.radians(second_data[3])
                )
                y = math.cos(math.radians(first_data[3])) + math.cos(
                    math.radians(second_data[3])
                )
                azimuth = math.degrees(math.atan2(x, y)) % 360
                altitude = min(first_data[2], second_data[2])
                if separation <= 1:
                    priority, confidence = 96, "high"
                elif separation <= 2:
                    priority, confidence = 89, "high"
                else:
                    priority, confidence = 77, "medium"
                is_tonight = day_offset == 0
                timing = (
                    f"Only {separation:.1f}° apart tonight"
                    if is_tonight
                    else f"Closest in {day_offset} nights · {separation:.1f}° apart"
                )
                title = f"{first.title()} & {second.title()}"
                candidates.append(
                    SkyEvent(
                        id=(
                            f"conjunction:{first}:{second}:"
                            f"{local_time.date().isoformat()}"
                        ),
                        kind="conjunction",
                        title=title,
                        timing=timing,
                        detail=(
                            f"Look {_cardinal(azimuth)} after dusk; "
                            "both worlds are above the horizon"
                        ),
                        priority=priority - min(day_offset * 2, 12),
                        confidence=confidence,
                        source="Skyfield/JPL ephemeris",
                        is_tonight=is_tonight,
                        peaks_at=local_time,
                        direction=_cardinal(azimuth),
                        altitude_degrees=altitude,
                        azimuth_degrees=azimuth,
                        separation_degrees=separation,
                        marker_radec=(first_data[0], first_data[1]),
                        secondary_marker_radec=(second_data[0], second_data[1]),
                    )
                )
    # Daily sampling can find the same slow conjunction repeatedly. Retain only
    # the closest night for each pair.
    best: dict[str, SkyEvent] = {}
    for event in candidates:
        pair = ":".join(event.id.split(":")[:3])
        previous = best.get(pair)
        if (
            previous is None
            or (event.separation_degrees or math.inf)
            < (previous.separation_degrees or math.inf)
        ):
            best[pair] = event
    return list(best.values())


def eclipse_events(
    night: Any,
    ephemeris: Any,
    observer_vector: Any,
    timescale: Any,
) -> list[SkyEvent]:
    try:
        from skyfield import almanac, eclipselib
    except ImportError:
        return []
    start = timescale.from_datetime(_utc(night.rendered_at))
    lunar_end = timescale.from_datetime(
        _utc(night.rendered_at + timedelta(days=45))
    )
    events: list[SkyEvent] = []
    try:
        eclipse_times, eclipse_kinds, _details = eclipselib.lunar_eclipses(
            start, lunar_end, ephemeris
        )
    except (KeyError, OSError, ValueError):
        eclipse_times, eclipse_kinds = (), ()
    labels = ("Penumbral lunar eclipse", "Partial lunar eclipse", "Total lunar eclipse")
    for sky_time, kind_value in zip(eclipse_times, eclipse_kinds):
        kind = int(kind_value)
        if not 0 <= kind < len(labels):
            continue
        local_peak = sky_time.utc_datetime().astimezone(night.sunset.tzinfo)
        apparent = observer_vector.at(sky_time).observe(ephemeris["moon"]).apparent()
        altitude, azimuth, _distance = apparent.altaz()
        altitude_value = float(altitude.degrees)
        if altitude_value <= 0:
            continue
        azimuth_value = float(azimuth.degrees) % 360
        is_tonight = _night_contains(night, local_peak)
        priority = (75, 92, 99)[kind] - (
            0 if is_tonight else min((local_peak.date() - night.night_date).days, 20)
        )
        detail = (
            "Subtle shading crosses the Moon"
            if kind == 0
            else "Earth's shadow visibly crosses the Moon"
        )
        events.append(
            SkyEvent(
                id=f"eclipse:lunar:{local_peak.strftime('%Y%m%dT%H%M')}",
                kind="eclipse",
                title=labels[kind],
                timing=_day_clock(local_peak, night.night_date),
                detail=f"{detail} · look {_cardinal(azimuth_value)}",
                priority=max(60, priority),
                confidence="high",
                source="Skyfield/JPL ephemeris",
                is_tonight=is_tonight,
                peaks_at=local_peak,
                direction=_cardinal(azimuth_value),
                altitude_degrees=altitude_value,
                azimuth_degrees=azimuth_value,
            )
        )
        break

    # Search new moons for a genuine local Sun/Moon overlap. This avoids the
    # common and misleading shortcut of reporting every global solar eclipse.
    solar_end = timescale.from_datetime(
        _utc(night.rendered_at + timedelta(days=90))
    )
    try:
        phase_times, phases = almanac.find_discrete(
            start, solar_end, almanac.moon_phases(ephemeris)
        )
    except (KeyError, OSError, ValueError):
        phase_times, phases = (), ()
    for new_moon, phase in zip(phase_times, phases):
        if int(phase) != 0:
            continue
        center = new_moon.utc_datetime()
        samples = [
            center + timedelta(minutes=5 * offset)
            for offset in range(-72, 73)
        ]
        sky_times = timescale.from_datetimes(samples)
        observer = observer_vector.at(sky_times)
        sun = observer.observe(ephemeris["sun"]).apparent()
        moon = observer.observe(ephemeris["moon"]).apparent()
        separation = sun.separation_from(moon).degrees
        sun_altitude, sun_azimuth, _distance = sun.altaz()
        sun_radius = 0.2666 / sun.distance().au
        moon_radius = [
            math.degrees(math.asin(1737.4 / distance))
            for distance in moon.distance().km
        ]
        viable = [
            index
            for index in range(len(samples))
            if float(sun_altitude.degrees[index]) >= -0.833
            and float(separation[index])
            <= float(sun_radius[index]) + moon_radius[index]
        ]
        if not viable:
            continue
        index = min(viable, key=lambda candidate: float(separation[candidate]))
        local_peak = samples[index].astimezone(night.sunset.tzinfo)
        separation_value = float(separation[index])
        sun_radius_value = float(sun_radius[index])
        moon_radius_value = float(moon_radius[index])
        central = separation_value < abs(moon_radius_value - sun_radius_value)
        if central:
            classification = (
                "Total solar eclipse"
                if moon_radius_value >= sun_radius_value
                else "Annular solar eclipse"
            )
        else:
            classification = "Partial solar eclipse"
        altitude_value = float(sun_altitude.degrees[index])
        azimuth_value = float(sun_azimuth.degrees[index]) % 360
        soon = (local_peak - night.rendered_at).total_seconds() <= 48 * 3600
        events.append(
            SkyEvent(
                id=f"eclipse:solar:{local_peak.strftime('%Y%m%dT%H%M')}",
                kind="eclipse",
                title=classification,
                timing=_day_clock(
                    local_peak,
                    night.night_date,
                    same_day_label="Today",
                ),
                detail=(
                    "Use certified eclipse glasses; ordinary sunglasses "
                    "are not safe for viewing the Sun"
                ),
                priority=100 if soon else 94,
                confidence="high",
                source="Skyfield/JPL local eclipse geometry",
                is_tonight=False,
                peaks_at=local_peak,
                direction=_cardinal(azimuth_value),
                altitude_degrees=altitude_value,
                azimuth_degrees=azimuth_value,
                separation_degrees=separation_value,
            )
        )
        break
    return events


def _parse_tle_catalog(value: str, *, starlink_only: bool = False) -> list[tuple[str, str, str]]:
    lines = [line.rstrip() for line in value.splitlines() if line.strip()]
    result: list[tuple[str, str, str]] = []
    for index in range(0, len(lines) - 2, 3):
        name, first, second = lines[index : index + 3]
        if (
            not first.startswith("1 ")
            or not second.startswith("2 ")
            or len(name) > 80
            or not name.isprintable()
        ):
            continue
        if starlink_only and "STARLINK" not in name.upper():
            continue
        result.append((name.strip(), first, second))
    return result


def _satellite_payload(
    visual: bytes,
    recent: bytes,
) -> dict[str, Any]:
    visual_text = visual.decode("ascii")
    recent_text = recent.decode("ascii")
    if not _parse_tle_catalog(visual_text):
        raise ValueError("CelesTrak visual catalog contains no valid TLEs")
    if not _parse_tle_catalog(recent_text, starlink_only=True):
        raise ValueError("CelesTrak recent catalog contains no Starlink TLEs")
    return {"visual": visual_text, "recent": recent_text}


def _satellite_catalog(
    now: datetime,
    cache: _EventCache,
    *,
    offline: bool,
    fetcher: FetchBytes,
) -> Mapping[str, Any] | None:
    payload = cache.get("satellites", now, _SATELLITE_CACHE_TTL)
    if payload is not None:
        return payload
    stale = cache.get("satellites", now, _SATELLITE_STALE_LIMIT)
    if offline:
        return stale
    try:
        payload = _satellite_payload(
            fetcher(
                _VISUAL_TLE_URL,
                _NETWORK_TIMEOUT_SECONDS,
                _TLE_MAX_BYTES,
            ),
            fetcher(
                _RECENT_TLE_URL,
                _NETWORK_TIMEOUT_SECONDS,
                _TLE_MAX_BYTES,
            ),
        )
        cache.put("satellites", now, payload)
        return payload
    except (OSError, TimeoutError, UnicodeError, ValueError):
        return stale


def satellite_events(
    night: Any,
    latitude: float,
    longitude: float,
    ephemeris: Any,
    timescale: Any,
    now: datetime,
    cache: _EventCache,
    *,
    offline: bool,
    fetcher: FetchBytes,
) -> list[SkyEvent]:
    payload = _satellite_catalog(
        now, cache, offline=offline, fetcher=fetcher
    )
    if not isinstance(payload, Mapping):
        return []
    visual = payload.get("visual")
    recent = payload.get("recent")
    if not isinstance(visual, str) or not isinstance(recent, str):
        return []
    try:
        from skyfield.api import EarthSatellite, wgs84
    except ImportError:
        return []
    topocentric = wgs84.latlon(latitude, longitude)
    earth_observer = ephemeris["earth"] + topocentric
    start = timescale.from_datetime(_utc(night.sunset))
    end = timescale.from_datetime(_utc(night.sunrise))
    catalogs = (
        ("visual", _parse_tle_catalog(visual)[:200]),
        ("starlink", _parse_tle_catalog(recent, starlink_only=True)[:120]),
    )
    best: dict[str, SkyEvent] = {}
    for group, entries in catalogs:
        for name, first, second in entries:
            try:
                satellite = EarthSatellite(
                    first, second, name=name, ts=timescale
                )
                epoch_age = abs(
                    (
                        _utc(night.observation_time)
                        - satellite.epoch.utc_datetime()
                    ).total_seconds()
                )
                maximum_age = 4 * 86400 if group == "starlink" else 14 * 86400
                if epoch_age > maximum_age:
                    continue
                event_times, event_codes = satellite.find_events(
                    topocentric,
                    start,
                    end,
                    altitude_degrees=30,
                )
            except (OSError, ValueError):
                continue
            for sky_time, code in zip(event_times, event_codes):
                if int(code) != 1:
                    continue
                try:
                    altitude, azimuth, _distance = (
                        satellite - topocentric
                    ).at(sky_time).altaz()
                    altitude_value = float(altitude.degrees)
                    azimuth_value = float(azimuth.degrees) % 360
                    sun_altitude, _sun_azimuth, _distance = (
                        earth_observer.at(sky_time)
                        .observe(ephemeris["sun"])
                        .apparent()
                        .altaz()
                    )
                    sunlit = bool(satellite.at(sky_time).is_sunlit(ephemeris))
                except (KeyError, OSError, ValueError):
                    continue
                if (
                    not sunlit
                    or float(sun_altitude.degrees) > -6
                    or altitude_value < 30
                ):
                    continue
                peak = sky_time.utc_datetime().astimezone(
                    night.sunset.tzinfo
                )
                normalized_name = name.upper()
                if group == "starlink":
                    bucket = "starlink"
                    title = "Recent Starlink pass"
                    priority = 80
                    detail = (
                        "Potentially visible while sunlit; brightness and "
                        "timing can vary after orbital maneuvers"
                    )
                elif "ISS" in normalized_name:
                    bucket = "bright"
                    title = "ISS pass"
                    priority = 96
                    detail = "A favorable, potentially bright sunlit pass"
                elif "CSS" in normalized_name or "TIANHE" in normalized_name:
                    bucket = "bright"
                    title = "Tiangong station pass"
                    priority = 90
                    detail = "A favorable sunlit space-station pass"
                elif normalized_name == "HST":
                    bucket = "bright"
                    title = "Hubble pass"
                    priority = 84
                    detail = "A favorable, potentially visible sunlit pass"
                else:
                    # The VISUAL group contains many large rocket bodies. Only
                    # surface the most favorable high passes among them.
                    if altitude_value < 55:
                        continue
                    bucket = "bright"
                    title = "Bright satellite pass"
                    priority = 72
                    detail = (
                        f"{name} · a favorable potentially visible sunlit pass"
                    )
                samples = [
                    sky_time.utc_datetime() + timedelta(seconds=offset)
                    for offset in (-120, -60, 0, 60, 120)
                ]
                track: list[tuple[float, float]] = []
                for track_time in timescale.from_datetimes(samples):
                    try:
                        ra, dec, _distance = (
                            satellite - topocentric
                        ).at(track_time).radec()
                    except (OSError, ValueError):
                        continue
                    track.append(
                        (float(ra.hours) * 15, float(dec.degrees))
                    )
                event = SkyEvent(
                    id=f"satellite:{bucket}:{peak.strftime('%Y%m%dT%H%M')}",
                    kind="satellite",
                    title=title,
                    timing=f"Best near {_clock(peak)}",
                    detail=f"{detail} · peaks {round(altitude_value)}° high",
                    priority=priority,
                    confidence="medium",
                    source="CelesTrak/18 SDS prediction",
                    is_tonight=True,
                    peaks_at=peak,
                    direction=_cardinal(azimuth_value),
                    altitude_degrees=altitude_value,
                    azimuth_degrees=azimuth_value,
                    track_radec=tuple(track),
                )
                previous = best.get(bucket)
                if previous is None or (
                    event.priority,
                    event.altitude_degrees or 0,
                ) > (
                    previous.priority,
                    previous.altitude_degrees or 0,
                ):
                    best[bucket] = event
    return list(best.values())


def _event_sort_key(
    event: SkyEvent,
    generated_at: datetime,
) -> tuple[Any, ...]:
    peak = event.peaks_at or event.starts_at or event.ends_at
    distance = (
        abs((_utc(peak) - _utc(generated_at)).total_seconds())
        if peak is not None
        else math.inf
    )
    kind_order = {
        "aurora": 0,
        "eclipse": 1,
        "meteor": 2,
        "conjunction": 3,
        "satellite": 4,
    }
    return (
        not event.is_tonight,
        -event.priority,
        distance,
        kind_order[event.kind],
        event.id,
    )


def _deduplicate(events: Iterable[SkyEvent]) -> list[SkyEvent]:
    result: list[SkyEvent] = []
    seen: set[str] = set()
    for event in events:
        if event.id in seen:
            continue
        seen.add(event.id)
        result.append(event)
    return result


def default_cache_path() -> Path:
    cache_root = os.environ.get("XDG_CACHE_HOME")
    if cache_root:
        return Path(cache_root).expanduser() / "eink-display" / "sky-events-v1.json"
    return Path(tempfile.gettempdir()) / "eink-display-sky-events-v1.json"


def collect_sky_events(
    night: Any,
    *,
    latitude: float,
    longitude: float,
    ephemeris: Any,
    observer_vector: Any,
    timescale: Any,
    moon: Any,
    coordinate_converter: CoordinateConverter,
    cache_path: Path | str | None = None,
    offline: bool = False,
    fetcher: FetchBytes = _network_fetch,
) -> SkyEventReport:
    """Calculate and rank a bounded event snapshot for one observing night."""
    generated_at = _aware(night.rendered_at)
    cache = _EventCache(
        Path(cache_path).expanduser()
        if cache_path
        else default_cache_path()
    )
    events: list[SkyEvent] = []
    events.extend(meteor_events(night, moon, coordinate_converter))
    events.extend(
        conjunction_events(night, ephemeris, observer_vector, timescale)
    )
    events.extend(
        eclipse_events(night, ephemeris, observer_vector, timescale)
    )
    events.extend(
        aurora_events(
            night,
            latitude,
            longitude,
            generated_at,
            cache,
            offline=offline,
            fetcher=fetcher,
        )
    )
    events.extend(
        satellite_events(
            night,
            latitude,
            longitude,
            ephemeris,
            timescale,
            generated_at,
            cache,
            offline=offline,
            fetcher=fetcher,
        )
    )
    ordered = sorted(
        _deduplicate(events),
        key=lambda event: _event_sort_key(event, generated_at),
    )
    return SkyEventReport(generated_at=generated_at, events=tuple(ordered[:8]))


def featured_event(events: Sequence[SkyEvent], night: Any) -> SkyEvent | None:
    """Choose one alert worthy of replacing the ordinary guide target."""
    for event in events:
        if event.is_tonight:
            return event
    for event in events:
        peak = event.peaks_at or event.starts_at
        if (
            peak is not None
            and event.priority >= 90
            and timedelta(0)
            <= _aware(peak) - _aware(night.rendered_at)
            <= timedelta(hours=48)
        ):
            return event
    return None
