"""SIF building and apptainer management utilities."""

from __future__ import annotations

import hashlib
import logging
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from hpc_cf.atomic_io import atomic_write_text
from hpc_cf.config import (
    APPTAINER_INSTALL_SCRIPT,
    APPTAINER_INSTALL_SCRIPT_SHA256,
    APPTAINER_INSTALL_SCRIPT_URL,
    APPTAINER_LOCAL_PREFIX,
    SCRIPTS_DIR,
    TOOLS_DIR,
)
from hpc_cf.config import (
    PROJECT_ROOT as _CONFIG_PROJECT_ROOT,
)
from hpc_cf.execution import ProjectLayout

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


SHM_MIN_BYTES = 4 * 1024**3


def _select_sif_staging_dir(
    image_size_bytes: int,
    *,
    shm_root: Path = Path("/dev/shm"),
) -> Path | None:
    """Return a writable ``/dev/shm`` temp dir when RAM free space suffices."""
    if not shm_root.is_dir() or not os.access(shm_root, os.W_OK):
        return None
    required = max(
        int(2.5 * max(image_size_bytes, 0)) + 2 * 1024**3,
        SHM_MIN_BYTES,
    )
    try:
        free = shutil.disk_usage(shm_root).free
    except OSError:
        return None
    if free < required:
        return None
    return Path(tempfile.mkdtemp(prefix="hpc-cf-sif-", dir=str(shm_root)))


# ── Build helpers ────────────────────────────────────────────────────────


def _oci_tar_id_sidecar(tar_path: Path) -> Path:
    """Sidecar path recording the OCI image Id that produced *tar_path*."""
    return tar_path.with_name(tar_path.name + ".id")


def _read_oci_tar_image_id(tar_path: Path) -> str | None:
    sidecar = _oci_tar_id_sidecar(tar_path)
    if not sidecar.is_file():
        return None
    recorded = sidecar.read_text(encoding="utf-8").strip()
    return recorded or None


def _write_oci_tar_image_id(tar_path: Path, image_id: str) -> None:
    sidecar = _oci_tar_id_sidecar(tar_path)
    atomic_write_text(sidecar, f"{image_id}\n")


