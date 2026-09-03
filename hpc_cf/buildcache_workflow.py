"""Buildcache producer/consumer orchestration (state machine).

Owns :class:`BuildcacheService` — build / publish / verify / resume / status.
Request DTOs live in :mod:`hpc_cf.requests`.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

from hpc_cf.environment import BuildcacheMode, BuildcachePolicy
from hpc_cf.execution import (
    BuildcacheCoverageRecord,
    ProjectLayout,
    SharedBuildcacheStore,
)
from hpc_cf.requests import BuildcacheRequest
from hpc_cf.validation import ValidationProfile

logger = logging.getLogger(__name__)


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
        backend: Any,
    ) -> None:
        from hpc_cf.buildcache import (
            failed_publish_output,
            failed_publish_step,
            inspect_image_digest,
            promote_producer_image,
            publish,
            publish_oci,
            publish_output_markers,
            publish_success_state,
            require_matching_image_lock,
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

            def run_publish() -> tuple[Any, int]:
                if backend.mode is BuildcacheMode.OCI:
                    username_var = backend.username_var
                    password_var = backend.password_var
                    credentials: dict[str, str] = {}
                    if username_var and password_var:
                        missing = [
                            name
                            for name in (username_var, password_var)
                            if not os.environ.get(name)
                        ]
                        if missing:
                            raise RuntimeError(
                                "oci publisher credential env vars not set: "
                                f"{sorted(missing)}"
                            )
                        credentials = {
                            username_var: os.environ[username_var],
                            password_var: os.environ[password_var],
                        }
                    return publish_oci(
                        engine=request.engine,
                        image_ref=temporary_image_ref,
                        env_name=spec.spack.env_name,
                        layout=self.layout,
                        mirror_url=str(backend.url),
                        username_var=username_var,
                        password_var=password_var,
                        credentials=credentials,
                        timeout_seconds=request.operation_timeout_seconds,
                    )
                return publish(
                    engine=request.engine,
                    image_ref=temporary_image_ref,
                    env_name=spec.spack.env_name,
                    layout=self.layout,
                    timeout_seconds=request.operation_timeout_seconds,
                )

            try:
                result, checked_count = run_publish()
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
            failed_step = "lock-bind"
            require_matching_image_lock(
                engine=request.engine,
                image_ref=temporary_image_ref,
                env_name=spec.spack.env_name,
                layout=self.layout,
                lock_path=lock_path,
                timeout_seconds=request.operation_timeout_seconds,
            )
            failed_step = "coverage"
            publish_success_state(
                store,
                run=run,
                lock_path=lock_path,
                record=BuildcacheCoverageRecord(
                    spack_version=spec.spack.version,
                    builder_image_digest=image_digest,
                    environment_provenance=environment_provenance,
                    padded_length=spec.spack.buildcache.padded_length,
                    signing_policy="unsigned",
                    check_returncode=0,
                    checked_spec_count=checked_count,
                    check_kind=(
                        "count" if backend.mode is BuildcacheMode.OCI else "live"
                    ),
                ),
                provenance={
                    **provenance,
                    "producer_image": stable_image_ref,
                    "producer_image_digest": image_digest,
                    "environment_provenance": environment_provenance,
                    **markers,
                },
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
            publish_success_state,
            remove_temporary_image,
            require_matching_image_lock,
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
        # Lazy import: workflows imports this module at top level.
        from hpc_cf.workflows import resolve_buildcache_backend

        backend = resolve_buildcache_backend(
            spec,
            mode_override=request.buildcache_mode,
            url_override=request.buildcache_url,
            username_var_override=request.buildcache_username_var,
            password_var_override=request.buildcache_password_var,
        )
        if request.action == "verify" and backend.mode is BuildcacheMode.OCI:
            raise RuntimeError(
                "buildcache verify is local-mode only: the live check cannot "
                "see oci mirrors; oci admission relies on coverage records"
            )
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
                    failed_step = "lock-bind"
                    require_matching_image_lock(
                        engine=request.engine,
                        image_ref=stable_image_ref,
                        env_name=spec.spack.env_name,
                        layout=self.layout,
                        lock_path=lock_path,
                        timeout_seconds=request.operation_timeout_seconds,
                    )
                    failed_step = "coverage"
                    publish_success_state(
                        store,
                        run=run,
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
                        provenance={
                            **provenance,
                            "producer_image": stable_image_ref,
                            "producer_image_digest": image_digest,
                            "environment_provenance": environment_provenance,
                        },
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
                    backend=backend,
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
            buildcache_mode=backend.mode.value,
            buildcache_url=backend.url,
            buildcache_username_var=backend.username_var,
            buildcache_password_var=backend.password_var,
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
                    build_secrets=list(request.build_secret),
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
                        backend=backend,
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
                backend=backend,
            )
        remove_temporary_image(
            engine=request.engine,
            image_ref=temporary_image_ref,
            layout=self.layout,
        )
        return 0
