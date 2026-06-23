"""SIF building and apptainer management utilities."""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
from datetime import datetime
from pathlib import Path

from hpc_cf.config import (
    APPTAINER_LOCAL_PREFIX,
    APPTAINER_INSTALL_SCRIPT,
    PROJECT_ROOT,
    SCRIPTS_DIR,
    TOOLS_DIR,
)
from hpc_cf.template import (
    render_template,
    resolve_output_image_tag,
    select_template,
)

logger = logging.getLogger(__name__)


# ── Command helpers ──────────────────────────────────────────────────────


def check_command_exists(cmd: str) -> bool:
    return shutil.which(cmd) is not None


def find_apptainer() -> str:
    """Return the apptainer (or singularity) binary path.

    Search order:
      1. Locally installed under tools/apptainer/bin/
      2. System PATH (apptainer, then singularity)
    """
    local_bin = APPTAINER_LOCAL_PREFIX / "bin" / "apptainer"
    if local_bin.exists() and os.access(local_bin, os.X_OK):
        return str(local_bin)
    for cmd in ("apptainer", "singularity"):
        path = shutil.which(cmd)
        if path:
            return path
    return ""


def ensure_apptainer(*, auto_confirm: bool = False) -> str:
    """Ensure apptainer is available; install if missing.

    The install prompt is skipped when *auto_confirm* is True (e.g. the CLI
    ``--yes`` flag), making this safe for non-interactive/CI use. Without it,
    a non-interactive context (EOF on stdin) raises with a hint instead of
    silently refusing.
    """
    apptainer = find_apptainer()
    if apptainer:
        logger.info("Found apptainer: %s", apptainer)
        return apptainer

    required_cmds = ["curl", "rpm2cpio", "cpio"]
    missing = [cmd for cmd in required_cmds if not check_command_exists(cmd)]
    if missing:
        hint = "apt-get install -y " + " ".join(missing)
        raise RuntimeError(
            f"Missing required command(s): {', '.join(missing)}\n"
            f"Install them first, e.g.:\n"
            f"  sudo {hint}"
        )

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

    logger.info(
        "apptainer not found. Will install (unprivileged) to: %s",
        APPTAINER_LOCAL_PREFIX,
    )
    if not auto_confirm:
        try:
            answer = input("Proceed with installation? [y/N] ").strip().lower()
        except EOFError:
            raise RuntimeError(
                "apptainer installation requires confirmation but stdin is "
                "non-interactive. Re-run with --yes (or auto_confirm=True) to "
                "proceed without a prompt."
            ) from None
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
        ver = result.stdout.strip()
        return ver.rsplit(" ", 1)[-1] if " " in ver else ver
    except Exception as exc:
        logger.debug("apptainer --version failed: %s", exc)
        return "unknown"


def _human_size(n_bytes: int) -> str:
    """Return a human-readable file size string."""
    for unit in ("B", "KB", "MB", "GB"):
        if abs(n_bytes) < 1024:
            return f"{n_bytes:.0f} {unit}"
        n_bytes /= 1024
    return f"{n_bytes:.0f} TB"


def _find_def_template(app_version: str) -> Path | None:
    """Look for a *.def.j2 in the spack-envs/<app_version>/ directory."""
    env_dir = PROJECT_ROOT / "spack-envs" / app_version
    candidates = sorted(env_dir.glob("*.def.j2"))
    return candidates[0] if candidates else None


# ── Build helpers ────────────────────────────────────────────────────────


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


def run_cmd(
    cmd: list[str],
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
) -> None:
    import shlex

    run_cwd = cwd or PROJECT_ROOT
    logger.info("Running: %s", shlex.join(cmd))
    subprocess.run(cmd, cwd=run_cwd, env=env, check=True)


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
    import shlex

    if not check_command_exists(engine):
        raise RuntimeError(f"{engine} command not found in PATH")

    from hpc_cf.template import detect_non_host_network

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
        cmd += shlex.split(opt)
    cmd.append(".")
    run_cmd(cmd)

    # Tag the builder stage for debugging (instant — all layers cached)
    builder_tag = f"{image}:{tag}-builder"
    logger.info("Tagging builder stage: %s", builder_tag)
    builder_cmd = [engine, "build", "--target", "builder",
                   "-f", str(dockerfile), "-t", builder_tag]
    if network_host:
        builder_cmd += ["--network", "host"]
    for arg in (build_args or []):
        builder_cmd += ["--build-arg", arg]
    for opt in (build_opts or []):
        builder_cmd += shlex.split(opt)
    builder_cmd.append(".")
    try:
        run_cmd(builder_cmd)
    except Exception as exc:
        logger.warning("Could not tag builder stage: %s", exc)


