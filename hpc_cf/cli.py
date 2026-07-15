"""CLI definitions and command dispatch for the HPC Container Factory.

This module is intentionally *thin*: argparse → request objects → services.

- ``hpc_cf.workflows`` — BuildRequest / AssetsRequest + services
- ``hpc_cf.sif`` — SIF building & apptainer management (still CLI-dispatched)
"""

from __future__ import annotations

import argparse
import logging
from pathlib import Path

from hpc_cf.sif import (
    build_sif,
    ensure_apptainer,
    pack_apptainer,
)
from hpc_cf.template import (
    _extract_available_versions,
    build_context,
    render_template,
    resolve_build_input,
    select_template,
)
from hpc_cf.validation import (
    ValidationFinding,
    ValidationProfile,
    ValidationReport,
    ValidationSeverity,
    validate_environment,
)
from hpc_cf.workflows import (
    AssetsService,
    BuildService,
    assets_request_from_args,
    build_request_from_args,
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
        "--app-version",
        "--env",
        dest="app_version",
        default=None,
        nargs="?",
        const="__LIST__",
        help="Environment name under spack-envs/ (full directory name). "
             "Pass without value to list available environments. "
             "--env is an alias for --app-version.",
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
    parser.add_argument(
        "--allow-reconcretize",
        action="store_true",
        help=(
            "Permit build/dockerfile without a non-empty spack.lock "
            "(fail-open; installs may re-concretize). Default is fail-closed."
        ),
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
    parser.add_argument(
        "--allow-concretize",
        action="store_true",
        help=(
            "When spack.lock is missing, run concretize+mirror instead of failing. "
            "Required for first-time lock generation after deleting a copied lock."
        ),
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="HPC Container Factory CLI",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python -m hpc_cf validate --app-version cp2k_opensource-2026.1-force-avx512\n"
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
        choices=["podman", "docker"],
        default="podman",
        help="Build engine (podman/docker). For Apptainer SIF use build-sif.",
    )
    build_parser_cmd.add_argument(
        "--image",
        default=None,
        help="Output image name (default auto from env directory name)",
    )
    build_parser_cmd.add_argument(
        "--tag",
        default=None,
        help="Output image tag (default auto from env directory name)",
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
        help="OCI image name (default: auto-detect from --app-version/--env)",
    )
    build_sif_parser.add_argument(
        "--docker-tag",
        default=None,
        help="OCI image tag (default: auto-detect from --app-version/--env)",
    )
    build_sif_parser.add_argument(
        "--output", "-o",
        type=Path,
        default=None,
        help="Output SIF file path (default: <image>_<tag>.sif)",
    )
    build_sif_parser.add_argument(
        "--app-version",
        "--env",
        dest="app_version",
        default=None,
        nargs="?",
        const="__LIST__",
        help="Environment name for auto image/tag detection (--env is an alias)",
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

    validate_parser = subparsers.add_parser(
        "validate",
        help="Static checks for an env (profiles: config, build-input, assets)",
    )
    validate_parser.add_argument(
        "--app-version",
        "--env",
        dest="app_version",
        default=None,
        nargs="?",
        const="__LIST__",
        help="Environment name under spack-envs/ (pass without value to list; "
             "--env is an alias).",
    )
    validate_parser.add_argument(
        "--template",
        type=Path,
        default=None,
        help="Explicit Dockerfile.j2 path (overrides --app-version resolution).",
    )
    validate_parser.add_argument(
        "--profile",
        choices=["config", "build-input", "assets", "template"],
        default="build-input",
        help=(
            "Validation profile: config/template (render), build-input (build), "
            "assets (assets workflow). Default: build-input."
        ),
    )
    validate_parser.add_argument(
        "--format",
        choices=["text", "json"],
        default="text",
        help="Output format for the validation report (default: text).",
    )

    return parser


# ── Main dispatch ────────────────────────────────────────────────────────


def run_new_cli(argv: list[str]) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)
        logging.getLogger("hpc_cf").setLevel(logging.DEBUG)

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
            print("Available --app-version/--env values:")
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
                    "or --app-version/--env for auto-detection."
                )
                return 1
            try:
                resolved_template = select_template(args.app_version, None)
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

    # ── validate ──
    if args.command == "validate":
        if args.app_version == "__LIST__":
            for v in _extract_available_versions():
                print(v)
            return 0

        profile = ValidationProfile.parse(args.profile)
        out_fmt = getattr(args, "format", "text")

        def _emit(report: ValidationReport) -> int:
            if out_fmt == "json":
                print(report.format_json(), end="")
            else:
                print(report.format_text(), end="")
            return 0 if report.ok else 1

        try:
            if args.template is not None:
                resolved_input = resolve_build_input(
                    explicit_template=args.template
                )
            else:
                if not args.app_version:
                    logger.error(
                        "validate requires --app-version/--env <env> "
                        "or --template <path>."
                    )
                    return 1
                resolved_input = resolve_build_input(args.app_version, None)
        except FileNotFoundError as exc:
            report = ValidationReport(
                profile=profile.value,
                env_name=(
                    Path(args.template).parent.name
                    if args.template is not None
                    else args.app_version
                ),
            )
            report.add(
                ValidationFinding(
                    code="template.missing",
                    severity=ValidationSeverity.ERROR,
                    message=str(exc),
                    path=str(args.template) if args.template else None,
                    fix_hint="Provide an existing Dockerfile.j2 path.",
                )
            )
            return _emit(report)

        env_dir = resolved_input.environment_dir
        report = validate_environment(
            env_dir, profile, env_config=resolved_input.environment_spec
        )

        # StrictUndefined render probe for config/template profile when a
        # concrete template path is available.
        if report.ok and profile is ValidationProfile.CONFIG:
            try:
                ctx = build_context(
                    use_mirror=False,
                    build_only=True,
                    app_version=args.app_version or env_dir.name,
                    template_path=resolved_input.render_template,
                    resolved=resolved_input,
                )
                render_template(resolved_input.render_template, ctx)
            except (FileNotFoundError, RuntimeError) as exc:
                report.add(
                    ValidationFinding(
                        code="template.render",
                        severity=ValidationSeverity.ERROR,
                        message=str(exc),
                        path=str(resolved_input.render_template),
                        fix_hint=(
                            "Fix undefined Jinja variables or template syntax "
                            "(StrictUndefined)."
                        ),
                    )
                )

        rc = _emit(report)
        if report.ok:
            logger.info(
                "✅ %s: %s checks passed", env_dir.name, profile.value
            )
        return rc

    # ── assets ──
    # Before the dockerfile/build --app-version gate: assets uses its own --env.
    if args.command == "assets":
        AssetsService().run(assets_request_from_args(args))
        logger.info("Done")
        return 0

    # ── Handle --app-version/--env without value → list available versions ──
    if getattr(args, "app_version", None) == "__LIST__":
        available_versions = _extract_available_versions()
        print("Available --app-version/--env values:")
        for v in available_versions:
            print(f"  {v}")
        return 0

    # Require an explicit env. Do NOT silently pick the first alphabetical
    # entry (that currently defaults to abacus and surprises users).
    if not getattr(args, "app_version", None):
        if getattr(args, "template", None):
            args.app_version = Path(args.template).parent.name
        else:
            available = _extract_available_versions()
            logger.error(
                "Specify --app-version/--env <env> (or --template <path>). "
                "Available:"
            )
            for v in available:
                print(f"  {v}")
            return 1

    # Mirror priority: --no-mirror > --mirror > default true
    if getattr(args, "no_mirror", False):
        use_mirror = False
    elif getattr(args, "mirror", False):
        use_mirror = True
    else:
        use_mirror = True

    if args.command == "dockerfile":
        return BuildService().run(
            build_request_from_args(args, use_mirror=use_mirror, render_only=True)
        )

    if args.command == "build":
        return BuildService().run(
            build_request_from_args(args, use_mirror=use_mirror, render_only=False)
        )

    parser.print_help()
    return 1
