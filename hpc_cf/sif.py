"""SIF building and apptainer management utilities."""

from __future__ import annotations

import hashlib
import logging
import os
import re
import shutil
import subprocess
from datetime import datetime
from pathlib import Path

from hpc_cf.config import (
    APPTAINER_INSTALL_SCRIPT,
    APPTAINER_INSTALL_SCRIPT_SHA256,
    APPTAINER_INSTALL_SCRIPT_URL,
    APPTAINER_LOCAL_PREFIX,
    PROJECT_ROOT as _CONFIG_PROJECT_ROOT,
    SCRIPTS_DIR,
    TOOLS_DIR,
)
from hpc_cf.execution import ProjectLayout
from hpc_cf.template import (
    render_template,
    resolve_output_image_tag,
    select_template,
)

logger = logging.getLogger(__name__)

# Module-level alias so tests can ``monkeypatch.setattr(sif, "PROJECT_ROOT", ...)``.
# Prefer :class:`~hpc_cf.execution.ProjectLayout` for new call paths.
PROJECT_ROOT = _CONFIG_PROJECT_ROOT

# Filename-safe subset for artifact basenames derived from OCI image/tag.
_SAFE_FILENAME_RE = re.compile(r"[^A-Za-z0-9._+-]+")


def _layout() -> ProjectLayout:
    return ProjectLayout(project_root=PROJECT_ROOT)


def _resolve_output_path(output: Path) -> Path:
    """Absolutize *output* relative to the process cwd before spawning a tool.

    Callers may pass a relative path while ``apptainer build`` runs with
    ``cwd=artifacts/``; resolving up-front keeps post-build ``stat`` correct.
    """
    path = output.expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    return path.resolve()


def _safe_filename_component(value: str) -> str:
    """Return a single path component safe for use under ``artifacts/``.

    Rejects empty / ``.`` / ``..`` after stripping separators and other
    characters that could escape the artifacts directory via ``Path`` joins.
    """
    cleaned = _SAFE_FILENAME_RE.sub("_", value.strip()).strip("_")
    if not cleaned or cleaned in {".", ".."} or set(cleaned) <= {"."}:
        raise ValueError(
            f"unsafe filename component after sanitization: {value!r}"
        )
    return cleaned


def _require_under(path: Path, root: Path, *, what: str) -> Path:
    """Fail closed when *path* resolves outside *root*."""
    resolved = path.resolve()
    root_resolved = root.resolve()
    if not resolved.is_relative_to(root_resolved):
        raise ValueError(
            f"{what} path escapes allowed root {root_resolved}: {resolved}"
        )
    return resolved


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _fetch_and_verify_install_script() -> Path:
    """Download the pinned install script and verify SHA256 (fail-closed)."""
    TOOLS_DIR.mkdir(parents=True, exist_ok=True)
    dest = APPTAINER_INSTALL_SCRIPT
    expected = APPTAINER_INSTALL_SCRIPT_SHA256

    if dest.is_file() and _sha256_file(dest) == expected:
        logger.info(
            "Using cached install-unprivileged.sh (sha256 verified)"
        )
    else:
        logger.info(
            "Downloading install-unprivileged.sh from pinned URL: %s",
            APPTAINER_INSTALL_SCRIPT_URL,
        )
        subprocess.run(
            ["curl", "-fsSL", "-o", str(dest), APPTAINER_INSTALL_SCRIPT_URL],
            check=True,
        )
        actual = _sha256_file(dest)
        if actual != expected:
            dest.unlink(missing_ok=True)
            raise RuntimeError(
                "Apptainer install script SHA256 mismatch "
                f"(expected {expected}, got {actual}). "
                "Refusing to execute untrusted script."
            )

    # Re-check immediately before chmod/exec so a TOCTOU race or a stale
    # cache write cannot skip verification.
    actual = _sha256_file(dest)
    if actual != expected:
        dest.unlink(missing_ok=True)
        raise RuntimeError(
            "Apptainer install script SHA256 mismatch "
            f"(expected {expected}, got {actual}). "
            "Refusing to execute untrusted script."
        )
    dest.chmod(0o755)
    return dest


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

    install_script = _fetch_and_verify_install_script()
    APPTAINER_LOCAL_PREFIX.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["bash", str(install_script), str(APPTAINER_LOCAL_PREFIX)],
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


