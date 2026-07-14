from __future__ import annotations

import os
from pathlib import Path
import re
import stat
import subprocess
import tempfile
import unittest


REPOSITORY = Path(__file__).resolve().parents[1]
INSTALLER = REPOSITORY / "deploy" / "install-raspberry-pi.sh"


class RaspberryPiInstallerTests(unittest.TestCase):
    maxDiff = None

    def run_installer(
        self,
        root: Path,
        *arguments: str,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        environment = os.environ.copy()
        environment["HOME"] = str(root.parent / "isolated-home")
        result = subprocess.run(
            [
                "/bin/bash",
                str(INSTALLER),
                "--destdir",
                str(root),
                *arguments,
            ],
            cwd=REPOSITORY,
            env=environment,
            text=True,
            capture_output=True,
            timeout=30,
            check=False,
        )
        if check and result.returncode != 0:
            self.fail(
                f"installer exited {result.returncode}\n"
                f"stdout:\n{result.stdout}\n"
                f"stderr:\n{result.stderr}"
            )
        return result

    @staticmethod
    def mode(path: Path) -> int:
        return stat.S_IMODE(path.stat().st_mode)

    def test_script_is_valid_bash(self):
        result = subprocess.run(
            ["/bin/bash", "-n", str(INSTALLER)],
            cwd=REPOSITORY,
            text=True,
            capture_output=True,
            timeout=10,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_staged_core_install_has_substituted_units_and_no_secret_leaks(self):
        with tempfile.TemporaryDirectory() as directory:
            stage = Path(directory) / "root"
            result = self.run_installer(
                stage,
                "--core-only",
                "--no-enable",
                "--no-start",
                "--install-dir",
                "/srv/eink-app",
                "--config-dir",
                "/srv/eink-config",
                "--state-dir",
                "/srv/eink-state",
                "--user",
                "frame-user",
                "--group",
                "frame-group",
            )

            app = stage / "srv/eink-app"
            config_dir = stage / "srv/eink-config"
            state = stage / "srv/eink-state"
            unit_dir = stage / "etc/systemd/system"
            config = config_dir / "runtime.toml"
            token_file = config_dir / "frame-server.token"

            self.assertTrue((app / "install-raspberry-pi.sh").is_file())
            self.assertTrue((app / "display_runtime/README.md").is_file())
            self.assertTrue((app / "INSTALLATION").is_file())
            self.assertFalse((app / ".venv").exists())
            self.assertTrue((state / "cache/matplotlib").is_dir())
            self.assertTrue((state / "frames").is_dir())
            self.assertTrue((state / "uploads").is_dir())
            self.assertEqual(self.mode(config), 0o640)
            self.assertEqual(self.mode(token_file), 0o640)
            self.assertEqual(self.mode(unit_dir / "eink-display-server.service"), 0o644)

            token = token_file.read_text(encoding="ascii").strip()
            self.assertRegex(token, r"^[0-9a-f]{64}$")
            self.assertNotIn(token, result.stdout)
            self.assertNotIn(token, result.stderr)

            units = {
                path.name: path.read_text(encoding="utf-8")
                for path in unit_dir.glob("eink-display-*")
            }
            self.assertEqual(
                set(units),
                {
                    "eink-display-server.service",
                    "eink-display-render@.service",
                    "eink-display-weather.timer",
                    "eink-display-birds.timer",
                    "eink-display-star-map.timer",
                },
            )
            combined_units = "\n".join(units.values())
            self.assertNotIn("@EINK_", combined_units)
            self.assertNotIn(str(stage), combined_units)
            self.assertNotIn(token, combined_units)
            self.assertIn("User=frame-user", combined_units)
            self.assertIn("Group=frame-group", combined_units)
            self.assertIn("WorkingDirectory=/srv/eink-app", combined_units)
            self.assertIn("--config /srv/eink-config/runtime.toml", combined_units)
            self.assertIn("--token-file /srv/eink-config/frame-server.token", combined_units)
            self.assertIn("ReadWritePaths=/srv/eink-state", combined_units)
            self.assertIn("ReadOnlyPaths=/srv/eink-state", combined_units)

            expected_timers = {
                "weather": "06:00:00",
                "birds": "10:00:00",
                "star-map": "20:00:00",
            }
            for mode, time_of_day in expected_timers.items():
                timer = units[f"eink-display-{mode}.timer"]
                self.assertIn(f"Unit=eink-display-render@{mode}.service", timer)
                self.assertIn(f"OnCalendar=*-*-* {time_of_day}", timer)
                self.assertIn("Persistent=true", timer)

            all_other_text = "\n".join(
                path.read_text(encoding="utf-8", errors="replace")
                for path in stage.rglob("*")
                if path.is_file() and path != token_file
            )
            self.assertNotIn(token, all_other_text)
            self.assertIn('/srv/eink-state/frames', config.read_text(encoding="utf-8"))
            self.assertIn('/srv/eink-state/uploads/latest.png', config.read_text(encoding="utf-8"))

    def test_staged_default_and_core_only_modes_both_install(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name, extra in (("full", ()), ("core", ("--core-only",))):
                with self.subTest(mode=name):
                    stage = root / name
                    self.run_installer(stage, *extra, "--no-enable", "--no-start")
                    self.assertTrue((stage / "opt/eink-display/INSTALLATION").is_file())
                    self.assertTrue(
                        (stage / "etc/systemd/system/eink-display-server.service").is_file()
                    )
                    self.assertFalse((stage / "opt/eink-display/.venv").exists())

    def test_reinstall_preserves_operator_config_token_and_state(self):
        with tempfile.TemporaryDirectory() as directory:
            stage = Path(directory) / "root"
            self.run_installer(stage, "--core-only", "--no-enable")
            config = stage / "etc/eink-display/runtime.toml"
            token = stage / "etc/eink-display/frame-server.token"
            state_sentinel = stage / "var/lib/eink-display/frames/operator-frame.ee02"
            custom_config = b"# operator configuration\n[runtime]\nstrict_sources = true\n"
            config.write_bytes(custom_config)
            original_token = token.read_bytes()
            state_sentinel.write_bytes(b"last-known-good")

            result = self.run_installer(stage, "--core-only", "--no-enable")

            self.assertEqual(config.read_bytes(), custom_config)
            self.assertEqual(token.read_bytes(), original_token)
            self.assertEqual(state_sentinel.read_bytes(), b"last-known-good")
            self.assertIn("preserving existing runtime configuration", result.stdout)
            self.assertIn("preserving existing bearer token", result.stdout)

    def test_rotate_token_and_force_config_are_explicit_and_back_up_config(self):
        with tempfile.TemporaryDirectory() as directory:
            stage = Path(directory) / "root"
            self.run_installer(stage, "--core-only", "--no-enable")
            config = stage / "etc/eink-display/runtime.toml"
            token = stage / "etc/eink-display/frame-server.token"
            custom_config = b"# irreplaceable operator settings\n"
            config.write_bytes(custom_config)
            original_token = token.read_text(encoding="ascii")

            result = self.run_installer(
                stage,
                "--core-only",
                "--no-enable",
                "--force-config",
                "--rotate-token",
            )

            replacement_token = token.read_text(encoding="ascii")
            self.assertNotEqual(replacement_token, original_token)
            self.assertRegex(replacement_token.strip(), r"^[0-9a-f]{64}$")
            self.assertNotEqual(config.read_bytes(), custom_config)
            backups = list(config.parent.glob("runtime.toml.backup.*"))
            self.assertEqual(len(backups), 1)
            self.assertEqual(backups[0].read_bytes(), custom_config)
            self.assertIn("backed up the previous runtime configuration", result.stdout)
            self.assertIn("rotated the bearer token", result.stdout)
            self.assertNotIn(replacement_token.strip(), result.stdout)

    def test_uninstall_preserves_data_and_purge_removes_it(self):
        with tempfile.TemporaryDirectory() as directory:
            stage = Path(directory) / "root"
            self.run_installer(stage, "--core-only", "--no-enable")
            config_dir = stage / "etc/eink-display"
            state_dir = stage / "var/lib/eink-display"
            app_dir = stage / "opt/eink-display"
            unit_dir = stage / "etc/systemd/system"
            sentinel = state_dir / "frames/last-known-good.ee02"
            sentinel.write_bytes(b"frame")
            token_before = (config_dir / "frame-server.token").read_bytes()

            result = self.run_installer(stage, "--uninstall")

            self.assertFalse(app_dir.exists())
            self.assertFalse(any(unit_dir.glob("eink-display-*")))
            self.assertTrue(config_dir.is_dir())
            self.assertEqual((config_dir / "frame-server.token").read_bytes(), token_before)
            self.assertEqual(sentinel.read_bytes(), b"frame")
            self.assertIn("preserved /etc/eink-display and /var/lib/eink-display", result.stdout)

            self.run_installer(stage, "--purge")
            self.assertFalse(config_dir.exists())
            self.assertFalse(state_dir.exists())

    def test_invalid_destdir_is_rejected_without_writes(self):
        relative = subprocess.run(
            ["/bin/bash", str(INSTALLER), "--destdir", "relative-stage"],
            cwd=REPOSITORY,
            text=True,
            capture_output=True,
            timeout=10,
            check=False,
        )
        self.assertNotEqual(relative.returncode, 0)
        self.assertIn("--destdir must be an absolute directory other than /", relative.stderr)
        self.assertFalse((REPOSITORY / "relative-stage").exists())

        root = subprocess.run(
            ["/bin/bash", str(INSTALLER), "--destdir", "/", "--dry-run"],
            cwd=REPOSITORY,
            text=True,
            capture_output=True,
            timeout=10,
            check=False,
        )
        self.assertNotEqual(root.returncode, 0)
        self.assertIn("--destdir must be an absolute directory other than /", root.stderr)

    def test_dry_run_describes_operation_without_creating_destdir(self):
        with tempfile.TemporaryDirectory() as directory:
            stage = Path(directory) / "never-created"
            result = self.run_installer(
                stage,
                "--core-only",
                "--no-enable",
                "--rotate-token",
                "--dry-run",
            )
            self.assertFalse(stage.exists())
            self.assertIn("dry run: would install runtime at /opt/eink-display", result.stdout)
            self.assertIn("would rotate the bearer token without printing it", result.stdout)
            self.assertNotRegex(result.stdout, re.compile(r"\b[0-9a-f]{64}\b"))


if __name__ == "__main__":
    unittest.main()
