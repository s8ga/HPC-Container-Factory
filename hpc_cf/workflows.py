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

from hpc_cf.execution import ProjectLayout
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

        # Single preflight site (run_assets must not re-validate).
        if request.env and request.env != "__LIST__":
            host_dir, _ = resolve_env_paths(request.env, layout=self.layout)
            try:
                spec = load_environment_spec(host_dir)
            except FileNotFoundError:
                spec = None
            if spec is not None and spec.method.requires_spack_assets:
                profile = profile_for_assets_action(request)
                run_static_checks(
                    host_dir,
                    spec,
                    profile=profile,
                    layout=self.layout,
                )

        run_assets(request, layout=self.layout)


class BuildService:
    """Orchestrate Dockerfile render and optional image build."""

    def __init__(self, layout: ProjectLayout | None = None) -> None:
        self.layout = layout or ProjectLayout.default()

    def run(self, request: BuildRequest) -> int:
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
        # dockerfile → config/template; build → build-input.
        profile = (
            ValidationProfile.CONFIG
            if request.render_only
            else ValidationProfile.BUILD_INPUT
        )
        run_static_checks(
            resolved.environment_dir,
            resolved.environment_spec,
            profile=profile,
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

        dockerfile = generate_dockerfile(
            template=request.template,
            app_version=request.app_version,
            output=request.output,
            use_mirror=request.use_mirror,
            build_only=request.build_only,
            layout=self.layout,
            allow_reconcretize=request.allow_reconcretize,
        )

        if request.engine not in ("podman", "docker"):
            raise ValueError(
                f"Unsupported build engine {request.engine!r}; "
                "use podman/docker, or `build-sif` for Apptainer SIF"
            )

        logger.info("Resolved image: %s:%s", resolved_image, resolved_tag)
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
