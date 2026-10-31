"""Unit tests for SpackOps stale-repo cleanup ordering and manual-package mirror exclusion."""

from __future__ import annotations

from unittest.mock import MagicMock

from hpc_cf.environment import EnvironmentSpec, ManualPackage, SpackConfig
from hpc_cf.spack_ops import SpackOps


class _DummyRunner:
    def exec(self, script: str, *, capture: bool = False, check: bool = True):
        return MagicMock(returncode=0, stdout="")

    def run_ephemeral(self, script: str) -> None:
        return None


def test_clean_stale_state_removes_dangling_repos_before_bootstrap() -> None:
    spec = EnvironmentSpec(
        spack=SpackConfig(version="1.2.0", env_name="vasp-env"),
    )
    ops = SpackOps(spec, _DummyRunner())
    script = ops._build_clean_stale_state_script()

    rm_pos = script.index('rm -f "${SPACK_USER_CONFIG_PATH}/repos.yaml"')
    boot_pos = script.index("\n    spack bootstrap now\n")
    assert rm_pos < boot_pos


def test_manual_package_excludes_proprietary_spec_while_keeping_private_flag() -> None:
    spec = EnvironmentSpec(
        spack=SpackConfig(version="1.2.0", env_name="vasp-env"),
        manual_packages=[
            ManualPackage(
                file="assets/manual-packages/vasp.6.6.1.tgz",
                dest="/opt/manual-package/",
            )
        ],
    )
    ops = SpackOps(spec, _DummyRunner())
    create_script = ops._build_mirror_create_script("/work/assets/spack-mirror")
    verify_script = ops._build_mirror_verify_script("/work/assets/spack-mirror")

    assert "--all -D --private --exclude-specs vasp" in create_script
    assert "--all -D --private --exclude-specs vasp" in verify_script
