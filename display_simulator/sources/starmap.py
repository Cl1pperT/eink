from __future__ import annotations

import hashlib
import importlib.util
import math
import os
import random
import tempfile
from pathlib import Path
from zoneinfo import ZoneInfo
from PIL import Image, ImageDraw, ImageOps

from ..models import RenderContext
from ..repositories import PROJECT_ROOT, find_repository
from .drawing import font

# Star maps are rendered to PNG inside the simulator's worker thread. On macOS,
# Matplotlib otherwise selects TkAgg and tries to create a second Tk GUI from
# that worker, which can segfault the entire simulator. Select the non-GUI
# raster backend before Starplot (and therefore Matplotlib) is imported.
os.environ["MPLBACKEND"] = "Agg"

# The development checkout includes Starplot's large immutable catalogs. Point
# Starplot at them before it is imported so a launch from another directory
# neither misses those files nor downloads duplicate copies there. An explicit
# STARPLOT_DATA_PATH remains authoritative.
_STARPLOT_DATA_FILES = (
    "constellations.0.3.3.parquet",
    "de421.bsp",
    "stars.bigksy.0.1.3.mag11.parquet",
)
if all((PROJECT_ROOT / name).is_file() for name in _STARPLOT_DATA_FILES):
    os.environ.setdefault("STARPLOT_DATA_PATH", str(PROJECT_ROOT))


# These source colors match the shared converter's 0.6-saturation driver
# targets. White intentionally stays full-scale so its tiny low-chroma blue
# component cannot trigger the generated-art blue bias. Large regions therefore
# land on one panel pigment instead of becoming a noisy light/dark dither, while
# the exported e-paper PNG still uses the canonical monitor-facing Spectra RGB
# values.
_INK_BLACK = "#000000"
_INK_WHITE = "#ffffff"
_INK_YELLOW = "#e2d82a"
_INK_RED = "#c32b2d"
_INK_BLUE = "#24239e"
_INK_GREEN = "#229c2a"

_PLANET_VISUALS: dict[str, dict[str, object]] = {
    "mercury": {
        "fill": _INK_WHITE,
        "edge": _INK_BLACK,
        "label": _INK_WHITE,
        "symbol": "circle_dot",
        "size": 48,
    },
    "venus": {
        "fill": _INK_YELLOW,
        "edge": _INK_WHITE,
        "label": _INK_WHITE,
        "symbol": "circle",
        "size": 66,
    },
    "mars": {
        "fill": _INK_RED,
        "edge": _INK_BLACK,
        "label": _INK_WHITE,
        "symbol": "circle_dot",
        "size": 58,
    },
    "jupiter": {
        "fill": _INK_YELLOW,
        "edge": _INK_RED,
        "label": _INK_WHITE,
        "symbol": "circle_dot",
        "size": 94,
    },
    "saturn": {
        "fill": _INK_YELLOW,
        "edge": _INK_WHITE,
        "label": _INK_WHITE,
        "symbol": "circle",
        "size": 72,
        "ring_fill": _INK_YELLOW,
        "ring_edge": _INK_WHITE,
        "ring_size": 132,
    },
    "uranus": {
        "fill": _INK_GREEN,
        "edge": _INK_BLUE,
        "label": _INK_WHITE,
        "symbol": "circle",
        "size": 62,
    },
    "neptune": {
        "fill": _INK_BLUE,
        "edge": _INK_WHITE,
        "label": _INK_WHITE,
        "symbol": "circle_dot",
        "size": 60,
    },
    "pluto": {
        "fill": _INK_WHITE,
        "edge": _INK_RED,
        "label": _INK_WHITE,
        "symbol": "circle_dot",
        "size": 44,
    },
}


def _stellar_color(star) -> str:
    """Approximate a star's visible color from its catalogued B-V index."""
    try:
        bv = float(star.bv)
    except (AttributeError, TypeError, ValueError):
        return _INK_WHITE
    if not math.isfinite(bv):
        return _INK_WHITE
    if bv < 0.25:
        return _INK_BLUE
    if bv < 0.65:
        return _INK_WHITE
    if bv < 1.2:
        return _INK_YELLOW
    return _INK_RED


def _planet_name(value) -> str:
    return str(getattr(value, "value", value)).strip().casefold()


