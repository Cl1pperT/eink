from __future__ import annotations

import os
from pathlib import Path


def find_repository(explicit: str, marker: str, env_name: str) -> Path | None:
    """Find an optional checkout without importing it during simulator startup."""
    values = [explicit, os.environ.get(env_name, "")]
    cwd = Path.cwd()
    values.extend((str(cwd), str(cwd.parent), str(Path.home() / "AvianVisitors"), str(Path.home() / "inkystarmap")))
    for value in values:
        if not str(value).strip():
            continue
        candidate = Path(value).expanduser().resolve()
        if (candidate / marker).exists():
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
                if (child / marker).exists():
                    return child
    return None


def require_repository(explicit: str, marker: str, env_name: str, project: str) -> Path:
    path = find_repository(explicit, marker, env_name)
    if path is None:
        raise RuntimeError(f"{project} checkout not found; configure its repository path or enable the demo fallback")
    return path
