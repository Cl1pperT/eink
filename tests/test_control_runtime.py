from __future__ import annotations

from datetime import datetime
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from display_control.settings import default_settings, save_settings
from display_runtime.config import load_runtime_config
from display_runtime.runtime import FrameRuntime
from tests.test_control_settings import sample_catalog


class ControlRuntimeIntegrationTests(unittest.TestCase):
    def _runtime(self, root: Path, settings_path: Path) -> FrameRuntime:
        config_path = root / "runtime.toml"
        config_path.write_text(
            '[control]\nsettings = "control.json"\n'
            '[location]\nname = "Configured location"\n'
            '[sources]\nphoto = "uploads/latest.png"\n',
            encoding="utf-8",
        )
        config = load_runtime_config(config_path)
        self.assertEqual(config.control_settings_path, settings_path.resolve())
        return FrameRuntime(config)

    def test_valid_overlay_is_reloaded_into_render_context(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "control.json"
            catalog = sample_catalog(root)
            value = default_settings(catalog)
            value["enabled_locations"] = ["bear_lake"]
            value["enabled_activities"] = ["hammocking"]
            value["recommendation_count"] = 2
            value["minimum_suitability"] = 0.7
            value["display"] = {
                "location_name": "Provo, Utah",
                "units": "metric",
                "caption": True,
            }
            value["activity_overrides"] = {
                "hammocking": {"estimated_great_days": 25}
            }
            save_settings(path, value, catalog)

            runtime = self._runtime(root, path)
            with patch("display_control.settings.discover_catalog", return_value=catalog):
                control = runtime._control_settings()
            context = runtime._context(datetime(2026, 7, 20, 8), allow_demo=True, control=control)
            self.assertEqual(context.location, "Provo, Utah")
            self.assertEqual(context.options["enabled_environments"], ("bear_lake",))
            self.assertEqual(context.options["enabled_activity_ids"], ("hammocking",))
            self.assertEqual(context.options["recommendation_count"], 2)
            self.assertEqual(context.options["minimum_suitability"], 0.7)
            self.assertEqual(context.options["weather_units"], "metric")
            self.assertTrue(context.options["weather_caption"])
            self.assertEqual(
                context.options["activity_overrides"]["hammocking"]["estimated_great_days"],
                25,
            )

    def test_corrupt_overlay_does_not_replace_trusted_toml_values(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "control.json"
            path.write_text("{not-json", encoding="utf-8")
            catalog = sample_catalog(root)
            runtime = self._runtime(root, path)
            with patch("display_control.settings.discover_catalog", return_value=catalog):
                control = runtime._control_settings()
            self.assertEqual(control, {})
            context = runtime._context(datetime(2026, 7, 20, 8), allow_demo=True, control=control)
            self.assertEqual(context.location, "Configured location")
            self.assertIsNone(context.options["enabled_environments"])

    def test_pi_style_photo_path_falls_back_to_runtime_upload_destination(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            control_dir = root / "control"
            control_dir.mkdir()
            runtime = self._runtime(root, root / "control.json")
            expected = root / "uploads" / "latest.png"
            expected.parent.mkdir()
            expected.write_bytes(b"placeholder")
            self.assertEqual(runtime._photo_path({}), expected.resolve())


if __name__ == "__main__":
    unittest.main()
