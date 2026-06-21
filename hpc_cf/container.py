"""Container lifecycle — manages Podman containers for Spack operations.

Replaces the container-management portions of:
  - ``scripts/build-mirror-in-container.sh`` (cmd_image, cmd_create_container,
    run_in_container, cmd_status)
  - ``scripts/prepare-bootstrap-cache.sh`` (_podman_run)
"""

from __future__ import annotations

import logging
import shlex
import subprocess
from pathlib import Path

from hpc_cf.config import PROJECT_ROOT

logger = logging.getLogger(__name__)


class Container:
    """Manages a persistent or ephemeral Podman container for Spack operations.

    Parameters
    ----------
    name:
        Container name (e.g. ``"hpc-mirror-builder-work"``).
    image:
        Container image tag (e.g. ``"hpc-mirror-builder"``).
    project_root:
        Host path that is bind-mounted at ``/work`` inside the container.
    podman_cmd:
        Podman executable (default ``"podman"``).
    extra_opts:
        Additional podman run/create options (e.g. ``["--dns=8.8.8.8"]``).
    """

    def __init__(
        self,
        name: str,
        image: str,
        project_root: Path | None = None,
        podman_cmd: str = "podman",
        extra_opts: list[str] | None = None,
    ) -> None:
        self.name = name
        self.image = image
        self.project_root = project_root or PROJECT_ROOT
        self.podman_cmd = podman_cmd
        self.extra_opts = extra_opts or []

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
        """Create and start a persistent mirror-worker container."""
        logger.info("Ensuring container exists: %s", self.name)

        # Ensure image exists
        if not self.image_exists:
            dockerfile = self.project_root / "containers" / "Dockerfile.mirror-builder"
            self.build_image(dockerfile)

        if self.container_exists:
            net = self.network_mode
            if net != "host":
                logger.warning(
                    "Existing container network mode is '%s', recreating with --network=host",
                    net,
                )
                self.destroy()
            elif self.is_running:
                logger.info("Container already running: %s", self.name)
                self._ps_table()
                return
            else:
                logger.info("Starting existing container: %s", self.name)
                self._run(["start", self.name])
                self._ps_table()
                return

        logger.info("Creating new container: %s", self.name)
        cmd = [
            "create",
            "--name", self.name,
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
        return subprocess.run(
            cmd,
            capture_output=capture,
            text=True if capture else False,
            check=check,
        )

    def _ps_table(self) -> None:
        self._run(
            ["ps", "--filter", f"name={self.name}",
             "--format", "table {{.Names}}\t{{.Status}}\t{{.Image}}"],
        )


# ── Module-level helpers ──────────────────────────────────────────────────


def _extra_opts_for_create(opts: list[str]) -> list[str]:
    """Flatten and return extra podman options."""
    result: list[str] = []
    for item in opts:
        result.extend(shlex.split(item))
    return result


def _common_run_args(ctr: "Container") -> list[str]:
    """Shared podman create/run options (plan 3.6 dedupe).

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
    except Exception:
        return "?"


def _count_broken_symlinks(path: Path) -> int:
    """Count broken symlinks under *path*."""
    try:
        result = subprocess.run(
            ["find", "-L", str(path), "-type", "l"],
            capture_output=True, text=True, check=False,
        )
        lines = [ln for ln in result.stdout.strip().splitlines() if ln]
        return len(lines)
    except Exception:
        return 0
