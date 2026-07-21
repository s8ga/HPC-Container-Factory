"""Buildcache policy and dedicated Spack publisher execution."""

from __future__ import annotations

import hashlib
import json
import logging
import re
import shlex
import subprocess
import uuid
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from hpc_cf.buildcache_ops import build_publish_script, build_verify_script
from hpc_cf.config import IMAGE_SPACK_ROOT
from hpc_cf.environment import BuildcachePolicy
from hpc_cf.execution import (
    BuildcacheCoverageRecord,
    BuildcacheRun,
    ProjectLayout,
    SharedBuildcacheStore,
)

logger = logging.getLogger(__name__)

_COUNT_RE = re.compile(r"^HPC_CF_CHECKED_SPEC_COUNT=(\d+)$", re.MULTILINE)
_PUSHED_COUNT_RE = re.compile(r"^HPC_CF_PUSHED_SPEC_COUNT=(\d+)$", re.MULTILINE)
_PUBLISH_STEP_RE = re.compile(
    r"^HPC_CF_BUILDCACHE_STEP=(publish|update-index|check)$",
    re.MULTILINE,
)
_IMAGE_LOCK_SHA_RE = re.compile(
    r"^HPC_CF_IMAGE_LOCK_SHA256=([0-9a-f]{64})$",
    re.MULTILINE,
)
DEFAULT_OPERATION_TIMEOUT_SECONDS = 24 * 60 * 60
_COVERAGE_PARTIAL_SUFFIX = ".partial"


def publish_output_markers(output: str) -> dict[str, object]:
    """Parse publisher stdout markers for partial-publish provenance."""
    pushed = _PUSHED_COUNT_RE.search(output)
    checked = _COUNT_RE.search(output)
    return {
        "partial_publish": "HPC_CF_PARTIAL_PUBLISH=1" in output,
        "pushed_spec_count": int(pushed.group(1)) if pushed else None,
        "checked_spec_count": int(checked.group(1)) if checked else None,
    }


def producer_image_ref(image: str, tag: str) -> str:
    """Return the stable OCI tag reserved for buildcache publication."""
    return f"{image}:{tag}-buildcache-producer"


def temporary_producer_image_ref(image: str, tag: str, run_id: str) -> str:
    """Return a run-unique producer tag that concurrent builds cannot share."""
    safe_run_id = re.sub(r"[^A-Za-z0-9_.-]", "-", run_id)
    return f"{image}:{tag}-buildcache-producer-{safe_run_id}"


def promote_producer_image(
    *,
    engine: str,
    temporary_ref: str,
    stable_ref: str,
    layout: ProjectLayout,
) -> None:
    """Atomically retag one completed temporary producer as the stable producer."""
    subprocess.run(
        [engine, "tag", temporary_ref, stable_ref],
        cwd=layout.project_root,
        text=True,
        capture_output=True,
        check=True,
    )


