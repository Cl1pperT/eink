from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from display_runtime.cli import main


class RuntimeCliTests(unittest.TestCase):
    def test_test_pattern_render_and_status_json(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "runtime.toml"
            config.write_text("", encoding="utf-8")
            output = root / "output"
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                code = main([
                    "--config", str(config),
                    "render", "test-pattern",
                    "--at", "2026-07-11T12:00:00-06:00",
                    "--output-dir", str(output),
                    "--no-rgb",
                    "--json",
                ])
            self.assertEqual(code, 0)
            result = json.loads(stdout.getvalue())
            self.assertEqual(result["mode"], "test-pattern")
            self.assertEqual(result["dimensions"], {"width": 1600, "height": 1200})
            self.assertTrue(Path(result["frame_path"]).is_file())
            self.assertEqual(result["wire"]["bytes"], 960_000)
            self.assertTrue(Path(result["wire"]["path"]).is_file())

            stdout = io.StringIO()
            with redirect_stdout(stdout):
                code = main([
                    "--config", str(config),
                    "status", "test-pattern",
                    "--output-dir", str(output),
                    "--json",
                ])
            self.assertEqual(code, 0)
            status = json.loads(stdout.getvalue())
            self.assertEqual(status["test-pattern"]["pixel_checksum"]["value"], result["checksum"])
            self.assertEqual(status["test-pattern"]["wire"]["sha256"], result["wire"]["checksum"])

    def test_missing_live_repository_returns_nonzero_without_traceback(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "runtime.toml"
            config.write_text("", encoding="utf-8")
            stderr = io.StringIO()
            with (
                mock.patch("display_runtime.runtime.find_repository", return_value=None),
                redirect_stderr(stderr),
            ):
                code = main(["--config", str(config), "render", "weather"])
            self.assertEqual(code, 3)
            self.assertIn("repository path is not configured", stderr.getvalue())
            self.assertNotIn("Traceback", stderr.getvalue())

    def test_invalid_config_returns_configuration_exit_code(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "runtime.toml"
            config.write_text("[runtime\n", encoding="utf-8")
            stderr = io.StringIO()
            with redirect_stderr(stderr):
                code = main(["--config", str(config), "check"])
            self.assertEqual(code, 2)
            self.assertIn("configuration error", stderr.getvalue())

    def test_network_commands_refuse_to_run_without_authentication(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "runtime.toml"
            config.write_text("", encoding="utf-8")
            for command in (("serve",), ("esp-sync", "weather")):
                with self.subTest(command=command):
                    stderr = io.StringIO()
                    with mock.patch.dict("os.environ", {}, clear=True), redirect_stderr(stderr):
                        code = main(["--config", str(config), *command])
                    self.assertEqual(code, 2)
                    self.assertIn("authentication token is required", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
