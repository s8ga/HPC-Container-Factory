"""Request/service orchestration boundary.

CLI code should only assemble :class:`BuildRequest` / :class:`AssetsRequest`
and invoke the corresponding service — domain logic lives here and in
``assets`` / ``template`` / ``sif``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from hpc_cf.environment import BuildcachePolicy
from hpc_cf.execution import (
    BuildcacheCoverageRecord,
    ProjectLayout,
    SharedBuildcacheStore,
)
from hpc_cf.validation import ValidationProfile

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AssetsRequest:
    """Typed assets workflow input (no argparse types)."""

    env: str | None = None
    podman_cmd: str = "podman"
    podman_opt: tuple[str, ...] = ()
    mirror_image: str = "hpc-mirror-builder"
    container_name: str = "hpc-mirror-builder-work"
    skip_image_build: bool = False
    force_bootstrap: bool = False
    create_container: bool = False
    prepare_bootstrap: bool = False
    download_mirror: bool = False
    verify_mirror: bool = False
    status: bool = False
    skip_create_container: bool = False
    skip_verify: bool = False
    allow_concretize: bool = False


@dataclass(frozen=True)
class BuildRequest:
    """Typed dockerfile/build workflow input."""

    app_version: str | None = None
    template: Path | None = None
    output: Path = Path("Dockerfile")
    use_mirror: bool = True
    build_only: bool = False
    engine: str = "podman"
    image: str | None = None
    tag: str | None = None
    network_host: bool = False
    build_args: tuple[str, ...] = ()
    build_opts: tuple[str, ...] = ()
    render_only: bool = False
    allow_reconcretize: bool = False
    buildcache: BuildcachePolicy | None = None


@dataclass(frozen=True)
class BuildcacheRequest:
    """Typed input for buildcache build/verify/status."""

    action: str
    env: str | None = None
    engine: str = "podman"
    image: str | None = None
    tag: str | None = None
    network_host: bool = False
    build_args: tuple[str, ...] = ()
    build_opts: tuple[str, ...] = ()
    output_format: str = "text"
    operation_timeout_seconds: int = 24 * 60 * 60


def assets_request_from_args(args: Any) -> AssetsRequest:
    """Map a parsed CLI namespace onto :class:`AssetsRequest`."""
    return AssetsRequest(
        env=getattr(args, "env", None),
        podman_cmd=getattr(args, "podman_cmd", "podman") or "podman",
        podman_opt=tuple(getattr(args, "podman_opt", None) or ()),
        mirror_image=getattr(args, "mirror_image", "hpc-mirror-builder"),
        container_name=getattr(args, "container_name", "hpc-mirror-builder-work"),
        skip_image_build=bool(getattr(args, "skip_image_build", False)),
        force_bootstrap=bool(getattr(args, "force_bootstrap", False)),
        create_container=bool(getattr(args, "create_container", False)),
        prepare_bootstrap=bool(getattr(args, "prepare_bootstrap", False)),
        download_mirror=bool(getattr(args, "download_mirror", False)),
        verify_mirror=bool(getattr(args, "verify_mirror", False)),
        status=bool(getattr(args, "status", False)),
        skip_create_container=bool(getattr(args, "skip_create_container", False)),
        skip_verify=bool(getattr(args, "skip_verify", False)),
        allow_concretize=bool(getattr(args, "allow_concretize", False)),
    )


def build_request_from_args(
    args: Any,
    *,
    use_mirror: bool,
    render_only: bool = False,
) -> BuildRequest:
    """Map a parsed CLI namespace onto :class:`BuildRequest`."""
    return BuildRequest(
        app_version=getattr(args, "app_version", None),
        template=getattr(args, "template", None),
        output=getattr(args, "output", None) or Path("Dockerfile"),
        use_mirror=use_mirror,
        build_only=bool(getattr(args, "build_only", False)),
        engine=getattr(args, "engine", "podman") or "podman",
        image=getattr(args, "image", None),
        tag=getattr(args, "tag", None),
        network_host=bool(getattr(args, "network_host", False)),
        build_args=tuple(getattr(args, "build_arg", None) or ()),
        build_opts=tuple(getattr(args, "build_opt", None) or ()),
        render_only=render_only,
        allow_reconcretize=bool(getattr(args, "allow_reconcretize", False)),
        buildcache=getattr(args, "buildcache", None),
    )


def buildcache_request_from_args(args: Any) -> BuildcacheRequest:
    return BuildcacheRequest(
        action=args.buildcache_action,
        env=getattr(args, "env", None),
        engine=getattr(args, "engine", "podman"),
        image=getattr(args, "image", None),
        tag=getattr(args, "tag", None),
        network_host=bool(getattr(args, "network_host", False)),
        build_args=tuple(getattr(args, "build_arg", None) or ()),
        build_opts=tuple(getattr(args, "build_opt", None) or ()),
        output_format=getattr(args, "format", "text"),
        operation_timeout_seconds=getattr(
            args, "operation_timeout_seconds", 24 * 60 * 60
        ),
    )


def profile_for_assets_action(request: AssetsRequest) -> ValidationProfile:
    """Select validation profile from the assets action flags.

    * ``status`` — config only (no large-asset requirements)
    * ``prepare-bootstrap`` / ``download-mirror`` / ``verify-mirror`` /
      default workflow — assets inputs (tarball / bootstrap / lock)
    """
    if request.status and not any(
        (
            request.prepare_bootstrap,
            request.download_mirror,
            request.verify_mirror,
        )
    ):
        return ValidationProfile.CONFIG
    return ValidationProfile.ASSETS


class AssetsService:
    """Orchestrate bootstrap / mirror / verify for one :class:`AssetsRequest`."""

    def __init__(self, layout: ProjectLayout | None = None) -> None:
        self.layout = layout or ProjectLayout.default()

    def run(self, request: AssetsRequest) -> None:
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
        effective_policy = resolve_consumer_policy(
            requested_policy,
            store,
            enabled=buildcache_enabled,
            lock_path=policy_lock,
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

        if request.render_only:
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
        lock = (
            store.consumer_lock()
            if effective_policy in (BuildcachePolicy.AUTO, BuildcachePolicy.ONLY)
            else nullcontext()
        )
        with lock:
            # A publisher may have changed health after the initial policy
            # decision but before this consumer acquired its shared lock.
            # Resolve again under the lock before rendering any cache mounts.
            effective_policy = resolve_consumer_policy(
                requested_policy,
                store,
                enabled=buildcache_enabled,
                lock_path=policy_lock,
            )
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


class BuildcacheService:
    """Build, publish, verify, or report the global Spack buildcache."""

    def __init__(self, layout: ProjectLayout | None = None) -> None:
        self.layout = layout or ProjectLayout.default()

    @staticmethod
    def _failure_output(exc: BaseException) -> str | None:
        output = getattr(exc, "stdout", None) or getattr(exc, "output", None)
        if isinstance(output, bytes):
            return output.decode(errors="replace")
        return output if isinstance(output, str) and output else None

    def _record_failure(
        self,
        *,
        store: SharedBuildcacheStore,
        run: Any,
        failed_step: str,
        exc: BaseException,
        provenance: dict[str, object],
        recovery: dict[str, object] | None = None,
    ) -> None:
        output = self._failure_output(exc)
        if output:
            run.log_path(failed_step).write_text(output, encoding="utf-8")
        try:
            store.write_provenance(
                run,
                {
                    **provenance,
                    **(recovery or {}),
                    "failed_step": failed_step,
                    "error": str(exc),
                },
            )
        except Exception as provenance_exc:
            logger.warning(
                "Could not write failed buildcache provenance for %s: %s",
                run.run_id,
                provenance_exc,
            )
        store.mark_unhealthy(
            run_id=run.run_id,
            failed_step=failed_step,
            error=str(exc),
            recovery=recovery,
        )

    def _complete_producer(
        self,
        *,
        store: SharedBuildcacheStore,
        run: Any,
        request: BuildcacheRequest,
        spec: Any,
        lock_path: Path,
        environment_provenance: dict[str, object],
        temporary_image_ref: str,
        image_digest: str,
        stable_image_ref: str,
        provenance: dict[str, object],
    ) -> None:
        from hpc_cf.buildcache import (
            failed_publish_output,
            failed_publish_step,
            inspect_image_digest,
            promote_producer_image,
            publish,
            publish_output_markers,
        )

        recovery: dict[str, object] = {
            "recoverable": True,
            "env": request.env,
            "lock_sha256": provenance["lock_sha256"],
            "spack_version": spec.spack.version,
            "recovery_image_ref": temporary_image_ref,
            "recovery_image_digest": image_digest,
            "stable_image_ref": stable_image_ref,
        }
        failed_step = "publish"
        try:
            try:
                result, checked_count = publish(
                    engine=request.engine,
                    image_ref=temporary_image_ref,
                    env_name=spec.spack.env_name,
                    layout=self.layout,
                    timeout_seconds=request.operation_timeout_seconds,
                )
            except Exception as exc:
                failed_step = failed_publish_step(exc)
                markers = publish_output_markers(failed_publish_output(exc))
                recovery.update(markers)
                # Push/index may have succeeded before check failed; keep the
                # image recoverable and never discard already-published binaries.
                if failed_step in {"update-index", "check"} or markers.get(
                    "pushed_spec_count"
                ):
                    recovery["partial_publish"] = bool(
                        markers.get("partial_publish")
                        or failed_step == "check"
                    )
                raise
            run.log_path("publish").write_text(
                result.stdout or "", encoding="utf-8"
            )
            markers = publish_output_markers(result.stdout or "")
            failed_step = "promote"
            promote_producer_image(
                engine=request.engine,
                temporary_ref=temporary_image_ref,
                stable_ref=stable_image_ref,
                layout=self.layout,
            )
            promoted_digest = inspect_image_digest(
                engine=request.engine,
                image_ref=stable_image_ref,
                layout=self.layout,
            )
            if promoted_digest != image_digest:
                raise RuntimeError(
                    "stable producer digest changed during promotion"
                )
            failed_step = "coverage"
            coverage_path = store.write_coverage(
                lock_path=lock_path,
                record=BuildcacheCoverageRecord(
                    spack_version=spec.spack.version,
                    builder_image_digest=image_digest,
                    environment_provenance=environment_provenance,
                    padded_length=spec.spack.buildcache.padded_length,
                    signing_policy="unsigned",
                    check_returncode=0,
                    checked_spec_count=checked_count,
                ),
            )
            failed_step = "provenance"
            store.write_provenance(
                run,
                {
                    **provenance,
                    "producer_image": stable_image_ref,
                    "producer_image_digest": image_digest,
                    "environment_provenance": environment_provenance,
                    **markers,
                },
            )
            failed_step = "state"
            store.mark_healthy(
                run_id=run.run_id,
                coverage_path=coverage_path,
            )
        except Exception as exc:
            self._record_failure(
                store=store,
                run=run,
                failed_step=failed_step,
                exc=exc,
                provenance=provenance,
                recovery=recovery,
            )
            raise

    def run(self, request: BuildcacheRequest) -> int:
        import hashlib
        import json

        from hpc_cf.buildcache import (
            collect_environment_provenance,
            inspect_image_digest,
            producer_image_ref,
            remove_temporary_image,
            require_verified_source_mirror,
            temporary_producer_image_ref,
            verify,
        )
        from hpc_cf.environment import load_environment_spec
        from hpc_cf.sif import build_docker_stage
        from hpc_cf.template import (
            generate_dockerfile,
            resolve_build_input,
            resolve_image_and_tag,
        )

        store = SharedBuildcacheStore(self.layout)
        if request.action not in {"build", "verify", "resume", "status"}:
            raise ValueError(
                f"Unsupported buildcache action {request.action!r}; "
                "use build, verify, resume, or status"
            )
        if request.output_format not in {"text", "json"}:
            raise ValueError(
                f"Unsupported buildcache output format {request.output_format!r}; "
                "use text or json"
            )
        if request.action == "status":
            try:
                health = store.read_health()
            except (FileNotFoundError, json.JSONDecodeError):
                health = {"healthy": False, "reason": "missing state"}
            if request.output_format == "json":
                print(json.dumps(health, indent=2, sort_keys=True))
            else:
                state = "healthy" if health.get("healthy") is True else "unhealthy"
                print(f"Buildcache: {state}")
                if health.get("reason"):
                    print(f"Reason: {health['reason']}")
                if health.get("failed_step"):
                    print(f"Failed step: {health['failed_step']}")
                if (
                    health.get("recoverable") is True
                    and health.get("recovery_image_ref")
                ):
                    print(f"Recovery image: {health['recovery_image_ref']}")
                if (
                    health.get("recoverable") is True
                    and health.get("recovery_image_digest")
                ):
                    print(
                        f"Recovery digest: {health['recovery_image_digest']}"
                    )
                elif health.get("recovery_image_ref"):
                    print(
                        "Retained image (not resumable): "
                        f"{health['recovery_image_ref']}"
                    )
            return 0 if health.get("healthy") is True else 1

        if not request.env:
            raise ValueError(f"buildcache {request.action} requires an environment")
        if request.operation_timeout_seconds <= 0:
            raise ValueError("operation timeout must be greater than zero")
        if request.engine not in {"podman", "docker"}:
            raise ValueError(
                f"Unsupported buildcache engine {request.engine!r}; "
                "use podman or docker"
            )
        resolved = resolve_build_input(request.env, None, layout=self.layout)
        spec = resolved.environment_spec or load_environment_spec(
            resolved.environment_dir
        )
        if not spec.spack.buildcache.enabled:
            raise RuntimeError(f"buildcache is not enabled for {request.env}")
        lock_path = resolved.environment_dir / "spack.lock"
        if not lock_path.is_file():
            lock_path = resolved.environment_dir / "spack-env-file" / "spack.lock"
        if not lock_path.is_file() or lock_path.stat().st_size == 0:
            raise FileNotFoundError(
                f"buildcache requires a non-empty lock: {lock_path}"
            )
        lock_sha256 = hashlib.sha256(lock_path.read_bytes()).hexdigest()
        if request.action == "build":
            from hpc_cf.env import run_static_checks

            run_static_checks(
                resolved.environment_dir,
                spec,
                profile=ValidationProfile.BUILD_INPUT,
                layout=self.layout,
                allow_reconcretize=False,
            )

        image, tag = resolve_image_and_tag(
            app_version=request.env,
            template=None,
            image_arg=request.image,
            tag_arg=request.tag,
            layout=self.layout,
        )
        stable_image_ref = producer_image_ref(image, tag)
        logger.info("Stable buildcache producer image: %s", stable_image_ref)
        environment_provenance = collect_environment_provenance(
            lock_path,
            resolved.environment_dir,
        )
        provenance: dict[str, object] = {
            "env": request.env,
            "spack_version": spec.spack.version,
            "lock_sha256": lock_sha256,
            "stable_image_ref": stable_image_ref,
        }

        if request.action == "verify":
            run = store.begin_run(request.env)
            with store.publisher_lock():
                store.ensure_store_root()
                failed_step = "verify"
                try:
                    image_digest = inspect_image_digest(
                        engine=request.engine,
                        image_ref=stable_image_ref,
                        layout=self.layout,
                    )
                    result, checked_count = verify(
                        engine=request.engine,
                        image_ref=stable_image_ref,
                        env_name=spec.spack.env_name,
                        layout=self.layout,
                        timeout_seconds=request.operation_timeout_seconds,
                    )
                    run.log_path("verify").write_text(
                        result.stdout or "", encoding="utf-8"
                    )
                    failed_step = "coverage"
                    coverage_path = store.write_coverage(
                        lock_path=lock_path,
                        record=BuildcacheCoverageRecord(
                            spack_version=spec.spack.version,
                            builder_image_digest=image_digest,
                            environment_provenance=environment_provenance,
                            padded_length=spec.spack.buildcache.padded_length,
                            signing_policy="unsigned",
                            check_returncode=0,
                            checked_spec_count=checked_count,
                        ),
                    )
                    failed_step = "provenance"
                    store.write_provenance(
                        run,
                        {
                            **provenance,
                            "producer_image": stable_image_ref,
                            "producer_image_digest": image_digest,
                            "environment_provenance": environment_provenance,
                        },
                    )
                    failed_step = "state"
                    store.mark_healthy(
                        run_id=run.run_id,
                        coverage_path=coverage_path,
                    )
                except Exception as exc:
                    self._record_failure(
                        store=store,
                        run=run,
                        failed_step=failed_step,
                        exc=exc,
                        provenance=provenance,
                    )
                    raise
            return 0

        if request.action == "resume":
            with store.publisher_lock():
                try:
                    health = store.read_health()
                except (FileNotFoundError, json.JSONDecodeError) as exc:
                    raise RuntimeError(
                        "cannot resume without the latest unhealthy state"
                    ) from exc
                if health.get("healthy") is not False:
                    raise RuntimeError(
                        "cannot resume because the latest state is not unhealthy"
                    )
                recovery_image_ref = health.get("recovery_image_ref")
                recovery_digest = health.get("recovery_image_digest")
                if (
                    health.get("recoverable") is not True
                    or not recovery_image_ref
                    or not recovery_digest
                ):
                    raise RuntimeError(
                        "latest unhealthy state has no recoverable producer image"
                    )
                if health.get("env") != request.env:
                    raise RuntimeError(
                        "recovery environment does not match requested environment"
                    )
                if health.get("lock_sha256") != lock_sha256:
                    raise RuntimeError(
                        "recovery lock SHA does not match the current lock SHA"
                    )
                if health.get("spack_version") != spec.spack.version:
                    raise RuntimeError(
                        "recovery Spack version does not match the environment"
                    )
                if health.get("stable_image_ref") != stable_image_ref:
                    raise RuntimeError(
                        "recovery stable producer image does not match the environment"
                    )
                try:
                    image_digest = inspect_image_digest(
                        engine=request.engine,
                        image_ref=str(recovery_image_ref),
                        layout=self.layout,
                    )
                except Exception as exc:
                    raise RuntimeError(
                        f"recovery image does not exist: {recovery_image_ref}"
                    ) from exc
                if image_digest != recovery_digest:
                    raise RuntimeError(
                        "recovery image digest does not match unhealthy state"
                    )
                run = store.begin_run(request.env)
                self._complete_producer(
                    store=store,
                    run=run,
                    request=request,
                    spec=spec,
                    lock_path=lock_path,
                    environment_provenance=environment_provenance,
                    temporary_image_ref=str(recovery_image_ref),
                    image_digest=image_digest,
                    stable_image_ref=stable_image_ref,
                    provenance=provenance,
                )
            remove_temporary_image(
                engine=request.engine,
                image_ref=str(recovery_image_ref),
                layout=self.layout,
            )
            return 0

        run = store.begin_run(request.env)
        require_verified_source_mirror(
            self.layout,
            env_name=request.env,
            lock_path=lock_path,
            spack_version=spec.spack.version,
        )
        # Producer install uses auto so already-published hashes can be
        # extracted; miss falls back to the verified source mirror. Keep
        # buildcache_producer so padded_length is still applied for push.
        store.ensure_store_root()
        dockerfile = generate_dockerfile(
            template=None,
            app_version=request.env,
            output=self.layout.project_root / "Dockerfile",
            use_mirror=True,
            build_only=True,
            layout=self.layout,
            allow_reconcretize=False,
            buildcache_policy=BuildcachePolicy.AUTO.value,
            buildcache_producer=True,
        )
        temporary_image_ref = temporary_producer_image_ref(
            image, tag, run.run_id
        )
        # Producer builds always disable layer cache so soft-fail installs
        # cannot reuse a stale builder-installed stage. CLI --build-opt appends.
        producer_build_opts = ["--no-cache", *list(request.build_opts)]
        try:
            with store.consumer_lock():
                build_docker_stage(
                    dockerfile=dockerfile,
                    image_ref=temporary_image_ref,
                    target="builder-installed",
                    engine=request.engine,
                    network_host=request.network_host,
                    build_args=list(request.build_args),
                    build_opts=producer_build_opts,
                )
        except Exception as docker_build_error:
            # Soft-fail producer installs normally tag the image (exit 0).
            # If the engine still reports failure but a tag exists, attempt
            # full publish; success → healthy (do not mark docker-build dead).
            try:
                image_digest = inspect_image_digest(
                    engine=request.engine,
                    image_ref=temporary_image_ref,
                    layout=self.layout,
                )
            except Exception:
                remove_temporary_image(
                    engine=request.engine,
                    image_ref=temporary_image_ref,
                    layout=self.layout,
                )
                self._record_failure(
                    store=store,
                    run=run,
                    failed_step="docker-build",
                    exc=docker_build_error,
                    provenance=provenance,
                )
                raise docker_build_error

            logger.warning(
                "Producer docker-build reported failure but image %s exists; "
                "attempting publish of installed specs",
                temporary_image_ref,
            )
            with store.publisher_lock():
                store.ensure_store_root()
                try:
                    self._complete_producer(
                        store=store,
                        run=run,
                        request=request,
                        spec=spec,
                        lock_path=lock_path,
                        environment_provenance=environment_provenance,
                        temporary_image_ref=temporary_image_ref,
                        image_digest=image_digest,
                        stable_image_ref=stable_image_ref,
                        provenance={
                            **provenance,
                            "docker_build_error": str(docker_build_error),
                        },
                    )
                except Exception as publish_exc:
                    raise publish_exc from docker_build_error
            remove_temporary_image(
                engine=request.engine,
                image_ref=temporary_image_ref,
                layout=self.layout,
            )
            return 0

        with store.publisher_lock():
            store.ensure_store_root()
            try:
                image_digest = inspect_image_digest(
                    engine=request.engine,
                    image_ref=temporary_image_ref,
                    layout=self.layout,
                )
            except Exception as exc:
                self._record_failure(
                    store=store,
                    run=run,
                    failed_step="inspect-image",
                    exc=exc,
                    provenance=provenance,
                    recovery={
                        "recoverable": False,
                        "env": request.env,
                        "lock_sha256": lock_sha256,
                        "spack_version": spec.spack.version,
                        "recovery_image_ref": temporary_image_ref,
                        "stable_image_ref": stable_image_ref,
                    },
                )
                raise
            self._complete_producer(
                store=store,
                run=run,
                request=request,
                spec=spec,
                lock_path=lock_path,
                environment_provenance=environment_provenance,
                temporary_image_ref=temporary_image_ref,
                image_digest=image_digest,
                stable_image_ref=stable_image_ref,
                provenance=provenance,
            )
        remove_temporary_image(
            engine=request.engine,
            image_ref=temporary_image_ref,
            layout=self.layout,
        )
        return 0
