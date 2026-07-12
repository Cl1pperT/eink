from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import hashlib
from typing import Final

from PIL import Image

from display_simulator.pipeline import SPECTRA_PALETTE


# Seeed_GFX User_Setups/Setup510_Seeed_XIAO_EPaper_13inch3_colorful.h
# configures the EE02/T133A01 sprite in this portrait backing orientation.
EE02_BUFFER_WIDTH: Final = 1200
EE02_BUFFER_HEIGHT: Final = 1600
EE02_PAYLOAD_BYTES: Final = EE02_BUFFER_WIDTH * EE02_BUFFER_HEIGHT // 2
EE02_WIRE_FORMAT: Final = "seeed-ee02-t133a01-4bpp-v1"

# Seeed_GFX TFT_eSPI.h, USE_COLORFULL_EPAPER. These are the actual 4-bit
# driver values, not ordinal palette indices.
EE02_NAMED_COLOR_CODES: Final[dict[str, int]] = {
    "black": 0xF,
    "white": 0x0,
    "yellow": 0xB,
    "red": 0x6,
    "blue": 0xD,
    "green": 0x2,
}
EE02_COLOR_CODES: Final[dict[tuple[int, int, int], int]] = dict(
    zip(SPECTRA_PALETTE, EE02_NAMED_COLOR_CODES.values())
)
EE02_CODE_COLORS: Final[dict[int, tuple[int, int, int]]] = {
    code: color for color, code in EE02_COLOR_CODES.items()
}


class EE02EncodingError(ValueError):
    """Base error for data that cannot be represented by the EE02 buffer."""


class EE02DimensionsError(EE02EncodingError):
    """The image or payload dimensions do not match the hardware contract."""


class EE02ColorError(EE02EncodingError):
    """A pixel or nibble is outside the six-color EE02 palette."""


class LandscapeRotation(str, Enum):
    """Rotation from the logical 1600x1200 image into the 1200x1600 sprite."""

    CLOCKWISE = "clockwise"
    COUNTER_CLOCKWISE = "counter-clockwise"

    @property
    def seeed_sprite_rotation(self) -> int:
        # These exactly mirror TFT_eSprite::drawPixel() for a 4bpp sprite.
        return 1 if self is LandscapeRotation.CLOCKWISE else 3


def parse_landscape_rotation(value: str | LandscapeRotation) -> LandscapeRotation:
    if isinstance(value, LandscapeRotation):
        return value
    normalized = str(value).strip().lower().replace("_", "-")
    aliases = {
        "clockwise": LandscapeRotation.CLOCKWISE,
        "cw": LandscapeRotation.CLOCKWISE,
        "rotation-1": LandscapeRotation.CLOCKWISE,
        "counter-clockwise": LandscapeRotation.COUNTER_CLOCKWISE,
        "counterclockwise": LandscapeRotation.COUNTER_CLOCKWISE,
        "ccw": LandscapeRotation.COUNTER_CLOCKWISE,
        "rotation-3": LandscapeRotation.COUNTER_CLOCKWISE,
    }
    try:
        return aliases[normalized]
    except KeyError as exc:
        raise EE02EncodingError(
            "landscape rotation must be clockwise or counter-clockwise"
        ) from exc


@dataclass(frozen=True, slots=True)
class EncodedEE02Frame:
    payload: bytes
    logical_width: int
    logical_height: int
    buffer_width: int
    buffer_height: int
    rotation: str
    seeed_sprite_rotation: int
    sha256: str

    @property
    def payload_bytes(self) -> int:
        return len(self.payload)


def rotate_to_ee02_buffer(
    image: Image.Image,
    landscape_rotation: str | LandscapeRotation = LandscapeRotation.CLOCKWISE,
) -> tuple[Image.Image, str, int]:
    """Return RGB pixels in Setup510's physical 1200x1600 backing order.

    A portrait source is already in backing order. A landscape source is
    rotated exactly as Seeed_GFX maps logical drawing coordinates for sprite
    rotation 1 (clockwise) or 3 (counter-clockwise).
    """
    rgb = image.convert("RGB")
    if rgb.size == (EE02_BUFFER_WIDTH, EE02_BUFFER_HEIGHT):
        return rgb, "none", 0
    if rgb.size != (EE02_BUFFER_HEIGHT, EE02_BUFFER_WIDTH):
        raise EE02DimensionsError(
            "EE02 input must be 1600x1200 landscape or 1200x1600 portrait; "
            f"got {rgb.width}x{rgb.height}"
        )
    rotation = parse_landscape_rotation(landscape_rotation)
    if rotation is LandscapeRotation.CLOCKWISE:
        # Logical (x, y) -> backing (1199-y, x), matching setRotation(1).
        backing = rgb.transpose(Image.Transpose.ROTATE_270)
    else:
        # Logical (x, y) -> backing (y, 1599-x), matching setRotation(3).
        backing = rgb.transpose(Image.Transpose.ROTATE_90)
    if backing.size != (EE02_BUFFER_WIDTH, EE02_BUFFER_HEIGHT):  # defensive
        raise EE02DimensionsError(f"rotated EE02 buffer has invalid size {backing.size}")
    return backing, rotation.value, rotation.seeed_sprite_rotation


