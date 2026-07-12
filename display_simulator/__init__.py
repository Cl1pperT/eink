"""Desktop simulator for a six-colour Spectra e-paper frame."""

from .models import Orientation, RenderContext, RenderResult
from .pipeline import SPECTRA_PALETTE, checksum_image, validate_palette

__all__ = [
    "Orientation",
    "RenderContext",
    "RenderResult",
    "SPECTRA_PALETTE",
    "checksum_image",
    "validate_palette",
]

__version__ = "0.1.0"
