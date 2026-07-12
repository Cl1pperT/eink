from datetime import datetime, time
import unittest

from display_simulator.schedule import ScheduleConfig, mode_for_time


class ScheduleTests(unittest.TestCase):
    def test_default_boundaries(self):
        cases = {
            (5, 59): "Star Map", (6, 0): "Weather", (9, 59): "Weather",
            (10, 0): "Birds", (19, 59): "Birds", (20, 0): "Star Map",
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
