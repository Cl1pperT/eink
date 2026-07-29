from contextlib import contextmanager
from datetime import datetime
import importlib.util
import os
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image, ImageColor

from display_simulator.models import Orientation, RenderContext
from display_simulator.models import ConversionSettings
from display_simulator.pipeline import SPECTRA_PALETTE, convert_to_spectra
from display_simulator.sources import BirdsSource, StarMapSource, TestPatternSource, WeatherSource
from display_simulator.sources.starmap import (
    _INK_BLACK,
    _INK_BLUE,
    _INK_GREEN,
    _INK_RED,
    _INK_WHITE,
    _INK_YELLOW,
    _PLANET_VISUALS,
    _colorful_plot_style,
    _fit_full_sky,
    _plot_colorful_planets,
    _stellar_color,
)
from display_simulator.avian_capture import DEMO_SPECIES


@contextmanager
def without_external_repositories():
    with tempfile.TemporaryDirectory() as directory:
        empty_root = Path(directory)
        with (
            patch("display_simulator.repositories.PROJECT_ROOT", empty_root),
            patch(
                "display_simulator.repositories.Path.cwd",
                return_value=empty_root / "cwd",
            ),
            patch(
                "display_simulator.repositories.Path.home",
                return_value=empty_root / "home",
            ),
            patch.dict(
                os.environ,
                {
                    "AVIANVISITORS_REPO": "",
                    "WEATHER_FRAME_REPO": "",
                    "INKYSTARMAP_REPO": "",
                },
                clear=False,
            ),
        ):
            yield


