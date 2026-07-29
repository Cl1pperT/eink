from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from functools import lru_cache
import hashlib
import importlib.util
import math
import os
import random
from pathlib import Path
from typing import Callable, Iterable
from zoneinfo import ZoneInfo
from PIL import Image, ImageColor, ImageDraw, ImageFont, ImageOps

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
_ATLAS_PALETTE = tuple(
    ImageColor.getrgb(color)
    for color in (
        _INK_BLACK,
        _INK_WHITE,
        _INK_YELLOW,
        _INK_RED,
        _INK_BLUE,
        _INK_GREEN,
    )
)

# Logical 1600x1200 layout. The bottom-right reserve belongs to the ESP32,
# which adds its handwritten battery signature only after validating the
# downloaded base frame.
_SKY_BOX = (24, 36, 1152, 1164)
_PANEL_DIVIDER_X = 1176
_PANEL_LEFT = 1210
_PANEL_RIGHT = 1564
_BATTERY_RESERVE = (1460, 1120, 1600, 1200)
_MAP_TRIM_FRACTION = 0.035
_BRIGHT_STAR_MAGNITUDE = 1.8
_STAR_LIMIT_MAGNITUDE = 5.2

_DIRECTION_NAMES = {
    0: "NORTH",
    90: "EAST",
    180: "SOUTH",
    270: "WEST",
}
_DIRECTION_ROTATIONS = {
    0: 180,
    90: 90,
    180: 0,
    270: -90,
}
_CARDINAL_BASE_ANGLES = {
    "N": -90,
    "E": 180,
    "S": 90,
    "W": 0,
}

_PLANET_VISUALS: dict[str, dict[str, object]] = {
    "mercury": {
        "fill": _INK_WHITE,
        "edge": _INK_BLACK,
        "size": 9,
    },
    "venus": {
        "fill": _INK_YELLOW,
        "edge": _INK_WHITE,
        "size": 11,
    },
    "mars": {
        "fill": _INK_RED,
        "edge": _INK_BLACK,
        "size": 10,
    },
    "jupiter": {
        "fill": _INK_YELLOW,
        "edge": _INK_WHITE,
        "size": 15,
    },
    "saturn": {
        "fill": _INK_YELLOW,
        "edge": _INK_WHITE,
        "size": 12,
    },
    "uranus": {
        "fill": _INK_GREEN,
        "edge": _INK_WHITE,
        "size": 10,
    },
    "neptune": {
        "fill": _INK_BLUE,
        "edge": _INK_WHITE,
        "size": 10,
    },
    "pluto": {
        "fill": _INK_WHITE,
        "edge": _INK_WHITE,
        "size": 7,
    },
}

_PLANET_ORDER = {
    name: index
    for index, name in enumerate(
        ("mercury", "venus", "mars", "jupiter", "saturn", "uranus", "neptune", "pluto")
    )
}
_TELESCOPE_WORLDS = frozenset(("uranus", "neptune", "pluto"))
_NAKED_EYE_PRIORITY = {
    "venus": 100,
    "jupiter": 94,
    "saturn": 84,
    "mars": 80,
    "mercury": 68,
}
_FEATURED_CONSTELLATIONS = {
    "ori": 12,
    "uma": 11,
    "cas": 10,
    "cyg": 10,
    "sco": 10,
    "sgr": 9,
    "leo": 9,
    "tau": 9,
    "gem": 8,
    "and": 8,
    "peg": 8,
    "aql": 7,
    "lyr": 7,
    "cma": 7,
    "boo": 7,
    "her": 6,
}


def _stellar_color(star) -> str:
    """Color only the brightest stars from their catalogued B-V index."""
    try:
        magnitude = float(star.magnitude)
    except (AttributeError, TypeError, ValueError):
        return _INK_WHITE
    if not math.isfinite(magnitude) or magnitude > _BRIGHT_STAR_MAGNITUDE:
        return _INK_WHITE
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


def _atlas_star_size(star) -> float:
    """Keep the dense atlas field crisp instead of filling it with large discs."""
    try:
        magnitude = float(star.magnitude)
    except (AttributeError, TypeError, ValueError):
        return 6
    if magnitude <= 0:
        return 160
    if magnitude <= 1:
        return 105
    if magnitude <= 2:
        return 66
    if magnitude <= 3:
        return 32
    if magnitude <= 4:
        return 16
    return 7


def _planet_name(value) -> str:
    return str(getattr(value, "value", value)).strip().casefold()


def _degrees(value) -> float:
    return float(getattr(value, "degrees", value))


def _normalize_direction(value: object) -> int:
    try:
        direction = int(value)
    except (TypeError, ValueError):
        return 180
    if direction in _DIRECTION_NAMES:
        return direction
    return min(
        _DIRECTION_NAMES,
        key=lambda candidate: _angular_distance(candidate, direction),
    )


@dataclass(frozen=True, slots=True)
class ObservingNight:
    rendered_at: datetime
    observation_time: datetime
    sunset: datetime
    sunrise: datetime
    night_date: date


