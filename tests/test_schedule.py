from datetime import date, datetime, time
import unittest
from zoneinfo import ZoneInfo

from display_runtime.schedule import (
    ScheduleConfig as RuntimeScheduleConfig,
    schedule_state,
)
from display_simulator.schedule import ScheduleConfig, mode_for_time


class ScheduleTests(unittest.TestCase):
    def test_default_boundaries(self):
        cases = {
            (5, 59): "Star Map", (6, 0): "Weather", (8, 59): "Weather",
            (9, 0): "Birds", (19, 59): "Birds", (20, 0): "Star Map",
        }
        for (hour, minute), expected in cases.items():
            with self.subTest(hour=hour, minute=minute):
                self.assertEqual(mode_for_time(datetime(2026, 1, 1, hour, minute)), expected)

    def test_configurable_boundaries(self):
        config = ScheduleConfig(time(7), time(11), time(21, 30))
        self.assertEqual(mode_for_time(time(6, 59), config), "Star Map")
        self.assertEqual(mode_for_time(time(7), config), "Weather")
        self.assertEqual(mode_for_time(time(11), config), "Birds")
        self.assertEqual(mode_for_time(time(21, 30), config), "Star Map")

    def test_runtime_boundaries_use_sunset_plus_thirty(self):
        zone = ZoneInfo("America/Denver")
        config = RuntimeScheduleConfig()

        def sunset(day: date) -> datetime:
            return datetime(
                day.year,
                day.month,
                day.day,
                20,
                43,
                33,
                200_000,
                tzinfo=zone,
            )

        cases = (
            ("2026-07-29T05:59:59-06:00", "star-map", "2026-07-29T12:00:00Z"),
            ("2026-07-29T06:00:00-06:00", "weather", "2026-07-29T15:00:00Z"),
            ("2026-07-29T09:00:00-06:00", "birds", "2026-07-30T03:13:34Z"),
            ("2026-07-29T21:13:34-06:00", "star-map", "2026-07-30T12:00:00Z"),
        )
        for timestamp, mode, next_wake in cases:
            with self.subTest(timestamp=timestamp):
                state = schedule_state(
                    datetime.fromisoformat(timestamp),
                    config,
                    latitude=39.7,
                    longitude=-105.0,
                    timezone_name="America/Denver",
                    sunset_provider=sunset,
                )
                self.assertEqual(state.mode, mode)
                self.assertEqual(
                    state.next_wake_at.isoformat().replace("+00:00", "Z"),
                    next_wake,
                )

    def test_runtime_deadline_respects_winter_timezone_offset(self):
        zone = ZoneInfo("America/Denver")

        def sunset(day: date) -> datetime:
            return datetime.combine(day, time(17, 3, 42), tzinfo=zone)

        state = schedule_state(
            datetime.fromisoformat("2026-12-21T09:00:00-07:00"),
            RuntimeScheduleConfig(),
            latitude=39.7,
            longitude=-105.0,
            timezone_name="America/Denver",
            sunset_provider=sunset,
        )
        self.assertEqual(state.mode, "birds")
        self.assertEqual(
            state.next_wake_at.isoformat(),
            "2026-12-22T00:33:42+00:00",
        )
