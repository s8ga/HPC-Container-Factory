"""Unit tests for --force-rm image build hygiene and container storage GC."""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from hpc_cf.cli import run_new_cli
from hpc_cf.sif import build_docker_like, build_docker_stage
from hpc_cf.storage_gc import (
    detect_active_builds,
    is_checkpoint_builder_tag,
    parse_buildah_working_containers,
    parse_checkpoint_tags,
    run_storage_gc,
)


def test_build_docker_like_defaults_force_rm_and_respects_override(tmp_path: Path) -> None:
    dockerfile = tmp_path / "Dockerfile"
    dockerfile.write_text(
        "FROM base AS builder-installed\nFROM builder-installed AS builder\nFROM builder AS final\n",
        encoding="utf-8",
    )
    calls: list[list[str]] = []
    with (
        patch("hpc_cf.sif.check_command_exists", return_value=True),
        patch("hpc_cf.sif.run_cmd", side_effect=lambda cmd, **_: calls.append(list(cmd))),
    ):
        build_docker_like(
            dockerfile=dockerfile,
            image="vasp_mkl",
            tag="6.6.1-avx2",
            engine="podman",
            network_host=True,
        )
        build_docker_stage(
            dockerfile=dockerfile,
            image_ref="vasp_mkl:stage",
            target="builder",
            engine="podman",
            network_host=False,
            build_opts=["--force-rm=false"],
        )

    for cmd in calls[:3]:
        assert cmd[:3] == ["podman", "build", "--force-rm"]
    assert calls[3].count("--force-rm") == 0
    assert "--force-rm=false" in calls[3]


def test_parse_buildah_containers_and_checkpoint_tags() -> None:
    raw = json.dumps(
        [
            {"id": "9fd66af164df8218", "builder": True, "containername": "debian-working-container"},
            {"id": "1111111111111111", "builder": False, "containername": "hpc-mirror-builder-work"},
        ]
    )
    assert parse_buildah_working_containers(raw) == [("9fd66af164df8218", "debian-working-container")]
    assert parse_buildah_working_containers("null") == []

    assert is_checkpoint_builder_tag("localhost/vasp_mkl:6.6.1-avx2-installed")
    assert is_checkpoint_builder_tag("localhost/vasp_mkl:6.6.1-avx2-builder")
    assert not is_checkpoint_builder_tag("localhost/cp2k:2026.2-buildcache-producer")
    assert not is_checkpoint_builder_tag("localhost/vasp_mkl:6.6.1-avx2")

    imgs = json.dumps(
        [{"Names": ["localhost/vasp_mkl:6.6.1-avx2", "localhost/vasp_mkl:6.6.1-avx2-builder"]}]
    )
    assert parse_checkpoint_tags(imgs) == ["localhost/vasp_mkl:6.6.1-avx2-builder"]


def test_detect_active_builds_and_gc_cli(tmp_path: Path, capsys) -> None:
    uid = os.getuid()
    build_pid = tmp_path / "101"
    build_pid.mkdir()
    (build_pid / "cmdline").write_bytes(b"/usr/bin/podman\x00build\x00-t\x00x\x00.\x00")
    exec_pid = tmp_path / "102"
    exec_pid.mkdir()
    (exec_pid / "cmdline").write_bytes(b"/usr/bin/podman\x00exec\x00hpc-mirror-builder-work\x00bash\x00")
    assert detect_active_builds(proc_root=tmp_path, uid=uid, self_pid=999) == [
        (101, "/usr/bin/podman build -t x .")
    ]

    with (
        patch("hpc_cf.storage_gc.shutil.which", return_value="/usr/bin/podman"),
        patch("hpc_cf.storage_gc.detect_active_builds", return_value=[(42, "podman build .")]),
        pytest.raises(RuntimeError, match="Refusing to run storage GC"),
    ):
        run_storage_gc(engine="podman", dry_run=False, force=False)

    run_calls: list[list[str]] = []

    def fake_run(cmd: list[str], **kwargs):
        run_calls.append(list(cmd))
        if cmd[:2] == ["buildah", "containers"]:
            return MagicMock(
                returncode=0,
                stdout=json.dumps([{"id": "abcdef1234567890", "builder": True, "containername": "w"}]),
            )
        if cmd[:2] == ["podman", "images"]:
            return MagicMock(
                returncode=0,
                stdout=json.dumps([{"Names": ["localhost/vasp_mkl:6.6.1-builder"]}]),
            )
        return MagicMock(returncode=0, stdout="")

    with (
        patch("hpc_cf.storage_gc.shutil.which", return_value="/usr/bin/podman"),
        patch("hpc_cf.storage_gc.detect_active_builds", return_value=[]),
        patch("hpc_cf.storage_gc.subprocess.run", side_effect=fake_run),
    ):
        assert run_new_cli(["gc", "--prune-builder-tags"]) == 0

    assert "buildah_working_containers=1 removed=1" in capsys.readouterr().out
    assert ["buildah", "rm", "abcdef1234567890"] in run_calls
    assert ["podman", "rmi", "localhost/vasp_mkl:6.6.1-builder"] in run_calls
    assert ["podman", "image", "prune", "-f"] in run_calls
