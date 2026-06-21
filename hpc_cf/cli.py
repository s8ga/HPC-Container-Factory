"""CLI definitions and command dispatch for the HPC Container Factory.

This module is intentionally *thin*: it defines argparse parsers and
dispatches to domain modules:

- ``hpc_cf.assets`` — assets/bootstrap/mirror workflows
- ``hpc_cf.template`` — Dockerfile rendering
- ``hpc_cf.sif`` — SIF building & apptainer management
"""

from __future__ import annotations

import argparse
from pathlib import Path

import logging

from hpc_cf.env import validate_manual_packages, validate_spack_assets
from hpc_cf.sif import (
    build_apptainer,
    build_docker_like,
    build_sif,
    ensure_apptainer,
    pack_apptainer,
)
from hpc_cf.template import (
    _extract_available_versions,
    generate_dockerfile,
    load_env_yaml,
    resolve_image_and_tag,
    select_template,
)

logger = logging.getLogger(__name__)


# ── Option helpers ───────────────────────────────────────────────────────


def add_template_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--template",
        type=Path,
        default=None,
        help="Explicit Dockerfile template path",
    )
    parser.add_argument(
        "--app",
        choices=["cp2k"],
        default="cp2k",
        help="Application type",
    )
    parser.add_argument(
        "--app-version",
        default=None,
        nargs="?",
        const="__LIST__",
        help="Application version used for template auto-selection. "
             "If omitted, defaults to the first available env under spack-envs/. "
             "Pass without value to list available versions.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("Dockerfile"),
        help="Output Dockerfile path",
    )
    parser.add_argument(
        "--mirror",
        action="store_true",
        help="Render template in mirror mode",
    )
    parser.add_argument(
        "--no-mirror",
        action="store_true",
        help="disable spack-mirror (override --mirror and config)",
    )
    parser.add_argument(
        "--build-only",
        action="store_true",
        help="Render only builder stage in templates that support it",
    )


