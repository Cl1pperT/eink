from __future__ import annotations

import hashlib
import importlib.util
import os
import random
import tempfile
from pathlib import Path
from zoneinfo import ZoneInfo
from PIL import Image, ImageDraw

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
            from starplot.styles import PlotStyle, extensions
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
        style = PlotStyle().extend(extensions.BLUE_GOLD, extensions.MAP, extensions.GRADIENT_PRE_DAWN)
        plot = HorizonPlot(altitude=(0, 90), azimuth=(min_azimuth, max_azimuth),
                           observer=observer, style=style, resolution=max(context.width, context.height) * 2, scale=0.9)
        plot.constellations()
        plot.stars(where=[_.magnitude < 4.6], where_labels=[_.magnitude < 2.1], style__marker__symbol="star_4")
        plot.horizon(); plot.planets(); plot.moon(true_size=True, show_phase=True)
        with tempfile.TemporaryDirectory(prefix="inkystarmap-simulator-") as directory:
            output = Path(directory) / "starmap.png"
            plot.export(str(output), transparent=True, padding=0.1)
            with Image.open(output) as opened:
                rgba = opened.convert("RGBA")
                background = Image.new("RGBA", rgba.size, (245, 240, 215, 255))
                return Image.alpha_composite(background, rgba).convert("RGB")

    def _demo(self, context: RenderContext) -> Image.Image:
        w, h = context.width, context.height
        dark = bool(context.options.get("dark_starmap", True))
        bg, fg, line = ((10, 18, 38), (245, 240, 215), (50, 90, 150)) if dark else ((245, 240, 215), (15, 25, 45), (80, 115, 165))
        image = Image.new("RGB", (w, h), bg)
        draw = ImageDraw.Draw(image)
        seed = int.from_bytes(hashlib.sha256(f"{context.location}{context.when:%Y-%m-%d}".encode()).digest()[:8], "big")
        rng = random.Random(seed)
        stars = [(rng.randint(60, w-60), rng.randint(60, int(h*.82)), rng.choice((2, 2, 3, 3, 4, 6))) for _ in range(190)]
        for x, y, radius in stars:
            draw.ellipse((x-radius, y-radius, x+radius, y+radius), fill=fg)
        for start in range(0, 72, 12):
            points = [(x, y) for x, y, _ in stars[start:start+7]]
            draw.line(points, fill=line, width=2)
        cardinal = font(max(24, w//45), bold=True)
        caption = font(max(18, w//60))
        for text, xy in (("N", (w//2, 25)), ("E", (w-50, h//2)), ("S", (w//2, int(h*.80))), ("W", (25, h//2))):
            draw.text(xy, text, font=cardinal, fill=fg, anchor="mm")
        draw.line((w*.05, h*.87, w*.95, h*.87), fill=line, width=2)
        draw.text((w//2, h*.92), f"Sky above {context.location} · {context.when:%Y-%m-%d  %H:%M}", font=caption, fill=fg, anchor="mm")
        return image
