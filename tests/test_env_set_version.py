"""env set-version: atomic version bump across the env's files.

cp2k 2026.2 → 2026.3 used to be ~13 hand edits (env.yaml, spack.yaml
spec, Dockerfile titles/labels, def headers) with nothing verifying the
result (round-2 audit 2.7). These tests pin the surgical replacement,
boundary rules, revert-on-failure, and the stale-lock gate.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hpc_cf.env_set_version import run_env_set_version
from hpc_cf.execution import ProjectLayout
from hpc_cf.requests import EnvSetVersionRequest


def _make_env(root: Path, *, with_lock: bool = False) -> Path:
    env_dir = root / "spack-envs" / "app-2026.2"
    conf = env_dir / "spack-env-file"
    conf.mkdir(parents=True)
    (conf / "env.yaml").write_text(
        "schema_version: 1\nmethod: spack\n"
        "spack:\n  version: '1.2.0'\n  env_name: app-env\n"
        "template_vars:\n  app_branch: release-2026.2\n",
        encoding="utf-8",
    )
    (conf / "spack.yaml").write_text(
        "spack:\n  specs:\n    - app@2026.2\n", encoding="utf-8"
    )
    (env_dir / "Dockerfile.j2").write_text(
        "# App 2026.2 image\nFROM debian:trixie AS builder\n"
        "RUN echo {{ app_branch }}\n",
        encoding="utf-8",
    )
    (env_dir / "app.def.j2").write_text(
        "# Definition for App 2026.2\nBootstrap: docker-archive\n",
        encoding="utf-8",
    )
    # Longer versions sharing the prefix must NOT be touched.
    (conf / "spack.yaml").write_text(
        "spack:\n  specs:\n    - app@2026.2\n    - dep@2026.2.1\n",
        encoding="utf-8",
    )
    if with_lock:
        (conf / "spack.lock").write_text('{"concrete": true}\n', encoding="utf-8")
    return env_dir


def _request(env_dir: Path, **overrides: object) -> EnvSetVersionRequest:
    base: dict[str, object] = {
        "env": env_dir.name,
        "old_version": "2026.2",
        "new_version": "2026.3",
    }
    base.update(overrides)
    return EnvSetVersionRequest(**base)  # type: ignore[arg-type]


def _layout(env_dir: Path) -> ProjectLayout:
    return ProjectLayout(project_root=env_dir.parent.parent)


def test_bumps_every_file_and_keeps_boundaries(tmp_path: Path) -> None:
    env_dir = _make_env(tmp_path)
    assert run_env_set_version(_request(env_dir), layout=_layout(env_dir)) == 0

    conf = env_dir / "spack-env-file"
    assert "app_branch: release-2026.3" in (conf / "env.yaml").read_text(
        encoding="utf-8"
    )
    spack_yaml = (conf / "spack.yaml").read_text(encoding="utf-8")
    assert "app@2026.3" in spack_yaml
    assert "dep@2026.2.1" in spack_yaml  # longer version untouched
    assert "App 2026.3 image" in (env_dir / "Dockerfile.j2").read_text(
        encoding="utf-8"
    )
    assert "App 2026.3" in (env_dir / "app.def.j2").read_text(encoding="utf-8")


def test_dry_run_writes_nothing(tmp_path: Path) -> None:
    env_dir = _make_env(tmp_path)
    before = (env_dir / "Dockerfile.j2").read_text(encoding="utf-8")
    assert (
        run_env_set_version(
            _request(env_dir, dry_run=True), layout=_layout(env_dir)
        )
        == 0
    )
    assert (env_dir / "Dockerfile.j2").read_text(encoding="utf-8") == before


def test_missing_lock_deletion_gate(tmp_path: Path) -> None:
    env_dir = _make_env(tmp_path, with_lock=True)
    rc = run_env_set_version(_request(env_dir), layout=_layout(env_dir))
    assert rc == 1
    assert (env_dir / "spack-env-file" / "spack.lock").is_file()

    assert (
        run_env_set_version(
            _request(env_dir, delete_lock=True), layout=_layout(env_dir)
        )
        == 0
    )
    assert not (env_dir / "spack-env-file" / "spack.lock").exists()


def test_wrong_from_token_refuses(tmp_path: Path) -> None:
    env_dir = _make_env(tmp_path)
    assert (
        run_env_set_version(
            _request(env_dir, old_version="1999.9"), layout=_layout(env_dir)
        )
        == 1
    )


def test_guard_failure_reverts_all_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env_dir = _make_env(tmp_path)
    originals = {
        p: p.read_text(encoding="utf-8")
        for p in (
            env_dir / "spack-env-file" / "env.yaml",
            env_dir / "spack-env-file" / "spack.yaml",
            env_dir / "Dockerfile.j2",
            env_dir / "app.def.j2",
        )
    }
    monkeypatch.setattr(
        "hpc_cf.env_set_version.check_env_repo_pins",
        lambda *a, **k: ["synthetic guard failure"],
    )
    assert run_env_set_version(_request(env_dir), layout=_layout(env_dir)) == 1
    for path, text in originals.items():
        assert path.read_text(encoding="utf-8") == text


def test_unknown_env_refused(tmp_path: Path) -> None:
    request = EnvSetVersionRequest(
        env="nope", old_version="1", new_version="2"
    )
    assert run_env_set_version(request, layout=ProjectLayout(project_root=tmp_path)) == 1