def _find_def_template(
    app_version: str,
    *,
    layout: ProjectLayout | None = None,
) -> Path | None:
    """Look for a *.def.j2 in the spack-envs/<app_version>/ directory."""
    env_dir = (layout or _layout()).spack_envs_dir / app_version
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
    layout: ProjectLayout | None = None,
) -> None:
    import shlex

    run_cwd = cwd or (layout or _layout()).project_root
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

    def _stage_command(target: str, output_tag: str) -> list[str]:
        command = [
            engine, "build", "--target", target,
            "-f", str(dockerfile), "-t", output_tag,
        ]
        if network_host:
            command += ["--network", "host"]
        for arg in build_args or []:
            command += ["--build-arg", arg]
        for opt in build_opts or []:
            command += shlex.split(opt)
        command.append(".")
        return command

    # Preserve both checkpoints: installed is publishable before gc/view/strip,
    # while builder keeps its historical post-install debugging semantics.
    supports_installed = (
        dockerfile.is_file()
        and "AS builder-installed" in dockerfile.read_text(encoding="utf-8")
    )
    if supports_installed:
        installed_tag = f"{image}:{tag}-installed"
        logger.info("Building installed stage: %s", installed_tag)
        run_cmd(_stage_command("builder-installed", installed_tag))

    builder_tag = f"{image}:{tag}-builder"
    logger.info("Building builder stage: %s", builder_tag)
    run_cmd(_stage_command("builder", builder_tag))

    # Continue to final image; builder layers come from cache.
    final_tag = f"{image}:{tag}"
    logger.info("Building final image: %s", final_tag)
    cmd = [engine, "build", "-f", str(dockerfile), "-t", final_tag]
    if network_host:
        cmd += ["--network", "host"]
    for arg in build_args or []:
        cmd += ["--build-arg", arg]
    for opt in build_opts or []:
        cmd += shlex.split(opt)
    cmd.append(".")
    run_cmd(cmd)


def build_docker_stage(
    *,
    dockerfile: Path,
    image_ref: str,
    target: str,
    engine: str,
    network_host: bool,
    build_args: list[str] | None = None,
    build_opts: list[str] | None = None,
) -> None:
    """Build one named OCI stage for producer workflows."""
    import shlex

    if not check_command_exists(engine):
        raise RuntimeError(f"{engine} command not found in PATH")
    command = [
        engine,
        "build",
        "--target",
        target,
        "-f",
        str(dockerfile),
        "-t",
        image_ref,
    ]
    if network_host:
        command += ["--network", "host"]
    for arg in build_args or []:
        command += ["--build-arg", arg]
    for opt in build_opts or []:
        command += shlex.split(opt)
    command.append(".")
    run_cmd(command)


