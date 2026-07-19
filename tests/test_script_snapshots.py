"""L2 snapshot tests for SpackOps._build_*_script helpers (plan 2.1).

These lock in the key tokens each generated bash script must contain, so a
future refactor of the script bodies is caught immediately. The _build_*
helpers are pure (return str, no container I/O), so we call them directly
on a SpackOps wired to a CapturingContainer.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
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


def _ops_with_repos() -> SpackOps:
    env = EnvConfig(spack=SpackConfig(
        version="1.2.0",
        env_name="cp2k-env",
        custom_repos=[
            CustomRepo(
                type="git",
                namespace="cp2k_dev",
                url="https://github.com/cp2k/cp2k.git",
                branch="support/v2026.2",
                sparse_path="tools/spack/spack_repo/cp2k_dev",
            ),
            CustomRepo(type="local", namespace="cp2k-env", path="repos"),
        ],
    ))
    return SpackOps(env, CapturingContainer())


def test_build_compiler_find_script() -> None:
    s = _ops()._build_compiler_find_script()
    assert "spack compiler find" in s


def _all_build_scripts(ops: SpackOps) -> list[str]:
    return [
        ops._build_compiler_find_script(),
        ops._build_clean_stale_state_script(),
        ops._build_bootstrap_mirror_script("/opt/bootstrap", binary_packages=True),
        ops._build_prepare_repos_script("/work/env"),
        ops._build_prepare_environment_script("/work/env", import_lock=False),
        ops._build_concretize_script("/work/env"),
        ops._build_mirror_create_script("/work/mirror"),
        ops._build_mirror_verify_script("/work/mirror"),
    ]


def test_all_scripts_have_pipefail() -> None:
    """Every _build_*_script must include 'set -o pipefail' so that
    ``cmd | tee`` pipelines correctly propagate spack's exit code."""
    for s in _all_build_scripts(_ops()):
        assert "set -o pipefail" in s, f"pipefail missing in script:\n{s[:200]}"


def test_all_scripts_have_errexit_not_nounset() -> None:
    """Scripts must use set -e for mid-script failure, but not blind set -u
    (Spack setup-env may reference unset variables)."""
    for s in _all_build_scripts(_ops()):
        assert "set -e" in s, f"errexit missing in script:\n{s[:200]}"
        assert "set -u" not in s, f"nounset must not be enabled:\n{s[:200]}"
        assert "set -euo" not in s, f"set -euo must not be used:\n{s[:200]}"


