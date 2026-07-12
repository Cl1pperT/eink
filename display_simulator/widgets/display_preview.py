from __future__ import annotations

import tkinter as tk
from tkinter import ttk

from PIL import Image, ImageDraw, ImageTk

from ..pipeline import physical_preview


class DisplayPreview(ttk.Frame):
    def __init__(self, master, **kwargs):
        super().__init__(master, **kwargs)
        self.canvas = tk.Canvas(self, background="#202225", highlightthickness=0)
        self.canvas.pack(fill="both", expand=True)
        self._native: Image.Image | None = None
        self._photo: ImageTk.PhotoImage | None = None
        self._physical = False
        self._pending: str | None = None
        self.canvas.bind("<Configure>", self._schedule_draw)

    def set_image(self, image: Image.Image | None, physical: bool = False) -> None:
        self._native = image.copy() if image else None
        self._physical = physical
        self._schedule_draw()

    def _schedule_draw(self, _event=None) -> None:
        if self._pending:
            self.after_cancel(self._pending)
        self._pending = self.after(40, self._draw)

    def _placeholder(self, size: tuple[int, int]) -> Image.Image:
        image = Image.new("RGB", size, "#d8d5cc")
        draw = ImageDraw.Draw(image)
        tile = max(12, min(size)//24)
        for y in range(0, size[1], tile):
            for x in range(0, size[0], tile):
                if (x//tile + y//tile) % 2:
                    draw.rectangle((x, y, x+tile, y+tile), fill="#c8c5bd")
        return image

    def _draw(self) -> None:
        self._pending = None
        cw, ch = max(10, self.canvas.winfo_width()), max(10, self.canvas.winfo_height())
        self.canvas.delete("all")
        source = self._native
        aspect = source.width/source.height if source else 4/3
        bezel, substrate = 18, 7
        available_w, available_h = max(1, cw-2*(bezel+substrate+20)), max(1, ch-2*(bezel+substrate+20))
        if available_w/available_h > aspect:
            ih, iw = available_h, round(available_h*aspect)
        else:
            iw, ih = available_w, round(available_w/aspect)
        x0, y0 = (cw-iw)//2, (ch-ih)//2
        self.canvas.create_rectangle(x0-bezel-substrate, y0-bezel-substrate, x0+iw+bezel+substrate, y0+ih+bezel+substrate, fill="#111315", outline="#050505", width=2)
        self.canvas.create_rectangle(x0-substrate, y0-substrate, x0+iw+substrate, y0+ih+substrate, fill="#eee9dc", outline="")
        image = source if source else self._placeholder((max(1, iw), max(1, ih)))
        if source and self._physical:
            image = physical_preview(image)
        image = image.resize((max(1, iw), max(1, ih)), Image.Resampling.LANCZOS)
        self._photo = ImageTk.PhotoImage(image)
        self.canvas.create_image(x0, y0, image=self._photo, anchor="nw")
