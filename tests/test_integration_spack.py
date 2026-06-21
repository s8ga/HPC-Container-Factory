"""Integration tests: _build_*_script against real spack 1.1.1.

These tests create a persistent container, set up a minimal spack env
(pkgconf — 0 deps) using spack CLI, and exercise the full pipeline.
Skipped by default; run with:

    pytest --run-integration
    # or
    python scripts/integration-test.py
"""
from __future__ import annotations

import pytest

from hpc_cf.config import ASSETS_DIR, PROJECT_ROOT
from hpc_cf.container import Container
from hpc_cf.spack_ops import EnvConfig, SpackConfig, SpackOps

SPACK_VERSION = "1.1.1"
IMAGE = "hpc-mirror-builder"
CONTAINER_NAME = "hpc-itest"
ENV_DIR = "/tmp/itest-env"
MIRROR_DIR = "/tmp/itest-mirror"


@pytest.fixture(scope="class")
def spack_ops():
    """Create container + SpackOps + minimal env (pkgconf) via spack CLI.

    Uses spack's own CLI (env create -d, add) — no hand-written YAML.
    Teardown destroys the container on class exit.
    """
    if not (ASSETS_DIR / f"spack-v{SPACK_VERSION}.tar.gz").exists():
        pytest.skip(f"assets/spack-v{SPACK_VERSION}.tar.gz not found")
    if not (ASSETS_DIR / f"bootstrap-{SPACK_VERSION}").is_dir():
        pytest.skip(f"assets/bootstrap-{SPACK_VERSION} not found")

    env_config = EnvConfig(spack=SpackConfig(version=SPACK_VERSION, env_name="itest"))
    ctr = Container(name=CONTAINER_NAME, image=IMAGE, project_root=PROJECT_ROOT)
    ops = SpackOps(env_config, ctr)

    ctr.create()
    try:
        ops.install_system_pkgs()
        ops.clean_stale_state()
        ops.compiler_find()

        # Create minimal env via spack CLI (no heredoc / manual YAML).
        # pkgconf: 0 deps, builtin package, tiny source — ideal for fast testing.
        ops.ctr.exec(f"""\
{ops._source_spack()}
rm -rf {ENV_DIR}
spack env create -d {ENV_DIR}
spack -e {ENV_DIR} add pkgconf
spack -e {ENV_DIR} config add concretizer:unify:true
""")

        yield ops
    finally:
        try:
            ctr.destroy()
        except Exception:
            pass


def _run_mirror(ops: SpackOps) -> dict:
    """Helper: clean, run mirror create, return parsed stats."""
    ops.clean_stale_state()
    ops.ctr.exec(ops._build_mirror_create_script(ENV_DIR, MIRROR_DIR))
    return ops._parse_mirror_stats()


@pytest.mark.integration
class TestSpackScriptsIntegration:
    """Validate _build_*_script methods against real spack inside a container."""

    def test_phase1_concretize_creates_lock(self, spack_ops: SpackOps):
        """_build_concretize_script is accepted by spack and produces spack.lock."""
        ops = spack_ops
        ops.clean_stale_state()
        ops.ctr.exec(ops._build_concretize_script(ENV_DIR))
        result = ops.ctr.exec(
            f"test -f {ENV_DIR}/spack.lock && echo LOCK_OK || echo LOCK_MISSING",
            capture=True,
        )
        assert "LOCK_OK" in (result.stdout or "")

    def test_phase2_mirror_fresh_added(self, spack_ops: SpackOps):
        """First mirror create — all packages are 'added', regex matches."""
        ops = spack_ops
        ops.ctr.exec(f"rm -rf {MIRROR_DIR}")
        stats = _run_mirror(ops)
        assert stats["failed"] != -1, f"regex didn't match spack output: {stats}"
        assert stats["failed"] == 0
        assert stats["added"] >= 1

    def test_phase3_mirror_rerun_present(self, spack_ops: SpackOps):
        """Re-run mirror create — all 'already present', regex matches."""
        stats = _run_mirror(spack_ops)
        assert stats["failed"] != -1, f"regex didn't match: {stats}"
        assert stats["failed"] == 0
        assert stats["present"] >= 1

    def test_phase3b_mirror_verify(self, spack_ops: SpackOps):
        """_build_mirror_verify_script works and stats parse correctly."""
        ops = spack_ops
        ops.clean_stale_state()
        ops.ctr.exec(ops._build_mirror_verify_script(ENV_DIR, MIRROR_DIR))
        stats = ops._parse_mirror_stats()
        assert stats["failed"] != -1, f"verify regex didn't match: {stats}"
        assert stats["failed"] == 0
        assert stats["present"] >= 1

    def test_phase4_forced_failure(self, spack_ops: SpackOps):
        """Bad proxy → fetch fails → 'failed' counter >= 1, regex matches."""
        ops = spack_ops
        ops.ctr.exec(
            "echo 'export https_proxy=http://127.0.0.1:1 "
            "http_proxy=http://127.0.0.1:1 all_proxy=http://127.0.0.1:1' "
            "> /tmp/home/.bash_profile"
        )
        ops.ctr.exec(f"rm -rf {MIRROR_DIR}")
        stats = _run_mirror(ops)
        ops.ctr.exec("rm -f /tmp/home/.bash_profile")
        assert stats["failed"] != -1, f"regex didn't match: {stats}"
        assert stats["failed"] >= 1
