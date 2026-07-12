from __future__ import annotations

from dataclasses import replace
from datetime import datetime
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image

from display_runtime.config import ConfigError, load_runtime_config
from display_runtime.runtime import FrameRuntime, SourcePolicyError, parse_render_time
from display_simulator.models import Orientation
from display_simulator.pipeline import checksum_image, validate_palette


class SolidSource:
    def __init__(self, color: tuple[int, int, int], name: str = "Test Pattern · solid") -> None:
        self.color = color
        self.name = name

    def render(self, _context):
        return Image.new("RGB", (40, 30), self.color)


class FailingSource:
    name = "Test Pattern · failure"

    def render(self, _context):
        raise RuntimeError("deliberate source failure")


def config_for(directory: Path, **updates):
    path = directory / "runtime.toml"
    path.write_text("", encoding="utf-8")
    config = load_runtime_config(path)
    return replace(
        config,
        output_directory=directory / "frames",
        write_rgb=False,
        **updates,
    )


class RuntimeConfigTests(unittest.TestCase):
    def test_relative_paths_resolve_from_config_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "runtime.toml"
            path.write_text(
                '[repositories]\navian_weather = "avian"\n'
                '[sources]\nphoto = "upload.png"\n'
                '[output]\ndirectory = "frames"\n',
                encoding="utf-8",
            )
            config = load_runtime_config(path)
            resolved = root.resolve()
            self.assertEqual(config.avian_weather_repo, resolved / "avian")
            self.assertEqual(config.photo_path, resolved / "upload.png")
            self.assertEqual(config.output_directory, resolved / "frames")

    def test_unknown_keys_and_invalid_timezone_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "runtime.toml"
            path.write_text("[runtime]\nunknown = true\n", encoding="utf-8")
            with self.assertRaisesRegex(ConfigError, "unknown configuration key"):
                load_runtime_config(path)
            path.write_text('[location]\ntimezone = "Mars/Olympus"\n', encoding="utf-8")
            with self.assertRaisesRegex(ConfigError, "unknown location.timezone"):
                load_runtime_config(path)
            path.write_text('location = "Provo"\n', encoding="utf-8")
            with self.assertRaisesRegex(ConfigError, "location.*must be a table"):
                load_runtime_config(path)

    def test_render_time_uses_configured_timezone(self):
        naive = parse_render_time("2026-07-11T21:30:00", "America/Denver")
        utc = parse_render_time("2026-07-12T03:30:00Z", "America/Denver")
        self.assertEqual(naive.utcoffset(), utc.utcoffset())
        self.assertEqual(naive, utc)


class FrameRuntimeTests(unittest.TestCase):
    def test_import_is_headless(self):
        self.assertNotIn("tkinter", sys.modules)

    def test_atomic_commit_and_persistent_unchanged_detection(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = config_for(root)
            factories = {"test-pattern": lambda: SolidSource((255, 0, 0))}
            when = parse_render_time("2026-07-11T12:00:00", config.timezone)
            first = FrameRuntime(config, source_factories=factories).render("test-pattern", when=when)
            second = FrameRuntime(config, source_factories=factories).render("test-pattern", when=when)

            self.assertTrue(first.changed)
            self.assertTrue(first.written)
            self.assertFalse(second.changed)
            self.assertFalse(second.written)
            self.assertEqual(first.frame_path, second.frame_path)
            self.assertTrue(first.frame_path.is_file())
            self.assertTrue(first.manifest_path.is_file())
            with Image.open(first.frame_path) as opened:
                output = opened.convert("RGB")
            self.assertEqual(output.size, (1600, 1200))
            self.assertTrue(validate_palette(output))
            self.assertEqual(checksum_image(output), first.checksum)
            manifest = json.loads(first.manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(manifest["pixel_checksum"]["value"], first.checksum)
            self.assertEqual(manifest["source"]["provenance"], "synthetic")
            self.assertFalse(list(config.output_directory.rglob("*.tmp")))

    def test_portrait_commit_is_native_size(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = config_for(root, orientation=Orientation.PORTRAIT)
            artifact = FrameRuntime(
                config,
                source_factories={"test-pattern": lambda: SolidSource((0, 0, 0))},
            ).render("test-pattern")
            self.assertEqual((artifact.width, artifact.height), (1200, 1600))
            with Image.open(artifact.frame_path) as opened:
                self.assertEqual(opened.size, (1200, 1600))

    def test_failed_source_preserves_last_known_good_pointer(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = config_for(root)
            good = FrameRuntime(
                config,
                source_factories={"test-pattern": lambda: SolidSource((0, 0, 0))},
            ).render("test-pattern")
            before = good.manifest_path.read_bytes()
            with self.assertRaisesRegex(RuntimeError, "deliberate source failure"):
                FrameRuntime(
                    config,
                    source_factories={"test-pattern": FailingSource},
                ).render("test-pattern")
            self.assertEqual(good.manifest_path.read_bytes(), before)
            self.assertTrue(good.frame_path.is_file())

    def test_commit_failure_preserves_previous_current_manifest(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = config_for(root)
            first = FrameRuntime(
                config,
                source_factories={"test-pattern": lambda: SolidSource((0, 0, 0))},
            ).render("test-pattern")
            before = first.manifest_path.read_bytes()
            with patch("display_runtime.runtime._atomic_write_json", side_effect=OSError("disk full")):
                with self.assertRaisesRegex(OSError, "disk full"):
                    FrameRuntime(
                        config,
                        source_factories={"test-pattern": lambda: SolidSource((255, 0, 0))},
                    ).render("test-pattern")
            self.assertEqual(first.manifest_path.read_bytes(), before)

    def test_strict_runtime_rejects_fallback_provenance(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = root / "avian"
            marker = repo / "frame" / "shoot.py"
            marker.parent.mkdir(parents=True)
            marker.write_text("# marker\n", encoding="utf-8")
            config = config_for(
                root,
                avian_weather_repo=repo,
                bird_source="http://birdnet.local",
                strict_sources=True,
            )
            runtime = FrameRuntime(
                config,
                source_factories={
                    "birds": lambda: SolidSource((255, 0, 0), "Birds · synthetic offline fallback")
                },
            )
            with self.assertRaisesRegex(SourcePolicyError, "synthetic content"):
                runtime.render("birds")
            self.assertFalse((config.output_directory / "birds" / "current.json").exists())

    def test_automatic_mode_resolves_in_configured_timezone_and_forbids_demo(self):
        with tempfile.TemporaryDirectory() as directory:
            config = config_for(Path(directory), strict_sources=False)
            runtime = FrameRuntime(config)
            morning = parse_render_time("2026-07-11T07:00:00", config.timezone)
            noon = parse_render_time("2026-07-11T12:00:00", config.timezone)
            night = parse_render_time("2026-07-11T21:00:00", config.timezone)
            self.assertEqual(runtime.resolve_mode("automatic", morning), "weather")
            self.assertEqual(runtime.resolve_mode("automatic", noon), "birds")
            self.assertEqual(runtime.resolve_mode("automatic", night), "star-map")
            with self.assertRaisesRegex(SourcePolicyError, "automatic mode never permits demo"):
                runtime.render("automatic", when=morning)


if __name__ == "__main__":
    unittest.main()
