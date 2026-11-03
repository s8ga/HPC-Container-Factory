"""Read-only overview command (``hpc_cf status``).

Operational state used to live in four places with no single view:
buildcache health/coverage/runs under assets/spack-buildcache-state/,
mirror runs under assets/spack-mirror/.hpc_cf/runs/, and per-env
lock/manifest/sidecar files (round-2 audit 2.9). This command only
READS — it invents no state and mutates nothing.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from hpc_cf.env import list_available_envs
from hpc_cf.environment import (
    RESOLVED_REPO_PINS_FILENAME,
    load_environment_spec,
    load_resolved_environment_spec,
)
from hpc_cf.execution import ProjectLayout
from hpc_cf.lock_manifest import LOCK_MANIFEST_FILENAME, compare_lock_inputs

logger = logging.getLogger(__name__)


def _lock_candidates(env_dir: Path) -> list[Path]:
    return [
        env_dir / "spack-env-file" / "spack.lock",
        env_dir / "spack.lock",
    ]


def _config_dir(env_dir: Path) -> Path:
    lock = next((p for p in _lock_candidates(env_dir) if p.is_file()), None)
    return lock.parent if lock else env_dir / "spack-env-file"


def _env_lock_state(env_dir: Path) -> str:
    lock = next((p for p in _lock_candidates(env_dir) if p.is_file()), None)
    if lock is None:
        return "no lock"
    conf = lock.parent
    manifest_path = conf / LOCK_MANIFEST_FILENAME
    if not manifest_path.is_file():
        return "lock, provenance unknown"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        # Same view builds compare against (resolved-repos.yaml applied).
        spec = load_resolved_environment_spec(env_dir)
        drift = compare_lock_inputs(manifest, spec, conf)
    except (ValueError, OSError):
        return "lock, unreadable manifest"
    if drift:
        return f"STALE ({len(drift)} input drift)"
    return "fresh"


def _latest_json_status(runs_dir: Path) -> str:
    """Newest run manifest's status field ('success'/'failed'/...)."""
    manifests = sorted(runs_dir.rglob("manifest.json"))
    if not manifests:
        return "-"
    try:
        data = json.loads(manifests[-1].read_text(encoding="utf-8"))
        return str(data.get("status", "unknown"))
    except (OSError, ValueError):
        return "unreadable"


def _buildcache_section(layout: ProjectLayout) -> list[str]:
    lines = ["buildcache:"]
    health_path = layout.buildcache_health_path
    if health_path.is_file():
        try:
            health = json.loads(health_path.read_text(encoding="utf-8"))
            lines.append(
                f"  health: {health.get('state', 'unknown')}"
                f" (updated {health.get('timestamp', '?')})"
            )
        except (OSError, ValueError):
            lines.append("  health: unreadable")
    else:
        lines.append("  health: no state file (never published here)")
    coverage = sorted(layout.buildcache_coverage_dir.glob("*.json"))
    lines.append(f"  coverage records: {len(coverage)}")
    runs = layout.buildcache_state_dir / "runs"
    run_dirs = [p for p in runs.iterdir() if p.is_dir()] if runs.is_dir() else []
    if run_dirs:
        lines.append(f"  runs retained: {len(run_dirs)} (latest: {sorted(run_dirs)[-1].name})")
    return lines


def _mirror_section(layout: ProjectLayout) -> list[str]:
    lines = ["source mirror:"]
    mirror = layout.spack_mirror_dir
    if not mirror.is_dir():
        lines.append("  absent (run `assets run --env <name>` to create)")
        return lines
    content = [entry for entry in mirror.iterdir() if entry.name != ".hpc_cf"]
    lines.append(f"  present ({len(content)} top-level entr{'y' if len(content) == 1 else 'ies'})")
    lines.append(f"  latest run: {_latest_json_status(layout.mirror_meta_dir / 'runs')}")
    return lines


def _envs_section(layout: ProjectLayout, env_filter: str | None) -> list[str]:
    envs = list_available_envs(layout=layout)
    if env_filter:
        envs = [name for name in envs if name == env_filter]
    lines = ["environments:"]
    if not envs:
        lines.append("  (none discovered)")
        return lines
    for name in envs:
        env_dir = layout.spack_envs_dir / name
        try:
            spec = load_environment_spec(env_dir)
            spack_ver = spec.spack.version
        except (ValueError, OSError):
            lines.append(f"  {name}: env.yaml unreadable")
            continue
        sidecar = "sidecar" if any(
            (env_dir / d / RESOLVED_REPO_PINS_FILENAME).is_file()
            for d in ("spack-env-file", ".")
        ) else ""
        suffix = f", {sidecar}" if sidecar else ""
        lines.append(
            f"  {name}: {_env_lock_state(env_dir)}, spack {spack_ver}{suffix}"
        )
    return lines


def run_status_command(
    *,
    env: str | None = None,
    layout: ProjectLayout | None = None,
) -> int:
    """Print the aggregate overview; returns a process exit code."""
    layout = layout or ProjectLayout.default()
    if env is not None and not (layout.spack_envs_dir / env).is_dir():
        logger.error("unknown env %r (no %s)", env, layout.spack_envs_dir / env)
        return 1
    for section in (
        _buildcache_section(layout),
        _mirror_section(layout),
        _envs_section(layout, env),
    ):
        print("\n".join(section))
    return 0