def inspect_local_image_id(
    engine: str,
    image_ref: str,
    *,
    cwd: Path | None = None,
) -> str | None:
    """Return the local image Id for *image_ref*, or None if missing."""
    try:
        result = subprocess.run(
            [engine, "image", "inspect", "--format", "{{.Id}}", image_ref],
            cwd=cwd,
            text=True,
            capture_output=True,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    image_id = result.stdout.strip()
    return image_id or None


def inspect_local_image_size(
    engine: str,
    image_ref: str,
    *,
    cwd: Path | None = None,
) -> int:
    """Return local OCI image size in bytes, or 0 if unavailable."""
    try:
        result = subprocess.run(
            [engine, "image", "inspect", "--format", "{{.Size}}", image_ref],
            cwd=cwd,
            text=True,
            capture_output=True,
            check=True,
        )
        return max(0, int(result.stdout.strip()))
    except (OSError, subprocess.CalledProcessError, ValueError):
        return 0


def select_engine_with_image(
    image_ref: str,
    *,
    cwd: Path | None = None,
) -> tuple[str, str]:
    """Pick podman/docker that actually has *image_ref*.

    Returns ``(engine, image_id)``. Prefers podman when both have the image.
    """
    candidates = [cmd for cmd in ("podman", "docker") if check_command_exists(cmd)]
    if not candidates:
        raise RuntimeError(
            "Neither podman nor docker found. "
            "Install one to export OCI images for SIF conversion."
        )

    tried: list[str] = []
    for engine in candidates:
        image_id = inspect_local_image_id(engine, image_ref, cwd=cwd)
        if image_id:
            return engine, image_id
        tried.append(engine)

    raise RuntimeError(
        f"Image {image_ref!r} not found locally in: {', '.join(tried)}. "
        "Build or pull the image first, then retry build-sif."
    )


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


def _secret_flags(build_secrets: list[str] | None) -> list[str]:
    """Render ``ID=ENVVAR`` build-secret requests as engine flags.

    The env-var form keeps credential values out of the command line (and
    therefore out of logs): the engine reads them from the environment at
    build time and mounts them via ``RUN --mount=type=secret``.
    """
    flags: list[str] = []
    for entry in build_secrets or []:
        secret_id, sep, env_name = entry.partition("=")
        if not sep or not secret_id or not env_name:
            raise ValueError(
                f"invalid --build-secret entry {entry!r}; expected ID=ENVVAR"
            )
        flags += ["--secret", f"id={secret_id},env={env_name}"]
    return flags


def _force_rm_flags(build_opts: list[str] | None) -> list[str]:
    """Default ``--force-rm`` unless overridden in *build_opts*.

    Ensures failed RUN steps destroy their Buildah working container instead of
    pinning OverlayFS parent layers indefinitely.
    """
    import shlex

    for opt in build_opts or []:
        for token in shlex.split(opt):
            if token == "--force-rm" or token.startswith("--force-rm="):
                return []
    return ["--force-rm"]


def build_docker_like(
    *,
    dockerfile: Path,
    image: str,
    tag: str,
    engine: str,
    network_host: bool,
    build_args: list[str] | None = None,
    build_opts: list[str] | None = None,
    build_secrets: list[str] | None = None,
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

    secret_flags = _secret_flags(build_secrets)
    force_rm = _force_rm_flags(build_opts)

    final_tag = f"{image}:{tag}"
    logger.info("Building final image: %s", final_tag)
    cmd = [engine, "build", *force_rm, "-f", str(dockerfile), "-t", final_tag]
    if network_host:
        cmd += ["--network", "host"]
    cmd += secret_flags
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
    build_secrets: list[str] | None = None,
) -> None:
    """Build one named OCI stage for producer workflows."""
    import shlex

    if not check_command_exists(engine):
        raise RuntimeError(f"{engine} command not found in PATH")
    command = [
        engine,
        "build",
        *_force_rm_flags(build_opts),
        "--target",
        target,
        "-f",
        str(dockerfile),
        "-t",
        image_ref,
    ]
    if network_host:
        command += ["--network", "host"]
    command += _secret_flags(build_secrets)
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
    smoke_cmd: str | None = None,
    yes: bool = False,
    layout: ProjectLayout | None = None,
) -> None:
    """Build a SIF image directly from an OCI image via ``docker-archive://``."""
    del app_version  # Accepted for CLI/API compat; runtime env bakes SINGULARITY_SHELL.
    apptainer = ensure_apptainer(auto_confirm=yes)
    root = layout or _layout()
    artifacts_dir = root.artifacts_dir
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    project_root = root.project_root

    oci_ref = f"{docker_image}:{docker_tag}"
    flat_image = _safe_filename_component(docker_image.rsplit("/", 1)[-1])
    safe_tag = _safe_filename_component(docker_tag)
    tar_name = f"{flat_image}_{safe_tag}.tar"

    # Resolve SIF output under project_root BEFORE exporting or creating temp dirs.
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

    engine, image_id = select_engine_with_image(oci_ref, cwd=project_root)
    logger.info("Using %s image Id %s for %s", engine, image_id, oci_ref)
    image_size = inspect_local_image_size(engine, oci_ref, cwd=project_root)
    shm_staging = _select_sif_staging_dir(image_size)

    try:
        if shm_staging is not None:
            shm_tmpdir = shm_staging / "tmp"
            shm_tmpdir.mkdir(parents=True, exist_ok=True)
            tar_path = shm_staging / tar_name
            logger.info("Staging OCI tar and APPTAINER_TMPDIR in /dev/shm: %s", shm_staging)
            run_cmd([engine, "save", "-o", str(tar_path), oci_ref], layout=root)
            tar_size = _human_size(tar_path.stat().st_size)
            logger.info("OCI tar (/dev/shm): %s (%s)", tar_path, tar_size)
            cmd = [
                apptainer, "build", "--force", "--disable-cache",
                "--mksquashfs-args", mksquashfs_args,
                str(sif_name), f"docker-archive://{tar_path}",
            ]
            run_env = {
                **os.environ,
                "APPTAINER_TMPDIR": str(shm_tmpdir),
                "SINGULARITY_TMPDIR": str(shm_tmpdir),
            }
            run_cmd(cmd, cwd=shm_staging, env=run_env, layout=root)
        else:
            tar_path = _require_under(
                artifacts_dir / tar_name, artifacts_dir, what="OCI tar"
            )
            recorded_id = _read_oci_tar_image_id(tar_path) if tar_path.exists() else None
            if tar_path.exists() and recorded_id == image_id:
                logger.info(
                    "Reusing existing OCI tar (image Id match): %s", tar_path
                )
            else:
                if tar_path.exists():
                    logger.info(
                        "OCI tar stale or missing Id sidecar "
                        "(recorded=%s, current=%s); re-exporting",
                        recorded_id,
                        image_id,
                    )
                    tar_path.unlink(missing_ok=True)
                    _oci_tar_id_sidecar(tar_path).unlink(missing_ok=True)
                logger.info("Exporting %s via %s ...", oci_ref, engine)
                run_cmd([engine, "save", "-o", str(tar_path), oci_ref], layout=root)
                _write_oci_tar_image_id(tar_path, image_id)

            tar_size = _human_size(tar_path.stat().st_size)
            logger.info("OCI tar: %s (%s)", tar_path, tar_size)
            cmd = [
                apptainer, "build", "--force", "--disable-cache",
                "--mksquashfs-args", mksquashfs_args,
                str(sif_name), f"docker-archive://{tar_name}",
            ]
            run_cmd(cmd, cwd=artifacts_dir, layout=root)
    finally:
        if shm_staging is not None and shm_staging.exists():
            shutil.rmtree(shm_staging, ignore_errors=True)

    sif_size = _human_size(sif_name.stat().st_size)
    logger.info("✅ SIF built: %s (%s)", sif_name, sif_size)

    if smoke_cmd:
        logger.info("Running SIF smoke check: %s", smoke_cmd)
        run_cmd(
            [apptainer, "exec", str(sif_name), "bash", "-lc", smoke_cmd],
            layout=root,
        )


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