def build_sif(
    *,
    docker_image: str,
    docker_tag: str,
    output: Path | None = None,
    app_version: str | None = None,
    mksquashfs_args: str = "-comp zstd -Xcompression-level 22 -b 1M",
    yes: bool = False,
) -> None:
    """Build a SIF image from an existing Docker/Podman OCI image."""
    apptainer = ensure_apptainer(auto_confirm=yes)
    artifacts_dir = PROJECT_ROOT / "artifacts"
    artifacts_dir.mkdir(parents=True, exist_ok=True)

    # Step 1: Export OCI image to tar
    oci_ref = f"{docker_image}:{docker_tag}"
    # Use only the last segment of the image name for filenames
    # e.g. "localhost/cp2k-opensource" → "cp2k-opensource"
    flat_image = docker_image.rsplit("/", 1)[-1]
    tar_name = f"{flat_image}_{docker_tag}.tar"
    tar_path = artifacts_dir / tar_name

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

    # Step 2: Render definition file
    def_template = _find_def_template(app_version) if app_version else None

    timestamp = datetime.now().isoformat()
    resolved_template = None
    if app_version:
        try:
            resolved_template = select_template("cp2k", app_version, None)
        except FileNotFoundError:
            pass
    default_image_name, default_image_tag = resolve_output_image_tag(resolved_template)

    def_context = {
        "docker_tar_filename": tar_name,
        "default_image_name": default_image_name,
        "default_image_tag": default_image_tag,
        "timestamp": timestamp,
    }

    if def_template:
        logger.info("Rendering def template: %s", def_template)
        def_content = render_template(def_template, def_context)

        def_file = artifacts_dir / f"{flat_image}_{docker_tag}.def"
        def_file.write_text(def_content, encoding="utf-8")
        logger.info("Definition file written: %s", def_file)

        sif_name = output or artifacts_dir / f"{flat_image}_{docker_tag}.sif"
        cmd = [apptainer, "build", "--force",
               "--mksquashfs-args", mksquashfs_args,
               str(sif_name), str(def_file)]
        run_cmd(cmd, cwd=artifacts_dir)
    else:
        logger.info("No def template found; building SIF directly from docker-archive")
        sif_name = output or artifacts_dir / f"{flat_image}_{docker_tag}.sif"
        cmd = [apptainer, "build", "--force",
               "--mksquashfs-args", mksquashfs_args,
               str(sif_name), f"docker-archive://{tar_name}"]
        run_cmd(cmd, cwd=artifacts_dir)

    sif_size = _human_size(Path(sif_name).stat().st_size)
    logger.info("✅ SIF built: %s (%s)", sif_name, sif_size)


def pack_apptainer(
    *,
    output: Path | None = None,
    no_sha256: bool = False,
) -> None:
    """Pack local apptainer installation into a makeself self-extracting archive."""
    if not APPTAINER_LOCAL_PREFIX.exists():
        raise RuntimeError(
            f"Local apptainer not found at {APPTAINER_LOCAL_PREFIX}. "
            "Run 'python -m hpc_cf build-sif --install-apptainer-only' first."
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

    artifacts_dir = PROJECT_ROOT / "artifacts"
    artifacts_dir.mkdir(parents=True, exist_ok=True)

    apptainer_ver = _get_apptainer_version()
    arch = os.uname().machine
    default_name = f"apptainer-{apptainer_ver}-{arch}.run"
    output_path = output or artifacts_dir / default_name

    staging_dir = artifacts_dir / "apptainer-bundle"
    if staging_dir.exists():
        shutil.rmtree(staging_dir)

    try:
        staging_dir.mkdir(parents=True)

        staging_apptainer = staging_dir / "apptainer"
        logger.info("Copying %s → %s ...", APPTAINER_LOCAL_PREFIX, staging_apptainer)
        shutil.copytree(APPTAINER_LOCAL_PREFIX, staging_apptainer, symlinks=True)

        shutil.copy2(activate_script, staging_dir / "activate-apptainer.sh")
        os.chmod(staging_dir / "activate-apptainer.sh", 0o755)

        total_size = sum(f.stat().st_size for f in staging_dir.rglob("*") if f.is_file())
        logger.info("Staging directory: %s (%s)", staging_dir, _human_size(total_size))

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
        if staging_dir.exists():
            logger.info("Cleaning up staging directory: %s", staging_dir)
            shutil.rmtree(staging_dir)
