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
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Mapping, Protocol

logger = logging.getLogger(__name__)

# Retention for ``assets/spack-mirror/.hpc_cf/runs/*`` (run-scoped logs/manifests).
DEFAULT_MIRROR_RUN_KEEP = 30

# How often ``exclusive_write`` re-logs while blocked on the mirror flock.
MIRROR_LOCK_WAIT_LOG_INTERVAL_S = 30.0
# Poll interval between nonblocking flock attempts while waiting.
MIRROR_LOCK_POLL_INTERVAL_S = 0.1


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
    def spack_buildcache_dir(self) -> Path:
        """Global opaque filesystem buildcache maintained only by Spack."""
        return self.assets_dir / "spack-buildcache"

    @property
    def buildcache_state_dir(self) -> Path:
        """Factory metadata beside, never inside, the Spack-owned store."""
        return self.assets_dir / "spack-buildcache-state"

    @property
    def buildcache_lock_path(self) -> Path:
        return self.buildcache_state_dir / "buildcache.lock"

    @property
    def buildcache_health_path(self) -> Path:
        return self.buildcache_state_dir / "health.json"

    @property
    def buildcache_coverage_dir(self) -> Path:
        return self.buildcache_state_dir / "coverage"

    @property
    def buildcache_runs_dir(self) -> Path:
        return self.buildcache_state_dir / "runs"

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

        When *spack_version* is given, only the exact ``bootstrap-<version>``
        directory is accepted — never silently fall back to another version.
        Without a version, return the first sorted ``bootstrap-*`` dir (status
        display convenience).
        """
        assets = self.assets_dir
        if not assets.is_dir():
            return None
        if spack_version:
            exact = self.bootstrap_dir(spack_version)
            return exact if exact.is_dir() else None
        for d in sorted(assets.iterdir()):
            if d.is_dir() and d.name.startswith("bootstrap-"):
                return d
        return None

    def resolve_env_paths(self, env_name: str) -> tuple[Path, Path]:
        """Return ``(host_env_dir, container_env_dir)`` under this layout.

        Handles the ``spack-env-file/`` subdirectory convention.
        Resolved host paths must stay under :attr:`project_root` so ``--env``
        values with ``..`` or absolute segments cannot escape the tree.
        """
        from hpc_cf.shell_quote import confine_to_root

        if not env_name or env_name != env_name.strip():
            raise ValueError(f"invalid --env name: {env_name!r}")
        # Single path component only — blocks ``../``, absolute, and nested names.
        env_leaf = Path(env_name).name
        if (
            env_leaf != env_name
            or env_leaf in (".", "..")
            or "/" in env_name
            or "\\" in env_name
        ):
            raise ValueError(
                f"--env must be a single directory name under spack-envs/, "
                f"got {env_name!r}"
            )

        # Resolve under spack-envs/ (implies project_root for the default layout).
        host_dir = confine_to_root(
            self.spack_envs_dir / env_leaf,
            root=self.spack_envs_dir,
            label="--env",
        )
        confine_to_root(host_dir, root=self.project_root, label="--env")
        container_dir = Path(f"/work/spack-envs/{env_leaf}")

        if (host_dir / "spack-env-file" / "env.yaml").exists() or (
            host_dir / "spack-env-file" / "spack.yaml"
        ).exists():
            host_dir = confine_to_root(
                host_dir / "spack-env-file",
                root=self.spack_envs_dir,
                label="--env",
            )
            confine_to_root(host_dir, root=self.project_root, label="--env")
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

    def container_buildcache_dir(self) -> str:
        """Read-only buildcache mount used by image consumers."""
        return "/opt/spack-buildcache"

    def container_publisher_buildcache_dir(self) -> str:
        """Read-write buildcache mount used only by dedicated publishers."""
        return "/work/assets/spack-buildcache"

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

    Locking is **writers-only** (``fcntl.LOCK_EX`` among assets mirror/verify
    writers). It does **not** exclude read-only consumers such as podman bind
    mounts of the mirror during image builds. Prefer a local filesystem for
    the shared mirror — NFS flock semantics can be weak or advisory-only.
    """

    def __init__(self, layout: ProjectLayout) -> None:
        self.layout = layout

    @contextmanager
    def exclusive_write(self) -> Iterator[None]:
        """Serialize writers across processes (fcntl flock).

        Tries nonblocking first; while blocked, logs at acquire start and
        about every :data:`MIRROR_LOCK_WAIT_LOG_INTERVAL_S` seconds.
        """
        lock_path = self.layout.mirror_lock_path
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with open(lock_path, "a+", encoding="utf-8") as lock_file:
            fd = lock_file.fileno()
            waited = False
            wait_started = time.monotonic()
            next_log_at = wait_started + MIRROR_LOCK_WAIT_LOG_INTERVAL_S
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    now = time.monotonic()
                    if not waited:
                        logger.info(
                            "Shared mirror lock busy (%s); waiting for other writer",
                            lock_path,
                        )
                        waited = True
                    if now >= next_log_at:
                        logger.info(
                            "Still waiting for shared mirror lock (%s) after %.0fs",
                            lock_path,
                            now - wait_started,
                        )
                        next_log_at = now + MIRROR_LOCK_WAIT_LOG_INTERVAL_S
                    time.sleep(MIRROR_LOCK_POLL_INTERVAL_S)
            if waited:
                logger.info(
                    "Acquired shared mirror lock after %.1fs: %s",
                    time.monotonic() - wait_started,
                    lock_path,
                )
            else:
                logger.debug("Acquired shared mirror lock: %s", lock_path)
            try:
                yield
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
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


