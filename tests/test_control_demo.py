from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from display_control.demo import (
    DEMO_DURATION_SECONDS,
    DEMO_MODES,
    PHOTO_MAX_DURATION_SECONDS,
    PHOTO_MIN_DURATION_SECONDS,
    DemoOverrideError,
    DemoOverrideStore,
    demo_path_for_settings,
    read_demo_override,
)


class DemoOverrideTests(unittest.TestCase):
    def setUp(self):
        self.environment = patch.dict(os.environ, {"EINK_CONTROL_DEMO": ""})
        self.environment.start()

    def tearDown(self):
        self.environment.stop()

    def test_each_allowed_mode_is_atomic_and_does_not_touch_settings(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = root / "settings.json"
            settings.write_text('{"display":{"mode":"automatic"}}\n', encoding="utf-8")
            before = settings.read_bytes()
            now = datetime(2026, 7, 27, 20, 0, tzinfo=timezone.utc)
            store = DemoOverrideStore(settings, clock=lambda: now)

            for mode in sorted(DEMO_MODES):
                with self.subTest(mode=mode):
                    status = store.activate(mode)
                    self.assertTrue(status["active"])
                    self.assertEqual(status["mode"], mode)
                    self.assertEqual(
                        status["remaining_seconds"],
                        DEMO_DURATION_SECONDS,
                    )
                    self.assertEqual(
                        status["duration_seconds"],
                        DEMO_DURATION_SECONDS,
                    )
                    self.assertEqual(
                        datetime.fromisoformat(
                            status["expires_at"].replace("Z", "+00:00")
                        ),
                        now + timedelta(seconds=DEMO_DURATION_SECONDS),
                    )
                    self.assertEqual(settings.read_bytes(), before)
                    self.assertFalse(list(root.glob("demo-override.json.*.tmp")))
                    persisted = json.loads(store.path.read_text(encoding="utf-8"))
                    self.assertEqual(
                        set(persisted),
                        {"schema_version", "mode", "started_at", "expires_at"},
                    )

    def test_exact_expiry_boundary_and_idempotent_cancel(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = Path(directory) / "settings.json"
            start = datetime(2026, 7, 27, 20, 0, tzinfo=timezone.utc)
            store = DemoOverrideStore(settings, clock=lambda: start)
            store.activate("birds")

            active = store.status(
                now=start + timedelta(seconds=DEMO_DURATION_SECONDS - 1)
            )
            self.assertTrue(active["active"])
            self.assertEqual(active["remaining_seconds"], 1)
            self.assertFalse(
                store.status(
                    now=start + timedelta(seconds=DEMO_DURATION_SECONDS)
                )["active"]
            )
            self.assertFalse(
                store.status(
                    now=start + timedelta(seconds=DEMO_DURATION_SECONDS + 1)
                )["active"]
            )
            self.assertFalse(store.cancel()["active"])
            self.assertFalse(store.cancel()["active"])
            self.assertFalse(store.path.exists())

    def test_photo_duration_accepts_five_minutes_and_full_timed_range(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = Path(directory) / "settings.json"
            start = datetime(2026, 7, 27, 20, 0, tzinfo=timezone.utc)
            store = DemoOverrideStore(settings, clock=lambda: start)

            for duration in (
                DEMO_DURATION_SECONDS,
                PHOTO_MIN_DURATION_SECONDS,
                PHOTO_MIN_DURATION_SECONDS + 1,
                6 * 60 * 60,
                PHOTO_MAX_DURATION_SECONDS,
            ):
                with self.subTest(duration=duration):
                    status = store.activate(
                        "uploaded-photo",
                        duration_seconds=duration,
                    )
                    self.assertEqual(status["duration_seconds"], duration)
                    self.assertEqual(status["remaining_seconds"], duration)
                    self.assertEqual(
                        datetime.fromisoformat(
                            status["expires_at"].replace("Z", "+00:00")
                        ),
                        start + timedelta(seconds=duration),
                    )
                    loaded = read_demo_override(settings, now=start)
                    self.assertIsNotNone(loaded)
                    self.assertEqual(loaded["duration_seconds"], duration)

    def test_timed_photo_expires_at_the_exact_deadline(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = Path(directory) / "settings.json"
            start = datetime(2026, 7, 27, 20, 0, tzinfo=timezone.utc)
            store = DemoOverrideStore(settings, clock=lambda: start)
            store.activate(
                "uploaded-photo",
                duration_seconds=PHOTO_MIN_DURATION_SECONDS,
            )
            deadline = start + timedelta(seconds=PHOTO_MIN_DURATION_SECONDS)

            almost_expired = store.status(
                now=deadline - timedelta(microseconds=1)
            )
            self.assertTrue(almost_expired["active"])
            self.assertEqual(almost_expired["remaining_seconds"], 1)
            self.assertFalse(store.status(now=deadline)["active"])
            self.assertFalse(
                store.status(now=deadline + timedelta(microseconds=1))["active"]
            )

    def test_invalid_photo_and_non_photo_durations_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            store = DemoOverrideStore(Path(directory) / "settings.json")
            invalid_photo_durations = (
                -1,
                0,
                DEMO_DURATION_SECONDS - 1,
                DEMO_DURATION_SECONDS + 1,
                PHOTO_MIN_DURATION_SECONDS - 1,
                PHOTO_MAX_DURATION_SECONDS + 1,
                True,
                1800.0,
                "1800",
                None,
            )
            for duration in invalid_photo_durations:
                with self.subTest(mode="uploaded-photo", duration=duration):
                    with self.assertRaisesRegex(
                        DemoOverrideError,
                        "duration|whole number",
                    ):
                        store.activate(
                            "uploaded-photo",
                            duration_seconds=duration,
                        )
                    self.assertFalse(store.path.exists())

            for mode in ("weather", "birds", "star-map"):
                with self.subTest(mode=mode):
                    with self.assertRaisesRegex(
                        DemoOverrideError,
                        "exactly five minutes",
                    ):
                        store.activate(
                            mode,
                            duration_seconds=PHOTO_MIN_DURATION_SECONDS,
                        )
                    self.assertFalse(store.path.exists())

    def test_persisted_duration_is_validated_from_timestamps(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = Path(directory) / "settings.json"
            path = demo_path_for_settings(settings)
            now = datetime(2026, 7, 27, 20, 0, tzinfo=timezone.utc)

            def write(mode, duration):
                path.write_text(
                    json.dumps(
                        {
                            "schema_version": 1,
                            "mode": mode,
                            "started_at": now.isoformat(),
                            "expires_at": (now + duration).isoformat(),
                        }
                    ),
                    encoding="utf-8",
                )

            write(
                "uploaded-photo",
                timedelta(seconds=PHOTO_MAX_DURATION_SECONDS),
            )
            loaded = read_demo_override(settings, now=now)
            self.assertIsNotNone(loaded)
            self.assertEqual(
                loaded["duration_seconds"],
                PHOTO_MAX_DURATION_SECONDS,
            )

            invalid = (
                ("weather", timedelta(seconds=PHOTO_MIN_DURATION_SECONDS)),
                (
                    "uploaded-photo",
                    timedelta(seconds=PHOTO_MIN_DURATION_SECONDS - 1),
                ),
                (
                    "uploaded-photo",
                    timedelta(seconds=PHOTO_MAX_DURATION_SECONDS + 1),
                ),
                (
                    "uploaded-photo",
                    timedelta(seconds=PHOTO_MIN_DURATION_SECONDS, microseconds=1),
                ),
            )
            for mode, duration in invalid:
                with self.subTest(mode=mode, duration=duration):
                    write(mode, duration)
                    self.assertIsNone(
                        read_demo_override(settings, now=now)
                    )

    def test_client_cannot_create_an_unsupported_or_indefinite_override(self):
        with tempfile.TemporaryDirectory() as directory:
            store = DemoOverrideStore(Path(directory) / "settings.json")
            for mode in ("automatic", "active", "test-pattern", "unknown", ""):
                with self.subTest(mode=mode):
                    with self.assertRaisesRegex(DemoOverrideError, "mode must be"):
                        store.activate(mode)

            now = datetime(2026, 7, 27, 20, 0, tzinfo=timezone.utc)
            path = store.path
            path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "mode": "weather",
                        "started_at": now.isoformat(),
                        "expires_at": (now + timedelta(hours=1)).isoformat(),
                    }
                ),
                encoding="utf-8",
            )
            self.assertIsNone(read_demo_override(store.settings_path, now=now))

    def test_missing_corrupt_and_unsafe_state_fall_back_to_normal_display(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = root / "settings.json"
            path = demo_path_for_settings(settings)
            now = datetime(2026, 7, 27, 20, 0, tzinfo=timezone.utc)
            self.assertIsNone(read_demo_override(settings, now=now))

            invalid_documents = (
                b"{not-json",
                json.dumps([]).encode(),
                json.dumps(
                    {
                        "schema_version": 1,
                        "mode": "weather",
                        "started_at": now.isoformat(),
                        "expires_at": (now + timedelta(minutes=5)).isoformat(),
                        "extra": True,
                    }
                ).encode(),
                b"x" * 5000,
            )
            for payload in invalid_documents:
                with self.subTest(size=len(payload)):
                    path.write_bytes(payload)
                    self.assertIsNone(read_demo_override(settings, now=now))

            target = root / "other.json"
            target.write_text("{}", encoding="utf-8")
            path.unlink()
            try:
                path.symlink_to(target)
            except (OSError, NotImplementedError):
                return
            self.assertIsNone(read_demo_override(settings, now=now))

    def test_naive_clocks_are_rejected_on_write(self):
        with tempfile.TemporaryDirectory() as directory:
            store = DemoOverrideStore(
                Path(directory) / "settings.json",
                clock=lambda: datetime(2026, 7, 27, 20, 0),
            )
            with self.assertRaisesRegex(DemoOverrideError, "timezone"):
                store.activate("weather")


if __name__ == "__main__":
    unittest.main()