def _fit_full_sky(image: Image.Image, size: tuple[int, int]) -> tuple[Image.Image, tuple[int, int, int, int]]:
    """Contain the full 180° horizon map instead of cropping edge planets."""
    rgb = image.convert("RGB")
    fitted = ImageOps.contain(rgb, size, method=Image.Resampling.LANCZOS)
    left = (size[0] - fitted.width) // 2
    top = (size[1] - fitted.height) // 2
    canvas = Image.new("RGB", size, _INK_BLACK)
    canvas.paste(fitted, (left, top))
    return canvas, (left, top, left + fitted.width, top + fitted.height)


def _decorate_full_sky(
    image: Image.Image,
    map_bounds: tuple[int, int, int, int],
    context: RenderContext,
) -> Image.Image:
    """Turn the contain margins into an intentional title and color key."""
    left, top, right, bottom = map_bounds
    draw = ImageDraw.Draw(image)
    width, height = image.size

    if top >= 72:
        title_font = font(max(26, width // 34), bold=True)
        meta_font = font(max(16, width // 70))
        draw.text(
            (width // 2, max(26, top * 0.34)),
            "TONIGHT'S SKY",
            font=title_font,
            fill=_INK_WHITE,
            anchor="mm",
        )
        draw.text(
            (width // 2, max(58, top * 0.72)),
            f"{context.location}  ·  {context.when:%Y-%m-%d  %H:%M}",
            font=meta_font,
            fill=_INK_YELLOW,
            anchor="mm",
        )

    lower_margin = height - bottom
    if lower_margin >= 72:
        key_font = font(max(15, width // 82), bold=True)
        label_font = font(max(14, width // 88))
        key_y = bottom + lower_margin * 0.58
        center = width // 2
        draw.text(
            (center - 315, key_y),
            "STAR TEMPERATURE",
            font=key_font,
            fill=_INK_WHITE,
            anchor="rm",
        )
        entries = (
            (_INK_BLUE, "HOT"),
            (_INK_WHITE, "NEUTRAL"),
            (_INK_YELLOW, "SUN-LIKE"),
            (_INK_RED, "COOL"),
        )
        x = center - 270
        radius = max(7, width // 145)
        for color, label in entries:
            draw.ellipse(
                (x - radius, key_y - radius, x + radius, key_y + radius),
                fill=color,
                outline=_INK_WHITE,
                width=max(1, radius // 4),
            )
            draw.text(
                (x + radius + 9, key_y),
                label,
                font=label_font,
                fill=_INK_WHITE,
                anchor="lm",
            )
            x += {
                "HOT": 150,
                "NEUTRAL": 180,
                "SUN-LIKE": 210,
                "COOL": 0,
            }[label]

    # A thin gold frame makes the full-map boundary clear without consuming
    # any astronomical field of view.
    draw.rectangle(
        (left, top, max(left, right - 1), max(top, bottom - 1)),
        outline=_INK_YELLOW,
        width=max(2, width // 600),
    )
    return image


def _colorful_plot_style(PlotStyle, extensions):
    """Build a bold style whose major colors survive six-pigment conversion."""
    base = PlotStyle().extend(
        extensions.BLUE_GOLD,
        extensions.MAP,
        extensions.GRADIENT_PRE_DAWN,
    )
    values = base.model_dump() if hasattr(base, "model_dump") else base.dict()
    values.update(
        {
            "background_color": [
                (0.0, _INK_YELLOW),
                (0.018, _INK_YELLOW),
                (0.045, _INK_GREEN),
                (0.085, _INK_GREEN),
                (0.13, _INK_BLUE),
                (0.52, _INK_BLUE),
                (0.68, _INK_BLACK),
                (1.0, _INK_BLACK),
            ],
            "figure_background_color": _INK_BLACK,
            "text_border_color": _INK_BLACK,
            "border_font_color": _INK_WHITE,
            "border_line_color": _INK_YELLOW,
            "border_bg_color": _INK_BLACK,
        }
    )
    values["star"]["marker"].update(
        color=_INK_WHITE,
        edge_color=_INK_BLACK,
        edge_width=1.2,
    )
    values["star"]["label"].update(
        font_color=_INK_WHITE,
        border_width=2.0,
        border_color=_INK_BLACK,
    )
    values["constellation_lines"].update(
        color=_INK_GREEN,
        width=4.2,
        alpha=1.0,
    )
    values["constellation_labels"].update(
        font_color=_INK_YELLOW,
        font_alpha=1.0,
        border_width=2.0,
        border_color=_INK_BLACK,
    )
    values["ecliptic"]["line"].update(
        color=_INK_RED,
        width=4.0,
        alpha=1.0,
    )
    values["ecliptic"]["label"].update(
        font_color=_INK_RED,
        border_color=_INK_BLACK,
    )
    values["moon"]["marker"].update(
        color=_INK_WHITE,
        edge_color=_INK_YELLOW,
        edge_width=2.5,
    )
    values["moon"]["label"].update(
        font_color=_INK_WHITE,
        border_width=2.0,
        border_color=_INK_BLACK,
    )
    values["horizon"]["line"].update(
        color=_INK_BLACK,
        edge_color=_INK_YELLOW,
    )
    values["horizon"]["label"].update(font_color=_INK_WHITE)
    return PlotStyle(**values)


def _plot_colorful_planets(
    plot,
    Planet,
    ObjectStyle,
    MarkerStyle,
    LabelStyle,
) -> tuple[str, ...]:
    """Plot fixed-size, palette-aware planet miniatures at live positions."""
    visible: list[str] = []
    for planet in Planet.all(plot.observer, plot.ephemeris_name):
        name = _planet_name(planet.name)
        visual = _PLANET_VISUALS.get(name)
        if visual is None or not plot.in_bounds(planet.ra, planet.dec):
            continue

        visible.append(name)
        ring_size = visual.get("ring_size")
        if ring_size is not None:
            plot.marker(
                planet.ra,
                planet.dec,
                style=ObjectStyle(
                    marker=MarkerStyle(
                        color=visual["ring_fill"],
                        edge_color=visual["ring_edge"],
                        edge_width=3.5,
                        symbol="ellipse",
                        size=ring_size,
                        fill="full",
                        zorder=1400,
                    ),
                    label=LabelStyle(),
                ),
                legend_label=None,
            )

        plot.marker(
            planet.ra,
            planet.dec,
            style=ObjectStyle(
                marker=MarkerStyle(
                    color=visual["fill"],
                    edge_color=visual["edge"],
                    edge_width=3.5,
                    symbol=visual["symbol"],
                    size=visual["size"],
                    fill="full",
                    zorder=1500,
                ),
                label=LabelStyle(
                    font_size=30,
                    font_weight=700,
                    font_color=visual["label"],
                    border_width=3,
                    border_color=_INK_BLACK,
                    offset_x="auto",
                    offset_y="auto",
                    zorder=2000,
                ),
            ),
            label=name.upper(),
            legend_label=None,
        )
    return tuple(visible)


class StarMapSource:
    name = "Star Map"

    def __init__(self) -> None:
        self.name = "Star Map"

    def render(self, context: RenderContext) -> Image.Image:
        value = str(context.options.get("starmap_source", "")).strip()
        allow_demo_fallback = bool(context.options.get("allow_demo_fallback", True))
        if value:
            path = Path(value).expanduser()
            if path.is_file():
                with Image.open(path) as image:
                    return image.convert("RGB")
            raise FileNotFoundError(f"Star-map image not found: {path}")
        repository = find_repository(str(context.options.get("inkystarmap_repo", "")), "src/inkystarmap/inkystarmap.py", "INKYSTARMAP_REPO")
        if repository and context.options.get("use_inkystarmap", True):
            if importlib.util.find_spec("starplot") is not None:
                image = self._render_inkystarmap(context)
                self.name = "Star Map · live inkystarmap/Starplot render"
                return image
            if not allow_demo_fallback:
                raise RuntimeError(
                    "inkystarmap rendering needs the optional Starplot dependencies; "
                    "install requirements-integrations.txt"
                )
            sample = repository / "inkystarmap2025.jpg"
            if sample.is_file():
                with Image.open(sample) as image:
                    result = image.convert("RGB")
                self.name = "Star Map · inkystarmap repository sample (install Starplot for live sky)"
                return result
            raise RuntimeError(
                "inkystarmap checkout found, but Starplot is not installed and its sample image is missing; "
                "install requirements-integrations.txt"
            )
        if not allow_demo_fallback:
            raise RuntimeError(
                "Live star-map rendering requires an explicit inkystarmap checkout "
                "and the Starplot dependencies"
            )
        self.name = "Star Map · offline Pillow fallback"
        return self._demo(context)

    def _render_inkystarmap(self, context: RenderContext) -> Image.Image:
        """Use inkystarmap's Starplot recipe, intentionally omitting `inky.auto`."""
        try:
            import matplotlib
            matplotlib.use("Agg", force=True)
            from starplot import HorizonPlot, Observer, _
            from starplot.models import Planet
            from starplot.styles import (
                LabelStyle,
                MarkerStyle,
                ObjectStyle,
                PlotStyle,
                extensions,
            )
        except ImportError as exc:
            raise RuntimeError("inkystarmap rendering needs the optional Starplot dependencies; use the demo chart or install requirements-integrations.txt") from exc
        when = context.when
        if when.tzinfo is None:
            when = when.replace(tzinfo=ZoneInfo(str(context.options.get("timezone", "America/Denver"))))
        observer = Observer(lat=float(context.options.get("latitude", 39.7392)),
                            lon=float(context.options.get("longitude", -104.9903)), dt=when)
        direction = int(context.options.get("direction", 180))
        min_azimuth, max_azimuth = direction - 90, direction + 90
        if max_azimuth > 360:
            max_azimuth -= 360
        style = _colorful_plot_style(PlotStyle, extensions)
        plot = HorizonPlot(altitude=(0, 90), azimuth=(min_azimuth, max_azimuth),
                           observer=observer, style=style, resolution=max(context.width, context.height) * 2, scale=0.9)
        plot.constellations()
        plot.stars(
            where=[_.magnitude < 4.6],
            where_labels=[_.magnitude < 2.1],
            style__marker__symbol="star_4",
            color_fn=_stellar_color,
        )
        plot.ecliptic()
        plot.horizon()
        _plot_colorful_planets(
            plot,
            Planet,
            ObjectStyle,
            MarkerStyle,
            LabelStyle,
        )
        plot.moon(true_size=True, show_phase=True)
        try:
            with tempfile.TemporaryDirectory(prefix="inkystarmap-simulator-") as directory:
                output = Path(directory) / "starmap.png"
                plot.export(str(output), transparent=True, padding=0.1)
                with Image.open(output) as opened:
                    rgba = opened.convert("RGBA")
                    background = Image.new("RGBA", rgba.size, (245, 240, 215, 255))
                    rendered = Image.alpha_composite(background, rgba).convert("RGB")
            fitted, map_bounds = _fit_full_sky(
                rendered,
                (context.width, context.height),
            )
            return _decorate_full_sky(fitted, map_bounds, context)
        finally:
            plot.close_fig()

    def _demo(self, context: RenderContext) -> Image.Image:
        w, h = context.width, context.height
        dark = bool(context.options.get("dark_starmap", True))
        bg, fg = (_INK_BLACK, _INK_WHITE) if dark else (_INK_WHITE, _INK_BLACK)
        image = Image.new("RGB", (w, h), bg)
        draw = ImageDraw.Draw(image)
        seed = int.from_bytes(hashlib.sha256(f"{context.location}{context.when:%Y-%m-%d}".encode()).digest()[:8], "big")
        rng = random.Random(seed)
        stars = [(rng.randint(60, w-60), rng.randint(60, int(h*.82)), rng.choice((2, 2, 3, 3, 4, 6))) for _ in range(190)]
        star_colors = (_INK_BLUE, _INK_WHITE, _INK_WHITE, _INK_YELLOW, _INK_RED)
        for index, (x, y, radius) in enumerate(stars):
            color = star_colors[(index + rng.randrange(len(star_colors))) % len(star_colors)]
            draw.ellipse((x-radius, y-radius, x+radius, y+radius), fill=color)
        constellation_colors = (_INK_GREEN, _INK_BLUE, _INK_YELLOW, _INK_RED)
        for start in range(0, 72, 12):
            points = [(x, y) for x, y, _ in stars[start:start+7]]
            draw.line(points, fill=constellation_colors[(start // 12) % len(constellation_colors)], width=3)
        ecliptic = [
            (x, int(h * 0.57 + math.sin(x / w * math.tau) * h * 0.08))
            for x in range(0, w + 1, max(1, w // 80))
        ]
        for index in range(0, len(ecliptic) - 1, 2):
            draw.line((ecliptic[index], ecliptic[index + 1]), fill=_INK_RED, width=3)
        cardinal = font(max(24, w//45), bold=True)
        caption = font(max(18, w//60))
        for text, xy in (("N", (w//2, 25)), ("E", (w-50, h//2)), ("S", (w//2, int(h*.80))), ("W", (25, h//2))):
            draw.text(xy, text, font=cardinal, fill=fg, anchor="mm")
        draw.line((w*.05, h*.87, w*.95, h*.87), fill=_INK_GREEN, width=3)
        draw.text((w//2, h*.92), f"Sky above {context.location} · {context.when:%Y-%m-%d  %H:%M}", font=caption, fill=fg, anchor="mm")
        return image
