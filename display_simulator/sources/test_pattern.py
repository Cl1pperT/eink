from __future__ import annotations

from PIL import Image, ImageDraw

from ..models import RenderContext
from ..pipeline import SPECTRA_PALETTE
from .drawing import font


class TestPatternSource:
    name = "Test Pattern"

    def render(self, context: RenderContext) -> Image.Image:
        w, h = context.width, context.height
        image = Image.new("RGB", (w, h), "white")
        draw = ImageDraw.Draw(image)
        title = font(max(28, w // 35), bold=True)
        small = font(max(13, w // 90))
        draw.text((30, 20), "SPECTRA 6 DISPLAY TEST", font=title, fill="black")
        sw = (w - 60) // 6
        names = ("BLACK", "WHITE", "YELLOW", "RED", "BLUE", "GREEN")
        for i, (color, name) in enumerate(zip(SPECTRA_PALETTE, names)):
            x = 30 + i*sw
            draw.rectangle((x, 80, x+sw, 190), fill=color, outline="black", width=2)
            ink = "white" if i in (0, 4, 5) else "black"
            draw.text((x+sw//2, 135), name, font=small, fill=ink, anchor="mm")
        for x in range(w-60):
            value = round(255*x/max(1, w-61))
            draw.line((30+x, 220, 30+x, 275), fill=(value, value, value))
            draw.line((30+x, 290, 30+x, 345), fill=(value, 100, 255-value))
        draw.rectangle((30, 375, w//2-15, int(h*.70)), fill="#68add4")
        draw.ellipse((w*.12, h*.38, w*.32, h*.61), fill="#e8c632", outline="#d1322f", width=10)
        draw.rectangle((w//2+15, 375, w-30, int(h*.70)), fill="#3e8048")
        for size, y in ((12, int(h*.72)), (16, int(h*.75)), (22, int(h*.79)), (30, int(h*.84))):
            text_font = font(size)
            draw.text((35, y), f"Fine black text at {size}px — The quick brown fox 0123456789", font=text_font, fill="black")
        draw.rectangle((w*.58, h*.73, w*.96, h*.88), fill="black")
        draw.text((w*.77, h*.805), "WHITE TEXT ON BLACK", font=title, fill="white", anchor="mm")
        for offset in range(8):
            x = 30 + offset*22
            draw.line((x, h-120, x, h-30), fill="black", width=1)
            draw.line((w-220+offset*22, h-120, w-180+offset*22, h-30), fill="black", width=1)
            draw.ellipse((w*.40+offset*28, h-105, w*.40+offset*28+8+offset, h-97+offset), outline="black", width=1)
        settings = context.options.get("settings_label", "")
        draw.text((w//2, h-18), settings, font=small, fill="black", anchor="ms")
        return image
