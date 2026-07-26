from __future__ import annotations

import importlib
import inspect
import math
import sys
from datetime import datetime
from pathlib import Path
from typing import Mapping
from PIL import Image, ImageDraw

from ..models import RenderContext
from ..repositories import find_repository
from .drawing import font


def _prepare_repository_import(repository: Path) -> None:
    """Make repository changes take effect in a long-lived simulator process."""

    resolved = repository.resolve(strict=False)
    existing = sys.modules.get("weather_frame")
    existing_file = getattr(existing, "__file__", None) if existing is not None else None
    belongs_to_repository = False
    if existing_file:
        try:
            Path(existing_file).resolve(strict=False).relative_to(resolved)
            belongs_to_repository = True
        except (OSError, ValueError):
            pass
    if existing is not None and not belongs_to_repository:
        for name in tuple(sys.modules):
            if name in ("weather_frame", "frame", "season") or name.startswith(
                ("weather_frame.", "frame.", "season.")
            ):
                sys.modules.pop(name, None)

    # AvianVisitors itself supplies weather_frame/frame; its parent commonly
    # supplies the sibling season package. Keep both ahead of earlier checkouts.
    candidates = [str(resolved)]
    if (resolved.parent / "season" / "__init__.py").is_file():
        candidates.append(str(resolved.parent))
    for candidate in reversed(candidates):
        while candidate in sys.path:
            sys.path.remove(candidate)
        sys.path.insert(0, candidate)


class WeatherSource:
    name = "Weather"

    def render(self, context: RenderContext) -> Image.Image:
        repository = find_repository(str(context.options.get("weather_repo", "")), "weather_frame/renderer.py", "WEATHER_FRAME_REPO")
        if repository:
            return self._render_repository(repository, context)
        if not context.offline:
            raise RuntimeError("Cl1pperT/AvianVisitors weather_frame checkout not found. Configure Weather repository or enable fixture weather.")
        return self._demo(context)

    def _render_repository(self, repository, context: RenderContext) -> Image.Image:
        repository = Path(repository)
        _prepare_repository_import(repository)
        try:
            weather = importlib.import_module("weather_frame.weather")
            renderer = importlib.import_module("weather_frame.renderer")
        except Exception as exc:
            raise RuntimeError(f"Could not import weather_frame from {repository}: {exc}") from exc

        if context.offline:
            condition_name = str(context.options.get("weather_condition", "clear")).upper()
            condition = getattr(weather.Condition, condition_name, weather.Condition.CLEAR)
            date = context.when.date()
            forecast = weather.DailyForecast(
                date=date, timezone=str(context.options.get("timezone", "America/Denver")), location_name=context.location,
                latitude=float(context.options.get("latitude", 39.7392)),
                longitude=float(context.options.get("longitude", -104.9903)),
                weather_code=0, condition=condition, high_c=27.0, low_c=14.0,
                precipitation_probability=10, precipitation_mm=0.0, rain_mm=0.0,
                snowfall_cm=0.0, cloud_cover_mean=18.0, wind_speed_max_kmh=18.0,
                wind_gust_max_kmh=28.0, wind_direction_deg=245.0,
                sunrise=datetime.combine(date, datetime.min.time()).replace(hour=6),
                sunset=datetime.combine(date, datetime.min.time()).replace(hour=20),
                precipitation_period=weather.PrecipitationPeriod.NONE,
            )
        else:
            provider = weather.OpenMeteoProvider()
            forecast = provider.fetch_today(
                context.location,
                country_code=str(context.options.get("country_code", "")),
                timeout=float(context.options.get("weather_timeout", 30)),
            )
        activity_names: tuple[str, ...] = ()
        try:
            activity_module = importlib.import_module("weather_frame.activities")
            recommend = activity_module.recommend_activities
            enabled = context.options.get("enabled_activity_ids")
            if isinstance(enabled, str):
                enabled = (enabled,)
            elif enabled is not None and not isinstance(enabled, (list, tuple, set, frozenset)):
                enabled = None
            overrides = context.options.get("activity_overrides")
            if not isinstance(overrides, Mapping):
                overrides = {}
            count = context.options.get("recommendation_count", 5)
            if not isinstance(count, int) or isinstance(count, bool):
                count = 5
            minimum = context.options.get("minimum_suitability", 0.0)
            if isinstance(minimum, bool) or not isinstance(minimum, (int, float)):
                minimum = 0.0
            parameters = inspect.signature(recommend).parameters
            recommendation_kwargs = {"limit": max(0, count)}
            if "enabled_activity_ids" in parameters:
                recommendation_kwargs["enabled_activity_ids"] = enabled
            if "activity_overrides" in parameters:
                recommendation_kwargs["activity_overrides"] = overrides
            if "minimum_suitability" in parameters:
                recommendation_kwargs["minimum_suitability"] = float(minimum)
            activity_names = tuple(recommend(forecast, **recommendation_kwargs))
        except (ImportError, RuntimeError, TypeError, ValueError):
            # Older AvianVisitors checkouts and installs without the sibling
            # season package continue to render the base weather scene.
            activity_names = ()

        render_kwargs = {
            "style": str(context.options.get("weather_style", "woodblock")),
            "caption": bool(context.options.get("weather_caption", False)),
            "units": str(context.options.get("weather_units", "imperial")),
            "scene_source": str(context.options.get("weather_scene_source", "auto")),
            "environment": str(context.options.get("weather_environment", "auto")),
        }
        render_parameters = inspect.signature(renderer.render_forecast).parameters
        if "enabled_environments" in render_parameters:
            render_kwargs["enabled_environments"] = context.options.get("enabled_environments")
        if "activity_names" in render_parameters:
            render_kwargs["activity_names"] = activity_names
        return renderer.render_forecast(forecast, **render_kwargs).convert("RGB")

    def _demo(self, context: RenderContext) -> Image.Image:
        w, h = context.width, context.height
        image = Image.new("RGB", (w, h), "#bcdcf0")
        draw = ImageDraw.Draw(image)
        horizon = int(h * 0.63)
        draw.rectangle((0, horizon, w, h), fill="#71945b")
        draw.ellipse((w * .68, h * .08, w * .86, h * .32), fill="#f2cd34", outline="#d7422c", width=max(3, w // 250))
        for x in range(-100, w + 200, 260):
            y = horizon + int(35 * math.sin(x / 180))
            draw.ellipse((x, y - 100, x + 360, y + 180), fill="#397844")
        font_big = font(max(44, w // 12), bold=True)
        body_font = font(max(20, w // 42))
        draw.rounded_rectangle((w*.06, h*.10, w*.54, h*.51), radius=28, fill="#f8f2df", outline="#26382e", width=5)
        draw.text((w*.10, h*.14), "72°", font=font_big, fill="#1b2736")
        draw.text((w*.10, h*.36), "Clear morning · H 81° / L 58°", font=body_font, fill="#28372f")
        draw.text((w*.06, h*.84), f"{context.location}  ·  {context.when:%A, %B %d · %I:%M %p}", font=body_font, fill="white", stroke_width=2, stroke_fill="#26382e")
        return image
