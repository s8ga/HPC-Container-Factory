"""Unit tests for v3 SIF direct docker-archive, /dev/shm fast path, and --smoke-cmd."""

from __future__ import annotations

import subprocess
from collections import namedtuple
from pathlib import Path
from unittest.mock import patch

import pytest

from hpc_cf import sif as sif_mod
from hpc_cf.cli import build_parser
from hpc_cf.execution import ProjectLayout

_DiskUsage = namedtuple("DiskUsage", ["total", "used", "free"])


def test_select_sif_staging_dir_threshold(tmp_path: Path, monkeypatch) -> None:
    shm = tmp_path / "shm"
    shm.mkdir()
    monkeypatch.setattr(
        sif_mod.shutil,
        "disk_usage",
        lambda _p: _DiskUsage(10 * 1024**3, 0, 5 * 1024**3),
    )
    # 2 GiB image requires 2.5*2 + 2 = 7 GiB > 5 GiB free -> fallback None
    assert sif_mod._select_sif_staging_dir(2 * 1024**3, shm_root=shm) is None

    # Tiny 100 MiB image requires max(2.25 GiB, 4 GiB) = 4 GiB <= 5 GiB free -> shm dir
    picked = sif_mod._select_sif_staging_dir(100 * 1024**2, shm_root=shm)
    assert picked is not None
    assert picked.parent == shm
    assert picked.is_dir()


def test_build_sif_uses_shm_fastpath_disable_cache_zstd22_and_smoke(tmp_path: Path) -> None:
    layout = ProjectLayout(project_root=tmp_path)
    layout.artifacts_dir.mkdir()
    shm_dir = tmp_path / "fake_shm"
    shm_dir.mkdir()
    output = tmp_path / "out.sif"
    observed: list[tuple[list[str], dict | None]] = []

    def fake_run(cmd: list[str], *, env: dict | None = None, **_kw: object) -> None:
        observed.append((list(cmd), env))
        if cmd[0] == "podman" and "save" in cmd:
            Path(cmd[cmd.index("-o") + 1]).write_bytes(b"tar")
        elif cmd[:2] == ["/usr/bin/apptainer", "build"]:
            assert shm_dir.exists()
            output.write_bytes(b"sif-bytes")
        elif cmd[:2] == ["/usr/bin/apptainer", "exec"]:
            # /dev/shm staging must already be freed before running --smoke-cmd
            assert not shm_dir.exists()

    with (
        patch.object(sif_mod, "ensure_apptainer", return_value="/usr/bin/apptainer"),
        patch.object(sif_mod, "select_engine_with_image", return_value=("podman", "sha256:abc")),
        patch.object(sif_mod, "inspect_local_image_size", return_value=2 * 1024**3),
        patch.object(sif_mod, "_select_sif_staging_dir", return_value=shm_dir),
        patch.object(sif_mod, "run_cmd", side_effect=fake_run),
    ):
        sif_mod.build_sif(
            docker_image="cp2k_opensource",
            docker_tag="master",
            output=output,
            smoke_cmd="cp2k.psmp --version",
            yes=True,
            layout=layout,
        )

    assert len(observed) == 3
    save_cmd, _ = observed[0]
    build_cmd, build_env = observed[1]
    smoke_cmd, _ = observed[2]

    assert save_cmd[:3] == ["podman", "save", "-o"]
    assert Path(save_cmd[3]).parent == shm_dir
    assert "--disable-cache" in build_cmd
    assert "-comp zstd -Xcompression-level 22 -b 1M" in build_cmd
    assert f"docker-archive://{shm_dir / 'cp2k_opensource_master.tar'}" in build_cmd
    assert build_env is not None
    assert build_env["APPTAINER_TMPDIR"] == str(shm_dir / "tmp")
    assert smoke_cmd == ["/usr/bin/apptainer", "exec", str(output), "bash", "-lc", "cp2k.psmp --version"]
    assert not shm_dir.exists()


def test_build_sif_cleans_shm_staging_on_failure_and_propagates_smoke_fail(tmp_path: Path) -> None:
    layout = ProjectLayout(project_root=tmp_path)
    layout.artifacts_dir.mkdir()
    shm_dir = tmp_path / "fake_shm_fail"
    shm_dir.mkdir()

    with (
        patch.object(sif_mod, "ensure_apptainer", return_value="/usr/bin/apptainer"),
        patch.object(sif_mod, "select_engine_with_image", return_value=("podman", "sha256:abc")),
        patch.object(sif_mod, "inspect_local_image_size", return_value=0),
        patch.object(sif_mod, "_select_sif_staging_dir", return_value=shm_dir),
        patch.object(sif_mod, "run_cmd", side_effect=subprocess.CalledProcessError(1, ["apptainer"])),
        pytest.raises(subprocess.CalledProcessError),
    ):
        sif_mod.build_sif(
            docker_image="demo",
            docker_tag="tag",
            yes=True,
            layout=layout,
        )
    assert not shm_dir.exists()


def test_build_sif_cli_parses_smoke_cmd() -> None:
    parser = build_parser()
    args = parser.parse_args(
        ["build-sif", "--docker-image", "img", "--docker-tag", "t", "--smoke-cmd", "echo ok"]
    )
    assert args.smoke_cmd == "echo ok"
    assert args.mksquashfs_args == "-comp zstd -Xcompression-level 22 -b 1M"
