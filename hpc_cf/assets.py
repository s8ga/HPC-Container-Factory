"""Assets workflow — bootstrap, mirror, verify, status.

Orchestrates ``Container`` and ``SpackOps`` to replace:
  - ``scripts/build-mirror-in-container.sh`` (cmd_image, cmd_create_container,
    cmd_mirror, cmd_verify, cmd_status, cmd_all)
  - ``scripts/prepare-bootstrap-cache.sh``
  - ``scripts/spack-common.sh`` (streamline_dispatch)

The CLI builds an :class:`~hpc_cf.requests.AssetsRequest` and calls
:class:`~hpc_cf.workflows.AssetsService`; this module owns the domain steps.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path

import yaml

from hpc_cf.container import Container
from hpc_cf.env import list_available_envs, spack_version_for_env
from hpc_cf.environment import (
    RESOLVED_REPO_PINS_FILENAME,
    EnvironmentSpec,
    SpackConfig,
    load_environment_spec,
)
from hpc_cf.execution import ProjectLayout, SharedMirrorStore
from hpc_cf.mirror_prune import (
    FETCH_SPECS_FILENAME,
    PRUNE_REPORT_FILENAME,
    apply_git_prune_plan,
    plan_git_side_prune,
)
from hpc_cf.requests import AssetsRequest
from hpc_cf.spack_ops import SpackOps, resolve_env_paths
from hpc_cf.template import detect_non_host_network
from hpc_cf.validation import BootstrapContract, is_nonempty_spack_lock

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
) -> tuple[EnvironmentSpec, SpackOps]:
    """Load env.yaml and create a SpackOps for the given environment."""
    host_dir, _ = resolve_env_paths(env_name, layout=layout)
    env_config = load_environment_spec(host_dir)
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

    Uses a minimal EnvironmentSpec with just the version to drive
    ``SpackOps.bootstrap_mirror()``.
    """
    env_config = EnvironmentSpec(
        spack=SpackConfig(
            version=spack_version,
            env_name="bootstrap-env",
        )
    )
    ops = SpackOps(env_config, ctr, layout=layout)
    ops.bootstrap_mirror(force=force)


# ── Mirror / verify ─────────────────────────────────────────────────────


def _write_resolved_repo_sidecar(ops: SpackOps, host_dir: Path) -> None:
    """Record floating-repo resolved tips next to spack.yaml (run output).

    Same two-stage doctrine as the lock: assets produces the record,
    builds consume it read-only (apply_resolved_repo_pins) so the
    image-side clone matches the sha the concretizer saw. Pinned repos
    are already their own record in env.yaml and never appear here.
    """
    if not ops.resolved_repo_pins:
        return
    repos_meta: dict[str, dict[str, str]] = {}
    for repo in ops.env.spack.custom_repos:
        sha = ops.resolved_repo_pins.get(repo.namespace)
        if sha is None:
            continue
        entry: dict[str, str] = {"commit": sha}
        if repo.url:
            entry["url"] = repo.url
        if repo.branch:
            entry["branch"] = repo.branch
        repos_meta[repo.namespace] = entry
    sidecar = host_dir / RESOLVED_REPO_PINS_FILENAME
    sidecar.write_text(
        yaml.safe_dump(
            {"schema_version": 1, "repos": repos_meta}, sort_keys=True
        ),
        encoding="utf-8",
    )
    logger.info(
        "Resolved repo pins recorded in %s (%s)",
        sidecar,
        ", ".join(sorted(repos_meta)),
    )


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

            _write_resolved_repo_sidecar(ops, host_dir)

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
        invalid = BootstrapContract.invalid_paths(bootstrap_dir)
        if not invalid:
            logger.info("Bootstrap metadata complete under %s", bootstrap_dir)
        else:
            logger.info(
                "Bootstrap cache incomplete under %s (missing/empty: %s); "
                "run --prepare-bootstrap to create",
                bootstrap_dir,
                ", ".join(str(path) for path in invalid),
            )
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


