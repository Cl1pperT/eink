from __future__ import annotations

import io

from PIL import Image, ImageCms


PAPER_COLOR = (246, 242, 228)
_SRGB_PROFILE = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB"))
_SRGB_PROFILE_BYTES = _SRGB_PROFILE.tobytes()


def convert_to_srgb(image: Image.Image) -> Image.Image:
    """Return an opaque, explicitly tagged sRGB image.

    Phone photos commonly carry Display-P3 profiles. Pillow's ordinary
    ``convert("RGB")`` changes the storage mode but does not transform those
    color values into sRGB, which leaves later palette matching with the wrong
    interpretation. Invalid or absent profiles safely fall back to sRGB.
    """

    profile_data = image.info.get("icc_profile")
    alpha: Image.Image | None = None
    if "A" in image.getbands() or "transparency" in image.info:
        rgba = image.convert("RGBA")
        alpha = rgba.getchannel("A")
        source = rgba.convert("RGB")
    else:
        source = image

    converted: Image.Image
    if profile_data:
        try:
            source_profile = ImageCms.ImageCmsProfile(io.BytesIO(profile_data))
            converted = ImageCms.profileToProfile(
                source,
                source_profile,
                _SRGB_PROFILE,
                outputMode="RGB",
            )
        except (ImageCms.PyCMSError, OSError, TypeError, ValueError):
            converted = source.convert("RGB")
    else:
        converted = source.convert("RGB")

    if alpha is not None:
        paper = Image.new("RGB", converted.size, PAPER_COLOR)
        paper.paste(converted, mask=alpha)
        converted = paper

    converted.info["icc_profile"] = _SRGB_PROFILE_BYTES
    return converted