def pack_spectra6_4bpp(image: Image.Image) -> bytes:
    """Pack an even-width six-color RGB image into Seeed's raw 4bpp order.

    Pixels are row-major. Even x occupies bits 7..4 and odd x occupies bits
    3..0, exactly as Seeed_GFX's 4bpp TFT_eSprite implementation.
    """
    rgb = image.convert("RGB")
    width, height = rgb.size
    if width < 1 or height < 1 or width % 2:
        raise EE02DimensionsError("4bpp images must have positive dimensions and an even width")
    raw = rgb.tobytes()
    payload = bytearray(width * height // 2)
    for output_index, offset in enumerate(range(0, len(raw), 6)):
        first = (raw[offset], raw[offset + 1], raw[offset + 2])
        second = (raw[offset + 3], raw[offset + 4], raw[offset + 5])
        try:
            high = EE02_COLOR_CODES[first]
        except KeyError as exc:
            pixel = output_index * 2
            raise EE02ColorError(
                f"unsupported RGB color {first} at ({pixel % width}, {pixel // width})"
            ) from exc
        try:
            low = EE02_COLOR_CODES[second]
        except KeyError as exc:
            pixel = output_index * 2 + 1
            raise EE02ColorError(
                f"unsupported RGB color {second} at ({pixel % width}, {pixel // width})"
            ) from exc
        payload[output_index] = (high << 4) | low
    return bytes(payload)


def unpack_spectra6_4bpp(payload: bytes, width: int, height: int) -> Image.Image:
    """Decode a raw Seeed 4bpp buffer to the runtime's six exact RGB values."""
    if width < 1 or height < 1 or width % 2:
        raise EE02DimensionsError("4bpp images must have positive dimensions and an even width")
    expected = width * height // 2
    if len(payload) != expected:
        raise EE02DimensionsError(
            f"4bpp payload must contain {expected} bytes for {width}x{height}; got {len(payload)}"
        )
    raw = bytearray(width * height * 3)
    for input_index, packed in enumerate(payload):
        for within_byte, code in enumerate((packed >> 4, packed & 0x0F)):
            try:
                color = EE02_CODE_COLORS[code]
            except KeyError as exc:
                pixel = input_index * 2 + within_byte
                raise EE02ColorError(
                    f"unsupported EE02 color code 0x{code:X} at ({pixel % width}, {pixel // width})"
                ) from exc
            offset = (input_index * 2 + within_byte) * 3
            raw[offset:offset + 3] = bytes(color)
    return Image.frombytes("RGB", (width, height), bytes(raw))


def encode_ee02(
    image: Image.Image,
    landscape_rotation: str | LandscapeRotation = LandscapeRotation.CLOCKWISE,
) -> EncodedEE02Frame:
    """Rotate a native Spectra frame and produce the exact raw EE02 buffer."""
    logical_width, logical_height = image.size
    backing, rotation, seeed_rotation = rotate_to_ee02_buffer(image, landscape_rotation)
    payload = pack_spectra6_4bpp(backing)
    if len(payload) != EE02_PAYLOAD_BYTES:  # defensive hardware invariant
        raise EE02DimensionsError(
            f"EE02 payload must contain {EE02_PAYLOAD_BYTES} bytes; got {len(payload)}"
        )
    return EncodedEE02Frame(
        payload=payload,
        logical_width=logical_width,
        logical_height=logical_height,
        buffer_width=backing.width,
        buffer_height=backing.height,
        rotation=rotation,
        seeed_sprite_rotation=seeed_rotation,
        sha256=hashlib.sha256(payload).hexdigest(),
    )


def decode_ee02(
    payload: bytes,
    *,
    logical_orientation: str = "landscape",
    landscape_rotation: str | LandscapeRotation = LandscapeRotation.CLOCKWISE,
) -> Image.Image:
    """Decode a raw EE02 buffer and restore its requested logical orientation."""
    backing = unpack_spectra6_4bpp(payload, EE02_BUFFER_WIDTH, EE02_BUFFER_HEIGHT)
    orientation = logical_orientation.strip().lower()
    if orientation == "portrait":
        return backing
    if orientation != "landscape":
        raise EE02DimensionsError("logical_orientation must be landscape or portrait")
    rotation = parse_landscape_rotation(landscape_rotation)
    if rotation is LandscapeRotation.CLOCKWISE:
        return backing.transpose(Image.Transpose.ROTATE_90)
    return backing.transpose(Image.Transpose.ROTATE_270)
