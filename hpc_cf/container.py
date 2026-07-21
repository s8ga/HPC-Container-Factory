"""Container lifecycle — Podman implementation of :class:`~hpc_cf.execution.RunnerPort`.

Replaces the container-management portions of:
  - ``scripts/build-mirror-in-container.sh`` (cmd_image, cmd_create_container,
    run_in_container, cmd_status)
  - ``scripts/prepare-bootstrap-cache.sh`` (_podman_run)
"""

from __future__ import annotations

import hashlib
import json
import logging
import shlex
import subprocess
from collections import deque
from pathlib import Path

from hpc_cf.execution import ProjectLayout

logger = logging.getLogger(__name__)

# Labels stamped on the persistent mirror-worker so create() can detect stale
# reuse after image rebuilds, project-root moves, or option changes.
WORKER_LABEL_IMAGE_ID = "hpc_cf.image_id"
WORKER_LABEL_FINGERPRINT = "hpc_cf.fingerprint"

# Streaming keeps only a bounded tail in memory (full log optional via file).
STREAM_TAIL_MAX_BYTES = 64 * 1024
# CalledProcessError.output is capped further so failure messages stay small.
STREAM_ERROR_TAIL_BYTES = 1024


class Container:
    """Podman-backed runner implementing ``exec`` / ``run_ephemeral``.

    Satisfies :class:`~hpc_cf.execution.RunnerPort` structurally.

    Parameters
    ----------
    name:
        Container name (e.g. ``"hpc-mirror-builder-work"``).
    image:
        Container image tag (e.g. ``"hpc-mirror-builder"``).
    project_root:
        Host path that is bind-mounted at ``/work`` inside the container.
        Defaults to :meth:`ProjectLayout.default` ``.project_root``.
    podman_cmd:
        Podman executable (default ``"podman"``).
    extra_opts:
        Additional podman run/create options (e.g. ``["--dns=8.8.8.8"]``).
    stream_log_path:
        When set, streaming mode appends every output line to this file while
        still keeping only a bounded in-memory tail.
    """

    def __init__(
        self,
        name: str,
        image: str,
        project_root: Path | None = None,
        podman_cmd: str = "podman",
        extra_opts: list[str] | None = None,
        stream_log_path: Path | None = None,
    ) -> None:
        self.name = name
        self.image = image
        self.project_root = project_root or ProjectLayout.default().project_root
        self.podman_cmd = podman_cmd
        self.extra_opts = extra_opts or []
        self.stream_log_path = stream_log_path

    # ── Properties ────────────────────────────────────────────────────────

    @property
    def image_exists(self) -> bool:
        result = self._run(
            ["image", "exists", self.image],
            capture=True, check=False,
        )
        return result.returncode == 0

    @property
    def container_exists(self) -> bool:
        result = self._run(
            ["container", "exists", self.name],
            capture=True, check=False,
        )
        return result.returncode == 0

    @property
    def is_running(self) -> bool:
        result = self._run(
            ["ps", "-q", "--filter", f"name={self.name}"],
            capture=True, check=False,
        )
        return bool(result.stdout.strip())

    @property
    def network_mode(self) -> str:
        result = self._run(
            ["inspect", "-f", "{{.HostConfig.NetworkMode}}", self.name],
            capture=True, check=False,
        )
        return result.stdout.strip() or "unknown"

    # ── Image management ─────────────────────────────────────────────────

    def build_image(self, dockerfile: Path) -> None:
        """Build the mirror-builder container image."""
        logger.info("Building mirror-builder image: %s", self.image)
        if not dockerfile.exists():
            raise FileNotFoundError(f"Dockerfile not found: {dockerfile}")

        self._run(
            ["build", "--network=host", "-t", self.image,
             "-f", str(dockerfile), str(self.project_root)],
        )
        self._run(["images", self.image, "--format",
                    "table {{.Repository}}\t{{.Tag}}\t{{.Size}}\t{{.CreatedAt}}"])
        logger.info("Image built: %s", self.image)

    def ensure_image(self, dockerfile: Path) -> None:
        """Build the image if it doesn't already exist."""
        if self.image_exists:
            logger.info("Image already exists: %s", self.image)
            return
        self.build_image(dockerfile)

    # ── Container lifecycle ───────────────────────────────────────────────

    def create(self) -> None:
        """Create and start a persistent mirror-worker container.

        Reuses an existing worker only when its ``hpc_cf.*`` labels match the
        current image Id and mount/network fingerprint; otherwise recreates.
        """
        logger.info("Ensuring container exists: %s", self.name)

        # Ensure image exists
        if not self.image_exists:
            dockerfile = self.project_root / "containers" / "Dockerfile.mirror-builder"
            self.build_image(dockerfile)

        expected_image_id = self._resolve_image_id()
        expected_fingerprint = self._worker_fingerprint()

        if self.container_exists:
            if self._worker_matches(expected_image_id, expected_fingerprint):
                if self.is_running:
                    logger.info("Container already running: %s", self.name)
                    self._ps_table()
                    return
                logger.info("Starting existing container: %s", self.name)
                self._run(["start", self.name])
                self._ps_table()
                return
            logger.warning(
                "Existing container %s fingerprint/image mismatch; recreating",
                self.name,
            )
            self.destroy()

        logger.info("Creating new container: %s", self.name)
        cmd = [
            "create",
            "--name", self.name,
            "--label", f"{WORKER_LABEL_IMAGE_ID}={expected_image_id}",
            "--label", f"{WORKER_LABEL_FINGERPRINT}={expected_fingerprint}",
            *_common_run_args(self),
            "bash", "-lc", "mkdir -p /tmp/home && tail -f /dev/null",
        ]
        self._run(cmd)
        self._run(["start", self.name])
        logger.info("Container created and started")
        self._ps_table()

    def destroy(self) -> None:
        """Stop and remove the container."""
        self._run(["rm", "-f", self.name], capture=True, check=False)

    # ── Execution ─────────────────────────────────────────────────────────

    def exec(
        self,
        script: str,
        *,
        capture: bool = False,
        check: bool = True,
    ) -> subprocess.CompletedProcess:
        """Execute a bash script inside the running container (``podman exec``)."""
        cmd = [
            "exec",
            self.name,
            "bash", "-lc", script,
        ]
        logger.debug("exec in container %s: %s", self.name, script[:200])
        return self._run(cmd, capture=capture, check=check)

    def run_ephemeral(
        self,
        script: str,
        *,
        capture: bool = False,
        check: bool = True,
    ) -> subprocess.CompletedProcess:
        """Run a one-off command in an ephemeral container (``podman run --rm``)."""
        cmd = [
            "run", "--rm",
            *_common_run_args(self),
            "bash", "-lc", f"mkdir -p /tmp/home && {script}",
        ]
        logger.debug("ephemeral run: %s", script[:200])
        return self._run(cmd, capture=capture, check=check)

    # ── Status display ────────────────────────────────────────────────────

    def status(
        self,
        *,
        bootstrap_dir: Path | None = None,
        mirror_dir: Path | None = None,
        env_name: str | None = None,
        spack_env_dir: Path | None = None,
    ) -> None:
        """Print a comprehensive status report (replaces ``cmd_status``)."""
        print("HPC-Container-Factory — Mirror Status")
        print("=======================================")
        print()

        # Container image
        print("📦 Container Image:")
        if self.image_exists:
            self._run(["images", self.image, "--format",
                        "  {{.Repository}}:{{.Tag}}  {{.Size}}  {{.CreatedAt}}"])
        else:
            print("  (not built)")
        print()

        # Mirror worker container
        print("🧱 Mirror Worker Container:")
        if self.container_exists:
            self._run(["ps", "-a", "--filter", f"name={self.name}",
                        "--format", "  {{.Names}}  {{.Status}}  {{.Image}}"])
        else:
            print("  (not created)")
        print()

        # Bootstrap mirror
        print("🔧 Bootstrap Mirror:")
        if bootstrap_dir and bootstrap_dir.exists() and any(bootstrap_dir.iterdir()):
            size = _du_sh(bootstrap_dir)
            print(f"  Path: {bootstrap_dir}")
            print(f"  Size: {size}")
            meta = bootstrap_dir / "metadata" / "sources" / "metadata.yaml"
            print(f"  Metadata: {'✓' if meta.exists() and meta.stat().st_size > 0 else '✗ (missing)'}")
        else:
            print("  (empty)")
        print()

        # Source mirror
        print("💿 Source Mirror:")
        if mirror_dir and mirror_dir.exists() and any(mirror_dir.iterdir()):
            size = _du_sh(mirror_dir)
            file_count = sum(1 for _ in mirror_dir.rglob("*") if _.is_file())
            print(f"  Path: {mirror_dir}")
            print(f"  Size: {size}")
            print(f"  Files: {file_count}")
            broken = _count_broken_symlinks(mirror_dir)
            if broken < 0:
                print("  Broken symlinks: (check failed)")
            else:
                print(f"  Broken symlinks: {broken} {'⚠' if broken else '✓'}")
        else:
            mirror_display = str(mirror_dir) if mirror_dir else "assets/spack-mirror"
            print(f"  ({mirror_display} — empty, run mirror command)")
        print()

        # Spack environment
        print("📋 Spack Environment:")
        print(f"  Name: {env_name or '(none)'}")
        print(f"  Path: {spack_env_dir or '(none)'}")
        if spack_env_dir:
            lock = spack_env_dir / "spack.lock"
            print(f"  spack.lock: {'✓' if lock.exists() else '✗'}")
        print()

    # ── Context manager ───────────────────────────────────────────────────

    def __enter__(self) -> Container:
        self.create()
        return self

    def __exit__(self, *exc) -> None:
        self.destroy()

    # ── Internal helpers ──────────────────────────────────────────────────

    def _run(
        self,
        args: list[str],
        *,
        capture: bool = False,
        check: bool = True,
    ) -> subprocess.CompletedProcess:
        cmd = [self.podman_cmd] + args
        logger.debug("podman: %s", shlex.join(cmd))

        if capture:
            # Buffered mode: quick commands whose stdout/stderr the caller
            # needs programmatic access to (image_exists, _parse_mirror_stats).
            return subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                check=check,
            )

        # Streaming mode: real-time line-by-line output via logger.
        # stderr is merged into stdout (stderr=STDOUT) to avoid pipe buffer
        # deadlock. podman and spack both emit progress info to stderr, so
        # the distinction is not meaningful for user-facing output.
        # In-memory retention is a bounded tail; optional stream_log_path
        # receives the full line stream.
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        tail = _BoundedLineBuffer(STREAM_TAIL_MAX_BYTES)
        log_file = None
        assert proc.stdout is not None  # guaranteed by stdout=PIPE
        try:
            if self.stream_log_path is not None:
                self.stream_log_path.parent.mkdir(parents=True, exist_ok=True)
                log_file = self.stream_log_path.open("a", encoding="utf-8")
            for line in iter(proc.stdout.readline, ""):
                stripped = line.rstrip("\n")
                if stripped:
                    tail.append(stripped)
                    logger.info("[podman] %s", stripped)
                    if log_file is not None:
                        log_file.write(stripped + "\n")
        finally:
            proc.stdout.close()
            if log_file is not None:
                log_file.close()
        returncode = proc.wait()

        output = tail.text()
        result = subprocess.CompletedProcess(cmd, returncode, output, "")
        if check and returncode != 0:
            error_output = _tail_bytes(output, STREAM_ERROR_TAIL_BYTES)
            raise subprocess.CalledProcessError(
                returncode, cmd, error_output, ""
            )
        return result

    def _ps_table(self) -> None:
        self._run(
            ["ps", "--filter", f"name={self.name}",
             "--format", "table {{.Names}}\t{{.Status}}\t{{.Image}}"],
        )

    def _resolve_image_id(self) -> str:
        """Return the local image Id for ``self.image`` (empty if missing)."""
        result = self._run(
            ["image", "inspect", "-f", "{{.Id}}", self.image],
            capture=True,
            check=False,
        )
        return (result.stdout or "").strip()

    def _worker_fingerprint(self) -> str:
        """Hash of mount/network/extra_opts that must match a reused worker."""
        payload = {
            "project_root": str(self.project_root.resolve()),
            "image": self.image,
            "network": "host",
            "userns": "keep-id",
            "home": "/tmp/home",
            "extra_opts": list(self.extra_opts),
        }
        blob = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()

    def _inspect_labels(self) -> dict[str, str]:
        result = self._run(
            ["inspect", "-f", "{{json .Config.Labels}}", self.name],
            capture=True,
            check=False,
        )
        raw = (result.stdout or "").strip()
        if not raw or raw == "null":
            return {}
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        if not isinstance(data, dict):
            return {}
        return {str(k): str(v) for k, v in data.items() if v is not None}

    def _worker_matches(self, image_id: str, fingerprint: str) -> bool:
        """True when an existing worker's labels match *image_id*/*fingerprint*."""
        if not image_id or not fingerprint:
            return False
        if self.network_mode != "host":
            return False
        labels = self._inspect_labels()
        return (
            labels.get(WORKER_LABEL_IMAGE_ID) == image_id
            and labels.get(WORKER_LABEL_FINGERPRINT) == fingerprint
        )