def build_sif(
    *,
    docker_image: str,
    docker_tag: str,
    output: Path | None = None,
    app_version: str | None = None,
    mksquashfs_args: str = "-comp zstd -Xcompression-level 22 -b 1M",
    yes: bool = False,
    layout: ProjectLayout | None = None,
) -> None:
    """Build a SIF image from an existing Docker/Podman OCI image."""
    apptainer = ensure_apptainer(auto_confirm=yes)
    root = layout or _layout()
    artifacts_dir = root.artifacts_dir
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    project_root = root.project_root

    # Step 1: Export OCI image to tar
    oci_ref = f"{docker_image}:{docker_tag}"
    # Use only the last segment of the image name for filenames
    # e.g. "localhost/cp2k-opensource" → "cp2k-opensource"
    flat_image = _safe_filename_component(docker_image.rsplit("/", 1)[-1])
    safe_tag = _safe_filename_component(docker_tag)
    tar_name = f"{flat_image}_{safe_tag}.tar"
    tar_path = _require_under(
        artifacts_dir / tar_name, artifacts_dir, what="OCI tar"
    )

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
        run_cmd([engine, "save", "-o", str(tar_path), oci_ref], layout=root)

    tar_size = _human_size(tar_path.stat().st_size)
    logger.info("OCI tar: %s (%s)", tar_path, tar_size)

    # Step 2: Render definition file
    def_template = (
        _find_def_template(app_version, layout=root) if app_version else None
    )

    timestamp = datetime.now().isoformat()
    resolved_template = None
    if app_version:
        try:
            resolved_template = select_template(app_version, None, layout=root)
        except FileNotFoundError:
            pass
    default_image_name, default_image_tag = resolve_output_image_tag(resolved_template)

    def_context = {
        "docker_tar_filename": tar_name,
        "default_image_name": default_image_name,
        "default_image_tag": default_image_tag,
        "timestamp": timestamp,
    }

    # Resolve relative --output against process cwd *before* apptainer runs
    # with cwd=artifacts/ (otherwise post-build Path.stat looks in the wrong place).
    # Explicit outputs must stay under the project root; default SIF under artifacts/.
    if output is not None:
        sif_name = _require_under(
            _resolve_output_path(output), project_root, what="SIF output"
        )
        sif_name.parent.mkdir(parents=True, exist_ok=True)
    else:
        sif_name = _require_under(
            artifacts_dir / f"{flat_image}_{safe_tag}.sif",
            artifacts_dir,
            what="SIF output",
        )

    if def_template:
        logger.info("Rendering def template: %s", def_template)
        def_content = render_template(def_template, def_context, layout=root)

        def_file = _require_under(
            artifacts_dir / f"{flat_image}_{safe_tag}.def",
            artifacts_dir,
            what="definition file",
        )
        def_file.write_text(def_content, encoding="utf-8")
        logger.info("Definition file written: %s", def_file)

        cmd = [apptainer, "build", "--force",
               "--mksquashfs-args", mksquashfs_args,
               str(sif_name), str(def_file)]
        run_cmd(cmd, cwd=artifacts_dir, layout=root)
    else:
        logger.info("No def template found; building SIF directly from docker-archive")
        cmd = [apptainer, "build", "--force",
               "--mksquashfs-args", mksquashfs_args,
               str(sif_name), f"docker-archive://{tar_name}"]
        run_cmd(cmd, cwd=artifacts_dir, layout=root)

    sif_size = _human_size(sif_name.stat().st_size)
    logger.info("✅ SIF built: %s (%s)", sif_name, sif_size)


def pack_apptainer(
    *,
    output: Path | None = None,
    no_sha256: bool = False,
    layout: ProjectLayout | None = None,
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

    root = layout or _layout()
    artifacts_dir = root.artifacts_dir
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    project_root = root.project_root

    apptainer_ver = _get_apptainer_version()
    arch = os.uname().machine
    default_name = (
        f"apptainer-{_safe_filename_component(apptainer_ver)}"
        f"-{_safe_filename_component(arch)}.run"
    )
    if output is not None:
        output_path = _require_under(
            _resolve_output_path(output), project_root, what="pack-apptainer output"
        )
        output_path.parent.mkdir(parents=True, exist_ok=True)
    else:
        output_path = _require_under(
            artifacts_dir / default_name, artifacts_dir, what="pack-apptainer output"
        )

    staging_dir = _require_under(
        artifacts_dir / "apptainer-bundle", artifacts_dir, what="pack staging"
    )
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
        run_cmd(cmd, layout=root)

        result_size = _human_size(output_path.stat().st_size)
        logger.info("✅ Packed: %s (%s, %.1fx compression)",
                     output_path, result_size, total_size / output_path.stat().st_size)

    finally:
        if staging_dir.exists():
            logger.info("Cleaning up staging directory: %s", staging_dir)
            shutil.rmtree(staging_dir)
