"""Request/service orchestration facade.

CLI and tests import request types and services from this module. Concrete
modules:

- :mod:`hpc_cf.requests` — request DTOs + argparse mappers
- :mod:`hpc_cf.buildcache_workflow` — buildcache state machine
- this module — :class:`AssetsService` / :class:`BuildService`

Domain steps live in ``assets`` / ``template`` / ``sif`` / ``buildcache``.
"""

from __future__ import annotations

import logging

from hpc_cf.buildcache_workflow import BuildcacheService
from hpc_cf.environment import BuildcachePolicy
from hpc_cf.execution import ProjectLayout, SharedBuildcacheStore, SharedMirrorStore
from hpc_cf.requests import (
    AssetsRequest,
    BuildRequest,
    BuildcacheRequest,
    assets_request_from_args,
    build_request_from_args,
    buildcache_request_from_args,
    profile_for_assets_action,
)
from hpc_cf.validation import ValidationProfile

logger = logging.getLogger(__name__)

__all__ = [
    "AssetsRequest",
    "AssetsService",
    "BuildRequest",
    "BuildService",
    "BuildcacheRequest",
    "BuildcacheService",
    "assets_request_from_args",
    "build_request_from_args",
    "buildcache_request_from_args",
    "profile_for_assets_action",
]


class AssetsService:
    """Orchestrate bootstrap / mirror / verify for one :class:`AssetsRequest`."""

    def __init__(self, layout: ProjectLayout | None = None) -> None:
        self.layout = layout or ProjectLayout.default()

    def run(self, request: AssetsRequest) -> None:
        # Top-level assets↔workflows cycle is gone (AssetsRequest lives in
        # requests); keep the local import so tests can patch
        # ``hpc_cf.assets.run_assets``.
        from hpc_cf.assets import run_assets
        from hpc_cf.env import run_static_checks
        from hpc_cf.environment import load_environment_spec
        from hpc_cf.spack_ops import resolve_env_paths
        from hpc_cf.validation import is_nonempty_spack_lock

        # Single preflight site (run_assets must not re-validate).
        if request.env and request.env != "__LIST__":
            host_dir, _ = resolve_env_paths(request.env, layout=self.layout)
            try:
                spec = load_environment_spec(host_dir)
            except FileNotFoundError as exc:
                raise FileNotFoundError(
                    f"assets preflight aborted for {request.env!r}: {exc}"
                ) from exc
            if spec.method.requires_spack_assets:
                profile = profile_for_assets_action(request)
                run_static_checks(
                    host_dir,
                    spec,
                    profile=profile,
                    layout=self.layout,
                )
                action_flags = any(
                    (
                        request.prepare_bootstrap,
                        request.download_mirror,
                        request.verify_mirror,
                        request.create_container,
                        request.status,
                    )
                )
                default_workflow = not action_flags
                needs_lock = (
                    default_workflow
                    or request.download_mirror
                    or request.verify_mirror
                )
                may_create_lock = request.allow_concretize and (
                    default_workflow or request.download_mirror
                )
                lock_path = host_dir / "spack.lock"
                if (
                    needs_lock
                    and not may_create_lock
                    and not is_nonempty_spack_lock(lock_path)
                ):
                    raise FileNotFoundError(
                        f"assets preflight requires a non-empty {lock_path}; "
                        "run with --allow-concretize during mirror creation "
                        "to produce it"
                    )

        run_assets(request, layout=self.layout)


