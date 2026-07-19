import tempfile
import unittest
from pathlib import Path

from display_simulator.config import load_config
from display_simulator.preferences import (
    load_preferences,
    repository_preference_value,
    save_preferences,
)


class PreferenceTests(unittest.TestCase):
    def test_paths_and_location_round_trip(self):
        data = {
            "location": "Moab, UT",
            "repositories": {"avian_weather": "/projects/avian", "inkystarmap": "/projects/stars"},
            "sources": {"bird": "http://birdnet.local", "starmap": "/frames/stars.png", "photo": "/photos/latest.jpg"},
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "preferences.json"
            self.assertEqual(save_preferences(data, path), path)
            self.assertEqual(load_preferences(path), data)

    def test_missing_or_invalid_preferences_are_ignored(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "preferences.json"
            self.assertEqual(load_preferences(path), {})
            path.write_text("not json")
            self.assertEqual(load_preferences(path), {})

    def test_project_defaults_use_one_shared_repository(self):
        repositories = load_config()["repositories"]
        self.assertIn("avian_weather", repositories)
        self.assertNotIn("avian", repositories)
        self.assertNotIn("weather", repositories)

    def test_untouched_discovery_does_not_become_a_saved_override(self):
        discovered = "/checkout/peacock/AvianVisitors"
        self.assertEqual(
            repository_preference_value(discovered, discovered, ""),
            "",
        )
        self.assertEqual(
            repository_preference_value(discovered, discovered, "/saved/parent"),
            "/saved/parent",
        )
        self.assertEqual(
            repository_preference_value(
                discovered,
                discovered,
                "",
                explicitly_selected=True,
            ),
            discovered,
        )
        self.assertEqual(
            repository_preference_value("/typed/repo", discovered, ""),
            "/typed/repo",
        )
