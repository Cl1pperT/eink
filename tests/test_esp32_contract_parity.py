from __future__ import annotations

from pathlib import Path
import re
import tomllib
import unittest

from display_runtime.ee02 import (
    EE02_BUFFER_HEIGHT,
    EE02_BUFFER_WIDTH,
    EE02_NAMED_COLOR_CODES,
    EE02_PAYLOAD_BYTES,
    EE02_WIRE_FORMAT,
)
from display_runtime.frame_server import (
    CONCRETE_MODES,
    FRAME_CONTENT_TYPE,
    frame_etag,
)


REPOSITORY = Path(__file__).resolve().parents[1]
FIRMWARE = REPOSITORY / "firmware" / "esp32-ee02"


def _minutes(value: str) -> int:
    hour, minute = (int(part) for part in value.split(":"))
    return hour * 60 + minute


class ESP32ContractParityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.contract = (FIRMWARE / "include/frame_contract.h").read_text(
            encoding="utf-8"
        )
        cls.device_config = (FIRMWARE / "include/device_config.h").read_text(
            encoding="utf-8"
        )
        cls.source = (FIRMWARE / "src/main.cpp").read_text(encoding="utf-8")
        with (REPOSITORY / "display_runtime/defaults.toml").open("rb") as handle:
            cls.defaults = tomllib.load(handle)

    def test_dimensions_wire_identifiers_and_payload_size_match_python(self):
        width = int(re.search(r"kBackingWidth\s*=\s*(\d+)", self.contract).group(1))
        height = int(re.search(r"kBackingHeight\s*=\s*(\d+)", self.contract).group(1))
        payload = int(re.search(r"kFrameBytes\s*==\s*(\d+)", self.contract).group(1))
        wire_format = re.search(r'kWireFormat\[\]\s*=\s*"([^"]+)"', self.contract).group(1)
        content_type = re.search(r'kContentType\[\]\s*=\s*"([^"]+)"', self.contract).group(1)

        self.assertEqual((width, height), (EE02_BUFFER_WIDTH, EE02_BUFFER_HEIGHT))
        self.assertEqual(payload, EE02_PAYLOAD_BYTES)
        self.assertEqual(wire_format, EE02_WIRE_FORMAT)
        self.assertEqual(content_type, FRAME_CONTENT_TYPE)

    def test_modes_and_palette_match_server_and_encoder(self):
        modes = set(re.findall(r'mode\s*==\s*"([^"]+)"', self.contract))
        codes = {int(value, 16) for value in re.findall(r'value\s*==\s*0x([0-9A-Fa-f]+)', self.contract)}
        self.assertEqual(modes, set(CONCRETE_MODES))
        self.assertEqual(codes, set(EE02_NAMED_COLOR_CODES.values()))

    def test_button_updater_uses_a_concrete_server_mode(self):
        mode = re.search(
            r'#define\s+EINK_FRAME_MODE\s+"([^"]+)"', self.device_config
        )
        self.assertIsNotNone(mode)
        self.assertIn(mode.group(1), CONCRETE_MODES)

    def test_etag_shape_matches_server(self):
        digest = "a" * 64
        self.assertEqual(frame_etag(digest), f'"sha256-{digest}"')
        self.assertIn('String("\\"sha256-") + sha + "\\""', self.source)


if __name__ == "__main__":
    unittest.main()