class BuildService:
    """Orchestrate Dockerfile render and optional image build."""

    def __init__(self, layout: ProjectLayout | None = None) -> None:
        self.layout = layout or ProjectLayout.default()

    def run(self, request: BuildRequest) -> int:
        from contextlib import nullcontext

        from hpc_cf.buildcache import (
            collect_environment_provenance,
            inspect_image_digest,
            producer_image_ref,
            require_coverage,
            resolve_consumer_policy,
            verify,
        )
        from hpc_cf.env import run_static_checks
        from hpc_cf.sif import build_docker_like
        from hpc_cf.template import (
            generate_dockerfile,
            resolve_build_input,
            resolve_image_and_tag,
        )

        resolved = resolve_build_input(
            request.app_version, request.template, layout=self.layout
        )
        spec = resolved.environment_spec
        requested_policy = request.buildcache or (
            spec.spack.buildcache.policy
            if spec is not None
            else BuildcachePolicy.NEVER
        )
        if (
            requested_policy is BuildcachePolicy.ONLY
            and request.allow_reconcretize
        ):
            raise ValueError(
                "--buildcache only cannot be combined with --allow-reconcretize"
            )
        store = SharedBuildcacheStore(self.layout)
        buildcache_enabled = bool(spec and spec.spack.buildcache.enabled)
        consumer_lock_path = resolved.environment_dir / "spack.lock"
        if not consumer_lock_path.is_file():
            consumer_lock_path = (
                resolved.environment_dir / "spack-env-file" / "spack.lock"
            )
        policy_lock = (
            consumer_lock_path if consumer_lock_path.is_file() else None
        )
        # dockerfile and build both use build-input so missing/empty
        # spack.lock fails closed unless --allow-reconcretize.
        run_static_checks(
            resolved.environment_dir,
            resolved.environment_spec,
            profile=ValidationProfile.BUILD_INPUT,
            layout=self.layout,
            allow_reconcretize=request.allow_reconcretize,
        )

        # For requested auto/only, hold the shared consumer lock before the
        # single policy resolve so health/coverage flips cannot upgrade a
        # never decision onto an unlocked build path.
        hold_consumer_lock = requested_policy in (
            BuildcachePolicy.AUTO,
            BuildcachePolicy.ONLY,
        )

        def _resolve_policy() -> BuildcachePolicy:
            return resolve_consumer_policy(
                requested_policy,
                store,
                enabled=buildcache_enabled,
                lock_path=policy_lock,
            )

        if request.render_only:
            lock = (
                store.consumer_lock() if hold_consumer_lock else nullcontext()
            )
            with lock:
                effective_policy = _resolve_policy()
                generate_dockerfile(
                    template=request.template,
                    app_version=request.app_version,
                    output=request.output,
                    use_mirror=request.use_mirror,
                    build_only=request.build_only,
                    layout=self.layout,
                    allow_reconcretize=request.allow_reconcretize,
                    buildcache_policy=effective_policy.value,
                )
            logger.info("Done")
            return 0

        resolved_image, resolved_tag = resolve_image_and_tag(
            app_version=request.app_version,
            template=request.template,
            image_arg=request.image,
            tag_arg=request.tag,
            layout=self.layout,
        )

        if request.engine not in ("podman", "docker"):
            raise ValueError(
                f"Unsupported build engine {request.engine!r}; "
                "use podman/docker, or `build-sif` for Apptainer SIF"
            )

        logger.info("Resolved image: %s:%s", resolved_image, resolved_tag)
        lock = store.consumer_lock() if hold_consumer_lock else nullcontext()
        # Hold mirror LOCK_SH for the whole OCI build when the Dockerfile will
        # bind-mount assets/spack-mirror, so assets writers cannot race it.
        mirror_lock = (
            SharedMirrorStore(self.layout).shared_read()
            if request.use_mirror
            else nullcontext()
        )
        with lock:
            with mirror_lock:
                effective_policy = _resolve_policy()
                dockerfile = generate_dockerfile(
                    template=request.template,
                    app_version=request.app_version,
                    output=request.output,
                    use_mirror=request.use_mirror,
                    build_only=request.build_only,
                    layout=self.layout,
                    allow_reconcretize=request.allow_reconcretize,
                    buildcache_policy=effective_policy.value,
                )
                if effective_policy is BuildcachePolicy.ONLY:
                    env_dir = resolved.environment_dir
                    lock_path = env_dir / "spack.lock"
                    if not lock_path.is_file():
                        lock_path = env_dir / "spack-env-file" / "spack.lock"
                    if spec is None:
                        raise RuntimeError(
                            "buildcache only requires an EnvironmentSpec"
                        )
                    producer_ref = producer_image_ref(resolved_image, resolved_tag)
                    environment_provenance = collect_environment_provenance(
                        lock_path,
                        resolved.environment_dir,
                    )
                    require_coverage(
                        self.layout,
                        lock_path,
                        spack_version=spec.spack.version,
                        builder_image=inspect_image_digest(
                            engine=request.engine,
                            image_ref=producer_ref,
                            layout=self.layout,
                        ),
                        padded_length=spec.spack.buildcache.padded_length,
                        environment_provenance=environment_provenance,
                    )
                    verify(
                        engine=request.engine,
                        image_ref=producer_ref,
                        env_name=spec.spack.env_name,
                        layout=self.layout,
                    )
                build_docker_like(
                    dockerfile=dockerfile,
                    image=resolved_image,
                    tag=resolved_tag,
                    engine=request.engine,
                    network_host=request.network_host,
                    build_args=list(request.build_args),
                    build_opts=list(request.build_opts),
                )
        logger.info("Done")
        return 0
