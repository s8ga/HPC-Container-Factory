"""Floating custom-repo resolution: markers → sidecar → build-time pins.

Contract: git custom_repos WITHOUT a commit float to their branch tip at
assets time. prepare_repos records the fetched tips as container markers,
the assets workflow persists them in resolved-repos.yaml beside spack.yaml,
and resolve_build_input applies them in memory (repo.commit + the
``<namespace>_repo_commit`` template var) so the image-side clone matches
the sha the concretizer saw. Pinned repos are never overridden; a missing
sidecar is a no-op (env.yaml static values remain the fallback).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
import yaml

from hpc_cf.assets import _write_resolved_repo_sidecar
from hpc_cf.environment import (
    RESOLVED_REPO_PINS_FILENAME,
    EnvironmentSpec,
    SpackConfig,
    apply_resolved_repo_pins,
)
from hpc_cf.execution import ProjectLayout
from hpc_cf.spack_ops import (
    CustomRepo,
    EnvConfig,
    SpackOps,
    _parse_resolved_repo_markers,
)
from hpc_cf.template import resolve_build_input

SHA = "0123456789abcdef0123456789abcdef01234567"
OTHER_SHA = "fedcba9876543210fedcba9876543210fedcba98"


class MarkerContainer:
    """RunnerPort stand-in whose marker cat returns scripted stdout."""

    def __init__(self, marker_text: str) -> None:
        self.marker_text = marker_text
        self.scripts: list[str] = []

    def exec(self, script, *, capture=False, check=True):  # type: ignore[override]
        self.scripts.append(script)
        return subprocess.CompletedProcess(
            args=[], returncode=0, stdout=self.marker_text, stderr=""
        )


def _float_spec() -> EnvironmentSpec:
    return EnvConfig(spack=SpackConfig(
        version="1.2.0",
        env_name="cp2k-env",
        custom_repos=[
            CustomRepo(
                type="git",
                namespace="cp2k_dev",
                url="https://github.com/cp2k/cp2k.git",
                branch="master",
                sparse_path="tools/spack/spack_repo/cp2k_dev",
            ),
            CustomRepo(
                type="git",
                namespace="s8_overrides",
                url="https://example.invalid/s8ga.git",
                branch="master",
                commit=OTHER_SHA,
            ),
        ],
    ))


# ── marker parsing ────────────────────────────────────────────────────────


def test_parse_markers_valid() -> None:
    assert _parse_resolved_repo_markers(
        f"cp2k_dev={SHA}\n\nother={OTHER_SHA}\n"
    ) == {"cp2k_dev": SHA, "other": OTHER_SHA}


@pytest.mark.parametrize("bad", ["cp2k_dev", "cp2k_dev=zz", "=zz", "junk-line"])
def test_parse_markers_malformed_raises(bad: str) -> None:
    with pytest.raises(ValueError, match="malformed resolved-repo marker"):
        _parse_resolved_repo_markers(bad)


def test_parse_markers_empty_is_noop() -> None:
    assert _parse_resolved_repo_markers("") == {}


# ── script emission ───────────────────────────────────────────────────────


def test_float_repo_script_records_marker_pinned_does_not() -> None:
    ops = SpackOps(_float_spec(), MarkerContainer(""))
    scripts = {
        repo.namespace: ops._prepare_git_repo(repo)
        for repo in ops.env.spack.custom_repos
        if repo.type == "git"
    }
    float_script = scripts["cp2k_dev"]
    assert 'echo "cp2k_dev=$(git rev-parse HEAD)"' in float_script
    assert "hpc-cf-resolved-repos" in float_script
    assert "git checkout" not in float_script
    pinned_script = scripts["s8_overrides"]
    assert "git rev-parse" not in pinned_script
    assert "git checkout" in pinned_script


def test_prepare_repos_reads_markers_back() -> None:
    ctr = MarkerContainer(f"cp2k_dev={SHA}\n")
    ops = SpackOps(_float_spec(), ctr)
    ops.prepare_repos("/work/env")
    assert ops.resolved_repo_pins == {"cp2k_dev": SHA}
    # The truncate-first guard keeps stale worker state out of the record.
    prepare_script = ctr.scripts[0]
    assert ": > /tmp/hpc-cf-resolved-repos" in prepare_script


# ── sidecar write + build-time application ────────────────────────────────


def test_write_sidecar_and_apply_roundtrip(tmp_path: Path) -> None:
    ops = SpackOps(_float_spec(), MarkerContainer(f"cp2k_dev={SHA}\n"))
    ops.resolved_repo_pins = {"cp2k_dev": SHA}
    _write_resolved_repo_sidecar(ops, tmp_path)

    sidecar = tmp_path / RESOLVED_REPO_PINS_FILENAME
    data = yaml.safe_load(sidecar.read_text(encoding="utf-8"))
    assert data["repos"]["cp2k_dev"]["commit"] == SHA
    assert data["repos"]["cp2k_dev"]["branch"] == "master"
    assert "s8_overrides" not in data["repos"]  # pinned repos stay in env.yaml

    spec = _float_spec()
    spec.template_vars["cp2k_dev_repo_commit"] = "fallback"
    apply_resolved_repo_pins(spec, tmp_path)
    float_repo, pinned_repo = spec.spack.custom_repos
    assert float_repo.commit == SHA
    assert spec.template_vars["cp2k_dev_repo_commit"] == SHA
    assert pinned_repo.commit == OTHER_SHA  # pin is authoritative


def test_apply_missing_sidecar_is_noop(tmp_path: Path) -> None:
    spec = _float_spec()
    spec.template_vars["cp2k_dev_repo_commit"] = "fallback"
    apply_resolved_repo_pins(spec, tmp_path)
    assert spec.spack.custom_repos[0].commit is None
    assert spec.template_vars["cp2k_dev_repo_commit"] == "fallback"


def test_apply_malformed_sidecar_raises(tmp_path: Path) -> None:
    (tmp_path / RESOLVED_REPO_PINS_FILENAME).write_text(
        "repos: [not, a, mapping]", encoding="utf-8"
    )
    with pytest.raises(ValueError, match="repos"):
        apply_resolved_repo_pins(_float_spec(), tmp_path)


def test_apply_bad_sha_raises(tmp_path: Path) -> None:
    (tmp_path / RESOLVED_REPO_PINS_FILENAME).write_text(
        yaml.safe_dump({"repos": {"cp2k_dev": {"commit": "nope"}}}),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="40-char hex commit"):
        apply_resolved_repo_pins(_float_spec(), tmp_path)


def test_write_sidecar_skips_when_no_floats(tmp_path: Path) -> None:
    ops = SpackOps(_float_spec(), MarkerContainer(""))
    ops.resolved_repo_pins = {}
    _write_resolved_repo_sidecar(ops, tmp_path)
    assert not (tmp_path / RESOLVED_REPO_PINS_FILENAME).exists()


# ── resolve_build_input integration ───────────────────────────────────────


def _write_env(tmp_path: Path) -> Path:
    env_dir = tmp_path / "spack-envs" / "float-env"
    conf = env_dir / "spack-env-file"
    conf.mkdir(parents=True)
    (conf / "env.yaml").write_text(
        "schema_version: 1\n"
        "spack:\n"
        "  version: '1.2.0'\n"
        "  env_name: cp2k-env\n"
        "  custom_repos:\n"
        "    - url: https://github.com/cp2k/cp2k.git\n"
        "      branch: master\n"
        "      sparse_path: tools/spack/spack_repo/cp2k_dev\n"
        "      namespace: cp2k_dev\n"
        "template_vars:\n"
        "  cp2k_dev_repo_commit: fallback\n",
        encoding="utf-8",
    )
    (env_dir / "Dockerfile.j2").write_text("FROM x\n", encoding="utf-8")
    return env_dir


def test_resolve_build_input_applies_sidecar(tmp_path: Path) -> None:
    env_dir = _write_env(tmp_path)
    (env_dir / "spack-env-file" / RESOLVED_REPO_PINS_FILENAME).write_text(
        yaml.safe_dump(
            {"repos": {"cp2k_dev": {"commit": SHA, "branch": "master"}}}
        ),
        encoding="utf-8",
    )
    resolved = resolve_build_input("float-env", layout=ProjectLayout(project_root=tmp_path))
    assert resolved.environment_spec is not None
    repo = resolved.environment_spec.spack.custom_repos[0]
    assert repo.commit == SHA
    assert resolved.environment_spec.template_vars["cp2k_dev_repo_commit"] == SHA


def test_resolve_build_input_without_sidecar_keeps_fallback(
    tmp_path: Path,
) -> None:
    _write_env(tmp_path)
    resolved = resolve_build_input("float-env", layout=ProjectLayout(project_root=tmp_path))
    assert resolved.environment_spec is not None
    repo = resolved.environment_spec.spack.custom_repos[0]
    assert repo.commit is None
    assert (
        resolved.environment_spec.template_vars["cp2k_dev_repo_commit"]
        == "fallback"
    )
