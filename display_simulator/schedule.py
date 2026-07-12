from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time


@dataclass(frozen=True, slots=True)
class ScheduleConfig:
    weather_start: time = time(6, 0)
    birds_start: time = time(10, 0)
    star_start: time = time(20, 0)


def mode_for_time(when: datetime | time, config: ScheduleConfig | None = None) -> str:
    """Return Weather, Birds, or Star Map for a simulated local time."""
    config = config or ScheduleConfig()
    value = when.time() if isinstance(when, datetime) else when
    if config.weather_start <= value < config.birds_start:
        return "Weather"
    if config.birds_start <= value < config.star_start:
        return "Birds"
    return "Star Map"


def parse_clock(value: str) -> time:
    hour, minute = (int(part) for part in value.strip().split(":", 1))
    return time(hour, minute)
