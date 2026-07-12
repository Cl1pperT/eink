from __future__ import annotations

from tkinter import StringVar, ttk


class StatusPanel(ttk.LabelFrame):
    def __init__(self, master, **kwargs):
        super().__init__(master, text="Frame diagnostics", padding=8, **kwargs)
        self.value = StringVar(value="No frame generated")
        ttk.Label(self, textvariable=self.value, justify="left", wraplength=360).pack(fill="x")

    def set(self, text: str) -> None:
        self.value.set(text)