def add_assets_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--env",
        default=None,
        nargs="?",
        const="__LIST__",
        help="Environment name under spack-envs/. Pass without value to list available envs.",
    )
    parser.add_argument(
        "--podman-cmd",
        default="podman",
        help="Container runtime command for mirror helpers",
    )
    parser.add_argument(
        "--podman-opt",
        action="append",
        default=[],
        help="Extra podman run/create option (repeatable), e.g. --podman-opt '--dns=8.8.8.8'",
    )
    parser.add_argument(
        "--mirror-image",
        default="hpc-mirror-builder",
        help="Mirror builder image name",
    )
    parser.add_argument(
        "--container-name",
        default="hpc-mirror-builder-work",
        help="Reusable mirror worker container name",
    )
    parser.add_argument(
        "--skip-image-build",
        action="store_true",
        help="Do not auto-build mirror builder image",
    )
    parser.add_argument(
        "--force-bootstrap",
        action="store_true",
        help="Regenerate bootstrap cache from scratch",
    )

    parser.add_argument(
        "--create-container",
        action="store_true",
        help="Create/start reusable mirror worker container",
    )
    parser.add_argument(
        "--prepare-bootstrap",
        action="store_true",
        help="Prepare bootstrap cache",
    )
    parser.add_argument(
        "--download-mirror",
        action="store_true",
        help="Download source mirror for --env",
    )
    parser.add_argument(
        "--verify-mirror",
        action="store_true",
        help="Verify source mirror for --env",
    )
    parser.add_argument(
        "--status",
        action="store_true",
        help="Show mirror/bootstrap status for --env",
    )

    parser.add_argument(
        "--skip-create-container",
        action="store_true",
        help="Default workflow only: run bootstrap/mirror without reusable container",
    )
    parser.add_argument(
        "--skip-verify",
        action="store_true",
        help="Default workflow only: skip verify after mirror download",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="HPC Container Factory CLI",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python -m hpc_cf dockerfile --app-version cp2k_rocm-2026.1-gfx942\n"
            "  python -m hpc_cf build --app-version cp2k_rocm-2026.1-gfx942\n"
            "  python -m hpc_cf assets --env cp2k_rocm-2026.1-gfx942\n"
            "  python -m hpc_cf assets --create-container\n"
            "  python -m hpc_cf assets --env cp2k_rocm-2026.1-gfx942 --download-mirror\n"
            "  python -m hpc_cf build-sif --app-version cp2k_opensource-2026.1-force-avx512\n"
            "  python -m hpc_cf build-sif --install-apptainer-only\n"
            "  python -m hpc_cf pack-apptainer\n"
        ),
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="Enable verbose logs")

    subparsers = parser.add_subparsers(dest="command")

    dockerfile_parser = subparsers.add_parser("dockerfile", help="Generate Dockerfile only")
    add_template_options(dockerfile_parser)

    build_parser_cmd = subparsers.add_parser("build", help="Generate Dockerfile and build image")
    add_template_options(build_parser_cmd)
    build_parser_cmd.add_argument(
        "--engine",
        choices=["podman", "docker", "apptainer"],
        default="podman",
        help="Build engine",
    )
    build_parser_cmd.add_argument(
        "--image",
        default=None,
        help="Output image name (default auto: opensource->cp2k_opensource, rocm->cp2k_rocm)",
    )
    build_parser_cmd.add_argument(
        "--tag",
        default=None,
        help="Output image tag (default auto: opensource->version, rocm->version-gpu)",
    )
    build_parser_cmd.add_argument(
        "--network-host",
        action="store_true",
        help="Build with --network host (podman/docker)",
    )
    build_parser_cmd.add_argument(
        "--build-arg",
        action="append",
        default=[],
        help="Pass --build-arg to podman/docker build (repeatable), e.g. --build-arg SPACK_MAKE_JOBS=8",
    )
    build_parser_cmd.add_argument(
        "--build-opt",
        action="append",
        default=[],
        help="Extra podman/docker build option (repeatable), e.g. --build-opt '--no-cache'",
    )

    assets_parser = subparsers.add_parser(
        "assets",
        help="Prepare bootstrap/mirror assets and mirror worker container",
    )
    add_assets_options(assets_parser)

    pack_apptainer_parser = subparsers.add_parser(
        "pack-apptainer",
        help="Pack local apptainer into a makeself self-extracting archive",
    )
    pack_apptainer_parser.add_argument(
        "--output", "-o",
        type=Path,
        default=None,
        help=(
            "Output .run file path "
            "(default: artifacts/apptainer-<version>-<arch>.run)"
        ),
    )
    pack_apptainer_parser.add_argument(
        "--no-sha256",
        action="store_true",
        help="Skip SHA256 checksum (faster)",
    )

    build_sif_parser = subparsers.add_parser(
        "build-sif",
        help="Convert Docker/Podman OCI image to Apptainer SIF",
    )
    build_sif_parser.add_argument(
        "--docker-image",
        default=None,
        help="OCI image name (default: auto-detect from --app-version)",
    )
    build_sif_parser.add_argument(
        "--docker-tag",
        default=None,
        help="OCI image tag (default: auto-detect from --app-version)",
    )
    build_sif_parser.add_argument(
        "--output", "-o",
        type=Path,
        default=None,
        help="Output SIF file path (default: <image>_<tag>.sif)",
    )
    build_sif_parser.add_argument(
        "--app",
        choices=["cp2k"],
        default="cp2k",
        help="Application type (for auto image/tag detection)",
    )
    build_sif_parser.add_argument(
        "--app-version",
        default=None,
        nargs="?",
        const="__LIST__",
        help="Application version for auto image/tag detection",
    )
    build_sif_parser.add_argument(
        "--mksquashfs-args",
        default="-comp zstd -Xcompression-level 22 -b 1M",
        help="mksquashfs compression arguments (default: '-comp zstd -Xcompression-level 22 -b 1M')",
    )
    build_sif_parser.add_argument(
        "--install-apptainer-only",
        action="store_true",
        help="Only install apptainer, do not build SIF",
    )
    build_sif_parser.add_argument(
        "--yes",
        "-y",
        action="store_true",
        help="Confirm apptainer installation without prompting (CI-friendly)",
    )

    return parser


# ── Main dispatch ────────────────────────────────────────────────────────


