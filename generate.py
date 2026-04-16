#!/usr/bin/env python3
"""Unified CLI for Dockerfile generation and offline asset preparation.

Main commands:
  - dockerfile: render Dockerfile from Jinja2 template
  - build: render + build image
  - assets: prepare bootstrap/mirror assets and mirror worker container

Legacy compatibility:
  Existing flag-only usage is still supported, e.g.:
    python generate.py --output Dockerfile --dry-run
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import shlex
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

try:
    import yaml
    from jinja2 import Environment, FileSystemLoader, TemplateNotFound, TemplateSyntaxError
except ImportError as exc:
    print(f"Error: Required package not installed: {exc}")
    print("Install dependencies with: pip install pyyaml jinja2")
    sys.exit(1)


logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).parent.absolute()
CONFIGS_DIR = PROJECT_ROOT / "configs"
TEMPLATES_DIR = PROJECT_ROOT / "templates"
SCRIPTS_DIR = PROJECT_ROOT / "scripts"
MIRROR_SCRIPT = SCRIPTS_DIR / "build-mirror-in-container.sh"
PREPARE_BOOTSTRAP_SCRIPT = SCRIPTS_DIR / "prepare-bootstrap-cache.sh"

SUBCOMMANDS = {"dockerfile", "build", "assets"}


def check_command_exists(cmd: str) -> bool:
    return shutil.which(cmd) is not None


def run_cmd(
    cmd: list[str],
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
) -> None:
    run_cwd = cwd or PROJECT_ROOT
    logger.info("Running: %s", shlex.join(cmd))
    subprocess.run(cmd, cwd=run_cwd, env=env, check=True)


def load_config(config_path: Path) -> dict:
    if not config_path.exists():
        raise FileNotFoundError(f"Config file not found: {config_path}")

    logger.info("Loading config: %s", config_path)
    with config_path.open("r", encoding="utf-8") as f:
        config = yaml.safe_load(f)

    if not config:
        raise ValueError(f"Config file is empty: {config_path}")

    return config


def extract_version_token(text: str) -> str | None:
    match = re.search(r"\b(\d+\.\d+(?:\.\d+)?)\b", text)
    if match:
        return match.group(1)
    return None


def detect_gpu_arch_from_template(template_path: Path | None) -> str | None:
    if not template_path or not template_path.exists():
        return None

    try:
        content = template_path.read_text(encoding="utf-8")
    except OSError:
        return None

    match = re.search(r'ARG\s+AMDGPU_TARGETS\s*=\s*"([^"]+)"', content)
    if not match:
        return None

    targets = match.group(1).strip()
    if not targets:
        return None
    return targets.split(",")[0].strip()


def infer_image_defaults(app_version: str, template_path: Path | None) -> tuple[str, str]:
    appv = (app_version or "").lower()
    template_name = template_path.name.lower() if template_path else ""
    variant_hint = f"{appv} {template_name}"

    # Prefer version from template file name when available, fallback to app-version token.
    version = extract_version_token(template_name) or extract_version_token(appv) or "latest"

    if "rocm" in variant_hint:
        arch = detect_gpu_arch_from_template(template_path) or "gfx942"
        if version == "latest":
            return "cp2k-rocm", arch
        return "cp2k-rocm", f"{version}-{arch}"

    if "opensource" in variant_hint:
        return "cp2k-opensource", version

    return "hpc-cp2k", "latest"


def load_env_yaml(template_path: Path | None) -> dict:
    """Load env.yaml from the same directory as the template (if inside spack-envs/)."""
    if not template_path:
        return {}
    env_yaml = template_path.parent / "env.yaml"
    if not env_yaml.exists():
        return {}
    with env_yaml.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def build_context(
    config: dict,
    *,
    use_mirror: bool,
    build_only: bool,
    app_version: str,
    template_path: Path | None,
) -> dict:
    env_config = load_env_yaml(template_path)

    # env.yaml images 段优先，versions.yaml images 段 fallback
    env_images = env_config.get("images", {})
    cfg_images = config.get("images", {})
    builder_base_image = env_images.get("builder", cfg_images.get("builder_base", "debian:trixie"))
    runtime_base_image = env_images.get("runtime", cfg_images.get("runtime_base", "debian:trixie-slim"))
    default_image_name, default_image_tag = infer_image_defaults(app_version, template_path)

    context = {
        "timestamp": datetime.now().isoformat(),
        "generated_with": "HPC Dockerfile Generator",
        "builder_base_image": builder_base_image,
        "runtime_base_image": runtime_base_image,
        "use_mirror": use_mirror,
        "build_only": build_only,
        "default_image_name": default_image_name,
        "default_image_tag": default_image_tag,
        # 注入 env.yaml 中的 template_vars 作为顶层变量
        **env_config.get("template_vars", {}),
        # 注入全局 config
        **config,
    }

    logger.debug("Build context keys: %s", list(context.keys()))
    return context


def _extract_available_versions() -> list[str]:
    """Scan spack-envs/ and templates/ for available Dockerfile templates."""
    versions: list[str] = []
    seen: set[str] = set()

    # 优先扫描 spack-envs/*/Dockerfile.j2 (新布局)
    spack_envs = PROJECT_ROOT / "spack-envs"
    if spack_envs.exists():
        for env_dir in sorted(spack_envs.iterdir()):
            if env_dir.is_dir() and (env_dir / "Dockerfile.j2").exists():
                name = env_dir.name  # e.g. "cp2k-opensource-2025.2"
                if name not in seen:
                    versions.append(name)
                    seen.add(name)

    # 回退扫描 templates/ (legacy 布局)
    for f in sorted(TEMPLATES_DIR.glob("Dockerfile-*.j2")):
        if f.name == "Dockerfile-base.j2":
            continue
        stem = f.name[len("Dockerfile-"):-len(".j2")]  # e.g. "cp2k-opensource-2025.2"
        if stem not in seen:
            versions.append(stem)
            seen.add(stem)

    return versions


def select_template(app: str, app_version: str, explicit_template: Path | None) -> Path:
    if explicit_template:
        if not explicit_template.exists():
            raise FileNotFoundError(f"Specified template not found: {explicit_template}")
        return explicit_template

    # 优先: spack-envs/<app-version>/Dockerfile.j2 (新布局，app_version 是完整目录名)
    env_dir = PROJECT_ROOT / "spack-envs" / app_version
    env_template = env_dir / "Dockerfile.j2"
    if env_template.exists():
        return env_template

    # 回退: spack-envs/<app>-<app-version>/Dockerfile.j2
    env_dir = PROJECT_ROOT / "spack-envs" / f"{app}-{app_version}"
    env_template = env_dir / "Dockerfile.j2"
    if env_template.exists():
        return env_template

    # Support user passing the template filename directly as app-version
    # e.g. "Dockerfile-cp2k-rocm-2026.1-gfx942" or "Dockerfile-cp2k-rocm-2026.1-gfx942.j2"
    raw = app_version
    if raw.startswith("Dockerfile-"):
        candidate = TEMPLATES_DIR / raw if raw.endswith(".j2") else TEMPLATES_DIR / f"{raw}.j2"
        if candidate.exists():
            return candidate

    # 回退: templates/Dockerfile-<app>-<app-version>.j2 (legacy)
    template_name = f"Dockerfile-{app}-{app_version}.j2"
    template_path = TEMPLATES_DIR / template_name
    if template_path.exists():
        return template_path

    available_versions = _extract_available_versions()
    available_list = "\n  ".join(available_versions)
    raise FileNotFoundError(
        f"No template found for --app-version '{app_version}'.\n"
        f"Available versions:\n  {available_list}\n"
        f"Usage: python generate.py build --app-version <version>"
    )


def render_template(template_path: Path, context: dict) -> str:
    logger.info("Rendering template: %s", template_path)

    env = Environment(
        loader=FileSystemLoader(str(template_path.parent)),
        trim_blocks=True,
        lstrip_blocks=True,
    )

    try:
        template = env.get_template(template_path.name)
    except TemplateNotFound as exc:
        raise FileNotFoundError(f"Template not found: {exc}") from exc

    try:
        return template.render(context)
    except TemplateSyntaxError as exc:
        raise RuntimeError(
            f"Jinja2 syntax error in {template_path}:{exc.lineno}: {exc.message}"
        ) from exc


def write_output(content: str, output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(content, encoding="utf-8")
    logger.info("Dockerfile written: %s", output_path)


def generate_dockerfile(
    *,
    config_path: Path,
    template: Path | None,
    app: str,
    app_version: str,
    output: Path,
    use_mirror: bool,
    build_only: bool,
) -> Path:
    config = load_config(config_path)
    template_path = select_template(app, app_version, template)
    context = build_context(
        config,
        use_mirror=use_mirror,
        build_only=build_only,
        app_version=app_version,
        template_path=template_path,
    )
    content = render_template(template_path, context)
    write_output(content, output)
    return output


def resolve_image_and_tag(
    *,
    app_version: str,
    template: Path | None,
    app: str,
    image_arg: str | None,
    tag_arg: str | None,
) -> tuple[str, str]:
    resolved_template = select_template(app, app_version, template)
    default_image, default_tag = infer_image_defaults(app_version, resolved_template)
    image = image_arg if image_arg else default_image
    tag = tag_arg if tag_arg else default_tag
    return image, tag


def build_docker_like(
    *,
    dockerfile: Path,
    image: str,
    tag: str,
    engine: str,
    network_host: bool,
    build_args: list[str] | None = None,
    build_opts: list[str] | None = None,
) -> None:
    if not check_command_exists(engine):
        raise RuntimeError(f"{engine} command not found in PATH")

    # Warn if non-host network is specified in build-opts
    non_host_mode = detect_non_host_network(build_opts)
    if non_host_mode:
        logger.warning(
            "Non-host network mode '%s' may prevent proxy access; prefer --network-host.",
            non_host_mode,
        )
    elif not network_host:
        logger.warning(
            "--network-host not set; build may fail if proxy or mirror access is required. "
            "Consider adding --network-host for reliable network access."
        )

    cmd = [engine, "build", "-f", str(dockerfile), "-t", f"{image}:{tag}"]
    if network_host:
        cmd += ["--network", "host"]
    for arg in (build_args or []):
        cmd += ["--build-arg", arg]
    for opt in (build_opts or []):
        # 每个 opt 可能是 "--key=value" 或 "--key value"，直接追加
        cmd += shlex.split(opt)
    cmd.append(".")
    run_cmd(cmd)


def build_apptainer(*, definition_file: Path, image: str, tag: str) -> None:
    if check_command_exists("apptainer"):
        tool = "apptainer"
    elif check_command_exists("singularity"):
        tool = "singularity"
    else:
        raise RuntimeError("Neither apptainer nor singularity command found in PATH")

    output_image = f"{image}_{tag}.sif"
    cmd = [tool, "build", "--force", "--fakeroot", output_image, str(definition_file)]
    run_cmd(cmd)


def mirror_env(base_env: dict[str, str], args: argparse.Namespace) -> dict[str, str]:
    env = dict(base_env)
    env["MIRROR_BUILDER_IMAGE"] = args.mirror_image
    env["MIRROR_CONTAINER_NAME"] = args.container_name
    env["PODMAN_CMD"] = args.podman_cmd
    if getattr(args, "podman_opt", None):
        expanded: list[str] = []
        for item in args.podman_opt:
            expanded.extend(shlex.split(item))
        env["EXTRA_PODMAN_OPTS"] = " ".join(expanded)
    return env


def detect_non_host_network(opts: list[str] | None) -> str | None:
    """Scan a list of options for a non-host --network/--net mode.

    Returns the detected mode string (e.g. 'bridge') or None if all host/absent.
    Works with both podman_opt ("--dns=8.8.8.8" per item) and build_opt format.
    """
    if not opts:
        return None

    tokens: list[str] = []
    for item in opts:
        tokens.extend(shlex.split(item))

    i = 0
    while i < len(tokens):
        token = tokens[i]
        mode: str | None = None

        if token.startswith("--network="):
            mode = token.split("=", 1)[1]
        elif token.startswith("--net="):
            mode = token.split("=", 1)[1]
        elif token in {"--network", "--net"}:
            if i + 1 < len(tokens):
                mode = tokens[i + 1]
                i += 1

        if mode and mode != "host":
            return mode
        i += 1

    return None


def call_mirror_script(
    command: str,
    *,
    args: argparse.Namespace,
    env_name: str | None,
    extra_args: list[str] | None = None,
) -> None:
    if not MIRROR_SCRIPT.exists():
        raise FileNotFoundError(f"Mirror helper script not found: {MIRROR_SCRIPT}")

    cmd = [str(MIRROR_SCRIPT)]
    if env_name:
        cmd += ["--env", env_name]
    cmd += ["--container-name", args.container_name, command]
    if extra_args:
        cmd += extra_args

    run_cmd(cmd, env=mirror_env(os.environ, args))


def call_prepare_bootstrap(
    *,
    args: argparse.Namespace,
    skip_image_build: bool,
    create_container: bool,
    use_container: bool,
) -> None:
    if not PREPARE_BOOTSTRAP_SCRIPT.exists():
        raise FileNotFoundError(
            f"Bootstrap helper script not found: {PREPARE_BOOTSTRAP_SCRIPT}"
        )

    cmd = [
        str(PREPARE_BOOTSTRAP_SCRIPT),
        "--container-name",
        args.container_name,
        "--image",
        args.mirror_image,
        "--podman",
        args.podman_cmd,
    ]

    if args.force_bootstrap:
        cmd.append("--force")
    if skip_image_build:
        cmd.append("--skip-image-build")
    if create_container:
        cmd.append("--create-container")
    if use_container:
        cmd.append("--use-container")

    run_cmd(cmd, env=mirror_env(os.environ, args))


def run_assets(args: argparse.Namespace) -> None:
    non_host_mode = detect_non_host_network(getattr(args, "podman_opt", None))
    if non_host_mode:
        logger.warning(
            "Non-host network mode '%s' may prevent proxy access; prefer --network=host.",
            non_host_mode,
        )

    # --env without value → list available environments
    if args.env == "__LIST__":
        envs = _list_available_envs()
        if envs:
            print("Available environments (--env <name>):")
            for e in envs:
                print(f"  {e}")
        else:
            print("No environments found under spack-envs/.")
        return

    image_ready = False

    def ensure_image() -> None:
        nonlocal image_ready
        if image_ready or args.skip_image_build:
            return
        call_mirror_script("image", args=args, env_name=None)
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

    if args.status:
        if not args.env:
            raise ValueError("--env is required for status")
        call_mirror_script("status", args=args, env_name=args.env)
        return

    # Default: one-command workflow
    if not action_flags:
        if not args.env:
            raise ValueError("--env is required for default assets workflow")

        ensure_image()
        if not args.skip_create_container:
            call_mirror_script("create-container", args=args, env_name=None)

        call_prepare_bootstrap(
            args=args,
            skip_image_build=True,
            create_container=not args.skip_create_container,
            use_container=not args.skip_create_container,
        )

        call_mirror_script("mirror", args=args, env_name=args.env)
        if not args.skip_verify:
            call_mirror_script("verify", args=args, env_name=args.env)
        return

    # Explicit actions mode
    if args.create_container:
        ensure_image()
        call_mirror_script("create-container", args=args, env_name=None)

    if args.prepare_bootstrap:
        ensure_image()
        call_prepare_bootstrap(
            args=args,
            skip_image_build=True,
            create_container=args.create_container,
            use_container=args.create_container,
        )

    if args.download_mirror:
        if not args.env:
            raise ValueError("--env is required with --download-mirror")
        ensure_image()
        call_mirror_script("mirror", args=args, env_name=args.env)

    if args.verify_mirror:
        if not args.env:
            raise ValueError("--env is required with --verify-mirror")
        ensure_image()
        call_mirror_script("verify", args=args, env_name=args.env)


def add_template_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--config",
        type=Path,
        default=CONFIGS_DIR / "versions.yaml",
        help="Path to versions.yaml config file",
    )
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
             "If omitted, defaults to opensource-2025.2. "
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


def _list_available_envs() -> list[str]:
    """List environment directories under spack-envs/ that contain env.yaml."""
    envs: list[str] = []
    spack_envs = PROJECT_ROOT / "spack-envs"
    if spack_envs.exists():
        for d in sorted(spack_envs.iterdir()):
            if d.is_dir() and (d / "env.yaml").exists():
                envs.append(d.name)
    return envs


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
            "  python generate.py dockerfile --app-version rocm-2026.1-gfx942\n"
            "  python generate.py build --app-version rocm-2026.1-gfx942\n"
            "  python generate.py assets --env cp2k-rocm-2026.1-gfx942\n"
            "  python generate.py assets --create-container\n"
            "  python generate.py assets --env cp2k-rocm-2026.1-gfx942 --download-mirror\n"
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
        help="Output image name (default auto: opensource->cp2k-opensource, rocm->cp2k-rocm)",
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
    assets_parser.add_argument(
        "--config",
        type=Path,
        default=CONFIGS_DIR / "versions.yaml",
        help="Path to versions.yaml config file",
    )
    add_assets_options(assets_parser)

    return parser


def run_new_cli(argv: list[str]) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.verbose:
        logger.setLevel(logging.DEBUG)

    # Handle --app-version without value → list available versions
    if getattr(args, "app_version", None) == "__LIST__":
        available_versions = _extract_available_versions()
        print("Available --app-version values:")
        for v in available_versions:
            print(f"  {v}")
        return 0

    # Default app-version when not specified at all
    if not getattr(args, "app_version", None):
        args.app_version = "opensource-2025.2"

    # 优先级：--no-mirror > --mirror > config（默认 true）
    if getattr(args, "no_mirror", False):
        use_mirror = False
    elif getattr(args, "mirror", False):
        use_mirror = True
    else:
        # 配置文件优先，默认 true
        config = load_config(args.config)
        use_mirror = config.get("spack", {}).get("use_mirror", True)

    if args.command == "dockerfile":
        generate_dockerfile(
            config_path=args.config,
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
            config_path=args.config,
            template=args.template,
            app=args.app,
            app_version=args.app_version,
            output=args.output,
            use_mirror=use_mirror,
            build_only=args.build_only,
        )

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
        run_assets(args)
        logger.info("Done")
        return 0

    parser.print_help()
    return 1


def build_legacy_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Legacy generate.py compatibility mode",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    add_template_options(parser)

    parser.add_argument("--build", action="store_true", help="Build image after generating Dockerfile")
    parser.add_argument(
        "--builder",
        choices=["docker", "apptainer"],
        default="docker",
        help="Legacy build engine option",
    )
    parser.add_argument(
        "--image",
        default=None,
        help="Image name (default auto by variant)",
    )
    parser.add_argument(
        "--tag",
        default=None,
        help="Image tag (default auto by variant)",
    )
    parser.add_argument("--dry-run", action="store_true", help="Generate only, skip build")
    parser.add_argument("-v", "--verbose", action="store_true", help="Enable verbose logs")
    return parser


def run_legacy_cli(argv: list[str]) -> int:
    parser = build_legacy_parser()
    args = parser.parse_args(argv)

    if args.verbose:
        logger.setLevel(logging.DEBUG)

    resolved_image, resolved_tag = resolve_image_and_tag(
        app_version=args.app_version,
        template=args.template,
        app=args.app,
        image_arg=args.image,
        tag_arg=args.tag,
    )

    dockerfile = generate_dockerfile(
        config_path=args.config,
        template=args.template,
        app=args.app,
        app_version=args.app_version,
        output=args.output,
        use_mirror=args.mirror,
        build_only=args.build_only,
    )

    if args.build and not args.dry_run:
        if args.builder == "apptainer":
            logger.info("Resolved image: %s:%s", resolved_image, resolved_tag)
            build_apptainer(definition_file=dockerfile, image=resolved_image, tag=resolved_tag)
        else:
            logger.info("Resolved image: %s:%s", resolved_image, resolved_tag)
            build_docker_like(
                dockerfile=dockerfile,
                image=resolved_image,
                tag=resolved_tag,
                engine="docker",
                network_host=False,
            )
    else:
        logger.info("Next: podman build -f %s -t %s:%s .", args.output, resolved_image, resolved_tag)

    logger.info("Done")
    return 0


def main() -> None:
    argv = sys.argv[1:]
    if not argv:
        parser = build_parser()
        parser.print_help()
        print("\nQuick start:")
        print("  python generate.py dockerfile --app-version rocm-2026.1-gfx942")
        print("  python generate.py build --app-version rocm-2026.1-gfx942")
        print("  python generate.py assets --env cp2k-rocm-2026.1-gfx942")
        print("\nLegacy mode is still supported:")
        print("  python generate.py --output Dockerfile --dry-run")
        sys.exit(0)

    try:
        if argv and argv[0] in SUBCOMMANDS:
            code = run_new_cli(argv)
        else:
            code = run_legacy_cli(argv)
    except (FileNotFoundError, RuntimeError, ValueError, subprocess.CalledProcessError) as exc:
        logger.error(str(exc))
        sys.exit(1)

    sys.exit(code)


if __name__ == "__main__":
    main()
