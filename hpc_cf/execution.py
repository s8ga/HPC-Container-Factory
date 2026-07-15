"""Execution ports and injectable project paths.

Keeps domain code free of hard-wired ``PROJECT_ROOT`` / ``ASSETS_DIR`` reads
and lets tests supply a fake runner instead of Podman.

Default layout (:meth:`ProjectLayout.default`) should be created at the CLI /
service boundary and threaded through; domain helpers accept an optional
``layout`` and fall back lazily for backwards compatibility.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import logging
import shutil
import subprocess
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Protocol

logger = logging.getLogger(__name__)

# Retention for ``assets/spack-mirror/.hpc_cf/runs/*`` (run-scoped logs/manifests).
DEFAULT_MIRROR_RUN_KEEP = 30


class RunnerPort(Protocol):
    """Minimal container execution surface used by SpackOps / assets."""

    def exec(
        self,
        script: str,
        *,
        capture: bool = False,
        check: bool = True,
    ) -> subprocess.CompletedProcess: ...

    def run_ephemeral(
        self,
        script: str,
        *,
        capture: bool = False,
        check: bool = True,
    ) -> subprocess.CompletedProcess: ...


@dataclass(frozen=True)
class ProjectLayout:
    """Injectable filesystem roots for the factory.

    Defaults match :mod:`hpc_cf.config` so production callers can use
    :meth:`default` without threading paths through every call.
    """

    project_root: Path

    @classmethod
    def default(cls) -> ProjectLayout:
        from hpc_cf.config import PROJECT_ROOT

        return cls(project_root=PROJECT_ROOT)

    @property
    def assets_dir(self) -> Path:
        return self.project_root / "assets"

    @property
    def spack_envs_dir(self) -> Path:
        return self.project_root / "spack-envs"

    @property
    def templates_dir(self) -> Path:
        return self.project_root / "templates"

    @property
    def containers_dir(self) -> Path:
        return self.project_root / "containers"

    @property
    def artifacts_dir(self) -> Path:
        return self.project_root / "artifacts"

    @property
    def spack_mirror_dir(self) -> Path:
        """Shared joint source-mirror root (layout stays cumulative)."""
        return self.assets_dir / "spack-mirror"

    @property
    def mirror_meta_dir(self) -> Path:
        """Metadata beside the shared cache; must not alter package trees."""
        return self.spack_mirror_dir / ".hpc_cf"

    @property
    def mirror_lock_path(self) -> Path:
        return self.mirror_meta_dir / "mirror.lock"

    @property
    def mirror_runs_dir(self) -> Path:
        return self.mirror_meta_dir / "runs"

    def bootstrap_dir(self, spack_version: str) -> Path:
        """Canonical ``assets/bootstrap-<version>`` path for *spack_version*."""
        return self.assets_dir / f"bootstrap-{spack_version}"

    def find_bootstrap_dir(self, spack_version: str | None = None) -> Path | None:
        """Return an ``assets/bootstrap-*`` directory, or None.

        When *spack_version* is given, prefer the exact match; otherwise (or if
        that path is missing) fall back to the first sorted ``bootstrap-*`` dir.
        """
        assets = self.assets_dir
        if not assets.is_dir():
            return None
        if spack_version:
            exact = self.bootstrap_dir(spack_version)
            if exact.is_dir():
                return exact
        for d in sorted(assets.iterdir()):
            if d.is_dir() and d.name.startswith("bootstrap-"):
                return d
        return None

    def resolve_env_paths(self, env_name: str) -> tuple[Path, Path]:
        """Return ``(host_env_dir, container_env_dir)`` under this layout.

        Handles the ``spack-env-file/`` subdirectory convention.
        """
        host_dir = self.spack_envs_dir / env_name
        container_dir = Path(f"/work/spack-envs/{env_name}")

        if (host_dir / "spack-env-file" / "env.yaml").exists() or (
            host_dir / "spack-env-file" / "spack.yaml"
        ).exists():
            host_dir = host_dir / "spack-env-file"
            container_dir = container_dir / "spack-env-file"

        return host_dir, container_dir

    def list_env_names(self) -> list[str]:
        """Environment directory names under ``spack-envs/`` that have env.yaml."""
        envs: list[str] = []
        root = self.spack_envs_dir
        if not root.exists():
            return envs
        for d in sorted(root.iterdir()):
            if d.is_dir() and (
                (d / "spack-env-file" / "env.yaml").exists()
                or (d / "env.yaml").exists()
            ):
                envs.append(d.name)
        return envs

    def mirror_builder_dockerfile(self) -> Path:
        return self.containers_dir / "Dockerfile.mirror-builder"

    def container_mirror_dir(self) -> str:
        return "/work/assets/spack-mirror"

    def container_run_dir(self, run_id: str) -> str:
        return f"/work/assets/spack-mirror/.hpc_cf/runs/{run_id}"


@dataclass(frozen=True)
class MirrorRun:
    """One assets/mirror invocation under the shared cache."""

    run_id: str
    host_dir: Path
    container_dir: str

    @property
    def create_log_container(self) -> str:
        return f"{self.container_dir}/mirror-create.log"

    @property
    def verify_log_container(self) -> str:
        return f"{self.container_dir}/mirror-verify.log"

    @property
    def manifest_path(self) -> Path:
        return self.host_dir / "manifest.json"


class SharedMirrorStore:
    """Process-level mutex + per-run dirs/manifests for ``assets/spack-mirror``.

    Package blobs remain in the shared joint cache; orchestration metadata
    lives under ``.hpc_cf/`` so existing bind-mount consumers stay compatible.
    """

    def __init__(self, layout: ProjectLayout) -> None:
        self.layout = layout

    @contextmanager
    def exclusive_write(self) -> Iterator[None]:
        """Serialize writers across processes (fcntl flock)."""
        lock_path = self.layout.mirror_lock_path
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with open(lock_path, "a+", encoding="utf-8") as lock_file:
            logger.debug("Acquiring shared mirror lock: %s", lock_path)
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
                logger.debug("Released shared mirror lock: %s", lock_path)

    def begin_run(self, env_name: str) -> MirrorRun:
        """Create a unique host+container log directory for this run."""
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        safe_env = env_name.replace("/", "_")
        run_id = f"{stamp}-{safe_env}-{uuid.uuid4().hex[:8]}"
        host_dir = self.layout.mirror_runs_dir / run_id
        host_dir.mkdir(parents=True, exist_ok=True)
        removed = self.cleanup_runs(keep=DEFAULT_MIRROR_RUN_KEEP)
        if removed:
            logger.info(
                "Pruned %d old mirror run dir(s) (keep=%d)",
                removed,
                DEFAULT_MIRROR_RUN_KEEP,
            )
        return MirrorRun(
            run_id=run_id,
            host_dir=host_dir,
            container_dir=self.layout.container_run_dir(run_id),
        )

    def cleanup_runs(self, *, keep: int = DEFAULT_MIRROR_RUN_KEEP) -> int:
        """Delete oldest run directories beyond *keep*.

        Run IDs are timestamp-prefixed, so lexicographic order matches age.
        Returns the number of directories removed. Safe to call while holding
        the mirror lock; never removes more than needed to satisfy *keep*.
        """
        if keep < 0:
            raise ValueError(f"keep must be >= 0, got {keep}")
        runs_dir = self.layout.mirror_runs_dir
        if not runs_dir.is_dir():
            return 0
        runs = sorted(p for p in runs_dir.iterdir() if p.is_dir())
        excess = len(runs) - keep
        if excess <= 0:
            return 0
        removed = 0
        for old in runs[:excess]:
            logger.debug("Removing old mirror run dir: %s", old)
            shutil.rmtree(old, ignore_errors=True)
            removed += 1
        return removed

    def write_manifest(
        self,
        run: MirrorRun,
        *,
        env_name: str,
        spack_version: str,
        lock_path: Path | None,
        stats: dict[str, int],
        status: str = "success",
        error: str | None = None,
    ) -> Path:
        """Persist env / spack version / lock hash / stats / status for this run.

        *status* is typically ``success`` or ``failed`` so run-scoped logs remain
        auditable after the process exits.
        """
        lock_hash = ""
        if lock_path is not None and lock_path.is_file():
            lock_hash = hashlib.sha256(lock_path.read_bytes()).hexdigest()
        payload: dict[str, object] = {
            "env": env_name,
            "spack_version": spack_version,
            "lock_hash": lock_hash,
            "stats": {
                "present": stats.get("present", -1),
                "added": stats.get("added", -1),
                "failed": stats.get("failed", -1),
            },
            "run_id": run.run_id,
            "status": status,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        if error:
            payload["error"] = error
        path = run.manifest_path
        path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        logger.info("Wrote mirror manifest (%s): %s", status, path)
        return path
