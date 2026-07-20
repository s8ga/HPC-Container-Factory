"""Opt-in L4 smoke: ABACUS 3.10.1 consumer build + runtime integration.

Opt-in only (``pytest --run-integration``).  Default ``pytest -q`` collects
these tests but skips them via ``conftest`` (same gate as L3).

Missing healthy buildcache admission, Spack assets, lock, or podman → skip
(not a false-green pass).  Runtime uses stock
``abacus_run_integration_tests.sh``; if the built image has no
``share/abacus/tests`` (``tests=false``), the runtime leg is skipped.
"""
from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from hpc_cf.buildcache import resolve_consumer_policy
from hpc_cf.environment import BuildcachePolicy
from hpc_cf.execution import ProjectLayout, SharedBuildcacheStore

ENV_NAME = "abacus_opensource-3.10.1-force-avx512"
SPACK_VERSION = "1.2.0"
ENGINE = "podman"
IMAGE_REF = "abacus_opensource:3.10.1-force-avx512"
PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _env_dir(layout: ProjectLayout | None = None) -> Path:
    root = (layout or ProjectLayout.default()).project_root
    return root / "spack-envs" / ENV_NAME


def _lock_path(layout: ProjectLayout | None = None) -> Path:
    return _env_dir(layout) / "spack-env-file" / "spack.lock"


def _integration_script(layout: ProjectLayout | None = None) -> Path:
    return _env_dir(layout) / "abacus_run_integration_tests.sh"


def _assets_ready(layout: ProjectLayout | None = None) -> bool:
    assets = (layout or ProjectLayout.default()).assets_dir
    return (assets / f"spack-v{SPACK_VERSION}.tar.gz").is_file() and (
        assets / f"bootstrap-{SPACK_VERSION}"
    ).is_dir()


def consumer_buildcache_admitted(
    layout: ProjectLayout | None = None,
) -> bool:
    """True when ``--buildcache auto`` would keep buildcache (not fall back)."""
    layout = layout or ProjectLayout.default()
    store = SharedBuildcacheStore(layout)
    lock = _lock_path(layout)
    policy_lock = lock if lock.is_file() else None
    effective = resolve_consumer_policy(
        BuildcachePolicy.AUTO,
        store,
        enabled=True,
        lock_path=policy_lock,
    )
    return effective is BuildcachePolicy.AUTO


def l4_skip_reason(layout: ProjectLayout | None = None) -> str | None:
    """Return a skip reason when L4 prerequisites are missing, else None."""
    layout = layout or ProjectLayout.default()
    if shutil.which(ENGINE) is None:
        return f"{ENGINE} not found on PATH"
    if not _assets_ready(layout):
        return (
            f"assets for spack {SPACK_VERSION} not found under {layout.assets_dir}"
        )
    lock = _lock_path(layout)
    if not lock.is_file() or lock.stat().st_size == 0:
        return f"non-empty spack.lock missing at {lock}"
    script = _integration_script(layout)
    if not script.is_file():
        return f"integration script missing at {script}"
    if not consumer_buildcache_admitted(layout):
        return (
            "buildcache not healthy/covered for consumer auto "
            f"(env={ENV_NAME}); refusing source-fallback L4"
        )
    return None


def _image_has_share_abacus_tests(image_ref: str) -> bool:
    """Probe whether the image ships Autotest trees for the stock script."""
    probe = (
        "ls -d /opt/spack/linux-x86_64_v3/abacus-*/share/abacus/tests "
        "2>/dev/null | head -1"
    )
    result = subprocess.run(
        [ENGINE, "run", "--rm", image_ref, "bash", "-lc", probe],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.returncode == 0 and bool((result.stdout or "").strip())


def _assert_integration_summary_passed(output: str) -> None:
    """Require Passed > 0 and Failed == 0 (all-skip exit 0 is not a pass)."""
    passed = re.search(r"Passed:\s+(\d+)", output)
    failed = re.search(r"Failed:\s+(\d+)", output)
    assert passed is not None, f"missing Passed summary in:\n{output[-2000:]}"
    assert failed is not None, f"missing Failed summary in:\n{output[-2000:]}"
    n_pass = int(passed.group(1))
    n_fail = int(failed.group(1))
    assert n_fail == 0, f"integration reported failures:\n{output[-2000:]}"
    assert n_pass > 0, (
        "integration Passed=0 (likely all groups skipped); "
        f"not treating as pass:\n{output[-2000:]}"
    )


@pytest.fixture(scope="module")
def abacus_l4_built_image():
    """Build the 3.10.1 consumer image once for this module (opt-in only)."""
    reason = l4_skip_reason()
    if reason is not None:
        pytest.skip(reason)

    # --network-host matches docs/QUICK_START and producer examples: the
    # ABACUS Dockerfile clones s8ga over HTTPS (needs host proxy/DNS).
    cmd = [
        sys.executable,
        "-m",
        "hpc_cf",
        "build",
        "--app-version",
        ENV_NAME,
        "--buildcache",
        "auto",
        "--network-host",
    ]
    completed = subprocess.run(
        cmd,
        cwd=PROJECT_ROOT,
        check=False,
    )
    if completed.returncode != 0:
        pytest.fail(
            f"hpc_cf build failed (rc={completed.returncode}) for {ENV_NAME} "
            "with --buildcache auto"
        )
    return IMAGE_REF


@pytest.mark.integration
def test_abacus_l4_consumer_build_smoke(abacus_l4_built_image: str) -> None:
    """Consumer build via ``hpc_cf build --buildcache auto`` must succeed."""
    inspect = subprocess.run(
        [ENGINE, "image", "exists", abacus_l4_built_image],
        check=False,
    )
    assert inspect.returncode == 0, (
        f"expected image {abacus_l4_built_image} after successful build"
    )
    assert abacus_l4_built_image == IMAGE_REF


@pytest.mark.integration
def test_abacus_l4_runtime_integration_smoke(abacus_l4_built_image: str) -> None:
    """Run stock ``abacus_run_integration_tests.sh``; require summary pass."""
    if not _image_has_share_abacus_tests(abacus_l4_built_image):
        pytest.skip(
            f"{abacus_l4_built_image} has no share/abacus/tests "
            "(concrete abacus tests=false); stock "
            "abacus_run_integration_tests.sh cannot run"
        )

    script = _integration_script()
    completed = subprocess.run(
        [
            ENGINE,
            "run",
            "--rm",
            "--network=host",
            "-v",
            f"{script}:/tmp/run_tests.sh:ro",
            abacus_l4_built_image,
            "bash",
            "/tmp/run_tests.sh",
        ],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    output = (completed.stdout or "") + (completed.stderr or "")
    assert completed.returncode == 0, (
        f"integration script rc={completed.returncode}\n{output[-4000:]}"
    )
    _assert_integration_summary_passed(output)