# ── Module-level helpers ──────────────────────────────────────────────────


class _BoundedLineBuffer:
    """Keep a byte-budgeted tail of streamed lines for CompletedProcess/errors."""

    def __init__(self, max_bytes: int) -> None:
        self._max_bytes = max(0, max_bytes)
        self._lines: deque[str] = deque()
        self._size = 0

    def append(self, line: str) -> None:
        encoded = len(line.encode("utf-8")) + 1  # account for rejoined newline
        self._lines.append(line)
        self._size += encoded
        while self._lines and self._size > self._max_bytes:
            old = self._lines.popleft()
            self._size -= len(old.encode("utf-8")) + 1

    def text(self) -> str:
        return "\n".join(self._lines)


def _tail_bytes(text: str, max_bytes: int) -> str:
    """Return the last *max_bytes* of *text* (UTF-8 safe, may drop a partial char)."""
    if max_bytes <= 0 or not text:
        return ""
    raw = text.encode("utf-8")
    if len(raw) <= max_bytes:
        return text
    chunk = raw[-max_bytes:]
    return chunk.decode("utf-8", errors="ignore")


def _extra_opts_for_create(opts: list[str]) -> list[str]:
    """Flatten and return extra podman options."""
    result: list[str] = []
    for item in opts:
        result.extend(shlex.split(item))
    return result


