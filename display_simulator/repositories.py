from __future__ import annotations

import os
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
COLOCATED_COLLECTIONS = ("peacock", "stars")
AVIAN_MARKERS = ("weather_frame/renderer.py", "frame/shoot.py")
AVIAN_ENV_NAMES = ("WEATHER_FRAME_REPO", "AVIANVISITORS_REPO")


def _candidate_values(
    explicit: str,
    env_names: tuple[str, ...],
) -> list[str | Path]:
    """Return repository locations in override-to-fallback order."""
    values: list[str | Path] = [explicit]
    values.extend(os.environ.get(name, "") for name in env_names)
    # These collection directories are intentionally anchored to this source
    # tree, rather than the launch directory.  A checkout can therefore be run
    # through an IDE, a shell in another directory, or the headless CLI without
    # re-selecting integrations in the simulator UI.
    values.extend(PROJECT_ROOT / name for name in COLOCATED_COLLECTIONS)
    cwd = Path.cwd()
    values.extend(
        (
            cwd,
            cwd.parent,
            Path.home() / "AvianVisitors",
            Path.home() / "inkystarmap",
        )
    )
    return values


def _find_repository(
    explicit: str,
    markers: tuple[str, ...],
    env_names: tuple[str, ...],
) -> Path | None:
    seen: set[Path] = set()
    for value in _candidate_values(explicit, env_names):
        if not str(value).strip():
            continue
        candidate = Path(value).expanduser().resolve()
        if candidate in seen:
            continue
        seen.add(candidate)
        if all((candidate / marker).exists() for marker in markers):
            return candidate
        # Folder pickers commonly land on a project collection such as
        # `stars/` while the checkout is `stars/integrations/inkystarmap/`.
        # Search only one child level (and the conventional integrations
        # level), avoiding an unbounded recursive filesystem scan.
        containers = (candidate, candidate / "integrations")
        for container in containers:
            try:
                children = sorted(path for path in container.iterdir() if path.is_dir())
            except OSError:
                continue
            for child in children:
                if all((child / marker).exists() for marker in markers):
                    return child
    return None


def find_repository(explicit: str, marker: str, env_name: str) -> Path | None:
    """Find an optional checkout without importing it during simulator startup."""
    return _find_repository(explicit, (marker,), (env_name,))


def find_avian_repository(explicit: str) -> Path | None:
    """Find the shared Peacock checkout required by both desktop adapters."""
    return _find_repository(explicit, AVIAN_MARKERS, AVIAN_ENV_NAMES)


def require_repository(explicit: str, marker: str, env_name: str, project: str) -> Path:
    path = find_repository(explicit, marker, env_name)
    if path is None:
        raise RuntimeError(f"{project} checkout not found; configure its repository path or enable the demo fallback")
    return path
