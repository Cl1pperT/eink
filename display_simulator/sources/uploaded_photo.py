from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
from typing import Any, Mapping

from PIL import Image, ImageDraw, ImageOps

from ..models import RenderContext
from .drawing import font


PHOTO_RECIPE_VERSION = 1
DEFAULT_PHOTO_CROP = {
    "center_x": 0.5,
    "center_y": 0.5,
    "zoom": 1.0,
}


def normalized_photo_crop(value: Any) -> dict[str, float]:
    crop = value if isinstance(value, Mapping) else {}

    def number(name: str, default: float) -> float:
        candidate = crop.get(name, default)
        if isinstance(candidate, bool) or not isinstance(candidate, (int, float)):
            return default
        number_value = float(candidate)
        return number_value if math.isfinite(number_value) else default

    return {
        "center_x": min(1.0, max(0.0, number("center_x", 0.5))),
        "center_y": min(1.0, max(0.0, number("center_y", 0.5))),
        "zoom": min(8.0, max(1.0, number("zoom", 1.0))),
    }


def photo_crop_box(
    source_size: tuple[int, int],
    target_size: tuple[int, int],
    crop: Mapping[str, Any] | None,
) -> tuple[int, int, int, int]:
    source_width, source_height = source_size
    target_width, target_height = target_size
    if min(source_width, source_height, target_width, target_height) <= 0:
        raise ValueError("photo and target dimensions must be positive")
    normalized = normalized_photo_crop(crop)
    target_aspect = target_width / target_height
    source_aspect = source_width / source_height
    if source_aspect >= target_aspect:
        base_height = float(source_height)
        base_width = base_height * target_aspect
    else:
        base_width = float(source_width)
        base_height = base_width / target_aspect
    crop_width = max(1.0, base_width / normalized["zoom"])
    crop_height = max(1.0, base_height / normalized["zoom"])
    left = min(
        source_width - crop_width,
        max(0.0, normalized["center_x"] * source_width - crop_width / 2),
    )
    top = min(
        source_height - crop_height,
        max(0.0, normalized["center_y"] * source_height - crop_height / 2),
    )
    right = left + crop_width
    bottom = top + crop_height
    integer_left = max(0, min(source_width - 1, round(left)))
    integer_top = max(0, min(source_height - 1, round(top)))
    integer_right = max(integer_left + 1, min(source_width, round(right)))
    integer_bottom = max(integer_top + 1, min(source_height, round(bottom)))
    return integer_left, integer_top, integer_right, integer_bottom


def crop_photo(
    image: Image.Image,
    target_size: tuple[int, int],
    crop: Mapping[str, Any] | None,
) -> Image.Image:
    box = photo_crop_box(image.size, target_size, crop)
    return image.crop(box).resize(target_size, Image.Resampling.LANCZOS)


def photo_recipe_digest(
    path: Path | str,
    rotation: int,
    caption: str,
    crop: Mapping[str, Any] | None,
    target_size: tuple[int, int] = (1600, 1200),
) -> str:
    source_digest = hashlib.sha256()
    with Path(path).expanduser().open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            source_digest.update(chunk)
    recipe = {
        "version": PHOTO_RECIPE_VERSION,
        "source_sha256": source_digest.hexdigest(),
        "rotation": int(rotation) % 360,
        "caption": str(caption).strip(),
        "crop": normalized_photo_crop(crop),
        "target_size": [int(target_size[0]), int(target_size[1])],
    }
    return hashlib.sha256(
        json.dumps(recipe, separators=(",", ":"), sort_keys=True).encode("utf-8")
    ).hexdigest()


class UploadedPhotoSource:
    name = "Uploaded Photo"

    def render(self, context: RenderContext) -> Image.Image:
        path = Path(str(context.options.get("photo_path", ""))).expanduser()
        if not path.is_file():
            raise FileNotFoundError("Choose a photo before converting it")
        with Image.open(path) as opened:
            image = ImageOps.exif_transpose(opened).convert("RGB")
        rotation = int(context.options.get("rotation", 0)) % 360
        if rotation:
            image = image.rotate(-rotation, expand=True, resample=Image.Resampling.BICUBIC)
        image = crop_photo(
            image,
            context.orientation.dimensions,
            context.options.get("photo_crop"),
        )
        caption = str(context.options.get("caption", "")).strip()
        if caption:
            draw = ImageDraw.Draw(image)
            caption_font = font(max(20, image.width // 35))
            box = draw.textbbox((0, 0), caption, font=caption_font)
            pad = max(10, image.width // 100)
            x, y = image.width//2, image.height - pad
            draw.rounded_rectangle((x-(box[2]-box[0])//2-pad, y-(box[3]-box[1])-pad*2, x+(box[2]-box[0])//2+pad, y), 10, fill=(245, 240, 220))
            draw.text((x, y-pad), caption, font=caption_font, fill=(20, 25, 25), anchor="ms")
        return image
