"""Assets workflow — bootstrap, mirror, verify, status.

Orchestrates ``Container`` and ``SpackOps`` to replace:
  - ``scripts/build-mirror-in-container.sh`` (cmd_image, cmd_create_container,
    cmd_mirror, cmd_verify, cmd_status, cmd_all)
  - ``scripts/prepare-bootstrap-cache.sh``
  - ``scripts/spack-common.sh`` (streamline_dispatch)

This module is the *only* place that wires together Container + SpackOps
into user-facing workflows.  The CLI layer (``cli.py``) calls
:func:`run_assets` and nothing else from here.
"""

from __future__ import annotations

import argparse
import logging
from pathlib import Path

from hpc_cf.config import PROJECT_ROOT
from hpc_cf.container import Container
from hpc_cf.env import list_available_envs, spack_version_for_env
from hpc_cf.spack_ops import (
    EXPECTED_BOOTSTRAP_BINARIES,
    EnvConfig,
    SpackConfig,
    SpackOps,
    load_env_config,
    resolve_env_paths,
)
from hpc_cf.template import detect_non_host_network

logger = logging.getLogger(__name__)


# ── Internal helpers ─────────────────────────────────────────────────────


def _make_container(args: argparse.Namespace) -> Container:
    """Build a Container from the CLI args namespace."""
    return Container(
        name=args.container_name,
        image=args.mirror_image,
        project_root=PROJECT_ROOT,
        podman_cmd=args.podman_cmd,
        extra_opts=getattr(args, "podman_opt", None) or [],
    )


def _make_spack_ops(env_name: str, container: Container) -> tuple[EnvConfig, SpackOps]:
    """Load env.yaml and create a SpackOps for the given environment."""
    host_dir, _ = resolve_env_paths(env_name)
    env_config = load_env_config(host_dir)
    ops = SpackOps(env_config, container)
    return env_config, ops


def find_bootstrap_dir() -> Path | None:
    """Return the first ``assets/bootstrap-*`` directory, or None.

    Single resolver for both status reporting and host-side verification
    (previously the loop was duplicated in _verify_host_side and run_status;
    plan 3.6).
    """
    assets = PROJECT_ROOT / "assets"
    if not assets.is_dir():
        return None
    for d in sorted(assets.iterdir()):
        if d.is_dir() and d.name.startswith("bootstrap-"):
            return d
    return None


# ── Image & container lifecycle ──────────────────────────────────────────


def ensure_image(ctr: Container) -> None:
    """Build the mirror-builder image if it doesn't exist."""
    dockerfile = PROJECT_ROOT / "containers" / "Dockerfile.mirror-builder"
    ctr.ensure_image(dockerfile)


# ── Bootstrap ────────────────────────────────────────────────────────────


def run_bootstrap(ctr: Container, *, spack_version: str, force: bool = False) -> None:
    """Generate bootstrap mirror for the given Spack version.

    Uses a minimal EnvConfig with just the version to drive ``SpackOps.bootstrap_mirror()``.
    """
    env_config = EnvConfig(
        spack=SpackConfig(
            version=spack_version,
            env_name="bootstrap-env",
        )
    )
    ops = SpackOps(env_config, ctr)
    ops.bootstrap_mirror(force=force)


# ── Mirror / verify ─────────────────────────────────────────────────────


def run_mirror(ctr: Container, env_name: str) -> None:
    """Generate source mirror for the given environment."""
    host_dir, container_dir = resolve_env_paths(env_name)

    if not (host_dir / "spack.yaml").exists():
        raise FileNotFoundError(f"spack.yaml not found: {host_dir}/spack.yaml")

    # Determine mode: if spack.lock exists → mirror only, else → all (concretize + mirror)
    env_config, ops = _make_spack_ops(env_name, ctr)
    mirror_dir = "/work/assets/spack-mirror"

    if (host_dir / "spack.lock").exists():
        ops.run_mirror_pipeline(str(container_dir), mirror_dir)
    else:
        logger.warning("spack.lock NOT found — switching to 'all' mode (concretize + mirror)")
        ops.run_all_pipeline(host_dir, str(container_dir), mirror_dir)

    logger.info("Source mirror generated")


def run_verify(ctr: Container, env_name: str) -> None:
    """Verify mirror completeness for the given environment."""
    host_dir, container_dir = resolve_env_paths(env_name)

    if not (host_dir / "spack.lock").exists():
        raise FileNotFoundError("spack.lock not found — run concretize or mirror first")

    mirror_dir = "/work/assets/spack-mirror"
    env_config, ops = _make_spack_ops(env_name, ctr)
    ops.run_verify_pipeline(str(container_dir), mirror_dir)

    # Layer 2: host-side structure verification
    _verify_host_side(mirror_dir_host=PROJECT_ROOT / "assets" / "spack-mirror")

    logger.info("Verification complete")


