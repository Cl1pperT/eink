from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
import math
from typing import Callable
from zoneinfo import ZoneInfo


@dataclass(frozen=True, slots=True)
class ScheduleConfig:
    weather_start: time = time(6, 0)
    birds_start: time = time(9, 0)
    star_sunset_offset_minutes: int = 30


@dataclass(frozen=True, slots=True)
class ScheduleState:
    mode: str
    next_wake_at: datetime


SunsetProvider = Callable[[date], datetime]


def _ceil_second(value: datetime) -> datetime:
    if value.microsecond == 0:
        return value
    return value.replace(microsecond=0) + timedelta(seconds=1)


def _local_boundary(day: date, value: time, zone: ZoneInfo) -> datetime:
    return datetime.combine(day, value, tzinfo=zone)


def schedule_state(
    when: datetime,
    config: ScheduleConfig,
    *,
    latitude: float,
    longitude: float,
    timezone_name: str,
    sunset_provider: SunsetProvider | None = None,
) -> ScheduleState:
    """Resolve the current automatic mode and its next absolute boundary."""

    zone = ZoneInfo(timezone_name)
    local = (
        when.replace(tzinfo=zone)
        if when.tzinfo is None or when.utcoffset() is None
        else when.astimezone(zone)
    )

    if sunset_provider is None:
        try:
            from astral import Observer
            from astral.sun import sun
        except ImportError as exc:  # pragma: no cover - installation preflight
            raise RuntimeError("automatic sunset scheduling requires Astral") from exc
        observer = Observer(latitude=latitude, longitude=longitude)

        def sunset_provider(day: date) -> datetime:
            return sun(observer, date=day, tzinfo=zone)["sunset"]

    weather = _local_boundary(local.date(), config.weather_start, zone)
    birds = _local_boundary(local.date(), config.birds_start, zone)
    try:
        sunset = sunset_provider(local.date())
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeError(
            f"could not determine sunset for {local.date().isoformat()}"
        ) from exc
    if (
        not isinstance(sunset, datetime)
        or sunset.tzinfo is None
        or sunset.utcoffset() is None
    ):
        raise RuntimeError("sunset provider returned a naive timestamp")
    stars = _ceil_second(
        sunset.astimezone(zone)
        + timedelta(minutes=config.star_sunset_offset_minutes)
    )
    if not weather < birds < stars:
        raise RuntimeError(
            "daily schedule must satisfy weather < birds < sunset-plus-offset"
        )

    if local < weather:
        mode = "star-map"
        next_wake = weather
    elif local < birds:
        mode = "weather"
        next_wake = birds
    elif local < stars:
        mode = "birds"
        next_wake = stars
    else:
        mode = "star-map"
        next_wake = _local_boundary(
            local.date() + timedelta(days=1),
            config.weather_start,
            zone,
        )

    # The protocol uses whole UTC seconds. Astral can return microseconds, so
    # ceiling the deadline ensures the ESP never wakes just before a boundary.
    next_utc = next_wake.astimezone(timezone.utc)
    return ScheduleState(
        mode=mode,
        next_wake_at=datetime.fromtimestamp(
            math.ceil(next_utc.timestamp()), tz=timezone.utc
        ),
    )


__all__ = ["ScheduleConfig", "ScheduleState", "SunsetProvider", "schedule_state"]
