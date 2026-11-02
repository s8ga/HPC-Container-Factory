"""Atomic file writes — readers never observe a torn or half-written file.

Sidecar and state files (lock-manifest, resolved-repos, mirror manifests,
prune reports, OCI id sidecars, rendered def files) are consumed
fail-closed: a truncated JSON/YAML aborts the run. Writing them in place
means an interrupt (Ctrl-C, runner kill, full disk) leaves exactly that
corrupt file behind. Write to a unique sibling temp file, then
``os.replace`` — the rename is atomic on POSIX, so the target is always
either the old complete file or the new complete file.
"""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import Mapping
from pathlib import Path


def atomic_write_text(path: Path, text: str) -> Path:
    """Atomically replace *path* with *text* (UTF-8)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(text, encoding="utf-8")
        # os.replace is atomic; Path.replace delegates to it.
        os.replace(temporary, path)
    finally:
        # On any failure the temp file must not linger (a crash between
        # write and replace leaves one behind only if the process dies,
        # which the unique-name scheme keeps harmless).
        temporary.unlink(missing_ok=True)
    return path


def atomic_write_json(path: Path, payload: Mapping[str, object]) -> Path:
    """Atomically replace *path* with *payload* rendered as sorted JSON."""
    return atomic_write_text(
        path, json.dumps(payload, indent=2, sort_keys=True) + "\n"
    )
