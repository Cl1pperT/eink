from __future__ import annotations

from pathlib import Path

from PIL import ImageFont


def font(size: int, bold: bool = False) -> ImageFont.ImageFont:
    """Resolve a common macOS/Linux font without requiring bundled assets."""
    names = (
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf" if bold else "/System/Library/Fonts/Supplemental/Arial.ttf",
        "/System/Library/Fonts/Helvetica.ttc",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    )
    for name in names:
        if Path(name).exists():
            try:
                return ImageFont.truetype(name, size)
            except OSError:
                pass
    try:
        return ImageFont.load_default(size=size)
    except TypeError:  # Pillow versions before scalable default fonts
        return ImageFont.load_default()
