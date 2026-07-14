from __future__ import annotations

from pathlib import Path
import re
import subprocess
import unittest


REPOSITORY = Path(__file__).resolve().parents[1]
FIRMWARE = REPOSITORY / "firmware" / "esp32-ee02"


class ESP32FirmwareContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.platformio = (FIRMWARE / "platformio.ini").read_text(encoding="utf-8")
        cls.contract = (FIRMWARE / "include/frame_contract.h").read_text(
            encoding="utf-8"
        )
        cls.driver = (FIRMWARE / "include/driver.h").read_text(encoding="utf-8")
        cls.device_config = (FIRMWARE / "include/device_config.h").read_text(
            encoding="utf-8"
        )
        cls.source = (FIRMWARE / "src/main.cpp").read_text(encoding="utf-8")
        cls.workflow = (
            REPOSITORY / ".github/workflows/esp32-firmware.yml"
        ).read_text(encoding="utf-8")

    def test_platform_and_seeed_gfx_are_immutable_and_target_ee02(self):
        self.assertIn("platformio/espressif32@7.0.0", self.platformio)
        self.assertIn("board = seeed_xiao_esp32s3", self.platformio)
        self.assertIn(
            "Seeed_GFX.git#0b13b21f284c9bce3351b394bbd871b688d6aec7",
            self.platformio,
        )
        for text in (self.platformio, self.driver):
            self.assertRegex(text, r"BOARD_SCREEN_COMBO(?:=|\s+)510")
            self.assertIn("USE_XIAO_EPAPER_DISPLAY_BOARD_EE02", text)

    def test_ci_builds_the_exact_production_environment(self):
        self.assertIn("platformio==6.1.19", self.workflow)
        self.assertIn("--project-dir firmware/esp32-ee02", self.workflow)
        self.assertIn("--environment ee02", self.workflow)
        self.assertIn("--target size", self.workflow)

    def test_wire_contract_is_exact(self):
        self.assertIn("kBackingWidth = 1200", self.contract)
        self.assertIn("kBackingHeight = 1600", self.contract)
        self.assertIn("kFrameBytes == 960000", self.contract)
        self.assertIn('"seeed-ee02-t133a01-4bpp-v1"', self.contract)
        self.assertIn('"application/vnd.seeed.ee02-4bpp"', self.contract)
        codes = set(re.findall(r"value == 0x([0-9A-F])", self.contract))
        self.assertEqual(codes, {"0", "2", "6", "B", "D", "F"})

    def test_download_is_authenticated_and_validated_before_refresh(self):
        for required in (
            'http.addHeader("Authorization"',
            'http.addHeader("If-None-Match"',
            'http.header("ETag")',
            'http.header("X-Frame-SHA256")',
            'http.header("X-Content-SHA256")',
            'http.header("X-Frame-Format")',
            "http.getSize() == static_cast<int>(eink::kFrameBytes)",
            "sha256Hex(staging, eink::kFrameBytes)",
            "hasOnlySupportedColors(staging, eink::kFrameBytes)",
        ):
            self.assertIn(required, self.source)

        verify_at = self.source.index("if (actualSha != expectedSha)")
        colors_at = self.source.index("hasOnlySupportedColors(staging")
        invalidate_at = self.source.index(
            "if (!invalidatePersistedDisplayState())", colors_at
        )
        refresh_at = self.source.index("refreshPanel(staging)")
        persist_at = self.source.index("persistDisplayState(mode, actualSha, etag)")
        self.assertLess(verify_at, colors_at)
        self.assertLess(colors_at, invalidate_at)
        self.assertLess(invalidate_at, refresh_at)
        self.assertLess(refresh_at, persist_at)

    def test_power_loss_marker_force_retry_and_bad_304_recovery_are_present(self):
        self.assertIn('kPreferenceValid[] = "state-valid"', self.source)
        self.assertIn("preferences.putBool(kPreferenceValid, false)", self.source)
        self.assertIn("preferences.putBool(kPreferenceValid, true)", self.source)
        self.assertIn("return syncFrame(mode, false, true);", self.source)
        self.assertIn(
            "if (!forceNextDownload || result == SyncResult::kUpdated)",
            self.source,
        )
        self.assertIn("EINK_FORCE_RETRY_INTERVAL_MS", self.source)

    def test_compile_time_guards_cover_psram_and_exact_driver_palette(self):
        self.assertIn("#ifndef BOARD_HAS_PSRAM", self.source)
        for color, code in (
            ("TFT_WHITE", "0x0"),
            ("TFT_GREEN", "0x2"),
            ("TFT_RED", "0x6"),
            ("TFT_YELLOW", "0xB"),
            ("TFT_BLUE", "0xD"),
            ("TFT_BLACK", "0xF"),
        ):
            self.assertIn(f"{color} == {code}", self.source)

    def test_placeholder_password_and_unsafe_http_configuration_are_rejected(self):
        self.assertIn('wifiPassword.startsWith("replace-with-")', self.source)
        self.assertIn("containsHttpUnsafeCharacters(EINK_FRAME_AUTH_TOKEN)", self.source)
        self.assertIn("authority.indexOf('@')", self.source)

    def test_automatic_schedule_matches_pi_boundaries(self):
        self.assertIn('#define EINK_DEFAULT_MODE "automatic"', self.device_config)
        self.assertIn("EINK_WEATHER_START_MINUTES 360", self.device_config)
        self.assertIn("EINK_BIRDS_START_MINUTES 600", self.device_config)
        self.assertIn("EINK_STAR_MAP_START_MINUTES 1200", self.device_config)
        self.assertIn("configTzTime(", self.source)
        self.assertIn("String scheduledMode()", self.source)
        self.assertIn('requestMode("automatic")', self.source)

    def test_verified_bytes_are_copied_without_firmware_rotation(self):
        begin_at = self.source.index("epaper.begin();")
        pointer_at = self.source.index("epaper.getPointer()", begin_at)
        copy_at = self.source.index("memcpy(displayBuffer", pointer_at)
        update_at = self.source.index("epaper.update();", copy_at)
        self.assertLess(begin_at, pointer_at)
        self.assertLess(pointer_at, copy_at)
        self.assertLess(copy_at, update_at)
        self.assertNotIn("epaper.setRotation(", self.source)
        self.assertNotIn("epaper.pushImage(", self.source)

    def test_real_credentials_are_not_part_of_the_project(self):
        ignored = (FIRMWARE / ".gitignore").read_text(encoding="utf-8")
        self.assertIn("include/secrets.h", ignored)
        relative_secret = "firmware/esp32-ee02/include/secrets.h"
        ignored_result = subprocess.run(
            ["git", "check-ignore", "--quiet", relative_secret],
            cwd=REPOSITORY,
            check=False,
        )
        self.assertEqual(ignored_result.returncode, 0)
        tracked_result = subprocess.run(
            ["git", "ls-files", "--error-unmatch", relative_secret],
            cwd=REPOSITORY,
            capture_output=True,
            check=False,
        )
        self.assertNotEqual(tracked_result.returncode, 0)
        example = (FIRMWARE / "include/secrets.example.h").read_text(
            encoding="utf-8"
        )
        self.assertIn("replace-with-pi-frame-server-token", example)
        self.assertIn("replace-with-pi-address", example)
        self.assertNotIn(".local", example)


if __name__ == "__main__":
    unittest.main()