@dataclass(frozen=True)
class BuildcacheRun:
    """Run-scoped sidecar location outside the opaque buildcache."""

    run_id: str
    host_dir: Path

    @property
    def provenance_path(self) -> Path:
        return self.host_dir / "provenance.json"

    def log_path(self, step: str) -> Path:
        safe_step = step.replace("/", "_")
        return self.host_dir / f"{safe_step}.log"


@dataclass(frozen=True)
class BuildcacheCoverageRecord:
    """Provenance and result of checking cacheable concrete specs.

    Coverage is deliberately fixed to non-external specs. External compilers
    and runtime packages are represented in a Spack DAG but are not pushed as
    binary packages.
    """

    spack_version: str
    builder_image_digest: str
    environment_provenance: Mapping[str, object]
    padded_length: int
    signing_policy: str
    check_returncode: int
    checked_spec_count: int


class SharedBuildcacheStore:
    """Host lock and sidecar state for one global Spack-owned buildcache.

    The buildcache root is opaque: this class may create the empty root for a
    publisher mount, but never lists, parses, copies, renames, or removes
    anything below it. Spack alone owns v3/blobs/index and future layouts.
    Factory state is kept in the sibling ``spack-buildcache-state`` directory.
    """

    def __init__(self, layout: ProjectLayout) -> None:
        self.layout = layout

    def ensure_store_root(self) -> Path:
        """Create only the mount root; never initialize internal layout."""
        self.layout.spack_buildcache_dir.mkdir(parents=True, exist_ok=True)
        self.layout.buildcache_state_dir.mkdir(parents=True, exist_ok=True)
        return self.layout.spack_buildcache_dir

    @contextmanager
    def _lock(self, mode: int) -> Iterator[None]:
        lock_path = self.layout.buildcache_lock_path
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with open(lock_path, "a+", encoding="utf-8") as lock_file:
            fcntl.flock(lock_file.fileno(), mode)
            try:
                yield
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    @contextmanager
    def publisher_lock(self) -> Iterator[None]:
        """Exclusive lock for push → update-index → check."""
        with self._lock(fcntl.LOCK_EX):
            yield

    @contextmanager
    def consumer_lock(self) -> Iterator[None]:
        """Shared lock held for an entire multi-stage OCI consumer build."""
        with self._lock(fcntl.LOCK_SH):
            yield

    def begin_run(self, env_name: str) -> BuildcacheRun:
        """Create a run sidecar directory without touching store internals."""
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        safe_env = env_name.replace("/", "_")
        run_id = f"{stamp}-{safe_env}-{uuid.uuid4().hex[:8]}"
        host_dir = self.layout.buildcache_runs_dir / run_id
        host_dir.mkdir(parents=True, exist_ok=False)
        return BuildcacheRun(run_id=run_id, host_dir=host_dir)

    def write_provenance(
        self,
        run: BuildcacheRun,
        provenance: Mapping[str, object],
    ) -> Path:
        """Write producer provenance to run-scoped sibling state."""
        payload = dict(provenance)
        payload["run_id"] = run.run_id
        payload["timestamp"] = datetime.now(timezone.utc).isoformat()
        self._write_json(run.provenance_path, payload)
        return run.provenance_path

    def mark_unhealthy(
        self,
        *,
        run_id: str,
        failed_step: str,
        error: str,
        recovery: Mapping[str, object] | None = None,
    ) -> Path:
        """Fail closed after any publisher push/index/check failure."""
        payload: dict[str, object] = {
            "healthy": False,
            "run_id": run_id,
            "failed_step": failed_step,
            "error": error,
        }
        if recovery:
            payload.update(recovery)
        return self._write_health(payload)

    def mark_healthy(self, *, run_id: str, coverage_path: Path) -> Path:
        """Mark the store healthy only after successful coverage checking."""
        return self._write_health(
            {
                "healthy": True,
                "run_id": run_id,
                "coverage_path": str(coverage_path),
            }
        )

    def _write_health(self, payload: dict[str, object]) -> Path:
        payload["timestamp"] = datetime.now(timezone.utc).isoformat()
        self._write_json(self.layout.buildcache_health_path, payload)
        return self.layout.buildcache_health_path

    def read_health(self) -> dict[str, Any]:
        """Read Factory health sidecar; never inspect Spack's index."""
        return json.loads(
            self.layout.buildcache_health_path.read_text(encoding="utf-8")
        )

    def write_coverage(
        self,
        *,
        lock_path: Path,
        record: BuildcacheCoverageRecord,
    ) -> Path:
        """Record check result keyed by lock SHA, not as a second index."""
        lock_sha256 = hashlib.sha256(lock_path.read_bytes()).hexdigest()
        path = self.layout.buildcache_coverage_dir / f"{lock_sha256}.json"
        payload: dict[str, object] = {
            "schema_version": 2,
            "lock_sha256": lock_sha256,
            "spack_version": record.spack_version,
            "builder_image_digest": record.builder_image_digest,
            "environment_provenance": dict(record.environment_provenance),
            "padded_length": record.padded_length,
            "signing_policy": record.signing_policy,
            "check_returncode": record.check_returncode,
            "checked_spec_count": record.checked_spec_count,
            "coverage": "non_external",
            "external_specs_excluded": True,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        self._write_json(path, payload)
        return path

    @staticmethod
    def _write_json(path: Path, payload: Mapping[str, object]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        temporary.write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        temporary.replace(path)
