from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any

from PIL import Image


class Orientation(str, Enum):
    LANDSCAPE = "landscape"
    PORTRAIT = "portrait"

    @property
    def dimensions(self) -> tuple[int, int]:
        return (1600, 1200) if self is Orientation.LANDSCAPE else (1200, 1600)


class FitMode(str, Enum):
    CROP = "Crop to fill"
    FIT = "Fit with border"
    STRETCH = "Stretch (not recommended)"


@dataclass(slots=True)
class ConversionSettings:
    dither: bool = True
    dither_method: str = "floyd-steinberg"
    saturation: float = 0.6
    blue_bias: float = 0.5


@dataclass(slots=True)
class RenderContext:
    orientation: Orientation = Orientation.LANDSCAPE
    when: datetime = field(default_factory=datetime.now)
    location: str = "Denver, Colorado"
    config_path: Path | None = None
    options: dict[str, Any] = field(default_factory=dict)
    offline: bool = True

    @property
    def width(self) -> int:
        return self.orientation.dimensions[0]

    @property
    def height(self) -> int:
        return self.orientation.dimensions[1]


@dataclass(slots=True)
class RenderResult:
    source_name: str
    rgb_image: Image.Image
    eink_image: Image.Image
    source_seconds: float
    conversion_seconds: float
    checksum: str
    changed: bool = True

    @property
    def dimensions(self) -> tuple[int, int]:
        return self.eink_image.size