def test_setup_env_propagates_intermediate_failure(tmp_path: Path) -> None:
    """With production shell options, a failing mid-script command must exit
    non-zero and skip later commands (silent-success regression guard)."""
    ops = SpackOps(
        EnvConfig(spack=SpackConfig(version="1.1.1", env_name="cp2k-env")),
        CapturingContainer(),
    )
    # Redirect user dirs into tmp so mkdir in _setup_env_vars is host-safe.
    ops.user_dir = str(tmp_path / "user")
    ops.user_cache = str(tmp_path / "cache")
    probe = f"""{ops._setup_env_vars()}
false
echo SHOULD_NOT_PRINT
"""
    result = subprocess.run(
        ["bash", "-c", probe],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert "SHOULD_NOT_PRINT" not in result.stdout


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


def test_source_spack_propagates_bootstrap_now_failure() -> None:
    """Idempotent bootstrap add may tolerate failure; bootstrap now must not."""
    s = _ops()._source_spack()
    assert "spack bootstrap now" in s
    for line in s.splitlines():
        if "spack bootstrap add" in line:
            assert "|| true" in line
        if "spack bootstrap now" in line:
            assert "|| true" not in line


def test_bootstrap_mirror_binary_only_no_fallback(tmp_path, monkeypatch) -> None:
    """bootstrap_mirror must NOT fall back to source on binary failure."""
    env = EnvConfig(spack=SpackConfig(version="1.1.1", env_name="test-env"))
    ctr = CapturingContainer()
    ops = SpackOps(env, ctr)

    # Redirect PROJECT_ROOT so local_dir lands in tmp_path
    monkeypatch.setattr("hpc_cf.config.PROJECT_ROOT", tmp_path)

    # Bypass metadata completeness check and verify (avoid real filesystem ops)
    monkeypatch.setattr(ops, "_bootstrap_metadata_complete", lambda _: False)
    monkeypatch.setattr(ops, "_verify_bootstrap", lambda _: None)

    # Make run_ephemeral fail immediately
    call_count = [0]

    def fail_once(script, *, capture=False, check=True):
        call_count[0] += 1
        raise subprocess.CalledProcessError(1, ["podman"])

    ctr.run_ephemeral = fail_once  # type: ignore[method-assign]

    with pytest.raises(subprocess.CalledProcessError):
        ops.bootstrap_mirror(force=True)

    # Must NOT have retried with source
    assert call_count[0] == 1


def test_bootstrap_mirror_always_binary(tmp_path, monkeypatch) -> None:
    """bootstrap_mirror must always pass binary_packages=True."""
    env = EnvConfig(spack=SpackConfig(version="1.1.1", env_name="test-env"))
    ctr = CapturingContainer()
    ops = SpackOps(env, ctr)

    monkeypatch.setattr("hpc_cf.config.PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(ops, "_bootstrap_metadata_complete", lambda _: False)
    monkeypatch.setattr(ops, "_verify_bootstrap", lambda _: None)

    ops.bootstrap_mirror(force=True)

    assert len(ctr.scripts) == 1
    assert "--binary-packages" in ctr.scripts[0]


def test_build_concretize_script() -> None:
    s = _ops()._build_concretize_script("/work/env")
    assert "concretize -f" in s
    assert "spack.lock" in s
    assert "spack env create" not in s
    assert "repo update builtin" not in s
    assert "spack repo add" not in s
    # env name from config is present
    assert "cp2k-env" in s


def test_build_mirror_create_script() -> None:
    s = _ops()._build_mirror_create_script("/work/mirror")
    assert "spack -e cp2k-env mirror create -d" in s
    assert "--all -D --private" in s
    assert "/tmp/mirror-output.log" in s
    assert "spack env activate ." not in s
    assert "repo update builtin" not in s


def test_build_mirror_verify_script() -> None:
    s = _ops()._build_mirror_verify_script("/work/mirror")
    assert "spack -e cp2k-env mirror create -d" in s
    assert "/tmp/verify-output.log" in s
    assert "spack env activate ." not in s
    assert "repo update builtin" not in s


def test_build_prepare_repos_script_fetches_without_registering() -> None:
    s = _ops_with_repos()._build_prepare_repos_script("/work/env")
    assert "git clone" in s
    assert "-b support/v2026.2" in s
    assert "/work/env/repos" in s
    assert "spack repo add" not in s


def test_build_prepare_repos_script_checks_out_commit_pin() -> None:
    sha = "d0ee3f460a2543c05c693317c767652abf964db7"
    env = EnvConfig(
        spack=SpackConfig(
            version="1.2.0",
            env_name="abacus-env",
            custom_repos=[
                CustomRepo(
                    type="git",
                    namespace="abacus",
                    url="https://github.com/s8ga/s8ga-spack-packages.git",
                    branch="master",
                    commit=sha,
                    sparse_path="spack_repo/abacus",
                ),
            ],
        )
    )
    s = SpackOps(env, CapturingContainer())._build_prepare_repos_script("/work/env")
    assert "git clone --filter=blob:none --sparse --no-checkout" in s
    assert f"git fetch --depth 1 origin {sha}" in s
    assert f"git checkout {sha}" in s
    assert "git sparse-checkout set spack_repo/abacus" in s
    assert "-b master" not in s
    assert "spack repo add" not in s


def test_build_prepare_environment_registers_repos_after_builtin() -> None:
    s = _ops_with_repos()._build_prepare_environment_script(
        "/work/env", import_lock=False,
    )
    create = s.index("spack env create")
    update = s.index("repo update builtin")
    git_add = s.index("/tmp/spack-repos/spack_repo/cp2k_dev")
    local_add = s.index("/work/env/repos")
    concretize = s.find("concretize -f")

    assert create < update < git_add < local_add
    assert concretize == -1
    assert s.count("--scope env:cp2k-env") == 2
    assert "spack -e cp2k-env repo list" in s


def test_build_prepare_environment_imports_lock_when_required() -> None:
    s = _ops_with_repos()._build_prepare_environment_script(
        "/work/has space/env", import_lock=True,
    )
    assert "spack.lock not found" in s
    assert "'/work/has space/env/spack.lock'" in s
    assert "var/spack/environments/cp2k-env/spack.lock" in s


def test_concretize_pipeline_prepares_environment_before_use(tmp_path: Path) -> None:
    ops = _ops_with_repos()
    ctr = ops.ctr
    assert isinstance(ctr, CapturingContainer)

    (tmp_path / "spack.lock").write_text("{}\n")
    ops.run_concretize_pipeline(tmp_path, "/work/env")

    def first_index(needle: str) -> int:
        for i, s in enumerate(ctr.scripts):
            if needle in s:
                return i
        raise AssertionError(f"no script containing {needle!r}: {ctr.scripts!r}")

    # Semantic order (not brittle positional indices): clean → fetch repos →
    # compiler find → prepare env (create + env-scope repos) → concretize.
    i_clean = first_index("rm -f")
    i_repos = first_index("git clone")
    i_compiler = first_index("spack compiler find")
    i_env = first_index("spack env create")
    i_concretize = first_index("concretize -f")

    assert i_clean < i_repos < i_compiler < i_env < i_concretize
    assert "--scope env:cp2k-env" in ctr.scripts[i_env]