def remove_temporary_image(
    *,
    engine: str,
    image_ref: str,
    layout: ProjectLayout,
) -> None:
    """Best-effort cleanup for a run-unique producer image."""
    try:
        subprocess.run(
            [engine, "image", "rm", image_ref],
            cwd=layout.project_root,
            text=True,
            capture_output=True,
            check=False,
            timeout=300,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning("Could not remove temporary producer image %s: %s", image_ref, exc)


def require_verified_source_mirror(
    layout: ProjectLayout,
    *,
    env_name: str,
    lock_path: Path,
    spack_version: str,
) -> Path:
    """Require a non-empty mirror and matching successful assets verification."""
    mirror = layout.spack_mirror_dir
    if not mirror.is_dir():
        raise RuntimeError(
            f"source mirror is missing: {mirror}; run `hpc_cf assets --env "
            f"{env_name} --verify-mirror` before buildcache build"
        )
    has_payload = any(
        path.is_file() and ".hpc_cf" not in path.relative_to(mirror).parts
        for path in mirror.rglob("*")
    )
    if not has_payload:
        raise RuntimeError(
            f"source mirror is empty: {mirror}; run assets download and verify "
            "before buildcache build"
        )

    lock_hash = hashlib.sha256(lock_path.read_bytes()).hexdigest()
    manifests = sorted(layout.mirror_runs_dir.glob("*/manifest.json"), reverse=True)
    for manifest in manifests:
        try:
            payload = json.loads(manifest.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        stats = payload.get("stats")
        if not (
            payload.get("env") == env_name
            and payload.get("spack_version") == spack_version
            and payload.get("lock_hash") == lock_hash
        ):
            continue
        if (
            payload.get("status") == "success"
            and isinstance(stats, dict)
            and stats.get("failed") == 0
            and (manifest.parent / "mirror-verify.log").is_file()
        ):
            return manifest
        raise RuntimeError(
            "source mirror latest matching assets run is not a successful "
            f"verification: {manifest}"
        )
    raise RuntimeError(
        "source mirror has no successful assets verification for this exact "
        f"environment, Spack version, and lock SHA; run `hpc_cf assets --env "
        f"{env_name} --verify-mirror` before buildcache build"
    )


def _healthy(store: SharedBuildcacheStore) -> bool:
    """True only when health claims healthy *and* its coverage file exists.

    Fail-closed: a healthy claim without a visible coverage file (crash window
    after ``mark_healthy`` / before rename under the old order, or a corrupted
    sidecar) must not admit consumers.
    """
    layout = store.layout
    if not layout.spack_buildcache_dir.is_dir():
        return False
    try:
        health = store.read_health()
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return False
    if health.get("healthy") is not True:
        return False
    coverage_path = health.get("coverage_path")
    if not isinstance(coverage_path, str) or not coverage_path:
        return False
    return Path(coverage_path).is_file()


def _has_successful_lock_coverage(
    store: SharedBuildcacheStore,
    lock_path: Path | None,
) -> bool:
    """True when this lock has a successful non-external coverage record."""
    if lock_path is None or not lock_path.is_file():
        return False
    if not store.layout.spack_buildcache_dir.is_dir():
        return False
    path = coverage_path_for_lock(store.layout, lock_path)
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return False
    return (
        record.get("schema_version") == 2
        and record.get("check_returncode") == 0
        and record.get("coverage") == "non_external"
        and record.get("external_specs_excluded") is True
    )


def resolve_consumer_policy(
    requested: BuildcachePolicy,
    store: SharedBuildcacheStore,
    *,
    enabled: bool = True,
    lock_path: Path | None = None,
) -> BuildcachePolicy:
    """Resolve auto fallback and strict only fail-closed behavior.

    Global ``health.json`` unhealthy still allows ``auto``/``only`` when the
    current environment has successful lock-SHA coverage (another env's failed
    publish must not block a covered consumer).
    """
    if requested is BuildcachePolicy.NEVER:
        return requested
    if not enabled:
        if requested is BuildcachePolicy.AUTO:
            return BuildcachePolicy.NEVER
        raise RuntimeError("buildcache is not enabled for this environment")
    if _healthy(store) or _has_successful_lock_coverage(store, lock_path):
        return requested
    if requested is BuildcachePolicy.AUTO:
        logger.warning("Buildcache missing or unhealthy; falling back to source install")
        return BuildcachePolicy.NEVER
    if not store.layout.spack_buildcache_dir.is_dir():
        raise RuntimeError("buildcache store does not exist; policy 'only' fails closed")
    raise RuntimeError("buildcache store is missing healthy state or is unhealthy")


def coverage_path_for_lock(layout: ProjectLayout, lock_path: Path) -> Path:
    digest = hashlib.sha256(lock_path.read_bytes()).hexdigest()
    return layout.buildcache_coverage_dir / f"{digest}.json"


def collect_environment_provenance(
    lock_path: Path,
    environment_dir: Path,
) -> dict[str, object]:
    """Collect auditable environment facts without inventing unknown values."""
    lock = json.loads(lock_path.read_text(encoding="utf-8"))
    concrete_specs = lock.get("concrete_specs", {})
    specs = (
        concrete_specs.values()
        if isinstance(concrete_specs, dict)
        else concrete_specs
        if isinstance(concrete_specs, list)
        else ()
    )
    operating_systems: set[str] = set()
    targets: set[str] = set()
    compilers: set[str] = set()
    for spec in specs:
        if not isinstance(spec, dict):
            continue
        arch = spec.get("arch")
        if isinstance(arch, dict):
            operating_system = arch.get("platform_os")
            if operating_system:
                operating_systems.add(str(operating_system))
            target = arch.get("target")
            if isinstance(target, dict):
                target = target.get("name")
            if target:
                targets.add(str(target))
        compiler = spec.get("compiler")
        if isinstance(compiler, dict):
            name = compiler.get("name")
            version = compiler.get("version")
            if name and version:
                compilers.add(f"{name}@{version}")
        elif compiler:
            compilers.add(str(compiler))

    repo_commits: dict[str, str] = {}
    spack_yaml = environment_dir / "spack.yaml"
    if spack_yaml.is_file():
        data: Any = yaml.safe_load(spack_yaml.read_text(encoding="utf-8")) or {}
        repos = data.get("spack", {}).get("repos", {}) if isinstance(data, dict) else {}
        if isinstance(repos, dict):
            for name, config in repos.items():
                if isinstance(config, dict) and config.get("commit"):
                    repo_commits[str(name)] = str(config["commit"])

    return {
        "operating_systems": sorted(operating_systems) or None,
        "targets": sorted(targets) or None,
        "compilers": sorted(compilers) or None,
        "repo_commits": dict(sorted(repo_commits.items())) or None,
    }


def inspect_image_digest(
    *,
    engine: str,
    image_ref: str,
    layout: ProjectLayout,
) -> str:
    """Resolve a mutable image tag to the immutable local image ID."""
    result = subprocess.run(
        [engine, "image", "inspect", "--format", "{{.Id}}", image_ref],
        cwd=layout.project_root,
        text=True,
        capture_output=True,
        check=True,
    )
    digest = result.stdout.strip()
    if not digest:
        raise RuntimeError(f"could not resolve image digest for {image_ref}")
    return digest


def require_coverage(
    layout: ProjectLayout,
    lock_path: Path,
    *,
    spack_version: str,
    builder_image: str,
    padded_length: int,
    environment_provenance: dict[str, object],
) -> dict[str, object]:
    """Require a successful non-external coverage record for this exact lock."""
    path = coverage_path_for_lock(layout, lock_path)
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError) as exc:
        raise RuntimeError(
            f"buildcache coverage missing or invalid for lock {lock_path}"
        ) from exc
    if (
        record.get("schema_version") != 2
        or
        record.get("check_returncode") != 0
        or record.get("coverage") != "non_external"
        or record.get("external_specs_excluded") is not True
        or record.get("spack_version") != spack_version
        or record.get("builder_image_digest") != builder_image
        or record.get("padded_length") != padded_length
        or record.get("environment_provenance") != environment_provenance
    ):
        raise RuntimeError(f"buildcache coverage is incompatible: {path}")
    return record


def image_env_lock_path(env_name: str) -> str:
    """Return the producer-image path for an environment ``spack.lock``."""
    if not env_name or "/" in env_name or env_name in {".", ".."}:
        raise ValueError(f"invalid Spack environment name: {env_name!r}")
    return f"{IMAGE_SPACK_ROOT}/var/spack/environments/{env_name}/spack.lock"


def inspect_image_lock_sha(
    *,
    engine: str,
    image_ref: str,
    env_name: str,
    layout: ProjectLayout,
    timeout_seconds: int = DEFAULT_OPERATION_TIMEOUT_SECONDS,
) -> str:
    """Return the SHA-256 of ``spack.lock`` inside a producer image."""
    lock_path = image_env_lock_path(env_name)
    script = (
        "python3 -c "
        + shlex.quote(
            "import hashlib, pathlib, sys; "
            f"p = pathlib.Path({lock_path!r}); "
            "sys.stdout.write("
            "'HPC_CF_IMAGE_LOCK_SHA256='"
            "+ hashlib.sha256(p.read_bytes()).hexdigest()"
            "+ chr(10)"
            ")"
        )
        + "\n"
    )
    result = run_in_installed_image(
        engine=engine,
        image_ref=image_ref,
        layout=layout,
        script=script,
        timeout_seconds=timeout_seconds,
    )
    match = _IMAGE_LOCK_SHA_RE.search(result.stdout or "")
    if match is None:
        raise RuntimeError(
            f"could not resolve image lock SHA for {image_ref} ({lock_path})"
        )
    return match.group(1)


def require_matching_image_lock(
    *,
    engine: str,
    image_ref: str,
    env_name: str,
    layout: ProjectLayout,
    lock_path: Path,
    timeout_seconds: int = DEFAULT_OPERATION_TIMEOUT_SECONDS,
) -> str:
    """Fail closed when the producer image lock differs from the host lock."""
    host_sha = hashlib.sha256(lock_path.read_bytes()).hexdigest()
    image_sha = inspect_image_lock_sha(
        engine=engine,
        image_ref=image_ref,
        env_name=env_name,
        layout=layout,
        timeout_seconds=timeout_seconds,
    )
    if image_sha != host_sha:
        raise RuntimeError(
            "producer image lock SHA does not match the host lock SHA: "
            f"image={image_sha} host={host_sha}"
        )
    return host_sha


def publish_success_state(
    store: SharedBuildcacheStore,
    *,
    run: BuildcacheRun,
    lock_path: Path,
    record: BuildcacheCoverageRecord,
    provenance: Mapping[str, object],
) -> Path:
    """Atomically publish coverage, provenance, and healthy store state.

    Coverage is staged under a non-admitted name, provenance is written, then
    coverage is renamed into place *before* ``mark_healthy``. Health is only
    claimed once the coverage file is visible, so a crash cannot leave
    ``healthy=true`` without a coverage file. Any failure before a successful
    rename leaves no admitted coverage record for this lock SHA.
    """
    lock_sha256 = hashlib.sha256(lock_path.read_bytes()).hexdigest()
    final_path = store.layout.buildcache_coverage_dir / f"{lock_sha256}.json"
    staging_path = store.layout.buildcache_coverage_dir / (
        f".{lock_sha256}.json.{uuid.uuid4().hex}{_COVERAGE_PARTIAL_SUFFIX}"
    )
    payload: dict[str, object] = {
        "schema_version": 2,
        "lock_sha256": lock_sha256,
        "spack_version": record.spack_version,
        "builder_image_digest": record.builder_image_digest,
        "environment_provenance": dict(record.environment_provenance),
        "padded_length": record.padded_length,
        "signing_policy": record.signing_policy,
        "check_returncode": record.check_returncode,
        "checked_spec_count": record.checked_spec_count,
        "coverage": "non_external",
        "external_specs_excluded": True,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    staging_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        staging_path.write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        store.write_provenance(run, provenance)
        # Coverage must be visible before health; otherwise AUTO may admit on
        # healthy alone while the coverage file is still missing.
        staging_path.replace(final_path)
        store.mark_healthy(run_id=run.run_id, coverage_path=final_path)
        return final_path
    except Exception:
        staging_path.unlink(missing_ok=True)
        raise


def run_in_installed_image(
    *,
    engine: str,
    image_ref: str,
    layout: ProjectLayout,
    script: str,
    writable: bool = False,
    timeout_seconds: int = DEFAULT_OPERATION_TIMEOUT_SECONDS,
) -> subprocess.CompletedProcess[str]:
    """Run a dedicated publisher/checker with the store mounted read-write."""
    sandboxed_script = (
        'mkdir -p "$SPACK_USER_CONFIG_PATH"\n'
        'test -w "$HOME"\n'
        'test -w "$SPACK_USER_CACHE_PATH"\n'
        'test -d "$SPACK_USER_CACHE_PATH/package_repos"\n'
        'test -w "$SPACK_USER_CONFIG_PATH"\n'
        f"{script}"
    )
    command = [
        engine,
        "run",
        "--rm",
    ]
    if engine == "podman":
        command.append("--userns=keep-id:uid=0,gid=0")
    command += [
        "--env",
        "HOME=/root",
        "--env",
        "SPACK_USER_CACHE_PATH=/root/.spack",
        "--env",
        "SPACK_USER_CONFIG_PATH=/tmp/hpc-cf-spack-config",
        "-v",
        (
            f"{layout.spack_buildcache_dir}:"
            f"{layout.container_publisher_buildcache_dir()}:"
            f"{'rw' if writable else 'ro'}"
        ),
        image_ref,
        "bash",
        "-lc",
        sandboxed_script,
    ]
    logger.info("Running dedicated buildcache operation in %s", image_ref)
    return subprocess.run(
        command,
        cwd=layout.project_root,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=True,
        timeout=timeout_seconds,
    )


def publish(
    *,
    engine: str,
    image_ref: str,
    env_name: str,
    layout: ProjectLayout,
    timeout_seconds: int = DEFAULT_OPERATION_TIMEOUT_SECONDS,
) -> tuple[subprocess.CompletedProcess[str], int]:
    script = build_publish_script(
        env_name=env_name,
        store_path=layout.container_publisher_buildcache_dir(),
    )
    result = run_in_installed_image(
        engine=engine,
        image_ref=image_ref,
        layout=layout,
        script=script,
        writable=True,
        timeout_seconds=timeout_seconds,
    )
    match = _COUNT_RE.search(result.stdout or "")
    if match is None:
        raise RuntimeError("publisher did not report explicit checked spec count")
    return result, int(match.group(1))


def failed_publish_step(exc: BaseException) -> str:
    """Return the last publisher phase reported before an operation failed."""
    output = getattr(exc, "stdout", None) or getattr(exc, "output", None) or ""
    if isinstance(output, bytes):
        output = output.decode(errors="replace")
    matches = _PUBLISH_STEP_RE.findall(str(output))
    return matches[-1] if matches else "publish"


def failed_publish_output(exc: BaseException) -> str:
    """Return publisher stdout/output text from a failed subprocess."""
    output = getattr(exc, "stdout", None) or getattr(exc, "output", None) or ""
    if isinstance(output, bytes):
        return output.decode(errors="replace")
    return str(output) if output else ""


def verify(
    *,
    engine: str,
    image_ref: str,
    env_name: str,
    layout: ProjectLayout,
    timeout_seconds: int = DEFAULT_OPERATION_TIMEOUT_SECONDS,
) -> tuple[subprocess.CompletedProcess[str], int]:
    script = build_verify_script(
        env_name=env_name,
        store_path=layout.container_publisher_buildcache_dir(),
    )
    result = run_in_installed_image(
        engine=engine,
        image_ref=image_ref,
        layout=layout,
        script=script,
        timeout_seconds=timeout_seconds,
    )
    match = _COUNT_RE.search(result.stdout or "")
    if match is None:
        raise RuntimeError("verification did not report explicit checked spec count")
    return result, int(match.group(1))