@dataclass(frozen=True, slots=True)
class PlanetPosition:
    name: str
    ra: float
    dec: float
    altitude: float
    azimuth: float


@dataclass(frozen=True, slots=True)
class SkyFeature:
    name: str
    iau_id: str
    altitude: float
    azimuth: float


@dataclass(frozen=True, slots=True)
class MoonDetails:
    name: str
    phase_angle: float
    illumination: float
    altitude: float
    azimuth: float
    ra: float
    dec: float


@dataclass(frozen=True, slots=True)
class GuideTarget:
    name: str
    altitude: float
    azimuth: float
    kind: str


@dataclass(frozen=True, slots=True)
class PlanetariumGuide:
    night: ObservingNight
    direction: int
    planets: tuple[PlanetPosition, ...]
    moon: MoonDetails
    featured: SkyFeature | None
    target: GuideTarget | None


SolarEventsProvider = Callable[[date], dict[str, datetime]]


def _resolve_observing_night(
    rendered_at: datetime,
    latitude: float,
    longitude: float,
    timezone_name: str,
    solar_events: SolarEventsProvider | None = None,
) -> ObservingNight:
    """Resolve the active/upcoming observing night and its sunset + 90 minutes."""
    zone = ZoneInfo(timezone_name)
    local = (
        rendered_at.replace(tzinfo=zone)
        if rendered_at.tzinfo is None or rendered_at.utcoffset() is None
        else rendered_at.astimezone(zone)
    )
    if solar_events is None:
        try:
            from astral import Observer as AstralObserver
            from astral.sun import sun
        except ImportError as exc:  # pragma: no cover - guarded by live preflight
            raise RuntimeError("Planetarium timing requires Astral") from exc
        astral_observer = AstralObserver(latitude=latitude, longitude=longitude)

        def solar_events(day: date) -> dict[str, datetime]:
            return sun(astral_observer, date=day, tzinfo=zone)

    def events(day: date) -> dict[str, datetime]:
        try:
            value = solar_events(day)
            sunset = value["sunset"]
            sunrise = value["sunrise"]
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError(
                f"Could not determine sunrise and sunset for {day.isoformat()}"
            ) from exc
        for event in (sunset, sunrise):
            if (
                not isinstance(event, datetime)
                or event.tzinfo is None
                or event.utcoffset() is None
            ):
                raise RuntimeError("Solar event provider returned a naive timestamp")
        return {
            "sunset": sunset.astimezone(zone),
            "sunrise": sunrise.astimezone(zone),
        }

    today = events(local.date())
    if local < today["sunrise"]:
        previous_day = local.date() - timedelta(days=1)
        sunset = events(previous_day)["sunset"]
        sunrise = today["sunrise"]
    else:
        sunset = today["sunset"]
        sunrise = events(local.date() + timedelta(days=1))["sunrise"]
    return ObservingNight(
        rendered_at=local,
        observation_time=sunset + timedelta(minutes=90),
        sunset=sunset,
        sunrise=sunrise,
        night_date=sunset.date(),
    )


def _angular_distance(first: float, second: float) -> float:
    return abs((float(first) - float(second) + 180.0) % 360.0 - 180.0)


