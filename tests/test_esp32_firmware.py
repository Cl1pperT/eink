from __future__ import annotations

from pathlib import Path
import re
import shutil
import subprocess
import tempfile
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
        cls.battery = (FIRMWARE / "include/battery_monitor.h").read_text(
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

    def test_power_loss_marker_and_bad_304_recovery_are_present(self):
        self.assertIn('kPreferenceValid[] = "state-valid"', self.source)
        self.assertIn("preferences.putBool(kPreferenceValid, false)", self.source)
        self.assertIn("preferences.putBool(kPreferenceValid, true)", self.source)
        self.assertIn("return syncFrame(mode, true);", self.source)

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

    def test_local_server_names_are_resolved_with_mdns(self):
        self.assertIn("#include <ESPmDNS.h>", self.source)
        self.assertIn('hostname.endsWith(".local")', self.source)
        self.assertIn("MDNS.queryHost(mdnsName, 5000)", self.source)

    def test_timer_or_mode_button_wakes_one_check_then_deep_sleep(self):
        self.assertIn('#define EINK_FRAME_MODE "active"', self.device_config)
        self.assertIn('mode == "active"', self.contract)
        self.assertIn("isFrameChannel", self.source)
        self.assertIn(
            "#define EINK_CHECK_INTERVAL_SECONDS 300ULL", self.device_config
        )
        for pin in ("GPIO_NUM_2", "GPIO_NUM_3", "GPIO_NUM_5"):
            self.assertIn(pin, self.source)
        for constant, mode in (
            ("kButton1FrameMode", "weather"),
            ("kButton2FrameMode", "birds"),
            ("kButton3FrameMode", "star-map"),
        ):
            declaration = f'constexpr char {constant}[] = "{mode}";'
            self.assertIn(declaration, self.source)
        self.assertIn("ESP_EXT1_WAKEUP_ANY_LOW", self.source)
        self.assertIn("esp_sleep_get_ext1_wakeup_status()", self.source)
        self.assertIn("frameModeForButtonWake(wakeStatus)", self.source)
        self.assertIn(
            "esp_sleep_enable_timer_wakeup(timerWakeSeconds * 1000000ULL)",
            self.source,
        )
        self.assertIn(
            "const char *requestedFrameMode = EINK_FRAME_MODE;", self.source
        )
        self.assertIn("syncFrame(requestedFrameMode);", self.source)
        self.assertIn("esp_deep_sleep_start();", self.source)
        self.assertNotIn("EINK_POLL_INTERVAL_MS", self.source)

    def test_verified_base_is_copied_then_only_battery_mark_is_drawn(self):
        pointer_at = self.source.index(
            "auto *displayBuffer = static_cast<uint8_t *>(epaper.getPointer())"
        )
        copy_at = self.source.index("memcpy(displayBuffer", pointer_at)
        mark_at = self.source.index("applyBatteryMark(displayBuffer)", copy_at)
        palette_at = self.source.index(
            "hasOnlySupportedColors(displayBuffer", mark_at
        )
        begin_at = self.source.index("epaper.begin();", palette_at)
        update_at = self.source.index("epaper.update();", copy_at)
        self.assertLess(pointer_at, copy_at)
        self.assertLess(copy_at, mark_at)
        self.assertLess(mark_at, palette_at)
        self.assertLess(palette_at, begin_at)
        self.assertLess(begin_at, update_at)
        self.assertLess(palette_at, update_at)
        self.assertLess(copy_at, update_at)
        self.assertIn("epaper.setRotation(1)", self.source)
        self.assertIn("epaper.setRotation(0)", self.source)
        self.assertEqual(self.source.count("epaper.drawString("), 1)
        self.assertNotIn("epaper.pushImage(", self.source)

    def test_ee02_gated_battery_adc_is_sampled_once_per_local_day(self):
        self.assertIn("#define EINK_BATTERY_ADC_PIN 1", self.device_config)
        self.assertIn("#define EINK_BATTERY_ENABLE_PIN 6", self.device_config)
        self.assertIn("#define EINK_BATTERY_SAMPLE_HOUR 6", self.device_config)
        self.assertIn("#define EINK_BATTERY_SAMPLE_COUNT 25", self.device_config)
        enable_at = self.source.index("digitalWrite(kBatteryEnablePin, HIGH)")
        discard_at = self.source.index(
            "analogReadMilliVolts(kBatteryAdcPin)", enable_at
        )
        disable_at = self.source.index(
            "digitalWrite(kBatteryEnablePin, LOW)", discard_at
        )
        self.assertLess(enable_at, discard_at)
        self.assertLess(discard_at, disable_at)
        self.assertIn("ADC_11db", self.source)
        self.assertIn("attemptedLocalDay != today", self.battery)
        self.assertIn('kPreferenceBatteryAttemptDay[] = "bat-try"', self.source)
        self.assertIn("initialBatteryAttempted", self.source)
        self.assertIn("persistBatteryState();", self.source)

    def test_daily_clock_is_ntp_backed_and_timer_aligns_to_six(self):
        self.assertIn(
            '#define EINK_TIMEZONE "MST7MDT,M3.2.0,M11.1.0"',
            self.device_config,
        )
        self.assertIn("configTzTime(EINK_TIMEZONE", self.source)
        self.assertIn("SNTP_SYNC_STATUS_COMPLETED", self.source)
        self.assertIn("target.tm_hour = EINK_BATTERY_SAMPLE_HOUR", self.source)
        self.assertIn("#define EINK_NTP_RETRY_WAKES 12", self.device_config)
        self.assertIn("clockAttemptedLocalDay == currentLocalDay", self.source)
        self.assertIn("ntpRetryWakesRemaining", self.source)
        self.assertIn(
            "if (untilTarget > 0 && untilTarget < wakeSeconds)",
            self.source,
        )

    def test_cached_and_physically_shown_battery_states_are_separate(self):
        battery_commit_at = self.source.index("persistBatteryState();")
        frame_sync_at = self.source.index("syncFrame(requestedFrameMode);")
        self.assertLess(battery_commit_at, frame_sync_at)
        self.assertIn('kPreferenceBatteryValid[] = "bat-valid"', self.source)
        self.assertIn('kPreferenceMarkValid[] = "mark-valid"', self.source)
        self.assertIn('kPreferenceMarkVersion[] = "mark-ver"', self.source)
        self.assertIn("batteryMarkNeedsRefresh()", self.source)
        self.assertIn("bypassValidator = true;", self.source)
        self.assertRegex(
            self.source,
            r"actualSha == displayedSha && etag == displayedEtag &&\s+"
            r"!batteryMarkNeedsRefresh\(\)",
        )

    def test_signature_is_small_handwritten_and_exact_palette(self):
        self.assertIn("setFreeFont(&Satisfy_24)", self.source)
        self.assertIn('snprintf(mark, sizeof(mark), "%u/100"', self.source)
        self.assertIn(
            "static_cast<unsigned>(battery.percent)",
            self.source,
        )
        self.assertIn("kBatteryMarkRightMargin = 18", self.source)
        self.assertIn("kBatteryMarkBottomMargin = 14", self.source)
        self.assertIn(
            "color == TFT_WHITE || color == TFT_YELLOW", self.source
        )
        self.assertIn("TFT_BLACK : TFT_WHITE", self.source)
        self.assertIn(
            "eink::clockwiseBackingPixelIndex(", self.source
        )

    def test_generic_lipo_curve_is_bounded_and_monotonic(self):
        points = [
            (int(millivolts), int(percent))
            for millivolts, percent in re.findall(
                r"\{(\d+),\s*(\d+)\}", self.battery
            )
        ]
        self.assertGreaterEqual(len(points), 8)
        self.assertEqual(points[0], (3300, 0))
        self.assertEqual(points[-1], (4200, 100))
        self.assertEqual(points, sorted(points))
        self.assertEqual(
            [percent for _, percent in points],
            sorted(percent for _, percent in points),
        )
        self.assertIn("kMinimumPlausibleBatteryMillivolts = 2500", self.battery)
        self.assertIn("kMaximumPlausibleBatteryMillivolts = 4350", self.battery)

    def test_host_battery_curve_and_daily_decisions(self):
        compiler = (
            shutil.which("c++")
            or shutil.which("clang++")
            or shutil.which("g++")
        )
        if compiler is None:
            self.skipTest("a host C++ compiler is not available")
        source = REPOSITORY / "tests/cpp/esp32_battery_monitor_test.cpp"
        with tempfile.TemporaryDirectory() as temporary_directory:
            executable = Path(temporary_directory) / "battery-monitor-test"
            compile_result = subprocess.run(
                [
                    compiler,
                    "-std=c++11",
                    "-Wall",
                    "-Wextra",
                    "-Werror",
                    "-I",
                    str(FIRMWARE / "include"),
                    str(source),
                    "-o",
                    str(executable),
                ],
                cwd=REPOSITORY,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(
                compile_result.returncode,
                0,
                compile_result.stdout + compile_result.stderr,
            )
            run_result = subprocess.run(
                [str(executable)],
                cwd=REPOSITORY,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(
                run_result.returncode,
                0,
                run_result.stdout + run_result.stderr,
            )

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
        self.assertIn('#define EINK_FRAME_AUTH_TOKEN ""', example)
        self.assertIn("replace-with-pi-address", example)
        self.assertNotIn(".local", example)


if __name__ == "__main__":
    unittest.main()
