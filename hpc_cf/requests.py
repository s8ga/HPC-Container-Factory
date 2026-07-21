"""Typed workflow request DTOs and CLI namespace mappers.

Kept separate from service modules so ``assets`` can depend on request types
without importing orchestration (avoids an assets↔workflows import cycle).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from hpc_cf.environment import BuildcachePolicy
from hpc_cf.validation import ValidationProfile


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