def run_new_cli(argv: list[str]) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.verbose:
        logger.setLevel(logging.DEBUG)

    # ── pack-apptainer ──
    if args.command == "pack-apptainer":
        pack_apptainer(
            output=getattr(args, "output", None),
            no_sha256=getattr(args, "no_sha256", False),
        )
        return 0

    # ── build-sif ──
    if args.command == "build-sif":
        if getattr(args, "app_version", None) == "__LIST__":
            available_versions = _extract_available_versions()
            print("Available --app-version values:")
            for v in available_versions:
                print(f"  {v}")
            return 0

        if getattr(args, "install_apptainer_only", False):
            apptainer_path = ensure_apptainer(auto_confirm=getattr(args, "yes", False))
            print(f"apptainer installed: {apptainer_path}")
            return 0

        docker_image = args.docker_image
        docker_tag = args.docker_tag

        if not docker_image or not docker_tag:
            if not getattr(args, "app_version", None):
                logger.error(
                    "Specify --docker-image and --docker-tag, "
                    "or --app-version for auto-detection."
                )
                return 1
            try:
                resolved_template = select_template(args.app, args.app_version, None)
            except FileNotFoundError:
                resolved_template = None
            from hpc_cf.template import resolve_output_image_tag
            auto_image, auto_tag = resolve_output_image_tag(resolved_template)
            docker_image = docker_image or auto_image
            docker_tag = docker_tag or auto_tag

        build_sif(
            docker_image=docker_image,
            docker_tag=docker_tag,
            output=args.output,
            app_version=getattr(args, "app_version", None),
            mksquashfs_args=args.mksquashfs_args,
            yes=getattr(args, "yes", False),
        )
        logger.info("Done")
        return 0

    # ── Handle --app-version without value → list available versions ──
    if getattr(args, "app_version", None) == "__LIST__":
        available_versions = _extract_available_versions()
        print("Available --app-version values:")
        for v in available_versions:
            print(f"  {v}")
        return 0

    # Default app-version when not specified at all.
    # Pick the first available env dynamically instead of hardcoding a version
    # that goes stale every release (plan A5).
    if not getattr(args, "app_version", None):
        available = _extract_available_versions()
        if available:
            args.app_version = available[0]
        else:
            logger.error(
                "No --app-version specified and no envs found under spack-envs/. "
                "Pass --app-version explicitly."
            )
            return 1

    # Mirror priority: --no-mirror > --mirror > default true
    if getattr(args, "no_mirror", False):
        use_mirror = False
    elif getattr(args, "mirror", False):
        use_mirror = True
    else:
        use_mirror = True

    if args.command == "dockerfile":
        generate_dockerfile(
            template=args.template,
            app=args.app,
            app_version=args.app_version,
            output=args.output,
            use_mirror=use_mirror,
            build_only=args.build_only,
        )
        logger.info("Done")
        return 0

    if args.command == "build":
        resolved_image, resolved_tag = resolve_image_and_tag(
            app_version=args.app_version,
            template=args.template,
            app=args.app,
            image_arg=args.image,
            tag_arg=args.tag,
        )

        dockerfile = generate_dockerfile(
            template=args.template,
            app=args.app,
            app_version=args.app_version,
            output=args.output,
            use_mirror=use_mirror,
            build_only=args.build_only,
        )

        # Validate manual_packages before starting the (expensive) build
        _resolved_template = select_template(args.app, args.app_version, args.template)
        _env_config = load_env_yaml(_resolved_template)
        validate_manual_packages(_env_config)
        validate_spack_assets(_env_config)

        if args.engine == "apptainer":
            logger.info("Resolved image: %s:%s", resolved_image, resolved_tag)
            build_apptainer(definition_file=dockerfile, image=resolved_image, tag=resolved_tag)
        else:
            logger.info("Resolved image: %s:%s", resolved_image, resolved_tag)
            build_docker_like(
                dockerfile=dockerfile,
                image=resolved_image,
                tag=resolved_tag,
                engine=args.engine,
                network_host=args.network_host,
                build_args=args.build_arg,
                build_opts=args.build_opt,
            )
        logger.info("Done")
        return 0

    if args.command == "assets":
        from hpc_cf.assets import run_assets
        run_assets(args)
        logger.info("Done")
        return 0

    parser.print_help()
    return 1
