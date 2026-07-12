"""Reusable control helpers kept separate from the application shell."""
from __future__ import annotations

from tkinter import ttk


def section(parent, title: str) -> ttk.LabelFrame:
    frame = ttk.LabelFrame(parent, text=title, padding=8)
    frame.columnconfigure(1, weight=1)
    return frame
