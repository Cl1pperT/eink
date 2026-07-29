from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from display_control.settings import (
    ActivityDefinition,
    Catalog,
    LocationDefinition,
    SettingsValidationError,
    default_settings,
    load_settings,
    save_settings,
    validate_settings,
)


def sample_catalog(root: Path) -> Catalog:
    conditions = {
        "temperature_f": {
            "tolerable_min": 30,
            "ideal_min": 50,
            "ideal_max": 72,
            "tolerable_max": 92,
            "weight": 4,
            "required": False,
        },
        "wind_mph": {
            "tolerable_min": 0,
            "ideal_min": 0,
            "ideal_max": 8,
            "tolerable_max": 25,
            "weight": 3,
            "required": False,
        },
    }
    return Catalog(
        root,
        (
            LocationDefinition("mount_timpanogos", "Mount Timpanogos", "Wasatch", True),
            LocationDefinition("bear_lake", "Bear Lake", "Turquoise lake", True),
        ),
        (
            ActivityDefinition("rock_climbing", "Rock climbing", 60, False, conditions, True),
            ActivityDefinition("hammocking", "Hammocking", 51, False, conditions, True),
        ),
        ("temperature_f", "wind_mph"),
    )


class ControlSettingsTests(unittest.TestCase):
    def test_defaults_are_complete_and_empty_activity_selection_is_valid(self):
        with tempfile.TemporaryDirectory() as directory:
            catalog = sample_catalog(Path(directory))
            settings = default_settings(catalog)
            self.assertEqual(settings["enabled_locations"], list(catalog.location_ids))
            self.assertEqual(settings["enabled_activities"], list(catalog.activity_ids))
            self.assertEqual(settings["schema_version"], 3)
            self.assertEqual(settings["display"]["mode"], "automatic")
            self.assertEqual(settings["stars"], {"direction": "south"})
            self.assertEqual(
                settings["birds"],
                {
                    "provider": "birdweather",
                    "postal_code": "84601",
                    "country": "us",
                    "lookback_days": 7,
                    "title": "Avian Visitors",
                    "subtitle": "Nearby This Week",
                },
            )
            settings["enabled_activities"] = []
            self.assertEqual(validate_settings(settings, catalog)["enabled_activities"], [])

    def test_v1_settings_migrate_without_losing_existing_choices(self):
        with tempfile.TemporaryDirectory() as directory:
            catalog = sample_catalog(Path(directory))
            legacy = default_settings(catalog)
            legacy["schema_version"] = 1
            legacy["display"].pop("mode")
            legacy.pop("birds")
            legacy.pop("stars")
            legacy["enabled_locations"] = ["provo_utah"]
            migrated = validate_settings(legacy, catalog)
            self.assertEqual(migrated["schema_version"], 3)
            self.assertEqual(migrated["enabled_locations"], ["mount_timpanogos"])
            self.assertEqual(migrated["display"]["mode"], "automatic")
            self.assertEqual(migrated["birds"]["postal_code"], "84601")
            self.assertEqual(migrated["stars"]["direction"], "south")

    def test_v2_settings_gain_default_star_direction(self):
        with tempfile.TemporaryDirectory() as directory:
            catalog = sample_catalog(Path(directory))
            legacy = default_settings(catalog)
            legacy["schema_version"] = 2
            legacy.pop("stars")
            migrated = validate_settings(legacy, catalog)
            self.assertEqual(migrated["schema_version"], 3)
            self.assertEqual(migrated["stars"], {"direction": "south"})

    def test_cardinal_star_directions_are_valid(self):
        with tempfile.TemporaryDirectory() as directory:
            catalog = sample_catalog(Path(directory))
            for direction in ("north", "east", "south", "west"):
                with self.subTest(direction=direction):
                    settings = default_settings(catalog)
                    settings["stars"]["direction"] = direction
                    self.assertEqual(
                        validate_settings(settings, catalog)["stars"]["direction"],
                        direction,
                    )

    def test_invalid_display_birdweather_and_star_fields_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            catalog = sample_catalog(Path(directory))
            cases = (
                (("display", "mode"), "live-microphone", "display.mode"),
                (("birds", "provider"), "birdnet", "birds.provider"),
                (("birds", "postal_code"), "../etc", "postal_code"),
                (("birds", "country"), "USA", "country"),
                (("birds", "lookback_days"), 0, "1 to 30"),
                (("stars", "direction"), "up", "stars.direction"),
            )
            for path, value, message in cases:
                with self.subTest(path=path):
                    settings = default_settings(catalog)
                    settings[path[0]][path[1]] = value
                    with self.assertRaisesRegex(SettingsValidationError, message):
                        validate_settings(settings, catalog)

    def test_invalid_ids_days_ranges_weights_and_nonfinite_values_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            catalog = sample_catalog(Path(directory))
            cases = (
                ("no location", {"enabled_locations": []}, "at least one"),
                ("unknown activity", {"enabled_activities": ["skiing"]}, "Unknown ID"),
                (
                    "bad days",
                    {"activity_overrides": {"rock_climbing": {"estimated_great_days": 366}}},
                    "0 to 365",
                ),
                (
                    "unordered",
                    {"activity_overrides": {"rock_climbing": {"conditions": {"wind_mph": {"ideal_min": 30}}}}},
                    "ascending",
                ),
                (
                    "zero weight",
                    {"activity_overrides": {"rock_climbing": {"conditions": {"wind_mph": {"weight": 0}}}}},
                    "positive",
                ),
                (
                    "not finite",
                    {"activity_overrides": {"rock_climbing": {"conditions": {"wind_mph": {"weight": float("nan")}}}}},
                    "finite",
                ),
            )
            for name, change, message in cases:
                with self.subTest(name=name):
                    value = default_settings(catalog)
                    value.update(change)
                    with self.assertRaisesRegex(SettingsValidationError, message):
                        validate_settings(value, catalog)

    def test_sparse_overrides_round_trip_atomically(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            catalog = sample_catalog(root)
            path = root / "state" / "settings.json"
            value = default_settings(catalog)
            value["enabled_locations"] = ["bear_lake"]
            value["stars"]["direction"] = "east"
            value["activity_overrides"] = {
                "rock_climbing": {
                    "estimated_great_days": 42,
                    "conditions": {"wind_mph": {"tolerable_max": 20, "required": True}},
                }
            }
            saved = save_settings(path, value, catalog)
            self.assertEqual(saved, load_settings(path, catalog, strict=True))
            self.assertEqual(json.loads(path.read_text()), saved)
            self.assertFalse(list(path.parent.glob("*.tmp")))

    def test_strict_load_reports_corrupt_state_while_normal_load_recovers_defaults(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            catalog = sample_catalog(root)
            path = root / "settings.json"
            path.write_text("{not-json", encoding="utf-8")
            with self.assertRaises(json.JSONDecodeError):
                load_settings(path, catalog, strict=True)
            self.assertEqual(load_settings(path, catalog), default_settings(catalog))


if __name__ == "__main__":
    unittest.main()