def _common_run_args(ctr: "Container") -> list[str]:
    """Shared podman create/run options (dedupe of create/run_ephemeral).

    The ``create`` and ``run_ephemeral`` paths used to repeat this identical
    option block; both now splat this helper.
    """
    return [
        *_extra_opts_for_create(ctr.extra_opts),
        "--network=host",
        "--userns=keep-id",
        "-e", "HOME=/tmp/home",
        "-v", f"{ctr.project_root}:/work:Z",
        ctr.image,
    ]


def _du_sh(path: Path) -> str:
    """Return human-readable directory size via ``du -sh``."""
    try:
        result = subprocess.run(
            ["du", "-sh", str(path)], capture_output=True, text=True, check=True,
        )
        return result.stdout.split()[0]
    except Exception as exc:
        logger.debug("du -sh failed for %s: %s", path, exc)
        return "?"


def _count_broken_symlinks(path: Path) -> int:
    """Count broken symlinks under *path*.

    Returns -1 if the check itself failed, so callers can distinguish
    "couldn't tell" from "0 broken".
    """
    try:
        result = subprocess.run(
            ["find", "-L", str(path), "-type", "l"],
            capture_output=True, text=True, check=False,
        )
        if result.returncode != 0:
            err = (result.stderr or "").strip() or (result.stdout or "").strip()
            logger.warning(
                "broken-symlink check failed for %s (rc=%s): %s",
                path,
                result.returncode,
                err or "(no stderr)",
            )
            return -1
        lines = [ln for ln in result.stdout.strip().splitlines() if ln]
        return len(lines)
    except Exception as exc:
        logger.warning("broken-symlink check failed for %s: %s", path, exc)
        return -1