def _azimuth_cardinal(value: float) -> str:
    labels = ("N", "NE", "E", "SE", "S", "SW", "W", "NW")
    return labels[int((float(value) + 22.5) // 45) % len(labels)]


def _format_clock(value: datetime) -> str:
    hour = value.hour % 12 or 12
    suffix = "AM" if value.hour < 12 else "PM"
    return f"{hour}:{value.minute:02d} {suffix}"


def _viewing_instruction(azimuth: float, altitude: float) -> str:
    if altitude >= 75:
        return f"Nearly overhead  ·  {round(altitude)}° high"
    return f"Face {_azimuth_cardinal(azimuth)}  ·  {round(altitude)}° high"


def _select_featured_constellation(
    candidates: Iterable[SkyFeature],
    direction: int,
) -> SkyFeature | None:
    eligible = [
        item
        for item in candidates
        if item.iau_id in _FEATURED_CONSTELLATIONS
        and item.altitude > 8
        and _angular_distance(item.azimuth, direction) <= 110
    ]
    if not eligible:
        return None

    def score(item: SkyFeature) -> tuple[float, float, str]:
        value = (
            item.altitude * 0.65
            - _angular_distance(item.azimuth, direction) * 0.55
            + _FEATURED_CONSTELLATIONS[item.iau_id]
        )
        return value, item.altitude, item.name

    return max(eligible, key=score)


def _select_guide_target(
    planets: Iterable[PlanetPosition],
    featured: SkyFeature | None,
    direction: int,
) -> GuideTarget | None:
    naked_eye = [
        planet
        for planet in planets
        if planet.name in _NAKED_EYE_PRIORITY and planet.altitude >= 8
    ]
    if naked_eye:
        planet = max(
            naked_eye,
            key=lambda item: (
                _NAKED_EYE_PRIORITY[item.name]
                + item.altitude * 0.5
                - _angular_distance(item.azimuth, direction) * 0.2,
                item.altitude,
            ),
        )
        return GuideTarget(
            name=planet.name.title(),
            altitude=planet.altitude,
            azimuth=planet.azimuth,
            kind="planet",
        )
    if featured is not None:
        return GuideTarget(
            name=featured.name,
            altitude=featured.altitude,
            azimuth=featured.azimuth,
            kind="constellation",
        )
    return None


def _atlas_plot_style(PlotStyle, extensions):
    """Build a restrained monochrome atlas style for a six-pigment panel."""
    base = PlotStyle().extend(extensions.GRAYSCALE_DARK, extensions.MAP)
    values = base.model_dump() if hasattr(base, "model_dump") else base.dict()
    values.update(
        {
            "background_color": _INK_BLACK,
            "figure_background_color": _INK_BLACK,
            "text_border_color": _INK_BLACK,
            "border_font_color": _INK_WHITE,
            "border_line_color": _INK_WHITE,
            "border_bg_color": _INK_BLACK,
        }
    )
    values["star"]["marker"].update(
        color=_INK_WHITE,
        edge_color=None,
        edge_width=0,
        symbol="circle",
        alpha=1.0,
    )
    values["star"]["label"].update(
        font_color=_INK_WHITE,
        border_width=1.0,
        border_color=_INK_BLACK,
    )
    values["constellation_lines"].update(
        color=_INK_WHITE,
        width=1.65,
        alpha=0.78,
    )
    values["constellation_labels"].update(
        font_color=_INK_WHITE,
        font_alpha=0.0,
        border_width=0.0,
        border_color=_INK_BLACK,
    )
    values["gridlines"]["line"].update(
        color=_INK_WHITE,
        width=0.9,
        alpha=0.34,
    )
    values["horizon"]["line"].update(
        color=_INK_WHITE,
        width=2.6,
        alpha=1.0,
        edge_width=0,
        edge_color=None,
    )
    values["horizon"]["label"].update(font_color=_INK_WHITE)
    return PlotStyle(**values)


def _colorful_plot_style(PlotStyle, extensions):
    """Backward-compatible name for callers of the former colorful style."""
    return _atlas_plot_style(PlotStyle, extensions)


def _visible_planets(plot, Planet) -> tuple[PlanetPosition, ...]:
    visible: list[PlanetPosition] = []
    for planet in Planet.all(plot.observer, plot.ephemeris_name):
        name = _planet_name(planet.name)
        if name not in _PLANET_VISUALS:
            continue
        try:
            azimuth, altitude = plot.observer._apparent(
                plot.ephemeris[f"{name} barycenter"],
                plot.ephemeris_name,
            )
        except (KeyError, OSError, ValueError):
            continue
        altitude_degrees = _degrees(altitude)
        if altitude_degrees <= 0:
            continue
        visible.append(
            PlanetPosition(
                name=name,
                ra=float(planet.ra),
                dec=float(planet.dec),
                altitude=altitude_degrees,
                azimuth=_degrees(azimuth) % 360,
            )
        )
    return tuple(sorted(visible, key=lambda item: _PLANET_ORDER[item.name]))


def _constellation_features(plot, Constellation, SkyfieldStar) -> tuple[SkyFeature, ...]:
    position = plot.observer.position(plot.ephemeris_name).at(plot.observer.timescale)
    features: list[SkyFeature] = []
    for constellation in Constellation.all():
        if constellation.iau_id not in _FEATURED_CONSTELLATIONS:
            continue
        try:
            apparent = position.observe(
                SkyfieldStar(
                    ra_hours=float(constellation.ra) / 15,
                    dec_degrees=float(constellation.dec),
                )
            ).apparent()
            altitude, azimuth, _distance = apparent.altaz()
        except (OSError, TypeError, ValueError):
            continue
        features.append(
            SkyFeature(
                name=str(constellation.name),
                iau_id=str(constellation.iau_id),
                altitude=float(altitude.degrees),
                azimuth=float(azimuth.degrees) % 360,
            )
        )
    return tuple(features)


def _moon_details(plot, Moon) -> MoonDetails:
    moon = Moon.get(observer=plot.observer, ephemeris=plot.ephemeris_name)
    azimuth, altitude = plot.observer._apparent(
        plot.ephemeris["moon"],
        plot.ephemeris_name,
    )
    return MoonDetails(
        name=str(moon.phase_description),
        phase_angle=float(moon.phase_angle) % 360,
        illumination=max(0.0, min(1.0, float(moon.illumination))),
        altitude=_degrees(altitude),
        azimuth=_degrees(azimuth) % 360,
        ra=float(moon.ra),
        dec=float(moon.dec),
    )


def _build_planetarium_guide(
    night: ObservingNight,
    direction: int,
    planets: tuple[PlanetPosition, ...],
    moon: MoonDetails,
    constellation_features: tuple[SkyFeature, ...],
) -> PlanetariumGuide:
    featured = _select_featured_constellation(constellation_features, direction)
    return PlanetariumGuide(
        night=night,
        direction=direction,
        planets=planets,
        moon=moon,
        featured=featured,
        target=_select_guide_target(planets, featured, direction),
    )


@lru_cache(maxsize=64)
def _load_typeface(path: str, size: int) -> ImageFont.ImageFont:
    try:
        return ImageFont.truetype(path, size)
    except OSError:
        return font(size, bold="Bold" in path or "SemiBold" in path)


def _guide_font(font_root: Path, size: int, kind: str = "regular") -> ImageFont.ImageFont:
    names = {
        "display": "gfs-didot/GFSDidot-Regular.ttf",
        "regular": "inter/Inter-Regular.ttf",
        "semibold": "inter/Inter-SemiBold.ttf",
        "bold": "inter/Inter-Bold.ttf",
    }
    return _load_typeface(str(font_root / names[kind]), size)


def _fit_text(
    draw: ImageDraw.ImageDraw,
    value: object,
    typeface: ImageFont.ImageFont,
    maximum_width: int,
) -> str:
    """Ellipsize one panel line without allowing it beyond its column."""
    text = str(value)
    if draw.textlength(text, font=typeface) <= maximum_width:
        return text
    suffix = "..."
    suffix_width = draw.textlength(suffix, font=typeface)
    if suffix_width >= maximum_width:
        return ""
    low, high = 0, len(text)
    while low < high:
        middle = (low + high + 1) // 2
        candidate = text[:middle].rstrip()
        if draw.textlength(candidate, font=typeface) + suffix_width <= maximum_width:
            low = middle
        else:
            high = middle - 1
    return f"{text[:low].rstrip()}{suffix}"


def _prepare_sky_image(raw: Image.Image, direction: int) -> Image.Image:
    """Crop Starplot's outer axes margin, scale, then rotate the sky artwork."""
    rgb = raw.convert("RGB")
    side = min(rgb.size)
    trim = max(0, round(side * _MAP_TRIM_FRACTION))
    left = (rgb.width - side) // 2 + trim
    top = (rgb.height - side) // 2 + trim
    right = (rgb.width + side) // 2 - trim
    bottom = (rgb.height + side) // 2 - trim
    cropped = rgb.crop((left, top, right, bottom))
    width = _SKY_BOX[2] - _SKY_BOX[0]
    height = _SKY_BOX[3] - _SKY_BOX[1]
    fitted = cropped.resize((width, height), Image.Resampling.LANCZOS)
    return fitted.rotate(
        _DIRECTION_ROTATIONS.get(direction, 0),
        resample=Image.Resampling.BICUBIC,
        expand=False,
        fillcolor=_INK_BLACK,
    )


def _snap_atlas_colors(image: Image.Image) -> Image.Image:
    """Remove colored dither triggers while retaining intentional ink colors.

    Matplotlib and Pillow anti-alias white geometry into neutral grays. Sending
    those grays through the six-pigment converter can produce red, blue, or
    green speckles. Neutral pixels are therefore snapped directly to black or
    white, with a low threshold that keeps the deliberately faint map grid.
    Chromatic pixels are snapped to their nearest calibrated source pigment.
    """
    rgb = image.convert("RGB")
    pixels = (
        rgb.get_flattened_data()
        if hasattr(rgb, "get_flattened_data")
        else rgb.getdata()
    )
    black, white = _ATLAS_PALETTE[:2]
    output: list[tuple[int, int, int]] = []
    for pixel in pixels:
        red, green, blue = pixel
        if max(pixel) - min(pixel) < 28:
            luminance = (299 * red + 587 * green + 114 * blue) // 1000
            output.append(white if luminance >= 64 else black)
            continue
        # A gold line blended against black is perceptually much closer to
        # yellow than green, even when raw Euclidean RGB distance says
        # otherwise. Preserve that hue before applying nearest-palette color.
        if min(red, green) >= blue + 12:
            output.append(_ATLAS_PALETTE[2])
            continue
        output.append(
            min(
                _ATLAS_PALETTE,
                key=lambda candidate: (
                    (red - candidate[0]) ** 2
                    + (green - candidate[1]) ** 2
                    + (blue - candidate[2]) ** 2
                ),
            )
        )
    snapped = Image.new("RGB", rgb.size, black)
    snapped.putdata(output)
    return snapped


def _plot_pixel(plot, ra: float, dec: float, raw_size: tuple[int, int]) -> tuple[float, float]:
    """Convert one Starplot RA/DEC coordinate to the uncropped PNG pixel space."""
    projected = plot._proj.transform_point(ra, dec, plot._crs)
    display_x, display_y = plot.ax.transData.transform(projected)
    figure_width = float(plot.fig.bbox.width)
    figure_height = float(plot.fig.bbox.height)
    return (
        display_x / figure_width * raw_size[0],
        raw_size[1] - display_y / figure_height * raw_size[1],
    )


def _map_point(
    raw_point: tuple[float, float],
    raw_size: tuple[int, int],
    direction: int,
) -> tuple[float, float]:
    """Apply the same crop/scale/rotation used for the sky raster to a point."""
    side = min(raw_size)
    trim = max(0, round(side * _MAP_TRIM_FRACTION))
    crop_left = (raw_size[0] - side) / 2 + trim
    crop_top = (raw_size[1] - side) / 2 + trim
    usable = side - trim * 2
    width = _SKY_BOX[2] - _SKY_BOX[0]
    height = _SKY_BOX[3] - _SKY_BOX[1]
    x = (raw_point[0] - crop_left) / usable * width
    y = (raw_point[1] - crop_top) / usable * height
    center_x = width / 2
    center_y = height / 2
    radians = math.radians(_DIRECTION_ROTATIONS.get(direction, 0))
    dx = x - center_x
    dy = y - center_y
    rotated_x = center_x + math.cos(radians) * dx + math.sin(radians) * dy
    rotated_y = center_y - math.sin(radians) * dx + math.cos(radians) * dy
    return _SKY_BOX[0] + rotated_x, _SKY_BOX[1] + rotated_y


def _keep_inside_sky(
    point: tuple[float, float],
    radius: float,
) -> tuple[int, int]:
    center_x = (_SKY_BOX[0] + _SKY_BOX[2]) / 2
    center_y = (_SKY_BOX[1] + _SKY_BOX[3]) / 2
    dx = point[0] - center_x
    dy = point[1] - center_y
    distance = math.hypot(dx, dy)
    # Keep low-altitude illustrations inside the N/E/S/W label ring. Their
    # panel readout retains the exact bearing and elevation.
    maximum = (_SKY_BOX[2] - _SKY_BOX[0]) / 2 - radius - 51
    if distance > maximum and distance:
        scale = maximum / distance
        dx *= scale
        dy *= scale
    return round(center_x + dx), round(center_y + dy)


def _draw_planet_icon(
    image: Image.Image,
    center: tuple[int, int],
    name: str,
    radius: int | None = None,
) -> None:
    """Draw a tiny, exact-palette planet illustration."""
    visual = _PLANET_VISUALS[name]
    draw = ImageDraw.Draw(image)
    x, y = center
    r = max(4, int(radius or visual["size"]))
    fill = str(visual["fill"])
    edge = str(visual["edge"])

    if name == "saturn":
        draw.ellipse(
            (x - round(r * 1.85), y - round(r * 0.72), x + round(r * 1.85), y + round(r * 0.72)),
            fill=_INK_YELLOW,
            outline=_INK_WHITE,
            width=max(1, r // 5),
        )
        draw.ellipse(
            (x - round(r * 1.38), y - round(r * 0.38), x + round(r * 1.38), y + round(r * 0.38)),
            fill=_INK_BLACK,
        )

    draw.ellipse(
        (x - r, y - r, x + r, y + r),
        fill=fill,
        outline=edge,
        width=max(1, r // 4),
    )

    if name == "mercury":
        dot = max(1, r // 4)
        for dx, dy in ((-r // 3, -r // 4), (r // 3, r // 5), (-r // 5, r // 2)):
            draw.ellipse((x + dx - dot, y + dy - dot, x + dx + dot, y + dy + dot), fill=_INK_BLACK)
    elif name == "venus":
        draw.arc(
            (x - r + 2, y - r + 2, x + r - 2, y + r - 2),
            265,
            95,
            fill=_INK_WHITE,
            width=max(2, r // 3),
        )
    elif name == "mars":
        dot = max(2, r // 3)
        draw.ellipse((x - dot, y - dot // 2, x + dot, y + dot // 2), fill=_INK_BLACK)
        draw.arc((x - r + 2, y - r + 2, x + r - 2, y + r - 2), 20, 155, fill=_INK_BLACK, width=2)
    elif name == "jupiter":
        for offset, color, line_width in (
            (-r // 3, _INK_RED, max(2, r // 5)),
            (0, _INK_WHITE, max(2, r // 6)),
            (r // 3, _INK_RED, max(2, r // 5)),
        ):
            half_width = int(math.sqrt(max(0, r * r - offset * offset))) - 2
            draw.line(
                (x - half_width, y + offset, x + half_width, y + offset),
                fill=color,
                width=line_width,
            )
        spot = max(2, r // 5)
        draw.ellipse((x + r // 4, y + 1, x + r // 4 + spot * 2, y + spot), fill=_INK_RED)
    elif name == "saturn":
        draw.line((x - r + 2, y, x + r - 2, y), fill=_INK_WHITE, width=max(1, r // 5))
    elif name == "uranus":
        draw.line((x - r + 2, y, x + r - 2, y), fill=_INK_WHITE, width=max(1, r // 4))
    elif name == "neptune":
        dot = max(2, r // 4)
        draw.ellipse((x - dot, y - dot, x + dot, y + dot), fill=_INK_WHITE)
    elif name == "pluto":
        dot = max(1, r // 3)
        draw.ellipse((x - dot, y - dot, x + dot, y + dot), fill=_INK_BLACK)


def _draw_moon_icon(
    image: Image.Image,
    center: tuple[int, int],
    radius: int,
    phase_angle: float,
) -> None:
    """Render a projected lunar phase using only black and white."""
    x0, y0 = center
    pixels = image.load()
    white = ImageColor.getrgb(_INK_WHITE)
    black = ImageColor.getrgb(_INK_BLACK)
    phase = math.radians(phase_angle % 360)
    for y in range(-radius, radius + 1):
        for x in range(-radius, radius + 1):
            normalized = (x * x + y * y) / float(radius * radius)
            if normalized > 1:
                continue
            z = math.sqrt(max(0.0, 1.0 - normalized))
            illuminated = (x / radius) * math.sin(phase) - z * math.cos(phase) > 0
            px, py = x0 + x, y0 + y
            if 0 <= px < image.width and 0 <= py < image.height:
                pixels[px, py] = white if illuminated else black
    ImageDraw.Draw(image).ellipse(
        (x0 - radius, y0 - radius, x0 + radius, y0 + radius),
        outline=_INK_WHITE,
        width=max(2, radius // 9),
    )


def _draw_cardinal_labels(
    image: Image.Image,
    direction: int,
    font_root: Path,
) -> None:
    draw = ImageDraw.Draw(image)
    center_x = (_SKY_BOX[0] + _SKY_BOX[2]) / 2
    center_y = (_SKY_BOX[1] + _SKY_BOX[3]) / 2
    radius = (_SKY_BOX[2] - _SKY_BOX[0]) / 2 - 23
    rotation = _DIRECTION_ROTATIONS.get(direction, 0)
    regular = _guide_font(font_root, 22, "semibold")
    selected_font = _guide_font(font_root, 28, "bold")
    selected = _DIRECTION_NAMES.get(direction, "SOUTH")[0]
    for label, base_angle in _CARDINAL_BASE_ANGLES.items():
        angle = math.radians(base_angle - rotation)
        x = round(center_x + math.cos(angle) * radius)
        y = round(center_y + math.sin(angle) * radius)
        draw.ellipse((x - 20, y - 20, x + 20, y + 20), fill=_INK_BLACK)
        draw.text(
            (x, y),
            label,
            font=selected_font if label == selected else regular,
            fill=_INK_WHITE,
            anchor="mm",
        )


def _draw_compass(
    image: Image.Image,
    center: tuple[int, int],
    direction: int,
    font_root: Path,
) -> None:
    draw = ImageDraw.Draw(image)
    x, y = center
    radius = 43
    draw.ellipse((x - radius, y - radius, x + radius, y + radius), outline=_INK_WHITE, width=2)
    draw.line((x, y - radius + 9, x, y + radius - 9), fill=_INK_WHITE, width=1)
    draw.line((x - radius + 9, y, x + radius - 9, y), fill=_INK_WHITE, width=1)
    label_font = _guide_font(font_root, 12, "bold")
    rotation = _DIRECTION_ROTATIONS.get(direction, 0)
    for label, base_angle in _CARDINAL_BASE_ANGLES.items():
        angle = math.radians(base_angle - rotation)
        px = round(x + math.cos(angle) * (radius - 12))
        py = round(y + math.sin(angle) * (radius - 12))
        draw.text((px, py), label, font=label_font, fill=_INK_WHITE, anchor="mm")
    draw.polygon(((x, y + 14), (x - 5, y + 4), (x + 5, y + 4)), fill=_INK_WHITE)


def _draw_planetarium_panel(
    image: Image.Image,
    context: RenderContext,
    guide: PlanetariumGuide,
    font_root: Path,
) -> None:
    draw = ImageDraw.Draw(image)
    display = lambda size: _guide_font(font_root, size, "display")
    regular = lambda size: _guide_font(font_root, size, "regular")
    semibold = lambda size: _guide_font(font_root, size, "semibold")
    bold = lambda size: _guide_font(font_root, size, "bold")

    draw.line((_PANEL_DIVIDER_X, 48, _PANEL_DIVIDER_X, 1152), fill=_INK_WHITE, width=2)
    draw.text((_PANEL_LEFT, 43), "Tonight's Sky", font=display(44), fill=_INK_WHITE)
    location_font = semibold(18)
    location = _fit_text(
        draw,
        context.location,
        location_font,
        _PANEL_RIGHT - _PANEL_LEFT,
    )
    draw.text((_PANEL_LEFT, 98), location, font=location_font, fill=_INK_WHITE)
    date_label = guide.night.night_date.strftime("%a · %b %d").upper()
    draw.text(
        (_PANEL_LEFT, 126),
        f"{date_label}  ·  {_format_clock(guide.night.observation_time)}",
        font=regular(16),
        fill=_INK_WHITE,
    )
    draw.text(
        (_PANEL_LEFT, 151),
        f"FACING {_DIRECTION_NAMES.get(guide.direction, 'SOUTH')}",
        font=bold(15),
        fill=_INK_WHITE,
    )
    draw.line((_PANEL_LEFT, 181, _PANEL_RIGHT, 181), fill=_INK_WHITE, width=1)

    draw.text((_PANEL_LEFT, 198), "MOON & LIGHT", font=bold(13), fill=_INK_WHITE)
    _draw_moon_icon(image, (_PANEL_LEFT + 43, 264), 34, guide.moon.phase_angle)
    draw.text((_PANEL_LEFT + 94, 219), guide.moon.name, font=display(25), fill=_INK_WHITE)
    draw.text(
        (_PANEL_LEFT + 94, 255),
        f"{round(guide.moon.illumination * 100)}% illuminated",
        font=regular(16),
        fill=_INK_WHITE,
    )
    draw.text((_PANEL_LEFT, 317), "SUNSET", font=bold(12), fill=_INK_WHITE)
    draw.text((_PANEL_LEFT, 338), _format_clock(guide.night.sunset), font=semibold(19), fill=_INK_WHITE)
    sunrise_x = _PANEL_LEFT + 182
    draw.text((sunrise_x, 317), "SUNRISE", font=bold(12), fill=_INK_WHITE)
    draw.text((sunrise_x, 338), _format_clock(guide.night.sunrise), font=semibold(19), fill=_INK_WHITE)
    draw.line((_PANEL_LEFT, 379, _PANEL_RIGHT, 379), fill=_INK_WHITE, width=1)

    draw.text((_PANEL_LEFT, 397), "BEST OBJECT TO FIND", font=bold(13), fill=_INK_WHITE)
    if guide.target is None:
        draw.text((_PANEL_LEFT, 426), "Explore overhead", font=display(31), fill=_INK_WHITE)
        instruction = "Use the compass and star colors"
    else:
        draw.text((_PANEL_LEFT, 426), guide.target.name, font=display(32), fill=_INK_WHITE)
        instruction = _viewing_instruction(
            guide.target.azimuth,
            guide.target.altitude,
        )
    draw.text((_PANEL_LEFT, 472), instruction, font=semibold(18), fill=_INK_WHITE)
    draw.text(
        (_PANEL_LEFT, 503),
        "Charted for 90 minutes after sunset",
        font=regular(14),
        fill=_INK_WHITE,
    )
    draw.line((_PANEL_LEFT, 552, _PANEL_RIGHT, 552), fill=_INK_WHITE, width=1)

    draw.text((_PANEL_LEFT, 570), "ABOVE THE HORIZON", font=bold(13), fill=_INK_WHITE)
    if not guide.planets:
        draw.text((_PANEL_LEFT, 612), "No planets above the horizon", font=regular(17), fill=_INK_WHITE)
    else:
        for index, planet in enumerate(guide.planets[:8]):
            column = index % 2
            row = index // 2
            x = _PANEL_LEFT + column * 180
            y = 612 + row * 52
            _draw_planet_icon(image, (x + 13, y + 14), planet.name, radius=10)
            draw.text((x + 32, y), planet.name.upper(), font=semibold(15), fill=_INK_WHITE)
            suffix = " · SCOPE" if planet.name in _TELESCOPE_WORLDS else ""
            draw.text(
                (x + 32, y + 23),
                f"{_azimuth_cardinal(planet.azimuth)} · {round(planet.altitude)}°{suffix}",
                font=regular(11 if suffix else 13),
                fill=_INK_WHITE,
            )
    draw.line((_PANEL_LEFT, 810, _PANEL_RIGHT, 810), fill=_INK_WHITE, width=1)

    draw.text((_PANEL_LEFT, 828), "FEATURED CONSTELLATION", font=bold(13), fill=_INK_WHITE)
    if guide.featured is None:
        draw.text((_PANEL_LEFT, 857), "Seasonal sky", font=display(31), fill=_INK_WHITE)
        featured_detail = "Gold guide unavailable tonight"
    else:
        draw.text((_PANEL_LEFT, 857), guide.featured.name, font=display(31), fill=_INK_WHITE)
        featured_detail = (
            f"Gold lines  ·  {_azimuth_cardinal(guide.featured.azimuth)}"
            f"  ·  {round(guide.featured.altitude)}° high"
        )
    draw.text((_PANEL_LEFT, 900), featured_detail, font=regular(16), fill=_INK_WHITE)
    draw.line((_PANEL_LEFT, 965, _PANEL_RIGHT, 965), fill=_INK_WHITE, width=1)

    draw.text((_PANEL_LEFT, 983), "ORIENTATION", font=bold(12), fill=_INK_WHITE)
    _draw_compass(image, (_PANEL_LEFT + 45, 1045), guide.direction, font_root)
    key_x = _PANEL_LEFT + 112
    draw.text((key_x, 983), "BRIGHT STAR COLOR", font=bold(12), fill=_INK_WHITE)
    key = (
        (_INK_BLUE, "HOT", 1018, 0),
        (_INK_WHITE, "NEUTRAL", 1018, 92),
        (_INK_YELLOW, "SUN-LIKE", 1060, 0),
        (_INK_RED, "COOL", 1060, 112),
    )
    for color, label, y, offset in key:
        x = key_x + offset
        draw.ellipse((x, y, x + 13, y + 13), fill=color, outline=_INK_WHITE, width=1)
        draw.text((x + 19, y + 6), label, font=regular(12), fill=_INK_WHITE, anchor="lm")

    # This is deliberately last: the firmware samples this backing region and
    # draws its device-owned handwritten charge estimate here in white.
    draw.rectangle(_BATTERY_RESERVE, fill=_INK_BLACK)


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
        """Render a full-sky atlas and Pillow guide, intentionally omitting `inky.auto`."""
        if (context.width, context.height) != (1600, 1200):
            raise RuntimeError(
                "The planetarium guide requires the display's landscape "
                "1600x1200 orientation"
            )
        try:
            import matplotlib
            matplotlib.use("Agg", force=True)
            from skyfield.api import Star as SkyfieldStar
            from starplot import Observer, ZenithPlot, _
            from starplot.models import Constellation, Moon, Planet
            from starplot.styles import LineStyle, PlotStyle, extensions
            from starplot.styles import fonts as starplot_fonts
        except ImportError as exc:
            raise RuntimeError("inkystarmap rendering needs the optional Starplot dependencies; use the demo chart or install requirements-integrations.txt") from exc

        latitude = float(context.options.get("latitude", 39.7392))
        longitude = float(context.options.get("longitude", -104.9903))
        timezone_name = str(context.options.get("timezone", "America/Denver"))
        night = _resolve_observing_night(
            context.when,
            latitude,
            longitude,
            timezone_name,
        )
        direction = _normalize_direction(context.options.get("direction", 180))
        # Runtime manifest generation reads this option after rendering. Keep
        # its metadata aligned with the cardinal orientation actually drawn.
        context.options["direction"] = direction
        observer = Observer(
            lat=latitude,
            lon=longitude,
            dt=night.observation_time,
        )
        style = _atlas_plot_style(PlotStyle, extensions)
        plot = ZenithPlot(
            observer=observer,
            style=style,
            resolution=max(context.width, context.height),
            scale=0.55,
        )
        try:
            # ZenithPlot's horizon call establishes the circular clip path.
            # Draw it before every object that must remain inside the visible
            # hemisphere.
            plot.horizon(labels=[])
            plot.gridlines(
                labels=False,
                ra_locations=list(range(0, 360, 15)),
                dec_locations=list(range(-75, 90, 15)),
            )

            planets = _visible_planets(plot, Planet)
            moon = _moon_details(plot, Moon)
            constellation_features = _constellation_features(
                plot,
                Constellation,
                SkyfieldStar,
            )
            guide = _build_planetarium_guide(
                night,
                direction,
                planets,
                moon,
                constellation_features,
            )
            plot.constellations()
            if guide.featured is not None:
                # Starplot 0.20.4's second constellation pass assumes an empty
                # spatial index. We do not draw constellation labels, so
                # resetting that collision-only index is safe and lets the
                # gold overlay use the public filtered-constellation API.
                plot._constellations_rtree = type(plot._constellations_rtree)()
                plot.constellations(
                    where=[_.iau_id == guide.featured.iau_id],
                    style=LineStyle(
                        color=_INK_YELLOW,
                        width=4.8,
                        alpha=1.0,
                        zorder=0,
                    ),
                )
            plot.stars(
                where=[_.magnitude < _STAR_LIMIT_MAGNITUDE],
                where_labels=[False],
                style__marker__symbol="circle",
                size_fn=_atlas_star_size,
                color_fn=_stellar_color,
                legend_label=None,
            )
            plot.fig.canvas.draw()
            rendered = Image.frombuffer(
                "RGBA",
                plot.fig.canvas.get_width_height(),
                plot.fig.canvas.buffer_rgba(),
                "raw",
                "RGBA",
                0,
                1,
            ).convert("RGB")

            canvas = Image.new("RGB", (context.width, context.height), _INK_BLACK)
            canvas.paste(_prepare_sky_image(rendered, direction), _SKY_BOX[:2])

            for planet in guide.planets:
                raw_point = _plot_pixel(plot, planet.ra, planet.dec, rendered.size)
                map_point = _keep_inside_sky(
                    _map_point(raw_point, rendered.size, direction),
                    float(_PLANET_VISUALS[planet.name]["size"]),
                )
                _draw_planet_icon(canvas, map_point, planet.name)
            if guide.moon.altitude > 0:
                raw_point = _plot_pixel(plot, guide.moon.ra, guide.moon.dec, rendered.size)
                map_point = _keep_inside_sky(
                    _map_point(raw_point, rendered.size, direction),
                    10,
                )
                _draw_moon_icon(canvas, map_point, 9, guide.moon.phase_angle)

            _draw_cardinal_labels(canvas, direction, starplot_fonts.FONTS_PATH)
            _draw_planetarium_panel(
                canvas,
                context,
                guide,
                starplot_fonts.FONTS_PATH,
            )
            context.options["star_observation_time"] = night.observation_time.isoformat()
            context.options["star_sunrise_time"] = night.sunrise.isoformat()
            context.options["star_night_date"] = night.night_date.isoformat()
            context.options["star_featured_constellation"] = (
                guide.featured.name if guide.featured is not None else None
            )
            return _snap_atlas_colors(canvas)
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
