"""Integration seams of the mirror prune: the container-side fetch-spec
export script and the assets-level ``run_mirror_prune`` orchestration
(export → parse → plan → apply/dry-run → report), without a container
runtime. The ops boundary is stubbed; everything below it (parse guards,
plan, apply, report) runs for real against a tmp layout.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from hpc_cf.assets import run_mirror_prune
from hpc_cf.environment import CustomRepo, SpackConfig
from hpc_cf.execution import ProjectLayout
from hpc_cf.spack_ops import EnvConfig, SpackOps

KEEP_SHA = "7269437d655783a26cba32aa88195b741ff496aa"
OLD_SHA = "0fabc3ba44823f257e70ce397d989c8de5e362c1"
TORCH_URL = "https://github.com/pytorch/pytorch.git"

FETCH_SPECS = [
    {
        "name": "py-torch",
        "version": "2.12.1",
        "kind": "git",
        "url": TORCH_URL,
        "commit": KEEP_SHA,
    }
]


class OpsStub:
    """Stands in for SpackOps: "exports" by writing the host sidecar."""

    def __init__(self, payload: str, host_sidecar: Path) -> None:
        self._payload = payload
        self._host_sidecar = host_sidecar
        self.exported_to: str | None = None

    def export_fetch_specs(self, out_path: str) -> None:
        self.exported_to = out_path
        self._host_sidecar.parent.mkdir(parents=True, exist_ok=True)
        self._host_sidecar.write_text(self._payload, encoding="utf-8")


def _seed_tree(mirror: Path) -> None:
    git = mirror / "_source-cache" / "git" / "pytorch" / "pytorch.git"
    git.mkdir(parents=True)
    (git / f"{KEEP_SHA}.tar.gz").write_bytes(b"y" * 100)
    os.utime(git / f"{KEEP_SHA}.tar.gz", (0, 0))
    (git / f"{OLD_SHA}.tar.gz").write_bytes(b"x" * 400)
    os.utime(git / f"{OLD_SHA}.tar.gz", (0, 0))
    pkg = mirror / "py-torch"
    pkg.mkdir()
    (pkg / "py-torch-2.12.1.tar.gz").symlink_to(
        os.path.relpath(git / f"{KEEP_SHA}.tar.gz", pkg)
    )
    (pkg / "py-torch-2.4.1.tar.gz").symlink_to(
        os.path.relpath(git / f"{OLD_SHA}.tar.gz", pkg)
    )


def _patch_ops(monkeypatch: pytest.MonkeyPatch, payload: str, layout: ProjectLayout) -> OpsStub:
    sidecar = layout.spack_mirror_dir / ".hpc_cf" / "fetch-specs.json"
    ops = OpsStub(payload, sidecar)
    monkeypatch.setattr("hpc_cf.assets._make_spack_ops", lambda *a, **k: (None, ops))
    return ops


def test_dry_run_deletes_nothing_but_writes_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    layout = ProjectLayout(project_root=tmp_path)
    _seed_tree(layout.spack_mirror_dir)
    ops = _patch_ops(monkeypatch, json.dumps(FETCH_SPECS), layout)

    run_mirror_prune(
        ctr=object(),
        env_name="cp2k_opensource-master-force-avx512",
        apply_plan=False,
        fresh_after=1.0,
        layout=layout,
    )

    assert ops.exported_to == (
        f"{layout.container_mirror_dir()}/.hpc_cf/fetch-specs.json"
    )
    assert (
        layout.spack_mirror_dir
        / "_source-cache/git/pytorch/pytorch.git"
        / f"{OLD_SHA}.tar.gz"
    ).exists()
    assert (layout.spack_mirror_dir / "py-torch/py-torch-2.4.1.tar.gz").is_symlink()
    report = json.loads(
        (layout.spack_mirror_dir / ".hpc_cf/prune-report.json").read_text()
    )
    assert report["applied"] is False
    assert report["freed_bytes"] == 400
    assert report["delete_blobs"] == [
        f"_source-cache/git/pytorch/pytorch.git/{OLD_SHA}.tar.gz"
    ]


def test_apply_deletes_blobs_and_repairs_links(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    layout = ProjectLayout(project_root=tmp_path)
    _seed_tree(layout.spack_mirror_dir)
    _patch_ops(monkeypatch, json.dumps(FETCH_SPECS), layout)

    run_mirror_prune(
        ctr=object(),
        env_name="cp2k_opensource-master-force-avx512",
        apply_plan=True,
        fresh_after=1.0,
        layout=layout,
    )

    git = layout.spack_mirror_dir / "_source-cache/git/pytorch/pytorch.git"
    assert (git / f"{KEEP_SHA}.tar.gz").exists()
    assert not (git / f"{OLD_SHA}.tar.gz").exists()
    assert (layout.spack_mirror_dir / "py-torch/py-torch-2.12.1.tar.gz").is_symlink()
    assert not (layout.spack_mirror_dir / "py-torch/py-torch-2.4.1.tar.gz").exists()
    report = json.loads(
        (layout.spack_mirror_dir / ".hpc_cf/prune-report.json").read_text()
    )
    assert report["applied"] is True


def test_refuses_empty_sidecar(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    layout = ProjectLayout(project_root=tmp_path)
    layout.spack_mirror_dir.mkdir(parents=True)
    _patch_ops(monkeypatch, "[]", layout)

    with pytest.raises(RuntimeError, match="empty"):
        run_mirror_prune(
            ctr=object(), env_name="env",
            apply_plan=True, fresh_after=1.0, layout=layout,
        )


def test_refuses_invalid_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    layout = ProjectLayout(project_root=tmp_path)
    layout.spack_mirror_dir.mkdir(parents=True)
    _patch_ops(monkeypatch, "{not json", layout)

    with pytest.raises(RuntimeError, match="not valid JSON"):
        run_mirror_prune(
            ctr=object(), env_name="env",
            apply_plan=True, fresh_after=1.0, layout=layout,
        )


class _NoopContainer:
    def exec(self, script: str, *, capture: bool = False, check: bool = True):
        return SimpleNamespace(returncode=0, stdout="", stderr="")


def _env_config() -> EnvConfig:
    return EnvConfig(
        spack=SpackConfig(
            version="1.2.0",
            env_name="cp2k-env",
            custom_repos=[
                CustomRepo(
                    type="git",
                    namespace="cp2k_dev",
                    url="https://github.com/cp2k/cp2k.git",
                    branch="master",
                )
            ],
        )
    )


def test_fetch_specs_script_content() -> None:
    ops = SpackOps(_env_config(), _NoopContainer())
    script = ops._build_fetch_specs_script(
        "/work/assets/spack-mirror/.hpc_cf/fetch-specs.json"
    )
    assert "spack -e cp2k-env python /tmp/hpc_cf_fetch_specs.py" in script
    assert "/work/assets/spack-mirror/.hpc_cf/fetch-specs.json" in script
    assert "<<'HPC_CF_PY'" in script  # quoted heredoc: no bash expansion
    assert "active_environment" in script
    assert 'ev.read("cp2k-env")' in script
    assert "__ENV_NAME__" not in script  # placeholder fully substituted
    assert "getattr(fetcher, \"commit\", None)" in script


def test_request_flag_mapping() -> None:
    from hpc_cf.requests import assets_request_from_args

    args = SimpleNamespace(prune_mirror=True, prune_mirror_apply=True)
    req = assets_request_from_args(args)
    assert req.prune_mirror and req.prune_apply
    req_off = assets_request_from_args(SimpleNamespace())
    assert not req_off.prune_mirror and not req_off.prune_apply


def test_cli_parse_prune_flags_end_to_end() -> None:
    """Regression: --prune-mirror-apply's argparse dest is
    prune_mirror_apply — the request mapping once read ``prune_apply`` and
    silently forced every CI prune into a dry run."""
    from hpc_cf.cli import build_parser
    from hpc_cf.requests import assets_request_from_args

    parser = build_parser()
    args = parser.parse_args(
        ["assets", "--env", "some-env", "--prune-mirror", "--prune-mirror-apply"]
    )
    req = assets_request_from_args(args)
    assert req.prune_mirror is True
    assert req.prune_apply is True

    dry = assets_request_from_args(
        parser.parse_args(["assets", "--env", "some-env", "--prune-mirror"])
    )
    assert dry.prune_mirror is True
    assert dry.prune_apply is False
