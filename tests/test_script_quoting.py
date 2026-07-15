"""Quoting regression for generated bash scripts.

Captures the script that SpackOps sends to the container and asserts that
config-derived paths with spaces are shlex-quoted, so a future refactor
can't silently regress to the old bare double-quote interpolation.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from hpc_cf.container import Container
from hpc_cf.spack_ops import CustomRepo, EnvConfig, SpackConfig, SpackOps


class CapturingContainer(Container):
    """A Container stand-in that records scripts instead of running them.

    SpackOps only calls ``ctr.exec`` / ``ctr.run_ephemeral`` with a bash script
    and (optionally) reads ``.stdout``; this records the script and returns a
    benign CompletedProcess.
    """

    def __init__(self) -> None:
        super().__init__(name="x", image="x")
        self.scripts: list[str] = []

    def exec(self, script, *, capture=False, check=True):  # type: ignore[override]
        self.scripts.append(script)
        return subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")

    def run_ephemeral(self, script, *, capture=False, check=True):  # type: ignore[override]
        self.scripts.append(script)
        return subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")


def _make_ops() -> tuple[CapturingContainer, SpackOps]:
    env = EnvConfig(spack=SpackConfig(version="1.1.1", env_name="cp2k-env"))
    ctr = CapturingContainer()
    return ctr, SpackOps(env, ctr)


def test_prepare_environment_quotes_paths_with_spaces() -> None:
    ctr, ops = _make_ops()
    ops.prepare_environment("/work/has space/dir", import_lock=True)
    assert ctr.scripts, "prepare_environment should emit a script"
    script = ctr.scripts[-1]
    assert "'/work/has space/dir/spack.yaml'" in script
    assert "'/work/has space/dir/spack.lock'" in script
    assert '"/work/has space/dir/spack.yaml"' not in script


def test_concretize_quotes_lock_destination_with_spaces(tmp_path: Path) -> None:
    ctr, ops = _make_ops()
    (tmp_path / "spack.lock").write_text("{}\n")
    ops.concretize(
        env_dir_host=tmp_path,
        env_dir_container="/work/has space/dir",
    )
    assert "'/work/has space/dir/spack.lock'" in ctr.scripts[-1]


def test_prepare_environment_quotes_env_name_with_spaces() -> None:
    env = EnvConfig(spack=SpackConfig(
        version="1.1.1",
        env_name="cp2k env",
        custom_repos=[
            CustomRepo(type="local", namespace="custom", path="repos"),
        ],
    ))
    ops = SpackOps(env, CapturingContainer())
    script = ops._build_prepare_environment_script(
        "/work/env",
        import_lock=False,
    )
    assert "'cp2k env'" in script
    assert "'env:cp2k env'" in script


def test_concretize_raises_when_host_lock_missing(tmp_path: Path) -> None:
    """Container exec success must not hide a missing host-side spack.lock."""
    _, ops = _make_ops()
    with pytest.raises(RuntimeError, match="spack.lock"):
        ops.concretize(env_dir_host=tmp_path, env_dir_container="/work/env")


def test_concretize_raises_when_host_lock_empty(tmp_path: Path) -> None:
    (tmp_path / "spack.lock").write_text("")
    _, ops = _make_ops()
    with pytest.raises(RuntimeError, match="spack.lock"):
        ops.concretize(env_dir_host=tmp_path, env_dir_container="/work/env")


def test_concretize_accepts_nonempty_host_lock(tmp_path: Path) -> None:
    (tmp_path / "spack.lock").write_text("{}\n")
    _, ops = _make_ops()
    ops.concretize(env_dir_host=tmp_path, env_dir_container="/work/env")


def test_mirror_create_quotes_paths_with_spaces() -> None:
    ctr, ops = _make_ops()
    # mirror_create also parses stats at the end; stub it to isolate quoting.
    ops._parse_mirror_stats = lambda _log: {"present": 0, "added": 0, "failed": 0}
    ops.mirror_create(mirror_dir_container="/work/mir ror")
    assert ctr.scripts
    script = ctr.scripts[-1]
    assert "'/work/mir ror'" in script
    assert '"/work/mir ror"' not in script

