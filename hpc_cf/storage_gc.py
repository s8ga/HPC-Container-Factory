"""Lightweight container storage GC for orphaned Buildah containers and layers."""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

_BUILDAH_SUBCOMMANDS = frozenset({
    "bud", "build", "build-using-dockerfile", "from", "run", "copy", "add", "commit",
})


def _is_build_argv(argv: list[str]) -> bool:
    if not argv:
        return False
    exe = Path(argv[0]).name
    rest = [arg for arg in argv[1:] if not arg.startswith("-")]
    if not rest:
        return False
    if exe in ("podman", "docker"):
        return rest[0] == "build" or (
            len(rest) >= 2 and rest[0] in ("image", "container") and rest[1] == "build"
        )
    return exe == "buildah" and rest[0] in _BUILDAH_SUBCOMMANDS


def detect_active_builds(
    *,
    proc_root: Path = Path("/proc"),
    uid: int | None = None,
    self_pid: int | None = None,
) -> list[tuple[int, str]]:
    """Return ``(pid, cmdline)`` pairs for active OCI builds owned by *uid*."""
    target_uid = os.getuid() if uid is None else uid
    current_pid = os.getpid() if self_pid is None else self_pid
    if not proc_root.is_dir():
        return []
    found: list[tuple[int, str]] = []
    for entry in sorted(proc_root.iterdir(), key=lambda p: p.name):
        if not entry.name.isdigit() or int(entry.name) == current_pid:
            continue
        try:
            if entry.stat().st_uid != target_uid:
                continue
            raw = (entry / "cmdline").read_bytes()
        except OSError:
            continue
        argv = [p.decode("utf-8", errors="replace") for p in raw.rstrip(b"\x00").split(b"\x00") if p]
        if _is_build_argv(argv):
            found.append((int(entry.name), " ".join(argv)))
    return found


def parse_buildah_working_containers(text: str) -> list[tuple[str, str]]:
    """Return ``(id, name)`` for ``builder=True`` entries in ``buildah containers --json``."""
    stripped = text.strip()
    if not stripped or stripped == "null":
        return []
    payload = json.loads(stripped) or []
    return [
        (str(item["id"]).strip(), str(item.get("containername") or ""))
        for item in payload
        if isinstance(item, dict) and item.get("builder") is True and str(item.get("id") or "").strip()
    ]


def is_checkpoint_builder_tag(tag_ref: str) -> bool:
    """Match ``*-installed`` / ``*-builder`` tags while preserving ``*-buildcache-producer``."""
    _, sep, tag = tag_ref.rpartition(":")
    if not sep or tag.endswith("-buildcache-producer"):
        return False
    return tag.endswith("-installed") or tag.endswith("-builder")


def parse_checkpoint_tags(text: str) -> list[str]:
    """Extract intermediate checkpoint tags from ``<engine> images --format json``."""
    stripped = text.strip()
    if not stripped or stripped == "null":
        return []
    records = (
        json.loads(stripped)
        if stripped.startswith("[")
        else [json.loads(ln) for ln in stripped.splitlines() if ln.strip()]
    )
    tags: list[str] = []
    for rec in records:
        if not isinstance(rec, dict):
            continue
        for name in rec.get("Names") or rec.get("RepoTags") or ():
            if isinstance(name, str) and is_checkpoint_builder_tag(name) and name not in tags:
                tags.append(name)
    return tags


def run_storage_gc(
    *,
    engine: str = "podman",
    dry_run: bool = False,
    prune_builder_tags: bool = False,
    prune_images: bool = True,
    force: bool = False,
) -> str:
    """Clean orphaned Buildah working containers, optional checkpoint tags, and dangling layers."""
    if shutil.which(engine) is None:
        raise RuntimeError(f"{engine} command not found in PATH")

    active = detect_active_builds()
    if active and not dry_run and not force:
        summary = "; ".join(f"pid={pid} ({cmd})" for pid, cmd in active)
        raise RuntimeError(
            f"Refusing to run storage GC while build(s) are active: {summary}. "
            "Pass --dry-run to inspect or --force to override."
        )

    lines = [f"mode={'dry-run' if dry_run else 'apply'} engine={engine}"]
    if active:
        lines.extend(f"  [active] pid={pid} {cmd}" for pid, cmd in active)

    if engine == "podman" and shutil.which("buildah") is not None:
        res = subprocess.run(["buildah", "containers", "--json"], capture_output=True, text=True, check=True)
        working = parse_buildah_working_containers(res.stdout)
        removed = 0
        if working and not dry_run:
            subprocess.run(["buildah", "rm", *(cid for cid, _ in working)], check=True)
            removed = len(working)
        lines.append(f"buildah_working_containers={len(working)} removed={removed}")
        lines.extend(f"  [working] {cid[:12]} {name}" for cid, name in working)

    if prune_builder_tags:
        res = subprocess.run([engine, "images", "--format", "json"], capture_output=True, text=True, check=True)
        tags = parse_checkpoint_tags(res.stdout)
        removed_tags = 0
        if tags and not dry_run:
            subprocess.run([engine, "rmi", *tags], check=True)
            removed_tags = len(tags)
        lines.append(f"checkpoint_tags={len(tags)} removed={removed_tags}")
        lines.extend(f"  [tag] {tag}" for tag in tags)

    if prune_images and not dry_run:
        subprocess.run([engine, "image", "prune", "-f"], capture_output=True, text=True, check=True)
        lines.append("image_prune=executed")

    return "\n".join(lines) + "\n"
