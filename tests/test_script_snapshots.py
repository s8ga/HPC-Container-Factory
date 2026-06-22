"""L2 snapshot tests for SpackOps._build_*_script helpers (plan 2.1).

These lock in the key tokens each generated bash script must contain, so a
future refactor of the script bodies is caught immediately. The _build_*
helpers are pure (return str, no container I/O), so we call them directly
on a SpackOps wired to a CapturingContainer.
"""
from __future__ import annotations

import subprocess

from hpc_cf.container import Container
from hpc_cf.spack_ops import CustomRepo, EnvConfig, SpackConfig, SpackOps


class CapturingContainer(Container):
    def __init__(self) -> None:
        super().__init__(name="x", image="x")
        self.scripts: list[str] = []

    def exec(self, script, *, capture=False, check=True):  # type: ignore[override]
        self.scripts.append(script)
        return subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")

    def run_ephemeral(self, script, *, capture=False, check=True):  # type: ignore[override]
        self.scripts.append(script)
        return subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")


def _ops() -> SpackOps:
    return SpackOps(EnvConfig(spack=SpackConfig(version="1.1.1", env_name="cp2k-env")), CapturingContainer())


def test_build_compiler_find_script() -> None:
    s = _ops()._build_compiler_find_script()
    assert "spack compiler find" in s


def test_build_clean_stale_state_script() -> None:
    s = _ops()._build_clean_stale_state_script()
    assert 'rm -f "${SPACK_USER_CONFIG_PATH}/repos.yaml"' in s
    assert 'rm -f "${SPACK_USER_CONFIG_PATH}/packages.yaml"' in s
    assert 'rm -rf "${env_dir}"/*' in s
    assert "/tmp/spack-repos" in s


def test_build_bootstrap_mirror_binary_flag() -> None:
    ops = _ops()
    binary = ops._build_bootstrap_mirror_script("/opt/bootstrap", binary_packages=True)
    plain = ops._build_bootstrap_mirror_script("/opt/bootstrap", binary_packages=False)
    assert "spack bootstrap mirror --binary-packages" in binary
    assert "--binary-packages" not in plain
    assert 'mkdir -p "/opt/bootstrap"' in binary
    # Both must clear stale state and run spack bootstrap mirror.
    for s in (binary, plain):
        assert 'rm -f "${SPACK_USER_CONFIG_PATH}/repos.yaml"' in s
        assert "spack bootstrap mirror" in s


def test_build_concretize_script() -> None:
    s = _ops()._build_concretize_script("/work/env")
    assert "spack env create" in s
    assert "concretize -f" in s
    assert "spack.lock" in s
    assert "repo update builtin" in s
    # env name from config is present
    assert "cp2k-env" in s


def test_build_mirror_create_script() -> None:
    s = _ops()._build_mirror_create_script("/work/env", "/work/mirror")
    assert "spack -e . mirror create -d" in s
    assert "--all -D --private" in s
    assert "/tmp/mirror-output.log" in s
    assert 'spack.lock not found' in s  # guard branch present


def test_build_mirror_verify_script() -> None:
    s = _ops()._build_mirror_verify_script("/work/env", "/work/mirror")
    assert "spack -e . mirror create -d" in s
    assert "/tmp/verify-output.log" in s


def test_build_register_repos_script_git_and_local() -> None:
    env = EnvConfig(spack=SpackConfig(
        version="1.1.1",
        env_name="cp2k-env",
        custom_repos=[
            CustomRepo(type="git", namespace="cp2k_dev_repo", url="https://github.com/cp2k/cp2k.git",
                       branch="support/v2026.1", sparse_path="tools/spack/cp2k_dev_repo"),
            CustomRepo(type="local", namespace="cp2k-env", path="repos"),
        ],
    ))
    ops = SpackOps(env, CapturingContainer())
    s = ops._build_register_repos_script("/work/env")
    # git repo: clone + register
    assert "git clone" in s
    assert "-b support/v2026.1" in s
    assert "spack repo add" in s
    # local repo: registered against env dir
    assert "/work/env/repos" in s