def _verify_host_side(*, mirror_dir_host: Path) -> None:
    """Host-side structure checks after container-side verify."""
    from hpc_cf.container import _count_broken_symlinks

    layer2_ok = True

    # Broken symlinks
    broken = _count_broken_symlinks(mirror_dir_host) if mirror_dir_host.exists() else 0
    if broken:
        logger.error("Broken symlinks found in mirror")
        layer2_ok = False
    else:
        logger.info("No broken symlinks in mirror")

    # Bootstrap metadata
    bootstrap_dir = find_bootstrap_dir()

    if bootstrap_dir:
        metadata = bootstrap_dir / "metadata" / "sources" / "metadata.yaml"
        if metadata.exists() and metadata.stat().st_size > 0:
            logger.info("Bootstrap metadata exists and is non-empty")
        else:
            logger.info(
                "Bootstrap metadata not yet generated: %s "
                "(run --prepare-bootstrap to create)", metadata,
            )

        for name in EXPECTED_BOOTSTRAP_BINARIES:
            f = bootstrap_dir / "metadata" / "binaries" / f"{name}.json"
            if not f.exists() or f.stat().st_size == 0:
                logger.debug("Missing optional binary metadata: %s", f)

    if layer2_ok:
        logger.info("All verification layers passed ✓")
    else:
        logger.warning("Some structure checks failed (mirror may still be functional)")


# ── Status ──────────────────────────────────────────────────────────────


def run_status(ctr: Container, env_name: str) -> None:
    """Print comprehensive status report."""
    host_dir, _ = resolve_env_paths(env_name)
    bootstrap_dir = find_bootstrap_dir()

    ctr.status(
        bootstrap_dir=bootstrap_dir,
        mirror_dir=PROJECT_ROOT / "assets" / "spack-mirror",
        env_name=env_name,
        spack_env_dir=host_dir,
    )


# ── Main entry point ────────────────────────────────────────────────────


def run_assets(args: argparse.Namespace) -> None:
    """Top-level assets workflow — called by the CLI dispatcher.

    Handles:
      - ``--env`` without value → list environments
      - Default one-command workflow (image → container → bootstrap → mirror → verify)
      - Explicit action flags (``--create-container``, ``--prepare-bootstrap``, etc.)
    """
    non_host_mode = detect_non_host_network(getattr(args, "podman_opt", None))
    if non_host_mode:
        logger.warning(
            "Non-host network mode '%s' may prevent proxy access; prefer --network=host.",
            non_host_mode,
        )

    # --env without value → list available environments
    if args.env == "__LIST__":
        envs = list_available_envs()
        if envs:
            print("Available environments (--env <name>):")
            for e in envs:
                print(f"  {e}")
        else:
            print("No environments found under spack-envs/.")
        return

    ctr = _make_container(args)
    image_ready = False

    def ensure_image_once() -> None:
        nonlocal image_ready
        if image_ready or args.skip_image_build:
            return
        ensure_image(ctr)
        image_ready = True

    action_flags = any(
        [
            args.prepare_bootstrap,
            args.download_mirror,
            args.verify_mirror,
            args.create_container,
            args.status,
        ]
    )

    # ── status ──
    if args.status:
        if not args.env:
            raise ValueError("--env is required for status")
        run_status(ctr, args.env)
        return

    # ── Default one-command workflow ──
    if not action_flags:
        if not args.env:
            raise ValueError("--env is required for default assets workflow")

        ensure_image_once()

        if not args.skip_create_container:
            ctr.create()

        spack_ver = spack_version_for_env(args.env)
        run_bootstrap(ctr, spack_version=spack_ver, force=args.force_bootstrap)

        run_mirror(ctr, args.env)

        if not args.skip_verify:
            run_verify(ctr, args.env)
        return

    # ── Explicit actions mode ──
    if args.create_container:
        ensure_image_once()
        ctr.create()

    if args.prepare_bootstrap:
        ensure_image_once()
        spack_ver = spack_version_for_env(getattr(args, "env", None))
        run_bootstrap(ctr, spack_version=spack_ver, force=args.force_bootstrap)

    if args.download_mirror:
        if not args.env:
            raise ValueError("--env is required with --download-mirror")
        ensure_image_once()
        run_mirror(ctr, args.env)

    if args.verify_mirror:
        if not args.env:
            raise ValueError("--env is required with --verify-mirror")
        ensure_image_once()
        run_verify(ctr, args.env)
