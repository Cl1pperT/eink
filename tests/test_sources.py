from contextlib import contextmanager
from datetime import date, datetime, timedelta
import importlib.util
import os
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

from PIL import Image, ImageColor, ImageDraw, ImageFont

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
    _ATLAS_PALETTE,
    _BATTERY_RESERVE,
    _PLANET_VISUALS,
    MoonDetails,
    ObservingNight,
    PlanetPosition,
    PlanetariumGuide,
    SkyFeature,
    _atlas_plot_style,
    _atlas_star_size,
    _angular_distance,
    _colorful_plot_style,
    _draw_planet_icon,
    _draw_planetarium_panel,
    _fit_text,
    _map_point,
    _normalize_direction,
    _prepare_sky_image,
    _resolve_observing_night,
    _select_featured_constellation,
    _snap_atlas_colors,
    _stellar_color,
    _viewing_instruction,
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
                self.assertEqual(
                    _stellar_color(SimpleNamespace(bv=bv, magnitude=1.2)),
                    expected,
                )
        self.assertEqual(
            _stellar_color(SimpleNamespace(bv=-0.2, magnitude=1.81)),
            _INK_WHITE,
        )
        self.assertEqual(
            _stellar_color(SimpleNamespace(bv=-0.2, magnitude=float("nan"))),
            _INK_WHITE,
        )
        self.assertEqual(_stellar_color(SimpleNamespace()), _INK_WHITE)

    def test_planet_visuals_cover_every_supported_world(self):
        expected = {
            "mercury", "venus", "mars", "jupiter",
            "saturn", "uranus", "neptune", "pluto",
        }
        self.assertEqual(set(_PLANET_VISUALS), expected)
        for name, visual in _PLANET_VISUALS.items():
            with self.subTest(planet=name):
                self.assertGreaterEqual(visual["size"], 7)
                self.assertIn(
                    visual["fill"],
                    {_INK_BLACK, _INK_WHITE, _INK_YELLOW, _INK_RED, _INK_BLUE, _INK_GREEN},
                )

    def test_planet_illustrations_include_saturns_ring_and_jupiters_bands(self):
        image = Image.new("RGB", (220, 100), _INK_BLACK)
        _draw_planet_icon(image, (55, 50), "saturn", radius=18)
        _draw_planet_icon(image, (160, 50), "jupiter", radius=18)

        saturn = image.crop((15, 20, 95, 80))
        jupiter = image.crop((135, 25, 185, 75))
        saturn_colors = set(saturn.getdata())
        jupiter_colors = set(jupiter.getdata())
        self.assertIn(ImageColor.getrgb(_INK_YELLOW), saturn_colors)
        self.assertIn(ImageColor.getrgb(_INK_WHITE), saturn_colors)
        self.assertIn(ImageColor.getrgb(_INK_RED), jupiter_colors)
        self.assertIn(ImageColor.getrgb(_INK_YELLOW), jupiter_colors)
        self.assertGreater(saturn.getbbox()[2] - saturn.getbbox()[0], 36)

    def test_full_sky_crop_and_rotation_preserve_native_map_box(self):
        source = Image.new("RGB", (1000, 1000), _INK_BLACK)
        source.paste(ImageColor.getrgb(_INK_RED), (20, 470, 90, 530))
        source.paste(ImageColor.getrgb(_INK_BLUE), (910, 470, 980, 530))

        fitted = _prepare_sky_image(source, 180)

        self.assertEqual(fitted.size, (1128, 1128))
        colors = set(fitted.getdata())
        self.assertIn(ImageColor.getrgb(_INK_RED), colors)
        self.assertIn(ImageColor.getrgb(_INK_BLUE), colors)

    def test_selected_cardinal_is_rotated_to_bottom(self):
        raw_points = {
            0: (500, 50),
            90: (50, 500),
            180: (500, 950),
            270: (950, 500),
        }
        for direction, raw_point in raw_points.items():
            with self.subTest(direction=direction):
                selected = _map_point(raw_point, (1000, 1000), direction)
                others = [
                    _map_point(point, (1000, 1000), direction)
                    for cardinal, point in raw_points.items()
                    if cardinal != direction
                ]
                self.assertGreater(selected[1], max(point[1] for point in others))

    def test_non_cardinal_direction_normalizes_to_nearest_orientation(self):
        self.assertEqual(_normalize_direction(37), 0)
        self.assertEqual(_normalize_direction(88), 90)
        self.assertEqual(_normalize_direction(181), 180)
        self.assertEqual(_normalize_direction(359), 0)
        self.assertEqual(_normalize_direction("invalid"), 180)

    def test_panel_text_is_ellipsized_to_available_width(self):
        draw = ImageDraw.Draw(Image.new("RGB", (200, 50)))
        typeface = ImageFont.load_default()

        fitted = _fit_text(draw, "A very long observing location name", typeface, 90)

        self.assertTrue(fitted.endswith("..."))
        self.assertLessEqual(draw.textlength(fitted, font=typeface), 90)

    def test_live_planetarium_rejects_portrait_instead_of_cropping_panel(self):
        with self.assertRaisesRegex(RuntimeError, "landscape 1600x1200"):
            StarMapSource()._render_inkystarmap(
                RenderContext(Orientation.PORTRAIT)
            )

    @unittest.skipUnless(importlib.util.find_spec("astral"), "Astral is optional")
    def test_observing_night_is_ninety_minutes_after_provo_sunset(self):
        rendered = datetime(2026, 7, 29, 19, 55, tzinfo=ZoneInfo("America/Denver"))

        night = _resolve_observing_night(
            rendered,
            40.2338,
            -111.6585,
            "America/Denver",
        )

        self.assertEqual(night.night_date, date(2026, 7, 29))
        self.assertEqual(night.observation_time - night.sunset, timedelta(minutes=90))
        self.assertEqual((night.sunset.hour, night.sunset.minute), (20, 43))
        self.assertEqual((night.observation_time.hour, night.observation_time.minute), (22, 13))
        self.assertEqual((night.sunrise.hour, night.sunrise.minute), (6, 23))

    def test_predawn_render_uses_previous_evening(self):
        zone = ZoneInfo("America/Denver")

        def events(day):
            return {
                "sunrise": datetime(day.year, day.month, day.day, 6, 15, tzinfo=zone),
                "sunset": datetime(day.year, day.month, day.day, 20, 30, tzinfo=zone),
            }

        night = _resolve_observing_night(
            datetime(2026, 7, 30, 2, 0, tzinfo=zone),
            40.2338,
            -111.6585,
            "America/Denver",
            events,
        )

        self.assertEqual(night.night_date, date(2026, 7, 29))
        self.assertEqual(night.sunset.day, 29)
        self.assertEqual(night.sunrise.day, 30)
        self.assertEqual((night.observation_time.hour, night.observation_time.minute), (22, 0))

    def test_featured_constellation_prefers_visible_direction_and_wraps_north(self):
        candidates = (
            SkyFeature("Cassiopeia", "cas", 35, 359),
            SkyFeature("Cygnus", "cyg", 60, 90),
            SkyFeature("Orion", "ori", -5, 2),
        )

        selected = _select_featured_constellation(candidates, 0)

        self.assertEqual(selected.name, "Cassiopeia")
        self.assertEqual(_angular_distance(359, 1), 2)

    def test_viewing_instruction_uses_overhead_wording_near_zenith(self):
        self.assertEqual(
            _viewing_instruction(225, 83),
            "Nearly overhead  ·  83° high",
        )
        self.assertEqual(
            _viewing_instruction(225, 42),
            "Face SW  ·  42° high",
        )

    def test_planetarium_panel_preserves_firmware_battery_reserve(self):
        zone = ZoneInfo("America/Denver")
        night = ObservingNight(
            rendered_at=datetime(2026, 7, 29, 19, 55, tzinfo=zone),
            observation_time=datetime(2026, 7, 29, 22, 13, tzinfo=zone),
            sunset=datetime(2026, 7, 29, 20, 43, tzinfo=zone),
            sunrise=datetime(2026, 7, 30, 6, 23, tzinfo=zone),
            night_date=date(2026, 7, 29),
        )
        guide = PlanetariumGuide(
            night=night,
            direction=180,
            planets=(
                PlanetPosition("venus", 0, 0, 12, 245),
                PlanetPosition("saturn", 0, 0, 38, 150),
            ),
            moon=MoonDetails("Waxing Gibbous", 130, 0.72, 40, 170, 0, 0),
            featured=SkyFeature("Scorpius", "sco", 24, 184),
            target=None,
        )
        image = Image.new("RGB", (1600, 1200), _INK_BLACK)

        _draw_planetarium_panel(
            image,
            RenderContext(
                Orientation.LANDSCAPE,
                night.rendered_at,
                "Provo, Utah",
            ),
            guide,
            Path("/missing-font-library"),
        )

        reserve = image.crop(_BATTERY_RESERVE)
        self.assertEqual(set(reserve.getdata()), {ImageColor.getrgb(_INK_BLACK)})

    @unittest.skipUnless(importlib.util.find_spec("starplot"), "Starplot is optional")
    def test_atlas_style_constructs_with_supported_starplot_api(self):
        from starplot.styles import PlotStyle, extensions

        style = _atlas_plot_style(PlotStyle, extensions)
        self.assertEqual(
            ImageColor.getrgb(style.background_color.as_hex()),
            ImageColor.getrgb(_INK_BLACK),
        )
        self.assertEqual(
            ImageColor.getrgb(style.constellation_lines.color.as_hex()),
            ImageColor.getrgb(_INK_WHITE),
        )
        self.assertEqual(
            ImageColor.getrgb(style.gridlines.line.color.as_hex()),
            ImageColor.getrgb(_INK_WHITE),
        )
        self.assertEqual(
            ImageColor.getrgb(style.horizon.line.color.as_hex()),
            ImageColor.getrgb(_INK_WHITE),
        )
        self.assertIsNone(style.star.marker.edge_color)
        self.assertEqual(
            ImageColor.getrgb(
                _colorful_plot_style(
                    PlotStyle,
                    extensions,
                ).background_color.as_hex()
            ),
            ImageColor.getrgb(_INK_BLACK),
        )

    def test_atlas_star_sizes_keep_faint_stars_small(self):
        bright = _atlas_star_size(SimpleNamespace(magnitude=-0.2))
        faint = _atlas_star_size(SimpleNamespace(magnitude=5.1))
        self.assertGreater(bright, faint)
        self.assertLessEqual(bright, 160)
        self.assertLessEqual(faint, 7)

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

    def test_atlas_palette_snap_keeps_neutral_geometry_monochrome(self):
        source = Image.new("RGB", (9, 1))
        source.putdata(
            (
                (0, 0, 0),
                (63, 63, 63),
                (64, 64, 64),
                (190, 190, 190),
                (88, 84, 16),
                ImageColor.getrgb(_INK_YELLOW),
                ImageColor.getrgb(_INK_RED),
                ImageColor.getrgb(_INK_BLUE),
                ImageColor.getrgb(_INK_GREEN),
            )
        )

        snapped = _snap_atlas_colors(source)

        self.assertEqual(
            list(snapped.get_flattened_data()),
            [
                ImageColor.getrgb(_INK_BLACK),
                ImageColor.getrgb(_INK_BLACK),
                ImageColor.getrgb(_INK_WHITE),
                ImageColor.getrgb(_INK_WHITE),
                ImageColor.getrgb(_INK_YELLOW),
                ImageColor.getrgb(_INK_YELLOW),
                ImageColor.getrgb(_INK_RED),
                ImageColor.getrgb(_INK_BLUE),
                ImageColor.getrgb(_INK_GREEN),
            ],
        )
        self.assertTrue(set(snapped.get_flattened_data()).issubset(set(_ATLAS_PALETTE)))

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
