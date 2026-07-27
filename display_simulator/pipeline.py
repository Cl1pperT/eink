from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass

from PIL import Image, ImageChops, ImageEnhance, ImageOps

from .color_management import convert_to_srgb
from .models import ConversionSettings, FitMode, RenderContext, RenderResult


# These values and the matching math mirror Cl1pperT/AvianVisitors'
# weather_frame/eink.py and Pimoroni's EL133UF1 driver. Driver order is
# black, white, yellow, red, blue, green.
DRIVER_DESATURATED_PALETTE: tuple[tuple[int, int, int], ...] = (
    (0, 0, 0), (255, 255, 255), (255, 255, 0),
    (255, 0, 0), (0, 0, 255), (0, 255, 0),
)
DRIVER_SATURATED_PALETTE: tuple[tuple[int, int, int], ...] = (
    (0, 0, 0), (161, 164, 165), (208, 190, 71),
    (156, 72, 75), (61, 59, 94), (58, 91, 70),
)
SPECTRA_PALETTE: tuple[tuple[int, int, int], ...] = (
    (26, 26, 28), (236, 234, 223), (198, 176, 74),
    (165, 60, 56), (49, 71, 130), (58, 110, 72),
)


def driver_matching_palette(saturation: float) -> tuple[tuple[int, int, int], ...]:
    saturation = float(saturation)
    if not 0 <= saturation <= 1:
        raise ValueError("saturation must be between 0 and 1")
    return tuple(
        tuple(int(sat[channel] * saturation + desat[channel] * (1 - saturation)) for channel in range(3))
        for sat, desat in zip(DRIVER_SATURATED_PALETTE, DRIVER_DESATURATED_PALETTE)
    )


def _palette_image(colors: tuple[tuple[int, int, int], ...]) -> Image.Image:
    palette = Image.new("P", (1, 1))
    flat = [channel for color in colors for channel in color]
    flat.extend(list(colors[0]) * ((768 - len(flat)) // 3))
    palette.putpalette(flat[:768])
    return palette


def _blue_hue_weight(value: int) -> int:
    if 115 <= value <= 195:
        return 255
    if 100 < value < 115:
        return round((value - 100) / 15 * 255)
    if 195 < value < 215:
        return round((215 - value) / 20 * 255)
    return 0


def apply_blue_bias(image: Image.Image, amount: float, saturation: float) -> Image.Image:
    amount = float(amount)
    if not 0 <= amount <= 1:
        raise ValueError("blue_bias must be between 0 and 1")
    rgb = image.convert("RGB")
    if amount == 0:
        return rgb
    hue, colorfulness, _value = rgb.convert("HSV").split()
    hue_mask = hue.point([_blue_hue_weight(value) for value in range(256)])
    color_mask = colorfulness.point([min(255, value * 3) for value in range(256)])
    mask = ImageChops.multiply(hue_mask, color_mask).point([round(value * amount) for value in range(256)])
    return Image.composite(Image.new("RGB", rgb.size, driver_matching_palette(saturation)[4]), rgb, mask)


def normalize_source(image: Image.Image, size: tuple[int, int], fit_mode: FitMode = FitMode.CROP) -> Image.Image:
    image = convert_to_srgb(ImageOps.exif_transpose(image))
    if fit_mode is FitMode.CROP:
        return ImageOps.fit(image, size, method=Image.Resampling.LANCZOS, centering=(0.5, 0.5))
    if fit_mode is FitMode.STRETCH:
        return image.resize(size, Image.Resampling.LANCZOS)
    fitted = ImageOps.contain(image, size, method=Image.Resampling.LANCZOS)
    canvas = Image.new("RGB", size, (246, 242, 228))
    canvas.paste(fitted, ((size[0] - fitted.width) // 2, (size[1] - fitted.height) // 2))
    return canvas


def convert_to_spectra(image: Image.Image, settings: ConversionSettings | None = None) -> Image.Image:
    settings = settings or ConversionSettings()
    rgb = apply_blue_bias(image, settings.blue_bias, settings.saturation)
    palette = _palette_image(driver_matching_palette(settings.saturation))
    dither = Image.Dither.FLOYDSTEINBERG if settings.dither and settings.dither_method == "floyd-steinberg" else Image.Dither.NONE
    indexed = rgb.quantize(palette=palette, dither=dither)
    indexed.putpalette([channel for color in SPECTRA_PALETTE for channel in color])
    return indexed.convert("RGB")


def unsupported_colors(image: Image.Image) -> set[tuple[int, int, int]]:
    rgb = image.convert("RGB")
    pixels = rgb.get_flattened_data() if hasattr(rgb, "get_flattened_data") else rgb.getdata()
    return set(pixels) - set(SPECTRA_PALETTE)


def validate_palette(image: Image.Image) -> bool:
    return not unsupported_colors(image)


def checksum_image(image: Image.Image) -> str:
    rgb = image.convert("RGB")
    digest = hashlib.sha256()
    digest.update(rgb.width.to_bytes(4, "big"))
    digest.update(rgb.height.to_bytes(4, "big"))
    digest.update(rgb.tobytes())
    return digest.hexdigest()


def physical_preview(image: Image.Image) -> Image.Image:
    """Monitor-only approximation; never use this image for native export."""
    result = ImageEnhance.Color(image.convert("RGB")).enhance(0.88)
    warmth = Image.new("RGB", result.size, (250, 241, 216))
    return Image.blend(result, warmth, 0.055)


@dataclass(slots=True)
class ImagePipeline:
    previous_checksum: str | None = None

    def render(self, source, context: RenderContext, settings: ConversionSettings, fit_mode: FitMode) -> RenderResult:
        started = time.perf_counter()
        source_image = source.render(context)
        source_seconds = time.perf_counter() - started
        if source_image.mode != "RGB":
            source_image = source_image.convert("RGB")
        started = time.perf_counter()
        rgb = normalize_source(source_image, context.orientation.dimensions, fit_mode)
        eink = convert_to_spectra(rgb, settings)
        conversion_seconds = time.perf_counter() - started
        if eink.size != context.orientation.dimensions or not validate_palette(eink):
            raise ValueError("Converted output failed native-size or six-colour validation")
        checksum = checksum_image(eink)
        return RenderResult(source.name, rgb, eink, source_seconds, conversion_seconds, checksum, True)

    def accept(self, result: RenderResult) -> RenderResult:
        """Commit only a result the UI accepted as current."""
        result.changed = result.checksum != self.previous_checksum
        self.previous_checksum = result.checksum
        return result
