from __future__ import annotations

import os
from pathlib import Path
import re
import stat
import subprocess
import tempfile
import tomllib
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
        source = INSTALLER.read_text(encoding="utf-8")
        self.assertIn("repair_venv_paths", source)
        self.assertIn('"$ACTIVE_VENV/bin/eink-display" --version', source)
        self.assertIn("backup_units_and_systemd_state", source)
        self.assertIn("rollback_post_swap", source)
        self.assertIn("trap finish_install EXIT", source)
        self.assertIn('systemctl reset-failed "${ENABLED_UNITS[@]}"', source)
        self.assertIn("eink-display-package.XXXXXX", source)
        self.assertIn('"$SOURCE_DIR/display_control"', source)

        with (REPOSITORY / "pyproject.toml").open("rb") as stream:
            package = tomllib.load(stream)
        self.assertEqual(
            package["project"]["scripts"]["eink-display-control"],
            "display_control.server:main",
        )
        self.assertIn(
            "display_control*",
            package["tool"]["setuptools"]["packages"]["find"]["include"],
        )

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
            control_token_file = config_dir / "control-panel.token"

            self.assertTrue((app / "install-raspberry-pi.sh").is_file())
            self.assertTrue((app / "display_runtime/README.md").is_file())
            self.assertTrue((app / "INSTALLATION").is_file())
            self.assertFalse((app / ".venv").exists())
            self.assertTrue((state / "cache/matplotlib").is_dir())
            self.assertTrue((state / "cache/starplot").is_dir())
            self.assertTrue((state / "frames").is_dir())
            self.assertTrue((state / "uploads").is_dir())
            self.assertTrue((state / "control").is_dir())
            self.assertEqual(self.mode(config), 0o640)
            self.assertEqual(self.mode(token_file), 0o640)
            self.assertFalse(control_token_file.exists())
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
                    "eink-display-control.service",
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
            self.assertNotIn("--access-token", combined_units)
            self.assertNotIn("control-panel.token", combined_units)
            self.assertIn(
                "--settings /srv/eink-state/control/settings.json",
                combined_units,
            )
            self.assertIn("ReadWritePaths=/srv/eink-state", combined_units)
            self.assertIn(
                "STARPLOT_DATA_PATH=/srv/eink-state/cache/starplot",
                combined_units,
            )
            self.assertIn("ReadOnlyPaths=/srv/eink-state", combined_units)
            self.assertIn("Restart=on-failure", units["eink-display-render@.service"])
            self.assertIn("RestartSec=15min", units["eink-display-render@.service"])

            expected_timers = {
                "weather": ("05:55:00",),
                "birds": ("08:55:00",),
                "star-map": (
                    "12:00:00",
                    "16:20:00",
                    "17:20:00",
                    "18:20:00",
                    "19:20:00",
                    "20:20:00",
                ),
            }
            for mode, times_of_day in expected_timers.items():
                timer = units[f"eink-display-{mode}.timer"]
                self.assertIn(f"Unit=eink-display-render@{mode}.service", timer)
                self.assertEqual(timer.count("OnCalendar="), len(times_of_day))
                for time_of_day in times_of_day:
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
            self.assertIn('/srv/eink-state/control/settings.json', config.read_text(encoding="utf-8"))

    def test_staged_default_and_core_only_modes_both_install(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name, extra in (("full", ()), ("core", ("--core-only",))):
                with self.subTest(mode=name):
                    stage = root / name
                    self.run_installer(stage, *extra, "--no-enable", "--no-start")
                    self.assertTrue((stage / "opt/eink-display/INSTALLATION").is_file())
                    if name == "full":
                        for catalog in (
                            "constellations.0.3.3.parquet",
                            "de421.bsp",
                            "stars.bigksy.0.1.3.mag11.parquet",
                        ):
                            self.assertTrue(
                                (stage / "var/lib/eink-display/cache/starplot" / catalog).is_file()
                            )
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
            legacy_control_token = stage / "etc/eink-display/control-panel.token"
            state_sentinel = stage / "var/lib/eink-display/frames/operator-frame.ee02"
            settings_sentinel = stage / "var/lib/eink-display/control/settings.json"
            custom_config = b"# operator configuration\n[runtime]\nstrict_sources = true\n"
            config.write_bytes(custom_config)
            original_token = token.read_bytes()
            legacy_control_token.write_bytes(b"legacy-phone-token\n")
            original_legacy_control_token = legacy_control_token.read_bytes()
            state_sentinel.write_bytes(b"last-known-good")
            settings_sentinel.write_bytes(b'{"schema_version": 1}\n')

            result = self.run_installer(stage, "--core-only", "--no-enable")

            self.assertEqual(config.read_bytes(), custom_config)
            self.assertEqual(token.read_bytes(), original_token)
            self.assertEqual(
                legacy_control_token.read_bytes(),
                original_legacy_control_token,
            )
            self.assertEqual(state_sentinel.read_bytes(), b"last-known-good")
            self.assertEqual(settings_sentinel.read_bytes(), b'{"schema_version": 1}\n')
            self.assertIn("preserving existing runtime configuration", result.stdout)
            self.assertIn("preserving existing bearer token", result.stdout)
            self.assertNotIn("control-panel token", result.stdout)

    def test_rotate_token_and_force_config_are_explicit_and_back_up_config(self):
        with tempfile.TemporaryDirectory() as directory:
            stage = Path(directory) / "root"
            self.run_installer(stage, "--core-only", "--no-enable")
            config = stage / "etc/eink-display/runtime.toml"
            token = stage / "etc/eink-display/frame-server.token"
            control_token = stage / "etc/eink-display/control-panel.token"
            custom_config = b"# irreplaceable operator settings\n"
            config.write_bytes(custom_config)
            original_token = token.read_text(encoding="ascii")
            self.assertFalse(control_token.exists())

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
            self.assertFalse(control_token.exists())
            self.assertNotEqual(config.read_bytes(), custom_config)
            backups = list(config.parent.glob("runtime.toml.backup.*"))
            self.assertEqual(len(backups), 1)
            self.assertEqual(backups[0].read_bytes(), custom_config)
            self.assertEqual(self.mode(backups[0]), 0o640)
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
            legacy_control_token = config_dir / "control-panel.token"
            legacy_control_token.write_bytes(b"legacy-phone-token\n")
            legacy_control_token_before = legacy_control_token.read_bytes()

            result = self.run_installer(stage, "--uninstall")

            self.assertFalse(app_dir.exists())
            self.assertFalse(any(unit_dir.glob("eink-display-*")))
            self.assertTrue(config_dir.is_dir())
            self.assertEqual((config_dir / "frame-server.token").read_bytes(), token_before)
            self.assertEqual(
                legacy_control_token.read_bytes(),
                legacy_control_token_before,
            )
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

        with tempfile.TemporaryDirectory() as directory:
            stage = Path(directory) / "root"
            overlap = self.run_installer(
                stage,
                "--config-dir",
                "/srv/eink-data",
                "--state-dir",
                "/srv/eink-data/state",
                check=False,
            )
            self.assertNotEqual(overlap.returncode, 0)
            self.assertIn("--config-dir and --state-dir must not overlap", overlap.stderr)
            self.assertFalse(stage.exists())

    def test_symlinked_writable_state_subdirectory_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            stage = root / "root"
            state = stage / "var/lib/eink-display"
            outside = root / "outside"
            state.mkdir(parents=True)
            outside.mkdir()
            (state / "cache").symlink_to(outside, target_is_directory=True)

            result = self.run_installer(
                stage,
                "--core-only",
                "--no-enable",
                check=False,
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("managed state path cache may not be a symbolic link", result.stderr)
            self.assertEqual(list(outside.iterdir()), [])

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
            self.assertIn(
                "would rotate the frame-server token without printing it",
                result.stdout,
            )
            self.assertNotRegex(result.stdout, re.compile(r"\b[0-9a-f]{64}\b"))


if __name__ == "__main__":
    unittest.main()
