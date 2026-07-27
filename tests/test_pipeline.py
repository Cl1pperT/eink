import io
import tempfile
import unittest
from pathlib import Path

from PIL import Image, ImageCms

from display_simulator.color_management import convert_to_srgb
from display_simulator.models import ConversionSettings, FitMode, Orientation, RenderContext
from display_simulator.pipeline import (
    DRIVER_DESATURATED_PALETTE, DRIVER_SATURATED_PALETTE, ImagePipeline,
    SPECTRA_PALETTE, apply_blue_bias, checksum_image, driver_matching_palette,
    normalize_source, validate_palette,
)


class SolidSource:
    name = "solid"
    def render(self, context):
        return Image.new("RGB", (300, 600), (92, 147, 203))


class PipelineTests(unittest.TestCase):
    def test_orientation_dimensions(self):
        self.assertEqual(Orientation.LANDSCAPE.dimensions, (1600, 1200))
        self.assertEqual(Orientation.PORTRAIT.dimensions, (1200, 1600))

    def test_crop_and_fit(self):
        source = Image.new("RGB", (200, 100), "red")
        crop = normalize_source(source, (100, 100), FitMode.CROP)
        fit = normalize_source(source, (100, 100), FitMode.FIT)
        self.assertEqual(crop.size, (100, 100))
        self.assertEqual(fit.size, (100, 100))
        self.assertEqual(fit.getpixel((0, 0)), (246, 242, 228))
        self.assertEqual(fit.getpixel((50, 50)), (255, 0, 0))

    def test_exif_orientation_is_applied(self):
        image = Image.new("RGB", (20, 10), "red")
        exif = image.getexif(); exif[274] = 6
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "oriented.jpg"
            image.save(path, exif=exif)
            with Image.open(path) as opened:
                result = normalize_source(opened, (10, 20), FitMode.STRETCH)
        self.assertEqual(result.size, (10, 20))

    def test_palette_validation(self):
        valid = Image.new("RGB", (2, 2), SPECTRA_PALETTE[2])
        invalid = Image.new("RGB", (2, 2), (1, 2, 3))
        self.assertTrue(validate_palette(valid))
        self.assertFalse(validate_palette(invalid))

    def test_driver_matching_palette_interpolation(self):
        self.assertEqual(driver_matching_palette(0), DRIVER_DESATURATED_PALETTE)
        self.assertEqual(driver_matching_palette(1), DRIVER_SATURATED_PALETTE)
        with self.assertRaises(ValueError):
            driver_matching_palette(1.1)

    def test_zero_blue_bias_does_not_modify_photo_colors(self):
        source = Image.new("RGB", (2, 1))
        source.putdata(((12, 145, 220), (220, 120, 45)))
        self.assertEqual(
            apply_blue_bias(source, amount=0, saturation=0.45).tobytes(),
            source.tobytes(),
        )

    def test_color_management_outputs_opaque_tagged_srgb(self):
        source = Image.new("RGBA", (1, 1), (20, 40, 60, 100))
        profile = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB"))
        source.info["icc_profile"] = profile.tobytes()

        converted = convert_to_srgb(source)

        self.assertEqual(converted.mode, "RGB")
        self.assertEqual(converted.getpixel((0, 0)), (157, 163, 162))
        output_profile = ImageCms.ImageCmsProfile(
            io.BytesIO(converted.info["icc_profile"])
        )
        self.assertIn("sRGB", ImageCms.getProfileName(output_profile))

    def test_checksum_deterministic_and_dimension_sensitive(self):
        first = Image.new("RGB", (10, 10), "white")
        second = first.copy()
        self.assertEqual(checksum_image(first), checksum_image(second))
        self.assertNotEqual(checksum_image(first), checksum_image(Image.new("RGB", (20, 5), "white")))

    def test_output_stays_native_and_refresh_state(self):
        pipeline = ImagePipeline()
        context = RenderContext(orientation=Orientation.PORTRAIT)
        settings = ConversionSettings(dither=False)
        one = pipeline.accept(pipeline.render(SolidSource(), context, settings, FitMode.FIT))
        two = pipeline.accept(pipeline.render(SolidSource(), context, settings, FitMode.FIT))
        self.assertEqual(one.eink_image.size, (1200, 1600))
        self.assertTrue(validate_palette(one.eink_image))
        self.assertTrue(one.changed)
        self.assertFalse(two.changed)
