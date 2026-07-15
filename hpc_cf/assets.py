"""Assets workflow — bootstrap, mirror, verify, status.

Orchestrates ``Container`` and ``SpackOps`` to replace:
  - ``scripts/build-mirror-in-container.sh`` (cmd_image, cmd_create_container,
    cmd_mirror, cmd_verify, cmd_status, cmd_all)
  - ``scripts/prepare-bootstrap-cache.sh``
  - ``scripts/spack-common.sh`` (streamline_dispatch)

The CLI builds an :class:`~hpc_cf.workflows.AssetsRequest` and calls
:class:`~hpc_cf.workflows.AssetsService`; this module owns the domain steps.
"""

from __future__ import annotations

import logging
from pathlib import Path

from hpc_cf.container import Container
from hpc_cf.env import list_available_envs, spack_version_for_env
from hpc_cf.execution import ProjectLayout, SharedMirrorStore
from hpc_cf.spack_ops import (
    EXPECTED_BOOTSTRAP_BINARIES,
    EnvConfig,
    SpackConfig,
    SpackOps,
    load_env_config,
    resolve_env_paths,
)
from hpc_cf.template import detect_non_host_network
from hpc_cf.validation import is_nonempty_spack_lock
from hpc_cf.workflows import AssetsRequest

logger = logging.getLogger(__name__)


# ── Internal helpers ─────────────────────────────────────────────────────


def _make_container(
    request: AssetsRequest,
    layout: ProjectLayout,
) -> Container:
    """Build a Podman Container from an assets request + layout."""
    return Container(
        name=request.container_name,
        image=request.mirror_image,
        project_root=layout.project_root,
        podman_cmd=request.podman_cmd,
        extra_opts=list(request.podman_opt),
    )


def _make_spack_ops(
    env_name: str,
    container: Container,
    *,
    layout: ProjectLayout | None = None,
) -> tuple[EnvConfig, SpackOps]:
    """Load env.yaml and create a SpackOps for the given environment."""
    host_dir, _ = resolve_env_paths(env_name, layout=layout)
    env_config = load_env_config(host_dir)
    ops = SpackOps(env_config, container, layout=layout)
    return env_config, ops


def find_bootstrap_dir(
    spack_version: str | None = None,
    *,
    layout: ProjectLayout | None = None,
) -> Path | None:
    """Return an ``assets/bootstrap-*`` directory, or None.

    When *spack_version* is given, only the exact
    ``assets/bootstrap-{spack_version}`` path is accepted (no silent
    fallback to another version). Without a version, fall back to the
    first sorted ``bootstrap-*`` directory for status display.
    """
    return (layout or ProjectLayout.default()).find_bootstrap_dir(spack_version)


# ── Image & container lifecycle ──────────────────────────────────────────


def ensure_image(ctr: Container, *, layout: ProjectLayout | None = None) -> None:
    """Build the mirror-builder image if it doesn't exist."""
    root = layout or ProjectLayout.default()
    dockerfile = root.mirror_builder_dockerfile()
    ctr.ensure_image(dockerfile)


# ── Bootstrap ────────────────────────────────────────────────────────────


def run_bootstrap(
    ctr: Container,
    *,
    spack_version: str,
    force: bool = False,
    layout: ProjectLayout | None = None,
) -> None:
    """Generate bootstrap mirror for the given Spack version.

    Uses a minimal EnvConfig with just the version to drive ``SpackOps.bootstrap_mirror()``.
    """
    env_config = EnvConfig(
        spack=SpackConfig(
            version=spack_version,
            env_name="bootstrap-env",
        )
    )
    ops = SpackOps(env_config, ctr, layout=layout)
    ops.bootstrap_mirror(force=force)


# ── Mirror / verify ─────────────────────────────────────────────────────


