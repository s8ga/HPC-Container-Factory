"""Quoting regression for generated bash scripts.

Captures the script that SpackOps sends to the container and asserts that
config-derived paths with spaces are shlex-quoted, so a future refactor
can't silently regress to the old bare double-quote interpolation.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from hpc_cf.container import Container
from hpc_cf.spack_ops import EnvConfig, SpackConfig, SpackOps


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


def test_concretize_quotes_paths_with_spaces() -> None:
    ctr, ops = _make_ops()
    # A path with a space must be shlex-quoted, not bare-double-quoted.
    ops.concretize(env_dir_host=Path("/host"), env_dir_container="/work/has space/dir")
    assert ctr.scripts, "concretize should emit a script"
    script = ctr.scripts[-1]
    # shlex.quote('/work/has space/dir/spack.yaml') == "'/work/has space/dir/spack.yaml'"
    assert "'/work/has space/dir/spack.yaml'" in script
    # The unsafe form must NOT appear.
    assert '"/work/has space/dir/spack.yaml"' not in script


def test_mirror_create_quotes_paths_with_spaces() -> None:
    ctr, ops = _make_ops()
    # mirror_create also parses stats at the end; stub it to isolate quoting.
    ops._parse_mirror_stats = lambda: {"present": 0, "added": 0, "failed": 0}
    ops.mirror_create(
        env_dir_container="/work/has space/env",
        mirror_dir_container="/work/mir ror",
    )
    assert ctr.scripts
    script = ctr.scripts[-1]
    assert "'/work/mir ror'" in script
    assert "'/work/has space/env/spack.yaml'" in script
    assert '"/work/mir ror"' not in script