class SourceTests(unittest.TestCase):
    def setUp(self):
        self.context = RenderContext(Orientation.LANDSCAPE, datetime(2026, 7, 10, 8), "Denver", offline=True)

    def test_offline_fallback_sources_return_rgb(self):
        with without_external_repositories():
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

    def test_offline_starmap_exercises_every_display_color(self):
        with without_external_repositories():
            image = StarMapSource().render(self.context)
        pixels = (
            image.get_flattened_data()
            if hasattr(image, "get_flattened_data")
            else image.getdata()
        )
        colors = set(pixels)
        expected = {
            ImageColor.getrgb(color)
            for color in (
                _INK_BLACK,
                _INK_WHITE,
                _INK_YELLOW,
                _INK_RED,
                _INK_BLUE,
                _INK_GREEN,
            )
        }
        self.assertTrue(expected.issubset(colors))

    def test_stellar_colors_follow_catalogued_temperature(self):
        cases = (
            (-0.2, _INK_BLUE),
            (0.24, _INK_BLUE),
            (0.25, _INK_WHITE),
            (0.64, _INK_WHITE),
            (0.65, _INK_YELLOW),
            (1.19, _INK_YELLOW),
            (1.2, _INK_RED),
            (float("nan"), _INK_WHITE),
            (None, _INK_WHITE),
        )
        for bv, expected in cases:
            with self.subTest(bv=bv):
                self.assertEqual(_stellar_color(SimpleNamespace(bv=bv)), expected)
        self.assertEqual(_stellar_color(SimpleNamespace()), _INK_WHITE)

    def test_planet_visuals_cover_every_supported_world(self):
        expected = {
            "mercury", "venus", "mars", "jupiter",
            "saturn", "uranus", "neptune", "pluto",
        }
        self.assertEqual(set(_PLANET_VISUALS), expected)
        self.assertIn("ring_size", _PLANET_VISUALS["saturn"])
        self.assertNotIn("ring_size", _PLANET_VISUALS["jupiter"])
        for name, visual in _PLANET_VISUALS.items():
            with self.subTest(planet=name):
                self.assertGreaterEqual(visual["size"], 40)
                self.assertEqual(visual["label"], _INK_WHITE)
                self.assertNotEqual(visual["symbol"], "circle_line")
                self.assertIn(
                    visual["fill"],
                    {_INK_BLACK, _INK_WHITE, _INK_YELLOW, _INK_RED, _INK_BLUE, _INK_GREEN},
                )

    def test_colored_planets_plot_only_visible_worlds_and_layer_saturns_ring(self):
        class Style:
            def __init__(self, **values):
                self.values = values

        class Planets:
            @staticmethod
            def all(_observer, _ephemeris):
                return iter(
                    (
                        SimpleNamespace(name="Mars", ra=1, dec=1),
                        SimpleNamespace(name=SimpleNamespace(value="saturn"), ra=2, dec=1),
                        SimpleNamespace(name="Neptune", ra=3, dec=-1),
                    )
                )

        class Plot:
            observer = object()
            ephemeris_name = "test.bsp"

            def __init__(self):
                self.calls = []

            @staticmethod
            def in_bounds(_ra, dec):
                return dec >= 0

            def marker(self, ra, dec, **values):
                self.calls.append((ra, dec, values))

        plot = Plot()
        visible = _plot_colorful_planets(
            plot,
            Planets,
            Style,
            Style,
            Style,
        )

        self.assertEqual(visible, ("mars", "saturn"))
        self.assertEqual(len(plot.calls), 3)
        labels = [call[2].get("label") for call in plot.calls]
        self.assertEqual(labels, ["MARS", None, "SATURN"])
        saturn_ring = plot.calls[1][2]["style"].values["marker"].values
        self.assertEqual(saturn_ring["symbol"], "ellipse")
        self.assertGreater(saturn_ring["size"], _PLANET_VISUALS["saturn"]["size"])
        self.assertTrue(all(call[2]["legend_label"] is None for call in plot.calls))

    def test_full_sky_fit_preserves_both_horizontal_edges(self):
        source = Image.new("RGB", (700, 400), _INK_BLACK)
        source.paste(ImageColor.getrgb(_INK_RED), (0, 0, 60, 400))
        source.paste(ImageColor.getrgb(_INK_BLUE), (640, 0, 700, 400))

        fitted, bounds = _fit_full_sky(source, (400, 300))

        self.assertEqual(fitted.size, (400, 300))
        self.assertEqual(bounds[0], 0)
        self.assertEqual(bounds[2], 400)
        pixels = (
            fitted.get_flattened_data()
            if hasattr(fitted, "get_flattened_data")
            else fitted.getdata()
        )
        colors = set(pixels)
        self.assertIn(ImageColor.getrgb(_INK_RED), colors)
        self.assertIn(ImageColor.getrgb(_INK_BLUE), colors)

    @unittest.skipUnless(importlib.util.find_spec("starplot"), "Starplot is optional")
    def test_colorful_styles_construct_with_supported_starplot_api(self):
        from starplot.styles import LabelStyle, MarkerStyle, ObjectStyle, PlotStyle, extensions

        style = _colorful_plot_style(PlotStyle, extensions)
        self.assertEqual(style.constellation_lines.color.as_hex(), _INK_GREEN)
        for name, visual in _PLANET_VISUALS.items():
            with self.subTest(planet=name):
                marker = MarkerStyle(
                    color=visual["fill"],
                    edge_color=visual["edge"],
                    edge_width=3.5,
                    symbol=visual["symbol"],
                    size=visual["size"],
                    fill="full",
                    zorder=1500,
                )
                ObjectStyle(marker=marker, label=LabelStyle())

    def test_star_art_source_colors_map_to_clean_individual_pigments(self):
        source_colors = (
            _INK_BLACK,
            _INK_WHITE,
            _INK_YELLOW,
            _INK_RED,
            _INK_BLUE,
            _INK_GREEN,
        )
        settings = ConversionSettings(
            dither=True,
            dither_method="floyd-steinberg",
            saturation=0.6,
            blue_bias=0.5,
        )
        for source, expected in zip(source_colors, SPECTRA_PALETTE):
            with self.subTest(source=source):
                converted = convert_to_spectra(
                    Image.new("RGB", (64, 64), ImageColor.getrgb(source)),
                    settings,
                )
                pixels = (
                    converted.get_flattened_data()
                    if hasattr(converted, "get_flattened_data")
                    else converted.getdata()
                )
                self.assertEqual(set(pixels), {expected})

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

    def test_birdweather_provider_uses_regional_settings_without_birdnet_url(self):
        commands = []

        def fake_run(command, **_kwargs):
            commands.append(command)
            output = Path(command[command.index("--out") + 1])
            width = int(command[command.index("--width") + 1])
            height = int(command[command.index("--height") + 1])
            from PIL import Image

            Image.new("RGB", (width * 2, height * 2), "white").save(output)
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            (repo / "frame").mkdir()
            (repo / "frame" / "birdweather.py").write_text("# marker\n")
            context = RenderContext(
                Orientation.LANDSCAPE,
                options={
                    "avian_repo": str(repo),
                    "bird_provider": "birdweather",
                    "bird_postal_code": "84601",
                    "bird_country": "us",
                    "bird_lookback_days": 14,
                    "bird_title": "Avian Visitors",
                    "bird_subtitle": "Nearby Fortnight",
                    "bird_source": "",
                },
            )
            with patch("display_simulator.sources.birds.subprocess.run", side_effect=fake_run):
                source = BirdsSource()
                image = source.render(context)

        self.assertEqual(image.size, (1600, 1200))
        self.assertEqual(source.name, "Birds · nearby BirdWeather reports")
        command = commands[0]
        self.assertEqual(command[command.index("--postal-code") + 1], "84601")
        self.assertEqual(command[command.index("--country") + 1], "us")
        self.assertEqual(command[command.index("--lookback-days") + 1], "14")
        self.assertEqual(command[command.index("--subtitle") + 1], "Nearby Fortnight")
        self.assertNotIn("birdnet.local", command)

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
        with without_external_repositories():
            with self.assertRaisesRegex(RuntimeError, "explicit inkystarmap checkout"):
                StarMapSource().render(context)

    def test_live_weather_missing_integration_is_clear(self):
        context = RenderContext(offline=False)
        with without_external_repositories():
            with self.assertRaisesRegex(RuntimeError, "weather_frame checkout not found"):
                WeatherSource().render(context)