def run_mirror(
    ctr: Container,
    env_name: str,
    *,
    layout: ProjectLayout | None = None,
    allow_concretize: bool = False,
) -> dict[str, int]:
    """Generate source mirror for the given environment.

    Acquires the shared-mirror write lock, writes a run-scoped log directory
    and a manifest (env, spack version, lock hash, stats).

    Missing ``spack.lock`` fails closed unless *allow_concretize* is true
    (explicit concretize+mirror escape hatch — never silent upgrade to all).
    """
    layout = layout or ProjectLayout.default()
    host_dir, container_dir = resolve_env_paths(env_name, layout=layout)

    if not (host_dir / "spack.yaml").exists():
        raise FileNotFoundError(f"spack.yaml not found: {host_dir}/spack.yaml")

    env_config, ops = _make_spack_ops(env_name, ctr, layout=layout)
    mirror_dir = layout.container_mirror_dir()
    store = SharedMirrorStore(layout)
    lock_path = host_dir / "spack.lock"
    has_lock = is_nonempty_spack_lock(lock_path)

    with store.exclusive_write():
        run = store.begin_run(env_name)
        stats: dict[str, int] = {"present": -1, "added": -1, "failed": -1}
        try:
            if has_lock:
                stats = ops.run_mirror_pipeline(
                    str(container_dir),
                    mirror_dir,
                    create_log=run.create_log_container,
                )
            elif allow_concretize:
                logger.warning(
                    "spack.lock missing — --allow-concretize: running "
                    "concretize + mirror"
                )
                stats = ops.run_all_pipeline(
                    host_dir,
                    str(container_dir),
                    mirror_dir,
                    create_log=run.create_log_container,
                )
            else:
                raise FileNotFoundError(
                    f"spack.lock not found or empty under {host_dir}; "
                    "run assets with --allow-concretize to produce a lock, "
                    "or place a non-empty spack.lock before --download-mirror"
                )

            store.write_manifest(
                run,
                env_name=env_name,
                spack_version=env_config.spack.version,
                lock_path=lock_path,
                stats=stats,
                status="success",
            )
        except Exception as exc:
            try:
                store.write_manifest(
                    run,
                    env_name=env_name,
                    spack_version=env_config.spack.version,
                    lock_path=lock_path if has_lock else None,
                    stats=stats,
                    status="failed",
                    error=str(exc),
                )
            except Exception as write_exc:
                logger.warning(
                    "Failed to write mirror failure manifest: %s", write_exc
                )
            raise

    logger.info("Source mirror generated (run %s)", run.run_id)
    return stats


def run_verify(
    ctr: Container,
    env_name: str,
    *,
    layout: ProjectLayout | None = None,
) -> dict[str, int]:
    """Verify mirror completeness for the given environment.

    The full transaction (container verify → host symlink checks → atomic
    success/failure manifest) runs under the shared mirror write lock so
    concurrent writers cannot race with verification.
    """
    layout = layout or ProjectLayout.default()
    host_dir, container_dir = resolve_env_paths(env_name, layout=layout)

    lock_path = host_dir / "spack.lock"
    if not is_nonempty_spack_lock(lock_path):
        raise FileNotFoundError(
            f"spack.lock not found or empty under {host_dir}; "
            "run concretize or mirror first"
        )

    mirror_dir = layout.container_mirror_dir()
    env_config, ops = _make_spack_ops(env_name, ctr, layout=layout)
    store = SharedMirrorStore(layout)
    stats: dict[str, int] = {"present": -1, "added": -1, "failed": -1}

    with store.exclusive_write():
        run = store.begin_run(f"{env_name}-verify")
        try:
            stats = ops.run_verify_pipeline(
                str(container_dir),
                mirror_dir,
                verify_log=run.verify_log_container,
            )
            _verify_host_side(
                mirror_dir_host=layout.spack_mirror_dir,
                spack_version=env_config.spack.version,
                layout=layout,
            )
            store.write_manifest(
                run,
                env_name=env_name,
                spack_version=env_config.spack.version,
                lock_path=host_dir / "spack.lock",
                stats=stats,
                status="success",
            )
        except Exception as exc:
            try:
                store.write_manifest(
                    run,
                    env_name=env_name,
                    spack_version=env_config.spack.version,
                    lock_path=host_dir / "spack.lock",
                    stats=stats,
                    status="failed",
                    error=str(exc),
                )
            except Exception as write_exc:
                logger.warning(
                    "Failed to write verify failure manifest: %s", write_exc
                )
            raise

    logger.info("Verification complete (run %s)", run.run_id)
    return stats


def _verify_host_side(
    *,
    mirror_dir_host: Path,
    spack_version: str | None = None,
    layout: ProjectLayout | None = None,
) -> None:
    """Host-side structure checks after container-side verify.

    Result semantics for broken-symlink probing:
      - ``broken > 0`` → hard failure (``RuntimeError``)
      - ``broken < 0`` → hard failure (probe inconclusive; refuse success)
      - ``broken == 0`` → pass
    """
    from hpc_cf.container import _count_broken_symlinks

    broken = _count_broken_symlinks(mirror_dir_host) if mirror_dir_host.exists() else 0
    if broken < 0:
        raise RuntimeError(
            f"Broken-symlink check failed for {mirror_dir_host} "
            "(could not determine status; treating as verify failure)"
        )
    if broken > 0:
        logger.error("Broken symlinks found in mirror")
        raise RuntimeError(
            f"Broken symlinks found in mirror ({broken}): {mirror_dir_host}"
        )
    logger.info("No broken symlinks in mirror")

    # Bootstrap metadata (prefer version declared in env.yaml)
    bootstrap_dir = find_bootstrap_dir(spack_version, layout=layout)

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
    elif spack_version:
        logger.warning(
            "Bootstrap cache missing for requested version %s "
            "(assets/bootstrap-%s); status/verify will not pretend another "
            "version matches",
            spack_version,
            spack_version,
        )

    logger.info("All verification layers passed ✓")