def run_mirror_prune(
    ctr: Container,
    env_name: str,
    *,
    apply_plan: bool,
    fresh_after: float,
    layout: ProjectLayout | None = None,
) -> None:
    """Prune the mirror's git cache down to this run's lock membership.

    ``fresh_after`` must be a timestamp captured right before this run's
    ``spack mirror create`` — blobs newer than it were fetched by this
    run (the branch-floating class relies on that signal; see
    :mod:`hpc_cf.mirror_prune`). Runs under the shared-mirror write lock:
    export fetch specs (container, same session as mirror create) → plan
    (pure) → apply (unless dry run). An empty or unparseable sidecar is a
    hard refusal: identity unknown means keep, but a silently-broken
    export must not disable pruning unnoticed.
    """
    layout = layout or ProjectLayout.default()
    mirror_host = layout.spack_mirror_dir
    sidecar_host = mirror_host / ".hpc_cf" / FETCH_SPECS_FILENAME
    sidecar_container = (
        f"{layout.container_mirror_dir()}/.hpc_cf/{FETCH_SPECS_FILENAME}"
    )

    _, ops = _make_spack_ops(env_name, ctr, layout=layout)
    store = SharedMirrorStore(layout)
    with store.exclusive_write():
        ops.export_fetch_specs(sidecar_container)
        if not sidecar_host.is_file() or sidecar_host.stat().st_size == 0:
            raise RuntimeError(
                f"fetch-spec sidecar missing after export: {sidecar_host}"
            )
        try:
            specs = json.loads(sidecar_host.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                f"fetch-spec sidecar is not valid JSON: {sidecar_host}"
            ) from exc
        if not isinstance(specs, list) or not specs:
            raise RuntimeError(
                f"fetch-spec sidecar is empty — recipe introspection "
                f"produced nothing; refusing to prune: {sidecar_host}"
            )

        plan = plan_git_side_prune(mirror_host, specs, fresh_after=fresh_after)
        for line in plan.summary:
            logger.info("mirror-prune: %s", line)

        report = {
            "applied": bool(apply_plan),
            "fresh_after": fresh_after,
            "freed_bytes": plan.freed_bytes,
            "delete_blobs": [str(p) for p in plan.delete_blobs],
            "delete_links": [str(p) for p in plan.delete_links],
            "remove_dirs": [str(p) for p in plan.remove_dirs],
            "guarded_dirs": list(plan.guarded_dirs),
        }
        report_host = mirror_host / ".hpc_cf" / PRUNE_REPORT_FILENAME
        report_host.parent.mkdir(parents=True, exist_ok=True)
        report_host.write_text(
            json.dumps(report, indent=2, sort_keys=True), encoding="utf-8"
        )

        if plan.is_noop:
            logger.info("mirror-prune: nothing to delete (steady state)")
        elif apply_plan:
            apply_git_prune_plan(mirror_host, plan)
            logger.info(
                "mirror-prune: applied — freed %.2f GiB from the git cache",
                plan.freed_bytes / 2**30,
            )
        else:
            logger.warning(
                "mirror-prune: DRY RUN — re-run with --prune-mirror-apply "
                "to delete %d blob(s) (%.2f GiB)",
                len(plan.delete_blobs),
                plan.freed_bytes / 2**30,
            )


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
            host_dir, _ = resolve_env_paths(request.env, layout=layout)
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

        # fresh_after anchors the branch-floating prune class to blobs
        # fetched by THIS run (restored seed content carries epoch-0 mtimes).
        mirror_started = time.time()
        run_mirror(
            ctr,
            request.env,
            layout=layout,
            allow_concretize=request.allow_concretize,
        )

        if not request.skip_verify:
            run_verify(ctr, request.env, layout=layout)

        if request.prune_mirror:
            run_mirror_prune(
                ctr,
                request.env,
                apply_plan=request.prune_apply,
                fresh_after=mirror_started,
                layout=layout,
            )
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
        mirror_started = time.time()
        run_mirror(
            ctr,
            request.env,
            layout=layout,
            allow_concretize=request.allow_concretize,
        )
        if request.prune_mirror:
            run_mirror_prune(
                ctr,
                request.env,
                apply_plan=request.prune_apply,
                fresh_after=mirror_started,
                layout=layout,
            )

    if request.verify_mirror:
        if not request.env:
            raise ValueError("--env is required with --verify-mirror")
        ensure_image_once()
        run_verify(ctr, request.env, layout=layout)
