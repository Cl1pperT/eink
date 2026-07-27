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
