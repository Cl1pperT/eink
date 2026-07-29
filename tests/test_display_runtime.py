from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image

from display_control.demo import DemoOverrideStore
from display_runtime.config import ConfigError, load_runtime_config
from display_runtime.ee02 import EE02_PAYLOAD_BYTES, LandscapeRotation
from display_runtime.runtime import FrameRuntime, SourcePolicyError, parse_render_time
from display_simulator.models import Orientation
from display_simulator.pipeline import ImagePipeline, checksum_image, validate_palette


class SolidSource:
    def __init__(self, color: tuple[int, int, int], name: str = "Test Pattern · solid") -> None:
        self.color = color
        self.name = name

    def render(self, _context):
        return Image.new("RGB", (40, 30), self.color)


class StarMetadataSource(SolidSource):
    def __init__(self, metadata: dict[str, str | None]) -> None:
        super().__init__(
            (10, 20, 40),
            "Star Map · live inkystarmap/Starplot render",
        )
        self.metadata = metadata

    def render(self, context):
        context.options.update(self.metadata)
        return super().render(context)


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


def create_colocated_repositories(project_root: Path) -> None:
    markers = (
        project_root / "peacock" / "AvianVisitors" / "weather_frame" / "renderer.py",
        project_root / "peacock" / "AvianVisitors" / "frame" / "shoot.py",
        project_root / "peacock" / "AvianVisitors" / "frame" / "birdweather.py",
        project_root
        / "stars"
        / "integrations"
        / "inkystarmap"
        / "src"
        / "inkystarmap"
        / "inkystarmap.py",
    )
    for marker in markers:
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text("# test marker\n", encoding="utf-8")


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
            path.write_text('[ee02]\nlandscape_rotation = "upside-down"\n', encoding="utf-8")
            with self.assertRaisesRegex(ConfigError, "clockwise or counter-clockwise"):
                load_runtime_config(path)

    def test_render_time_uses_configured_timezone(self):
        naive = parse_render_time("2026-07-11T21:30:00", "America/Denver")
        utc = parse_render_time("2026-07-12T03:30:00Z", "America/Denver")
        self.assertEqual(naive.utcoffset(), utc.utcoffset())
        self.assertEqual(naive, utc)

    def test_photo_conversion_is_separate_from_generated_art(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "runtime.toml"
            path.write_text("", encoding="utf-8")
            config = load_runtime_config(path)
            self.assertEqual(config.photo_conversion.saturation, 0.35)
            self.assertEqual(config.photo_conversion.blue_bias, 0.0)
            self.assertEqual(config.conversion.saturation, 0.6)
            self.assertEqual(config.conversion.blue_bias, 0.5)


class FrameRuntimeTests(unittest.TestCase):
    def test_import_is_headless(self):
        self.assertNotIn("tkinter", sys.modules)

    def test_check_auto_discovers_colocated_repositories(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project_root = root / "project"
            create_colocated_repositories(project_root)
            config = config_for(root)

            with (
                patch("display_simulator.repositories.PROJECT_ROOT", project_root),
                patch.dict(
                    "os.environ",
                    {
                        "AVIANVISITORS_REPO": "",
                        "WEATHER_FRAME_REPO": "",
                        "INKYSTARMAP_REPO": "",
                    },
                ),
                patch("display_runtime.runtime.importlib.util.find_spec", return_value=object()),
            ):
                modes = FrameRuntime(config).check()["modes"]

            for mode in ("weather", "birds", "star-map"):
                with self.subTest(mode=mode):
                    self.assertEqual(modes[mode], {"ready": True, "reason": "ready"})

    def test_check_does_not_replace_invalid_explicit_repositories(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project_root = root / "project"
            create_colocated_repositories(project_root)
            invalid_avian = root / "invalid-avian"
            invalid_stars = root / "invalid-stars"
            config = config_for(
                root,
                avian_weather_repo=invalid_avian,
                inkystarmap_repo=invalid_stars,
            )

            with (
                patch("display_simulator.repositories.PROJECT_ROOT", project_root),
                patch.dict(
                    "os.environ",
                    {
                        "AVIANVISITORS_REPO": "",
                        "WEATHER_FRAME_REPO": "",
                        "INKYSTARMAP_REPO": "",
                    },
                ),
                patch("display_runtime.runtime.importlib.util.find_spec", return_value=object()),
            ):
                modes = FrameRuntime(config).check()["modes"]

            expected_markers = {
                "weather": invalid_avian / "weather_frame" / "renderer.py",
                "birds": invalid_avian / "frame" / "shoot.py",
                "star-map": invalid_stars / "src" / "inkystarmap" / "inkystarmap.py",
            }
            for mode, marker in expected_markers.items():
                with self.subTest(mode=mode):
                    self.assertFalse(modes[mode]["ready"])
                    self.assertIn("repository is invalid", modes[mode]["reason"])
                    self.assertIn(str(marker), modes[mode]["reason"])

    def test_check_does_not_replace_invalid_environment_repositories(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project_root = root / "project"
            create_colocated_repositories(project_root)
            invalid_avian = root / "invalid-avian"
            invalid_stars = root / "invalid-stars"
            config = config_for(root)

            with (
                patch("display_simulator.repositories.PROJECT_ROOT", project_root),
                patch.dict(
                    "os.environ",
                    {
                        "AVIANVISITORS_REPO": str(invalid_avian),
                        "WEATHER_FRAME_REPO": str(invalid_avian),
                        "INKYSTARMAP_REPO": str(invalid_stars),
                    },
                ),
                patch("display_runtime.runtime.importlib.util.find_spec", return_value=object()),
            ):
                modes = FrameRuntime(config).check()["modes"]

            expected_markers = {
                "weather": invalid_avian / "weather_frame" / "renderer.py",
                "birds": invalid_avian / "frame" / "shoot.py",
                "star-map": invalid_stars / "src" / "inkystarmap" / "inkystarmap.py",
            }
            for mode, marker in expected_markers.items():
                with self.subTest(mode=mode):
                    self.assertFalse(modes[mode]["ready"])
                    self.assertIn("repository is invalid", modes[mode]["reason"])
                    self.assertIn(str(marker), modes[mode]["reason"])

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
            self.assertTrue(first.wire_path.is_file())
            self.assertEqual(first.wire_path.stat().st_size, EE02_PAYLOAD_BYTES)
            self.assertTrue(first.manifest_path.is_file())
            with Image.open(first.frame_path) as opened:
                output = opened.convert("RGB")
            self.assertEqual(output.size, (1600, 1200))
            self.assertTrue(validate_palette(output))
            self.assertEqual(checksum_image(output), first.checksum)
            manifest = json.loads(first.manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(manifest["pixel_checksum"]["value"], first.checksum)
            self.assertEqual(manifest["wire"]["sha256"], first.wire_checksum)
            self.assertEqual(manifest["wire"]["buffer_dimensions"], {"width": 1200, "height": 1600})
            self.assertEqual(manifest["wire"]["nibble_order"], "even-x-high-odd-x-low")
            self.assertEqual(manifest["files"]["ee02_4bpp"]["bytes"], 960_000)
            self.assertEqual(manifest["source"]["provenance"], "synthetic")
            self.assertFalse(list(config.output_directory.rglob("*.tmp")))

    def test_star_manifest_records_direction_even_when_pixels_are_unchanged(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = config_for(root)
            runtime = FrameRuntime(
                config,
                source_factories={
                    "star-map": lambda: SolidSource(
                        (10, 20, 40),
                        "Star Map · live inkystarmap/Starplot render",
                    )
                },
            )
            when = parse_render_time("2026-07-27T22:00:00", config.timezone)

            with patch.object(
                runtime,
                "_control_settings",
                return_value={"star_direction": "east"},
            ):
                east = runtime.render("star-map", when=when, allow_demo=True)
            east_manifest = json.loads(east.manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(
                east_manifest["view"],
                {"direction_degrees": 90, "direction_cardinal": "E"},
            )

            with patch.object(
                runtime,
                "_control_settings",
                return_value={"star_direction": "west"},
            ):
                west = runtime.render("star-map", when=when, allow_demo=True)
            west_manifest = json.loads(west.manifest_path.read_text(encoding="utf-8"))
            self.assertTrue(west.changed)
            self.assertTrue(west.written)
            self.assertEqual(east.wire_checksum, west.wire_checksum)
            self.assertEqual(
                west_manifest["view"],
                {"direction_degrees": 270, "direction_cardinal": "W"},
            )

            runtime.source_factories["star-map"] = lambda: SolidSource(
                (10, 20, 40),
                "Star Map · configured static image",
            )
            with patch.object(
                runtime,
                "_control_settings",
                return_value={"star_direction": "north"},
            ):
                static = runtime.render("star-map", when=when, allow_demo=True)
            static_manifest = json.loads(
                static.manifest_path.read_text(encoding="utf-8")
            )
            self.assertTrue(static.changed)
            self.assertNotIn("view", static_manifest)

    def test_star_manifest_rewrites_when_observing_night_changes_with_same_pixels(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = config_for(root)
            metadata = {
                "star_observation_time": "2026-07-27T22:10:00-06:00",
                "star_sunrise_time": "2026-07-28T06:21:00-06:00",
                "star_night_date": "2026-07-27",
                "star_featured_constellation": "Scorpius",
            }
            runtime = FrameRuntime(
                config,
                source_factories={
                    "star-map": lambda: StarMetadataSource(dict(metadata)),
                },
            )
            when = parse_render_time("2026-07-27T12:00:00", config.timezone)

            with patch.object(
                runtime,
                "_control_settings",
                return_value={"star_direction": "south"},
            ):
                first = runtime.render("star-map", when=when, allow_demo=True)
            first_manifest = json.loads(first.manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(
                first_manifest["view"]["observation_time"],
                "2026-07-27T22:10:00-06:00",
            )
            self.assertEqual(first_manifest["view"]["night_date"], "2026-07-27")
            self.assertEqual(
                first_manifest["view"]["sunrise_time"],
                "2026-07-28T06:21:00-06:00",
            )
            self.assertEqual(
                first_manifest["view"]["featured_constellation"],
                "Scorpius",
            )

            metadata.update(
                {
                    "star_observation_time": "2026-07-28T22:09:00-06:00",
                    "star_sunrise_time": "2026-07-29T06:22:00-06:00",
                    "star_night_date": "2026-07-28",
                    "star_featured_constellation": "Sagittarius",
                }
            )
            with patch.object(
                runtime,
                "_control_settings",
                return_value={"star_direction": "south"},
            ):
                second = runtime.render("star-map", when=when, allow_demo=True)
            second_manifest = json.loads(second.manifest_path.read_text(encoding="utf-8"))

            self.assertEqual(first.wire_checksum, second.wire_checksum)
            self.assertTrue(second.changed)
            self.assertTrue(second.written)
            self.assertEqual(second_manifest["view"]["night_date"], "2026-07-28")
            self.assertEqual(
                second_manifest["view"]["featured_constellation"],
                "Sagittarius",
            )

    def test_uploaded_photo_uses_photo_only_conversion(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            photo = root / "photo.png"
            Image.new("RGB", (40, 30), (30, 140, 210)).save(photo)
            config = config_for(root, photo_path=photo)
            seen = {}
            original_render = ImagePipeline.render

            def capture_settings(pipeline, source, context, settings, fit_mode):
                seen[source.name] = settings
                return original_render(
                    pipeline,
                    source,
                    context,
                    settings,
                    fit_mode,
                )

            factories = {
                "uploaded-photo": lambda: SolidSource(
                    (30, 140, 210), "Uploaded Photo · test"
                ),
                "test-pattern": lambda: SolidSource(
                    (30, 140, 210), "Test Pattern · test"
                ),
            }
            with patch.object(ImagePipeline, "render", new=capture_settings):
                runtime = FrameRuntime(config, source_factories=factories)
                photo_artifact = runtime.render("uploaded-photo")
                runtime.render("test-pattern")

            self.assertIs(seen["Uploaded Photo · test"], config.photo_conversion)
            self.assertIs(seen["Test Pattern · test"], config.conversion)
            manifest = json.loads(
                photo_artifact.manifest_path.read_text(encoding="utf-8")
            )
            self.assertRegex(manifest["photo"]["recipe_sha256"], r"^[0-9a-f]{64}$")
            self.assertEqual(
                manifest["photo"]["crop"],
                {"center_x": 0.5, "center_y": 0.5, "zoom": 1.0},
            )

    def test_photo_recipe_commit_advances_when_palette_pixels_are_unchanged(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            photo = root / "photo.png"
            Image.new("RGB", (40, 30), (30, 140, 210)).save(photo)
            runtime = FrameRuntime(
                config_for(root, photo_path=photo),
                source_factories={
                    "uploaded-photo": lambda: SolidSource(
                        (30, 140, 210),
                        "Uploaded Photo · test",
                    )
                },
            )
            controls = (
                {
                    "photo_enabled": True,
                    "photo_crop": {
                        "center_x": 0.5,
                        "center_y": 0.5,
                        "zoom": 1.0,
                    },
                },
                {
                    "photo_enabled": True,
                    "photo_crop": {
                        "center_x": 0.25,
                        "center_y": 0.75,
                        "zoom": 2.0,
                    },
                },
            )

            with patch.object(
                runtime,
                "_control_settings",
                side_effect=controls,
            ):
                first = runtime.render("uploaded-photo")
                first_manifest = json.loads(
                    first.manifest_path.read_text(encoding="utf-8")
                )
                second = runtime.render("uploaded-photo")

            second_manifest = json.loads(
                second.manifest_path.read_text(encoding="utf-8")
            )
            self.assertEqual(first.wire_checksum, second.wire_checksum)
            self.assertTrue(second.changed)
            self.assertTrue(second.written)
            self.assertNotEqual(
                first_manifest["photo"]["recipe_sha256"],
                second_manifest["photo"]["recipe_sha256"],
            )
            self.assertEqual(
                second_manifest["photo"]["crop"],
                {"center_x": 0.25, "center_y": 0.75, "zoom": 2.0},
            )

    def test_portrait_commit_is_native_size(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = config_for(root, orientation=Orientation.PORTRAIT)
            artifact = FrameRuntime(
                config,
                source_factories={"test-pattern": lambda: SolidSource((0, 0, 0))},
            ).render("test-pattern")
            self.assertEqual((artifact.width, artifact.height), (1200, 1600))
            self.assertEqual(artifact.wire_rotation, "none")
            self.assertEqual(artifact.seeed_sprite_rotation, 0)
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

    def test_wire_write_failure_preserves_previous_current_manifest(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = config_for(root)
            first = FrameRuntime(
                config,
                source_factories={"test-pattern": lambda: SolidSource((0, 0, 0))},
            ).render("test-pattern")
            before = first.manifest_path.read_bytes()
            with patch("display_runtime.runtime._atomic_write_bytes", side_effect=OSError("disk full")):
                with self.assertRaisesRegex(OSError, "disk full"):
                    FrameRuntime(
                        config,
                        source_factories={"test-pattern": lambda: SolidSource((255, 0, 0))},
                    ).render("test-pattern")
            self.assertEqual(first.manifest_path.read_bytes(), before)
            self.assertEqual(first.wire_path.stat().st_size, EE02_PAYLOAD_BYTES)

    def test_corrupt_cached_wire_is_repaired_without_claiming_pixel_change(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = config_for(root)
            factories = {"test-pattern": lambda: SolidSource((255, 0, 0))}
            first = FrameRuntime(config, source_factories=factories).render("test-pattern")
            first.wire_path.write_bytes(b"corrupt")
            repaired = FrameRuntime(config, source_factories=factories).render("test-pattern")
            self.assertFalse(repaired.changed)
            self.assertTrue(repaired.written)
            self.assertEqual(repaired.wire_path.stat().st_size, EE02_PAYLOAD_BYTES)
            self.assertEqual(repaired.wire_path, first.wire_path)

    def test_landscape_rotation_change_advances_wire_checksum(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = config_for(root)

            class AsymmetricSource:
                name = "Test Pattern · asymmetric"

                def render(self, _context):
                    image = Image.new("RGB", (1600, 1200), (236, 234, 223))
                    image.putpixel((0, 0), (26, 26, 28))
                    return image

            factories = {"test-pattern": AsymmetricSource}
            clockwise = FrameRuntime(config, source_factories=factories).render("test-pattern")
            counter_config = replace(
                config, landscape_rotation=LandscapeRotation.COUNTER_CLOCKWISE
            )
            counter = FrameRuntime(counter_config, source_factories=factories).render("test-pattern")
            self.assertTrue(counter.changed)
            self.assertNotEqual(clockwise.wire_checksum, counter.wire_checksum)
            self.assertEqual(counter.wire_rotation, "counter-clockwise")

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

    def test_active_mode_uses_control_selection_and_configured_timezone_schedule(self):
        with tempfile.TemporaryDirectory() as directory:
            config = config_for(Path(directory))
            runtime = FrameRuntime(config)
            morning_utc = datetime.fromisoformat("2026-07-11T14:00:00+00:00")
            noon_utc = datetime.fromisoformat("2026-07-11T18:00:00+00:00")
            night_utc = datetime.fromisoformat("2026-07-12T03:00:00+00:00")

            with patch.object(
                runtime, "_control_settings", return_value={"display_mode": "automatic"}
            ) as settings:
                self.assertEqual(runtime.resolve_active_mode(morning_utc), "weather")
                self.assertEqual(runtime.resolve_active_mode(noon_utc), "birds")
                self.assertEqual(runtime.resolve_active_mode(night_utc), "star-map")
            self.assertTrue(
                all(
                    call.kwargs == {"fail_closed": True}
                    for call in settings.call_args_list
                )
            )

            with patch.object(
                runtime, "_control_settings", return_value={"display_mode": "uploaded-photo"}
            ):
                self.assertEqual(runtime.resolve_active_mode(night_utc), "uploaded-photo")

            with patch.object(
                runtime, "_control_settings", return_value={"display_mode": "not-a-mode"}
            ):
                with self.assertRaisesRegex(SourcePolicyError, "display.mode"):
                    runtime.resolve_active_mode(noon_utc)

    def test_active_demo_override_expires_back_to_saved_mode_or_schedule(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings_path = root / "control.json"
            config = config_for(root, control_settings_path=settings_path)
            runtime = FrameRuntime(config)
            morning = datetime.fromisoformat("2026-07-11T14:00:00+00:00")
            store = DemoOverrideStore(settings_path, clock=lambda: morning)
            store.activate("birds")

            with patch.object(
                runtime,
                "_control_settings",
                return_value={"display_mode": "automatic"},
            ):
                self.assertEqual(runtime.resolve_active_mode(morning), "birds")
                active = runtime.resolve_active_state(morning)
                self.assertEqual(
                    active.next_wake_at,
                    morning + timedelta(minutes=5),
                )
                self.assertEqual(
                    runtime.resolve_active_mode(morning + timedelta(minutes=4, seconds=59)),
                    "birds",
                )
                self.assertEqual(
                    runtime.resolve_active_mode(morning + timedelta(minutes=5)),
                    "weather",
                )

            night = datetime.fromisoformat("2026-07-12T03:00:00+00:00")
            store.activate("star-map", now=night)
            with patch.object(
                runtime,
                "_control_settings",
                return_value={"display_mode": "uploaded-photo"},
            ):
                self.assertEqual(runtime.resolve_active_mode(night), "star-map")
                self.assertEqual(
                    runtime.resolve_active_mode(night + timedelta(minutes=5)),
                    "uploaded-photo",
                )

            store.path.write_text("{not-json", encoding="utf-8")
            with patch.object(
                runtime,
                "_control_settings",
                return_value={"display_mode": "automatic"},
            ):
                self.assertEqual(runtime.resolve_active_mode(morning), "weather")

    def test_timed_photo_owns_next_wake_until_its_exact_expiry(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings_path = root / "control.json"
            config = config_for(root, control_settings_path=settings_path)
            runtime = FrameRuntime(config)
            start = datetime.fromisoformat("2026-07-11T14:50:00+00:00")
            duration = 2 * 60 * 60
            store = DemoOverrideStore(settings_path, clock=lambda: start)
            store.activate("uploaded-photo", duration_seconds=duration)

            with patch.object(
                runtime,
                "_control_settings",
                return_value={"display_mode": "automatic"},
            ):
                state = runtime.resolve_active_state(start)
                self.assertEqual(state.mode, "uploaded-photo")
                self.assertEqual(
                    state.next_wake_at,
                    start + timedelta(seconds=duration),
                )
                self.assertEqual(
                    runtime.resolve_active_mode(
                        start + timedelta(seconds=duration - 1)
                    ),
                    "uploaded-photo",
                )
                self.assertEqual(
                    runtime.resolve_active_mode(
                        start + timedelta(seconds=duration)
                    ),
                    "birds",
                )


if __name__ == "__main__":
    unittest.main()