# ── Status ──────────────────────────────────────────────────────────────


def run_status(
    ctr: Container,
    env_name: str,
    *,
    layout: ProjectLayout | None = None,
) -> None:
    """Print comprehensive status report."""
    layout = layout or ProjectLayout.default()
    host_dir, _ = resolve_env_paths(env_name, layout=layout)
    bootstrap_dir = find_bootstrap_dir(
        spack_version_for_env(env_name, layout=layout),
        layout=layout,
    )

    ctr.status(
        bootstrap_dir=bootstrap_dir,
        mirror_dir=layout.spack_mirror_dir,
        env_name=env_name,
        spack_env_dir=host_dir,
    )


# ── Main entry point ────────────────────────────────────────────────────


def run_assets(
    request: AssetsRequest,
    *,
    layout: ProjectLayout | None = None,
) -> None:
    """Top-level assets workflow — called by :class:`~hpc_cf.workflows.AssetsService`.

    Handles:
      - ``env == "__LIST__"`` → list environments
      - Default one-command workflow (image → container → bootstrap → mirror → verify)
      - Explicit action flags (create_container, prepare_bootstrap, etc.)
    """
    layout = layout or ProjectLayout.default()

    # Methods that do not require Spack assets (e.g. no_spack) skip entirely.
    # Validation preflight lives solely in AssetsService (profile by action).
    if request.env and request.env != "__LIST__":
        try:
            from hpc_cf.environment import load_environment_spec
            from hpc_cf.spack_ops import resolve_env_paths as _resolve

            host_dir, _ = _resolve(request.env, layout=layout)
            spec = load_environment_spec(host_dir)
            if not spec.method.requires_spack_assets:
                print(
                    f"Env '{request.env}' is method={spec.method.value} — "
                    f"no spack assets needed."
                )
                return
        except FileNotFoundError:
            pass

    non_host_mode = detect_non_host_network(list(request.podman_opt) or None)
    if non_host_mode:
        logger.warning(
            "Non-host network mode '%s' may prevent proxy access; prefer --network=host.",
            non_host_mode,
        )

    # --env without value → list available environments
    if request.env == "__LIST__":
        envs = list_available_envs(layout=layout)
        if envs:
            print("Available environments (--env <name>):")
            for e in envs:
                print(f"  {e}")
        else:
            print("No environments found under spack-envs/.")
        return

    ctr = _make_container(request, layout)
    image_ready = False

    def ensure_image_once() -> None:
        nonlocal image_ready
        if image_ready or request.skip_image_build:
            return
        ensure_image(ctr, layout=layout)
        image_ready = True

    action_flags = any(
        [
            request.prepare_bootstrap,
            request.download_mirror,
            request.verify_mirror,
            request.create_container,
            request.status,
        ]
    )

    # ── status ──
    if request.status:
        if not request.env:
            raise ValueError("--env is required for status")
        run_status(ctr, request.env, layout=layout)
        return

    # ── Default one-command workflow ──
    if not action_flags:
        if not request.env:
            raise ValueError("--env is required for default assets workflow")

        ensure_image_once()

        if not request.skip_create_container:
            ctr.create()

        spack_ver = spack_version_for_env(request.env, layout=layout)
        run_bootstrap(
            ctr,
            spack_version=spack_ver,
            force=request.force_bootstrap,
            layout=layout,
        )

        run_mirror(
            ctr,
            request.env,
            layout=layout,
            allow_concretize=request.allow_concretize,
        )

        if not request.skip_verify:
            run_verify(ctr, request.env, layout=layout)
        return

    # ── Explicit actions mode ──
    if request.create_container:
        ensure_image_once()
        ctr.create()

    if request.prepare_bootstrap:
        ensure_image_once()
        spack_ver = spack_version_for_env(request.env, layout=layout)
        run_bootstrap(
            ctr,
            spack_version=spack_ver,
            force=request.force_bootstrap,
            layout=layout,
        )

    if request.download_mirror:
        if not request.env:
            raise ValueError("--env is required with --download-mirror")
        ensure_image_once()
        run_mirror(
            ctr,
            request.env,
            layout=layout,
            allow_concretize=request.allow_concretize,
        )

    if request.verify_mirror:
        if not request.env:
            raise ValueError("--env is required with --verify-mirror")
        ensure_image_once()
        run_verify(ctr, request.env, layout=layout)
