#!/usr/bin/env python3
"""Unified CLI for Dockerfile generation and offline asset preparation.

Commands:
  dockerfile       render Dockerfile from Jinja2 template
  build            render + build image
  build-sif        convert OCI image to Apptainer SIF
  pack-apptainer   pack apptainer into portable self-extracting archive
  assets           prepare bootstrap/mirror assets
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
TOOLS_DIR = PROJECT_ROOT / "tools"
MIRROR_SCRIPT = SCRIPTS_DIR / "build-mirror-in-container.sh"
PREPARE_BOOTSTRAP_SCRIPT = SCRIPTS_DIR / "prepare-bootstrap-cache.sh"
APPTAINER_INSTALL_SCRIPT = TOOLS_DIR / "install-unprivileged.sh"
APPTAINER_LOCAL_PREFIX = TOOLS_DIR / "apptainer"



def check_command_exists(cmd: str) -> bool:
    return shutil.which(cmd) is not None


def find_apptainer() -> str:
    """Return the apptainer (or singularity) binary path.

    Search order:
      1. Locally installed under tools/apptainer/bin/
      2. System PATH (apptainer, then singularity)
    """
    # 1. Local install
    local_bin = APPTAINER_LOCAL_PREFIX / "bin" / "apptainer"
    if local_bin.exists() and os.access(local_bin, os.X_OK):
        return str(local_bin)
    # 2. System
    for cmd in ("apptainer", "singularity"):
        path = shutil.which(cmd)
        if path:
            return path
    return ""


def ensure_apptainer() -> str:
    """Ensure apptainer is available; install if missing.

    Always downloads install-unprivileged.sh from the upstream URL to ensure
    the latest version is used.  Checks for required host commands (curl,
    rpm2cpio, cpio) and prompts the user before proceeding with the
    installation.

    Uses tools/install-unprivileged.sh to install into tools/apptainer/.
    """
    apptainer = find_apptainer()
    if apptainer:
        logger.info("Found apptainer: %s", apptainer)
        return apptainer

    # Check required host commands first
    required_cmds = ["curl", "rpm2cpio", "cpio"]
    missing = [cmd for cmd in required_cmds if not check_command_exists(cmd)]
    if missing:
        hint = "apt-get install -y " + " ".join(missing)
        raise RuntimeError(
            f"Missing required command(s): {', '.join(missing)}\n"
            f"Install them first, e.g.:\n"
            f"  sudo {hint}"
        )

    # Always download the latest install script from upstream
    install_url = (
        "https://raw.githubusercontent.com/apptainer/apptainer"
        "/main/tools/install-unprivileged.sh"
    )
    logger.info("Downloading install-unprivileged.sh from upstream ...")
    TOOLS_DIR.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["curl", "-fsSL", "-o", str(APPTAINER_INSTALL_SCRIPT), install_url],
        check=True,
    )
    APPTAINER_INSTALL_SCRIPT.chmod(0o755)

    # Prompt user before installing
    logger.info(
        "apptainer not found. Will install (unprivileged) to: %s",
        APPTAINER_LOCAL_PREFIX,
    )
    try:
        answer = input("Proceed with installation? [y/N] ").strip().lower()
    except EOFError:
        answer = "n"
    if answer not in ("y", "yes"):
        raise RuntimeError("apptainer installation cancelled by user")

    APPTAINER_LOCAL_PREFIX.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["bash", str(APPTAINER_INSTALL_SCRIPT), str(APPTAINER_LOCAL_PREFIX)],
        check=True,
    )

    apptainer = find_apptainer()
    if not apptainer:
        raise RuntimeError(
            f"apptainer installation failed — binary not found in {APPTAINER_LOCAL_PREFIX}/bin/"
        )
    logger.info("✅ apptainer installed: %s", apptainer)
    return apptainer


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

    # For spack-envs layout (spack-envs/<env-dir>/Dockerfile.j2), derive tag
    # from the directory name by stripping the known app prefix.
    # This preserves variant suffixes like "-force-avx512".
    # Example: cp2k-opensource-2025.2-force-avx512 → tag = 2025.2-force-avx512
    #          cp2k-rocm-2026.1-gfx942         → tag = 2026.1-<gpu_arch>
    env_dir_name = template_path.parent.name.lower() if template_path else ""

    if "rocm" in variant_hint:
        arch = detect_gpu_arch_from_template(template_path) or "gfx942"
        # Prefer full tag from directory name (e.g. "2026.1-gfx942")
        rocm_prefix = "cp2k-rocm-"
        if env_dir_name.startswith(rocm_prefix):
            tag = env_dir_name[len(rocm_prefix):]
            # Replace gpu arch placeholder with detected arch if needed
            tag = re.sub(r"gfx\w+$", arch, tag)
        else:
            version = extract_version_token(template_name) or extract_version_token(appv) or "latest"
            tag = f"{version}-{arch}" if version != "latest" else arch
        return "cp2k-rocm", tag

    if "opensource" in variant_hint:
        oss_prefix = "cp2k-opensource-"
        if env_dir_name.startswith(oss_prefix):
            tag = env_dir_name[len(oss_prefix):]
        else:
            tag = extract_version_token(template_name) or extract_version_token(appv) or "latest"
        return "cp2k-opensource", tag

    return "hpc-cp2k", "latest"


def load_env_yaml(template_path: Path | None) -> dict:
    """Load env.yaml from the spack-env-file/ subdirectory or template directory."""
    if not template_path:
        return {}
    # New layout: spack-envs/<env>/Dockerfile.j2 + spack-envs/<env>/spack-env-file/env.yaml
    env_yaml = template_path.parent / "spack-env-file" / "env.yaml"
    if not env_yaml.exists():
        # Fallback: env.yaml alongside template (old layout)
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


def _human_size(n_bytes: int) -> str:
    """Return a human-readable file size string."""
    for unit in ("B", "KB", "MB", "GB"):
        if abs(n_bytes) < 1024:
            return f"{n_bytes:.0f} {unit}"
        n_bytes /= 1024
    return f"{n_bytes:.0f} TB"


def _find_def_template(app_version: str) -> Path | None:
    """Look for a cp2k.def.j2 in the spack-envs/<app_version>/ directory."""
    env_dir = PROJECT_ROOT / "spack-envs" / app_version
    candidate = env_dir / "cp2k.def.j2"
    return candidate if candidate.exists() else None


def _get_apptainer_version() -> str:
    """Extract version string from the locally installed apptainer."""
    apptainer = find_apptainer()
    if not apptainer:
        return "unknown"
    try:
        result = subprocess.run(
            [apptainer, "--version"],
            capture_output=True, text=True, check=True,
        )
        # Output is like "apptainer 1.4.5-3.el8" or just "1.4.5"
        ver = result.stdout.strip()
        # Take last space-separated field if there are multiple
        return ver.rsplit(" ", 1)[-1] if " " in ver else ver
    except Exception:
        return "unknown"


def pack_apptainer(
    *,
    output: Path | None = None,
    no_sha256: bool = False,
) -> None:
    """Pack local apptainer installation into a makeself self-extracting archive.

    Creates a portable ``.run`` file that bundles:
      - ``apptainer/`` directory (from ``tools/apptainer/``)
      - ``activate-apptainer.sh`` (from ``scripts/activate-apptainer.sh``)

    Uses gzip compression for maximum portability (gzip is available on every
    Linux system, unlike zstd which may not be installed on target HPC clusters).

    After extracting on a target machine, users just need to::

        source ./activate-apptainer.sh
        apptainer shell /path/to/image.sif

    Args:
        output: Output ``.run`` file path. Defaults to
            ``artifacts/apptainer-<version>-<arch>.run``.
        no_sha256: Skip SHA256 checksum if True.
    """
    # ── Pre-flight checks ────────────────────────────────────────────────
    if not APPTAINER_LOCAL_PREFIX.exists():
        raise RuntimeError(
            f"Local apptainer not found at {APPTAINER_LOCAL_PREFIX}. "
            "Run 'python generate.py build-sif --install-apptainer-only' first."
        )

    if not check_command_exists("makeself"):
        raise RuntimeError(
            "makeself not found. Install it first:\n"
            "  sudo apt install makeself    # Debian/Ubuntu\n"
            "  sudo dnf install makeself    # RHEL/Fedora"
        )

    activate_script = SCRIPTS_DIR / "activate-apptainer.sh"
    if not activate_script.exists():
        raise FileNotFoundError(f"activate-apptainer.sh not found at {activate_script}")

    # ── Determine output path ────────────────────────────────────────────
    artifacts_dir = PROJECT_ROOT / "artifacts"
    artifacts_dir.mkdir(parents=True, exist_ok=True)

    apptainer_ver = _get_apptainer_version()
    arch = os.uname().machine  # e.g. x86_64, aarch64
    default_name = f"apptainer-{apptainer_ver}-{arch}.run"
    output_path = output or artifacts_dir / default_name

    # ── Prepare staging directory in artifacts/ ──────────────────────────
    # makeself uses the staging directory name as the extraction subdirectory.
    # Use a clean name so the user gets: <target>/apptainer-bundle/
    staging_dir = artifacts_dir / f"apptainer-bundle"
    if staging_dir.exists():
        shutil.rmtree(staging_dir)

    try:
        staging_dir.mkdir(parents=True)

        # Use a clean directory name that will appear in the extracted archive
        staging_apptainer = staging_dir / "apptainer"
        logger.info("Copying %s → %s ...", APPTAINER_LOCAL_PREFIX, staging_apptainer)
        shutil.copytree(APPTAINER_LOCAL_PREFIX, staging_apptainer, symlinks=True)

        # Copy activate script into staging root
        shutil.copy2(activate_script, staging_dir / "activate-apptainer.sh")
        os.chmod(staging_dir / "activate-apptainer.sh", 0o755)

        # Compute uncompressed size
        total_size = sum(f.stat().st_size for f in staging_dir.rglob("*") if f.is_file())
        logger.info("Staging directory: %s (%s)", staging_dir, _human_size(total_size))

        # ── Build makeself command ───────────────────────────────────────
        # Use gzip (not zstd) for maximum portability — gzip is available on
        # every Linux system, while zstd may not be installed on target HPC
        # clusters.  The size difference (~35 MB vs ~58 MB) is acceptable.
        cmd = [
            "makeself",
            "--notemp",
            "--gzip",
            "--complevel", "9",
            "--tar-quietly",
            "--noprogress",
        ]
        if not no_sha256:
            cmd.append("--sha256")
        cmd += [
            str(staging_dir),
            str(output_path),
            f"Apptainer {apptainer_ver} ({arch})",
            "./activate-apptainer.sh",
        ]

        logger.info("Running makeself (gzip -9) ...")
        run_cmd(cmd)

        result_size = _human_size(output_path.stat().st_size)
        logger.info("✅ Packed: %s (%s, %.1fx compression)",
                     output_path, result_size, total_size / output_path.stat().st_size)

    finally:
        # Always clean up staging directory
        if staging_dir.exists():
            logger.info("Cleaning up staging directory: %s", staging_dir)
            shutil.rmtree(staging_dir)


def build_sif(
    *,
    docker_image: str,
    docker_tag: str,
    output: Path | None = None,
    app_version: str | None = None,
) -> None:
    """Build a SIF image from an existing Docker/Podman OCI image.

    Workflow:
      1. Ensure apptainer is available (install if needed).
      2. Export the OCI image as a tar to ``artifacts/``.
      3. Render ``cp2k.def.j2`` template (if available) or generate a minimal
         def file that uses ``Bootstrap: docker-archive``.
      4. Run ``apptainer build`` to produce the SIF in ``artifacts/``.

    Args:
        docker_image: OCI image name (e.g. 'cp2k-opensource').
        docker_tag: OCI image tag (e.g. '2025.2-force-avx512').
        output: Output SIF path. Defaults to ``artifacts/<image>_<tag>.sif``.
        app_version: Environment name (e.g. 'cp2k-opensource-2025.2-force-avx512')
                     used to locate a ``cp2k.def.j2`` template.
    """
    apptainer = ensure_apptainer()
    artifacts_dir = PROJECT_ROOT / "artifacts"
    artifacts_dir.mkdir(parents=True, exist_ok=True)

    # ── Step 1: Export OCI image to tar ──────────────────────────────────
    oci_ref = f"{docker_image}:{docker_tag}"
    tar_name = f"{docker_image}_{docker_tag}.tar"
    tar_path = artifacts_dir / tar_name

    # Detect available container engine for export
    engine = None
    for cmd in ("podman", "docker"):
        if check_command_exists(cmd):
            engine = cmd
            break
    if not engine:
        raise RuntimeError(
            "Neither podman nor docker found. "
            "Install one to export OCI images for SIF conversion."
        )

    if tar_path.exists():
        logger.info("Reusing existing OCI tar: %s", tar_path)
    else:
        logger.info("Exporting %s via %s ...", oci_ref, engine)
        run_cmd([engine, "save", "-o", str(tar_path), oci_ref])

    tar_size = _human_size(tar_path.stat().st_size)
    logger.info("OCI tar: %s (%s)", tar_path, tar_size)

    # ── Step 2: Render definition file ───────────────────────────────────
    def_template = _find_def_template(app_version) if app_version else None

    # Build context for Jinja2 rendering
    timestamp = datetime.now().isoformat()
    resolved_template = None
    if app_version:
        try:
            resolved_template = select_template("cp2k", app_version, None)
        except FileNotFoundError:
            pass
    default_image_name, default_image_tag = infer_image_defaults(
        app_version or docker_tag, resolved_template
    )

    def_context = {
        "docker_tar_filename": tar_name,
        "default_image_name": default_image_name,
        "default_image_tag": default_image_tag,
        "timestamp": timestamp,
    }

    if def_template:
        logger.info("Rendering def template: %s", def_template)
        def_content = render_template(def_template, def_context)
    else:
        # Fallback: minimal def file with MOTD via SINGULARITY_SHELL wrapper.
        # apptainer shell uses "bash --norc" which skips BASH_ENV, /etc/bash.bashrc,
        # and ~/.bashrc. The only hook is SINGULARITY_SHELL: if set and executable,
        # the shell action exec's it instead of "bash --norc".
        def_content = (
            f"Bootstrap: docker-archive\n"
            f"From: {tar_name}\n"
            f"\n"
            f"%environment\n"
            f"    export SINGULARITY_SHELL=/usr/local/bin/hpc-shell-wrapper.sh\n"
            f"\n"
            f"%post\n"
            f"    cat > /usr/local/bin/hpc-shell-wrapper.sh <<'WRAPPER_EOF'\n"
            f"#!/bin/bash\n"
            f'    if [ "${{APPTAINER_COMMAND:-}}" = "shell" ]; then\n'
            f"        /usr/local/bin/hpc-motd.sh 2>/dev/null || true\n"
            f"    fi\n"
            f'    exec /bin/bash --norc "$@"\n'
            f"WRAPPER_EOF\n"
            f"    chmod 755 /usr/local/bin/hpc-shell-wrapper.sh\n"
        )
        logger.info("Using auto-generated minimal def file")

    def_file = artifacts_dir / f"{docker_image}_{docker_tag}.def"
    def_file.write_text(def_content, encoding="utf-8")
    logger.info("Definition file written: %s", def_file)

    # ── Step 3: Build SIF ────────────────────────────────────────────────
    sif_name = output or artifacts_dir / f"{docker_image}_{docker_tag}.sif"

    try:
        cmd = [apptainer, "build", "--force", str(sif_name), str(def_file)]
        run_cmd(cmd, cwd=artifacts_dir)
        sif_size = _human_size(Path(sif_name).stat().st_size)
        logger.info("✅ SIF built: %s (%s)", sif_name, sif_size)
    finally:
        # Keep def file for reference (user can inspect / debug)
        pass


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
            if d.is_dir() and (
                (d / "spack-env-file" / "env.yaml").exists()
                or (d / "env.yaml").exists()
            ):
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
            "  python generate.py build-sif --app-version cp2k-opensource-2025.2-force-avx512\n"
            "  python generate.py build-sif --install-apptainer-only\n"
            "  python generate.py pack-apptainer\n"
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
        "--install-apptainer-only",
        action="store_true",
        help="Only install apptainer, do not build SIF",
    )

    return parser


def run_new_cli(argv: list[str]) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.verbose:
        logger.setLevel(logging.DEBUG)

    # ── pack-apptainer: does not need config/template/mirror ──
    if args.command == "pack-apptainer":
        pack_apptainer(
            output=getattr(args, "output", None),
            no_sha256=getattr(args, "no_sha256", False),
        )
        return 0

    # ── build-sif: does not need config/template/mirror ──
    if args.command == "build-sif":
        # --app-version without value → list available versions
        if getattr(args, "app_version", None) == "__LIST__":
            available_versions = _extract_available_versions()
            print("Available --app-version values:")
            for v in available_versions:
                print(f"  {v}")
            return 0

        # Install-only mode
        if getattr(args, "install_apptainer_only", False):
            apptainer_path = ensure_apptainer()
            print(f"apptainer installed: {apptainer_path}")
            return 0

        # Resolve OCI image name and tag
        docker_image = args.docker_image
        docker_tag = args.docker_tag

        if not docker_image or not docker_tag:
            if not getattr(args, "app_version", None):
                logger.error(
                    "Specify --docker-image and --docker-tag, "
                    "or --app-version for auto-detection."
                )
                return 1
            # Resolve template just for image/tag inference
            try:
                resolved_template = select_template(args.app, args.app_version, None)
            except FileNotFoundError:
                resolved_template = None
            auto_image, auto_tag = infer_image_defaults(args.app_version, resolved_template)
            docker_image = docker_image or auto_image
            docker_tag = docker_tag or auto_tag

        build_sif(
            docker_image=docker_image,
            docker_tag=docker_tag,
            output=args.output,
            app_version=getattr(args, "app_version", None),
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


def main() -> None:
    argv = sys.argv[1:]
    if not argv:
        parser = build_parser()
        parser.print_help()
        print("\nQuick start:")
        print("  python generate.py dockerfile --app-version rocm-2026.1-gfx942")
        print("  python generate.py build --app-version rocm-2026.1-gfx942")
        print("  python generate.py assets --env cp2k-rocm-2026.1-gfx942")
        print("  python generate.py build-sif --app-version cp2k-opensource-2025.2-force-avx512")
        print("  python generate.py pack-apptainer")
        sys.exit(0)

    try:
        code = run_new_cli(argv)
    except (FileNotFoundError, RuntimeError, ValueError, subprocess.CalledProcessError) as exc:
        logger.error(str(exc))
        sys.exit(1)

    sys.exit(code)


if __name__ == "__main__":
    main()
