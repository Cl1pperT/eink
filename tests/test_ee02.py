from __future__ import annotations

import hashlib
import unittest

from PIL import Image

from display_runtime.ee02 import (
    EE02_BUFFER_HEIGHT,
    EE02_BUFFER_WIDTH,
    EE02_COLOR_CODES,
    EE02_NAMED_COLOR_CODES,
    EE02_PAYLOAD_BYTES,
    EE02ColorError,
    EE02DimensionsError,
    LandscapeRotation,
    decode_ee02,
    encode_ee02,
    pack_spectra6_4bpp,
    rotate_to_ee02_buffer,
    unpack_spectra6_4bpp,
)
from display_simulator.pipeline import SPECTRA_PALETTE


BLACK, WHITE, YELLOW, RED, BLUE, GREEN = SPECTRA_PALETTE


class EE02EncoderTests(unittest.TestCase):
    def test_exact_seeed_color_codes(self):
        self.assertEqual(
            EE02_NAMED_COLOR_CODES,
            {"black": 0xF, "white": 0x0, "yellow": 0xB, "red": 0x6, "blue": 0xD, "green": 0x2},
        )
        self.assertEqual(
            [EE02_COLOR_CODES[color] for color in SPECTRA_PALETTE],
            [0xF, 0x0, 0xB, 0x6, 0xD, 0x2],
        )

    def test_golden_nibbles_even_pixel_high_odd_pixel_low(self):
        image = Image.new("RGB", (6, 1))
        image.putdata(SPECTRA_PALETTE)
        self.assertEqual(pack_spectra6_4bpp(image), bytes.fromhex("f0 b6 d2"))

    def test_clockwise_landscape_coordinates_match_seeed_rotation_one(self):
        image = Image.new("RGB", (1600, 1200), WHITE)
        probes = {
            (0, 0): BLACK,
            (1599, 0): RED,
            (0, 1199): BLUE,
            (1599, 1199): GREEN,
            (321, 456): YELLOW,
        }
        for point, color in probes.items():
            image.putpixel(point, color)
        encoded = encode_ee02(image, LandscapeRotation.CLOCKWISE)
        self.assertEqual(encoded.payload_bytes, 960_000)
        self.assertEqual(encoded.payload[599], 0x0F)
        self.assertEqual(encoded.payload[959_999], 0x06)
        self.assertEqual(encoded.payload[0], 0xD0)
        self.assertEqual(encoded.payload[959_400], 0x20)
        self.assertEqual(encoded.payload[192_971], 0x0B)
        self.assertEqual(encoded.rotation, "clockwise")
        self.assertEqual(encoded.seeed_sprite_rotation, 1)

    def test_landscape_rotation_corner_geometry(self):
        image = Image.new("RGB", (1600, 1200), WHITE)
        image.putpixel((0, 0), BLACK)
        image.putpixel((1599, 0), RED)
        image.putpixel((0, 1199), BLUE)
        image.putpixel((1599, 1199), GREEN)

        clockwise, _, seeed_one = rotate_to_ee02_buffer(image, "clockwise")
        self.assertEqual(clockwise.size, (1200, 1600))
        self.assertEqual(clockwise.getpixel((1199, 0)), BLACK)
        self.assertEqual(clockwise.getpixel((1199, 1599)), RED)
        self.assertEqual(clockwise.getpixel((0, 0)), BLUE)
        self.assertEqual(clockwise.getpixel((0, 1599)), GREEN)
        self.assertEqual(seeed_one, 1)

        counter, _, seeed_three = rotate_to_ee02_buffer(image, "counter-clockwise")
        self.assertEqual(counter.getpixel((0, 1599)), BLACK)
        self.assertEqual(counter.getpixel((0, 0)), RED)
        self.assertEqual(counter.getpixel((1199, 1599)), BLUE)
        self.assertEqual(counter.getpixel((1199, 0)), GREEN)
        self.assertEqual(seeed_three, 3)

    def test_landscape_and_portrait_round_trip(self):
        landscape = Image.new("RGB", (1600, 1200), WHITE)
        for index, color in enumerate(SPECTRA_PALETTE):
            landscape.paste(color, (index * 200, index * 150, index * 200 + 137, index * 150 + 111))
        for rotation in LandscapeRotation:
            encoded = encode_ee02(landscape, rotation)
            decoded = decode_ee02(
                encoded.payload,
                logical_orientation="landscape",
                landscape_rotation=rotation,
            )
            self.assertEqual(decoded.tobytes(), landscape.tobytes())

        portrait = landscape.transpose(Image.Transpose.ROTATE_270)
        encoded = encode_ee02(portrait)
        decoded = decode_ee02(encoded.payload, logical_orientation="portrait")
        self.assertEqual(decoded.tobytes(), portrait.tobytes())
        self.assertEqual(encoded.rotation, "none")
        self.assertEqual(encoded.seeed_sprite_rotation, 0)

    def test_full_frame_payload_size_and_hash_goldens(self):
        black = encode_ee02(Image.new("RGB", (1600, 1200), BLACK))
        white = encode_ee02(Image.new("RGB", (1200, 1600), WHITE))
        self.assertEqual(EE02_PAYLOAD_BYTES, EE02_BUFFER_WIDTH * EE02_BUFFER_HEIGHT // 2)
        self.assertEqual(len(black.payload), EE02_PAYLOAD_BYTES)
        self.assertEqual(
            black.sha256,
            "41dd379966e7d1bd11145160760f3ab137aaa4b3517d23c6b8758364660c4b62",
        )
        self.assertEqual(
            white.sha256,
            "b9163d03c43083a18e6101539b555cb5e363eed61fa4b3a3b54f50ae60eb5b52",
        )
        self.assertEqual(black.sha256, hashlib.sha256(bytes([0xFF]) * EE02_PAYLOAD_BYTES).hexdigest())
        self.assertEqual(white.sha256, hashlib.sha256(bytes(EE02_PAYLOAD_BYTES)).hexdigest())

    def test_invalid_colors_dimensions_payloads_and_codes_are_rejected(self):
        with self.assertRaisesRegex(EE02ColorError, r"\(1, 0\)"):
            invalid = Image.new("RGB", (2, 1), WHITE)
            invalid.putpixel((1, 0), (1, 2, 3))
            pack_spectra6_4bpp(invalid)
        with self.assertRaisesRegex(EE02DimensionsError, "even width"):
            pack_spectra6_4bpp(Image.new("RGB", (3, 1), WHITE))
        with self.assertRaisesRegex(EE02DimensionsError, "1600x1200"):
            encode_ee02(Image.new("RGB", (800, 600), WHITE))
        with self.assertRaisesRegex(EE02DimensionsError, "must contain"):
            unpack_spectra6_4bpp(b"\x00", 4, 1)
        with self.assertRaisesRegex(EE02ColorError, "0x1"):
            unpack_spectra6_4bpp(b"\x10", 2, 1)


if __name__ == "__main__":
    unittest.main()
