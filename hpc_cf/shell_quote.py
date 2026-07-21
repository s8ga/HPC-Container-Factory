"""Shell-safe quoting and project-root path confinement.

Use :func:`shell_quote` for every config-derived token embedded in generated
bash (Dockerfiles, container scripts). Use :func:`confine_to_root` whenever a
caller-supplied path is joined under the factory tree (``--env``,
``manual_packages.file``, templates).
"""

from __future__ import annotations

import shlex
from pathlib import Path


def shell_quote(value: object) -> str:
    """Return a POSIX shell-safe single-quoted form of *value*."""
    return shlex.quote(str(value))


def confine_to_root(
    path: Path | str,
    *,
    root: Path,
    label: str = "path",
) -> Path:
    """Resolve *path* and require it stays under *root*.

    Raises:
        ValueError: if the resolved path escapes *root* (``..``, symlink, etc.).
    """
    root_resolved = root.resolve()
    resolved = Path(path).resolve()
    if not resolved.is_relative_to(root_resolved):
        raise ValueError(
            f"{label} escapes project root: {path!s} -> {resolved} "
            f"(root={root_resolved})"
        )
    return resolved
