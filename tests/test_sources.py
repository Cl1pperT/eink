from datetime import datetime
import os
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

from display_simulator.models import Orientation, RenderContext
from display_simulator.sources import BirdsSource, StarMapSource, TestPatternSource, WeatherSource
from display_simulator.avian_capture import DEMO_SPECIES


class SourceTests(unittest.TestCase):
    def setUp(self):
        self.context = RenderContext(Orientation.LANDSCAPE, datetime(2026, 7, 10, 8), "Denver", offline=True)

    def test_offline_fallback_sources_return_rgb(self):
        for source in (WeatherSource(), BirdsSource(), StarMapSource()):
            with self.subTest(source=source.name):
                image = source.render(self.context)
                self.assertEqual(image.mode, "RGB")
                self.assertEqual(image.size, (1600, 1200))

    def test_test_pattern_generation(self):
        image = TestPatternSource().render(self.context)
        self.assertEqual(image.mode, "RGB")
        self.assertEqual(image.size, (1600, 1200))
        self.assertGreater(len(image.getcolors(maxcolors=10_000_000)), 20)

    def test_demo_species_exercise_rarity_weighted_score(self):
        by_name = {item["com"]: item for item in DEMO_SPECIES}
        rare = by_name["Pygmy Nuthatch"]
        common = by_name["House Finch"]
        self.assertLess(rare["n"], common["n"])
        self.assertGreater(rare["n"] * rare["rarity_weight"], common["n"] * common["rarity_weight"])

    def test_starmap_uses_repository_sample_when_starplot_is_absent(self):
        from PIL import Image
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory)
            checkout = parent / "integrations" / "inkystarmap"
            marker = checkout / "src" / "inkystarmap" / "inkystarmap.py"
            marker.parent.mkdir(parents=True)
            marker.write_text("# marker\n")
            Image.new("RGB", (640, 480), (12, 34, 56)).save(checkout / "inkystarmap2025.jpg")
            context = RenderContext(
                Orientation.LANDSCAPE,
                datetime(2026, 7, 10, 22),
                options={"inkystarmap_repo": str(parent), "use_inkystarmap": True},
            )
            with patch("display_simulator.sources.starmap.importlib.util.find_spec", return_value=None):
                source = StarMapSource()
                image = source.render(context)
        self.assertEqual(image.size, (640, 480))
        self.assertIn("repository sample", source.name)

    def test_starmap_forces_offscreen_matplotlib_backend(self):
        self.assertEqual(os.environ.get("MPLBACKEND"), "Agg")

    def test_avian_viewer_uses_native_horizontal_viewport(self):
        commands = []

        def fake_run(command, **_kwargs):
            commands.append(command)
            output = Path(command[command.index("--out") + 1])
            width = int(command[command.index("--width") + 1])
            height = int(command[command.index("--height") + 1])
            scale = int(command[command.index("--dsf") + 1])
            from PIL import Image
            Image.new("RGB", (width * scale, height * scale), "white").save(output)
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            (repo / "frame").mkdir()
            (repo / "frame" / "shoot.py").write_text("# test marker\n")
            context = RenderContext(
                Orientation.LANDSCAPE,
                datetime(2026, 7, 10, 12),
                options={"avian_repo": str(repo), "bird_source": "http://birdnet.local", "demo_birds": False},
            )
            with patch("display_simulator.sources.birds.subprocess.run", side_effect=fake_run):
                source = BirdsSource()
                image = source.render(context)

        self.assertEqual(image.size, (1600, 1200))
        self.assertIn("horizontal viewer", source.name)
        command = commands[0]
        self.assertEqual(command[command.index("--width") + 1], "800")
        self.assertEqual(command[command.index("--height") + 1], "600")
        self.assertEqual(command[command.index("--dsf") + 1], "2")
        self.assertEqual(command[command.index("--window-hours") + 1], "168")
        self.assertEqual(command[command.index("--collage-vh") + 1], "76")
        self.assertEqual(command[command.index("--cluster-xbias") + 1], "2.1")
        self.assertEqual(command[command.index("--cluster-ybias") + 1], "1.15")
        self.assertEqual(command[command.index("--count-exp") + 1], "0.65")
        self.assertEqual(command[command.index("--packing-budget") + 1], "0.78")

    def test_portrait_bird_viewer_keeps_upstream_frame_defaults(self):
        context = RenderContext(Orientation.PORTRAIT)
        self.assertEqual(BirdsSource._layout_arguments(context), [])

    def test_unreachable_birdnet_uses_original_local_viewer_fixture(self):
        calls = []

        def fake_run(command, **_kwargs):
            calls.append(command)
            if len(calls) == 1:
                return SimpleNamespace(returncode=1, stdout="", stderr="ERR_NAME_NOT_RESOLVED")
            output = Path(command[command.index("--out") + 1])
            width = int(command[command.index("--width") + 1])
            height = int(command[command.index("--height") + 1])
            from PIL import Image
            Image.new("RGB", (width * 2, height * 2), "white").save(output)
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            (repo / "frame").mkdir()
            (repo / "frame" / "shoot.py").write_text("# marker\n")
            context = RenderContext(
                Orientation.LANDSCAPE,
                datetime(2026, 7, 10, 12),
                options={"avian_repo": str(repo), "bird_source": "http://birdnet.local", "demo_birds": False},
            )
            with patch("display_simulator.sources.birds.subprocess.run", side_effect=fake_run):
                source = BirdsSource()
                image = source.render(context)

        self.assertEqual(len(calls), 2)
        self.assertEqual(image.size, (1600, 1200))
        self.assertIn("birdnet.local unavailable", source.name)

    def test_strict_bird_capture_does_not_substitute_demo(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            (repo / "frame").mkdir()
            (repo / "frame" / "shoot.py").write_text("# marker\n")
            context = RenderContext(
                Orientation.LANDSCAPE,
                datetime(2026, 7, 10, 12),
                options={
                    "avian_repo": str(repo),
                    "bird_source": "http://birdnet.local",
                    "demo_birds": False,
                    "allow_demo_fallback": False,
                },
            )
            source = BirdsSource()
            with patch.object(source, "_capture_avian", side_effect=RuntimeError("offline")):
                with patch.object(source, "_capture_avian_demo") as demo:
                    with self.assertRaisesRegex(RuntimeError, "offline"):
                        source.render(context)
            demo.assert_not_called()

    def test_strict_starmap_requires_live_integration(self):
        context = RenderContext(
            Orientation.LANDSCAPE,
            datetime(2026, 7, 10, 22),
            options={"allow_demo_fallback": False, "inkystarmap_repo": ""},
        )
        with self.assertRaisesRegex(RuntimeError, "explicit inkystarmap checkout"):
            StarMapSource().render(context)

    def test_live_weather_missing_integration_is_clear(self):
        context = RenderContext(offline=False)
        with self.assertRaisesRegex(RuntimeError, "weather_frame checkout not found"):
            WeatherSource().render(context)
