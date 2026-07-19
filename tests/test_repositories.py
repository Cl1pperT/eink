from contextlib import chdir
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from display_simulator import repositories


def write_marker(repository: Path, marker: str) -> Path:
    path = repository / marker
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("# marker\n", encoding="utf-8")
    return repository


def write_avian_markers(repository: Path) -> Path:
    for marker in repositories.AVIAN_MARKERS:
        write_marker(repository, marker)
    return repository


class RepositoryDiscoveryTests(unittest.TestCase):
    def test_finds_checkout_below_integrations_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory)
            checkout = parent / "integrations" / "inkystarmap"
            write_marker(checkout, "src/inkystarmap/inkystarmap.py")
            self.assertEqual(
                repositories.find_repository(
                    str(parent),
                    "src/inkystarmap/inkystarmap.py",
                    "UNUSED_TEST_REPO",
                ),
                checkout.resolve(),
            )

    def test_finds_colocated_peacock_and_stars_from_another_working_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project_root = root / "eink"
            elsewhere = root / "elsewhere"
            elsewhere.mkdir()
            peacock = write_marker(
                project_root / "peacock" / "AvianVisitors",
                "weather_frame/renderer.py",
            )
            stars = write_marker(
                project_root / "stars" / "integrations" / "inkystarmap",
                "src/inkystarmap/inkystarmap.py",
            )

            with (
                patch.object(repositories, "PROJECT_ROOT", project_root),
                patch.dict(
                    os.environ,
                    {"UNUSED_WEATHER_REPO": "", "UNUSED_STARS_REPO": ""},
                    clear=False,
                ),
                chdir(elsewhere),
            ):
                self.assertEqual(
                    repositories.find_repository(
                        "", "weather_frame/renderer.py", "UNUSED_WEATHER_REPO"
                    ),
                    peacock.resolve(),
                )
                self.assertEqual(
                    repositories.find_repository(
                        "",
                        "src/inkystarmap/inkystarmap.py",
                        "UNUSED_STARS_REPO",
                    ),
                    stars.resolve(),
                )

    def test_explicit_and_environment_overrides_precede_colocated_checkout(self):
        marker = "frame/shoot.py"
        env_name = "TEST_AVIANVISITORS_REPO"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            colocated = write_marker(
                root / "project" / "peacock" / "AvianVisitors", marker
            )
            environment = write_marker(root / "environment", marker)
            explicit = write_marker(root / "explicit", marker)

            with (
                patch.object(repositories, "PROJECT_ROOT", root / "project"),
                patch.dict(os.environ, {env_name: str(environment)}, clear=False),
            ):
                self.assertEqual(
                    repositories.find_repository(str(explicit), marker, env_name),
                    explicit.resolve(),
                )
                self.assertEqual(
                    repositories.find_repository("", marker, env_name),
                    environment.resolve(),
                )
                self.assertNotEqual(environment.resolve(), colocated.resolve())

    def test_stale_explicit_path_falls_back_to_colocated_checkout(self):
        marker = "weather_frame/renderer.py"
        env_name = "UNUSED_STALE_REPOSITORY"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project_root = root / "project"
            colocated = write_marker(
                project_root / "peacock" / "AvianVisitors", marker
            )
            stale = root / "removed-checkout"

            with (
                patch.object(repositories, "PROJECT_ROOT", project_root),
                patch.dict(os.environ, {env_name: ""}, clear=False),
            ):
                self.assertEqual(
                    repositories.find_repository(str(stale), marker, env_name),
                    colocated.resolve(),
                )

    def test_shared_avian_resolver_requires_both_adapters_and_has_stable_precedence(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project_root = root / "project"
            colocated = write_avian_markers(
                project_root / "peacock" / "AvianVisitors"
            )
            weather_override = write_marker(
                root / "weather-only",
                "weather_frame/renderer.py",
            )
            avian_override = write_avian_markers(root / "avian-override")
            explicit = write_avian_markers(root / "explicit")

            with (
                patch.object(repositories, "PROJECT_ROOT", project_root),
                patch.dict(
                    os.environ,
                    {
                        "WEATHER_FRAME_REPO": str(weather_override),
                        "AVIANVISITORS_REPO": str(avian_override),
                    },
                    clear=False,
                ),
            ):
                self.assertEqual(
                    repositories.find_avian_repository(""),
                    avian_override.resolve(),
                )
                write_marker(weather_override, "frame/shoot.py")
                self.assertEqual(
                    repositories.find_avian_repository(""),
                    weather_override.resolve(),
                )
                self.assertEqual(
                    repositories.find_avian_repository(str(explicit)),
                    explicit.resolve(),
                )
                self.assertNotEqual(explicit.resolve(), colocated.resolve())
