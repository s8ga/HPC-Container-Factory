"""Lock input manifest — provenance sidecar for spack.lock.

spack.lock stores no environment-definition hash (its top level holds only
``_meta``/``spack``/``roots``/``concrete_specs``), so nothing links a lock
to the spack.yaml and custom_repos pins it was concretized against. Editing
either after an assets run therefore sails through every preflight and the
image silently builds the old dependency graph (audit finding 2.2).

The manifest follows the factory's two-stage doctrine: assets writes it
next to a freshly concretized spack.lock, builds/validate compare the
current inputs against it and fail closed on drift.

``custom_repos_pins`` records only env.yaml-pinned git repos (static pins);
floating repos are covered by the resolved-repos.yaml sidecar instead.
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from hpc_cf.atomic_io import atomic_write_json
from hpc_cf.environment import EnvironmentSpec

logger = logging.getLogger(__name__)

LOCK_MANIFEST_FILENAME = "lock-manifest.json"
_LOCK_MANIFEST_SCHEMA_VERSION = 1


def compute_spack_yaml_sha256(config_dir: Path) -> str | None:
    """Sha256 of the raw spack.yaml bytes next to the lock, None if absent."""
    spack_yaml = config_dir / "spack.yaml"
    if not spack_yaml.is_file():
        return None
    return hashlib.sha256(spack_yaml.read_bytes()).hexdigest()


def current_custom_repos_pins(spec: EnvironmentSpec) -> dict[str, str]:
    """namespace → commit for git custom_repos pinned in env.yaml."""
    pins: dict[str, str] = {}
    for repo in spec.spack.custom_repos:
        if repo.type == "git" and repo.commit and repo.namespace:
            pins[repo.namespace] = repo.commit
    return dict(sorted(pins.items()))


def build_manifest(spec: EnvironmentSpec, config_dir: Path) -> dict[str, Any]:
    return {
        "schema_version": _LOCK_MANIFEST_SCHEMA_VERSION,
        "spack_yaml_sha256": compute_spack_yaml_sha256(config_dir),
        "custom_repos_pins": current_custom_repos_pins(spec),
        "spack_version": spec.spack.version,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


def write_lock_manifest(spec: EnvironmentSpec, config_dir: Path) -> Path:
    """Write the manifest next to spack.yaml/spack.lock; returns its path."""
    manifest = build_manifest(spec, config_dir)
    path = config_dir / LOCK_MANIFEST_FILENAME
    atomic_write_json(path, manifest)
    logger.info("Lock input manifest written: %s", path)
    return path


def read_lock_manifest(config_dir: Path) -> dict[str, Any] | None:
    """Parse the manifest; None when absent, ValueError when malformed."""
    path = config_dir / LOCK_MANIFEST_FILENAME
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"malformed {LOCK_MANIFEST_FILENAME}: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(
            f"malformed {LOCK_MANIFEST_FILENAME}: expected a JSON object"
        )
    return data


def compare_lock_inputs(
    manifest: dict[str, Any],
    spec: EnvironmentSpec,
    config_dir: Path,
) -> list[str]:
    """Human-readable drift between the manifest and the current inputs.

    Empty list means the lock was concretized against exactly what is on
    disk now (spack.yaml bytes + env.yaml repo pins).
    """
    drift: list[str] = []

    recorded_yaml = manifest.get("spack_yaml_sha256")
    current_yaml = compute_spack_yaml_sha256(config_dir)
    if recorded_yaml != current_yaml:
        drift.append(
            "spack.yaml changed since concretization "
            f"(recorded sha256 {recorded_yaml}, current {current_yaml})"
        )

    recorded_pins = manifest.get("custom_repos_pins")
    if not isinstance(recorded_pins, dict):
        recorded_pins = {}
    current_pins = current_custom_repos_pins(spec)
    for namespace in sorted(set(recorded_pins) | set(current_pins)):
        recorded = recorded_pins.get(namespace)
        current = current_pins.get(namespace)
        if recorded != current:
            drift.append(
                f"custom_repos[{namespace!r}] pin changed since "
                f"concretization (recorded {recorded}, current {current})"
            )
    return drift
