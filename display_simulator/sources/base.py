from __future__ import annotations

from typing import Protocol

from PIL import Image

from ..models import RenderContext


class FrameSource(Protocol):
    name: str

    def render(self, context: RenderContext) -> Image.Image:
        """Return a full RGB Pillow image before e-ink conversion."""
        ...
