from __future__ import annotations

from pathlib import Path
from PIL import Image, ImageDraw, ImageOps

from ..models import RenderContext
from .drawing import font


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
