"""L0/L1 contracts for ABACUS L4 smoke helpers and opt-in collection."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from hpc_cf.execution import ProjectLayout, SharedBuildcacheStore

from tests.test_integration_abacus_l4 import (
    ENV_NAME,
    FLAT_AUTOTEST_PROBE,
    SHARE_ABACUS_TESTS_PROBE,
    _assert_integration_summary_passed,
    consumer_buildcache_admitted,
    l4_skip_reason,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ENV_310 = PROJECT_ROOT / "spack-envs" / "abacus_opensource-3.10.1-force-avx512"
ENV_39 = PROJECT_ROOT / "spack-envs" / "abacus_opensource-3.9.0.27-force-avx512"
PADDED_FIND = "find /opt/spack -type d -path '*/share/abacus/tests'"


def test_l4_integration_module_skipped_without_run_integration() -> None:
    """Default pytest must not execute L4 body (collection skip only)."""
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "tests/test_integration_abacus_l4.py",
            "-q",
            "--tb=no",
        ],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    out = result.stdout or ""
    assert "skipped" in out.lower()
    assert "failed" not in out.lower()


def test_assert_integration_summary_requires_nonzero_passes() -> None:
    summary = (
        "================================================================\n"
        "  Summary\n"
        "================================================================\n"
        "  Total:     10\n"
        "  Passed:    0\n"
        "  Failed:    0\n"
        "  Skipped:   10\n"
    )
    try:
        _assert_integration_summary_passed(summary)
    except AssertionError as exc:
        assert "Passed=0" in str(exc)
    else:
        raise AssertionError("expected all-skip summary to fail assertion")


def test_assert_integration_summary_accepts_clean_pass() -> None:
    summary = (
        "  Passed:    10\n"
        "  Failed:    0\n"
        "  Skipped:   0\n"
    )
    _assert_integration_summary_passed(summary)


def test_l4_probe_uses_padded_find_fallback() -> None:
    """L4 discovery must match module-runner padded find (not short path only)."""
    assert PADDED_FIND in SHARE_ABACUS_TESTS_PROBE
    assert "linux-x86_64_v3/abacus-*/share/abacus/tests" in SHARE_ABACUS_TESTS_PROBE
    assert "integrate/Autotest.sh" in FLAT_AUTOTEST_PROBE
    assert "CASES_CPU.txt" in FLAT_AUTOTEST_PROBE


def test_integration_scripts_padded_find_and_layouts() -> None:
    """3.10 flat Autotest; 3.9 grouped dirs; both padded-aware."""
    s310 = (ENV_310 / "abacus_run_integration_tests.sh").read_text(encoding="utf-8")
    s39 = (ENV_39 / "abacus_run_integration_tests.sh").read_text(encoding="utf-8")
    assert PADDED_FIND in s310
    assert PADDED_FIND in s39
    assert "INTEGRATE=" in s310 and "Autotest.sh" in s310
    assert "CASES_CPU.txt" in s310
    assert "flat Autotest" in s310
    assert "DIRS=" not in s310
    assert "DIRS=" in s39 and "01_PW" in s39


def test_module_runners_reject_zero_total() -> None:
    """Empty discovery must not exit 0 (Total:0 false green)."""
    for env in (ENV_310, ENV_39):
        text = (env / "abacus_run_module_tests.sh").read_text(encoding="utf-8")
        assert "TOTAL -eq 0" in text
        assert "no module tests discovered" in text


def test_consumer_buildcache_admitted_false_when_store_missing(
    tmp_path: Path,
) -> None:
    layout = ProjectLayout(project_root=tmp_path)
    env_dir = layout.project_root / "spack-envs" / ENV_NAME / "spack-env-file"
    env_dir.mkdir(parents=True)
    (env_dir / "spack.lock").write_text('{"concrete_specs":{}}\n', encoding="utf-8")
    assert consumer_buildcache_admitted(layout) is False


def test_l4_skip_reason_reports_unhealthy_buildcache(tmp_path: Path) -> None:
    layout = ProjectLayout(project_root=tmp_path)
    store = SharedBuildcacheStore(layout)
    store.ensure_store_root()
    (store.layout.spack_buildcache_dir / "sentinel").write_text("x", encoding="utf-8")
    store.mark_unhealthy(
        run_id="l4-contract",
        failed_step="unit-test",
        error="contract unhealthy",
    )

    # Satisfy non-buildcache prereqs so skip reason targets coverage/health.
    assets = layout.assets_dir
    assets.mkdir(parents=True, exist_ok=True)
    (assets / "spack-v1.2.0.tar.gz").write_bytes(b"fake")
    (assets / "bootstrap-1.2.0").mkdir()
    env_dir = layout.project_root / "spack-envs" / ENV_NAME
    (env_dir / "spack-env-file").mkdir(parents=True)
    (env_dir / "spack-env-file" / "spack.lock").write_text(
        json.dumps({"concrete_specs": {}}),
        encoding="utf-8",
    )
    (env_dir / "abacus_run_integration_tests.sh").write_text(
        "#!/bin/bash\n",
        encoding="utf-8",
    )

    reason = l4_skip_reason(layout)
    assert reason is not None
    assert "buildcache" in reason.lower()
