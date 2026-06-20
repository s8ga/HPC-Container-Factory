"""L3: env.yaml parsing consistency.

Pins the contract that there is ONE file-location resolver (``find_env_yaml``)
used by both ``load_env_yaml`` and ``load_env_config``, preferring
``spack-env-file/env.yaml`` and falling back to a bare ``env.yaml``.

Before the A2 refactor these two loaders had REVERSED lookup orders and could
return different files for the same env. This test fails until A2 lands.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from hpc_cf.env import find_env_yaml, load_env_yaml
from hpc_cf.spack_ops import load_env_config


def _make_env(tmp_path: Path, *, nested: bool, body: str) -> Path:
    """Create an env dir; nested=True puts env.yaml under spack-env-file/."""
    env_dir = tmp_path / "my-env"
    env_dir.mkdir()
    target = env_dir / "spack-env-file" if nested else env_dir
    if nested:
        target.mkdir()
    (target / "env.yaml").write_text(body)
    return env_dir


def test_find_env_yaml_prefers_nested(tmp_path: Path) -> None:
    nested_body = "images:\n  builder: nested.img\n"
    bare_body = "images:\n  builder: bare.img\n"
    env_dir = _make_env(tmp_path, nested=True, body=nested_body)
    # Also drop a bare one to prove nested wins.
    (env_dir / "env.yaml").write_text(bare_body)

    resolved = find_env_yaml(env_dir)
    assert resolved == env_dir / "spack-env-file" / "env.yaml"


def test_find_env_yaml_falls_back_to_bare(tmp_path: Path) -> None:
    env_dir = _make_env(tmp_path, nested=False, body="images:\n  builder: bare.img\n")
    assert find_env_yaml(env_dir) == env_dir / "env.yaml"


def test_find_env_yaml_missing_raises(tmp_path: Path) -> None:
    env_dir = tmp_path / "empty-env"
    env_dir.mkdir()
    with pytest.raises(FileNotFoundError):
        find_env_yaml(env_dir)


def test_both_loaders_read_same_file(tmp_path: Path) -> None:
    """The bug: load_env_yaml (nested-first) vs load_env_config (bare-first)
    returned different files when both existed. After A2 both use find_env_yaml."""
    nested_body = (
        "spack:\n  version: '1.1.1'\n  env_name: agree-env\n"
        "images:\n  builder: nested.img\n"
    )
    bare_body = (
        "spack:\n  version: '0.0.0'\n  env_name: WRONG\n"
        "images:\n  builder: bare.img\n"
    )
    env_dir = _make_env(tmp_path, nested=True, body=nested_body)
    (env_dir / "env.yaml").write_text(bare_body)

    # load_env_yaml takes a template_path; use a synthetic one inside the dir.
    tpl = env_dir / "Dockerfile.j2"
    tpl.write_text("FROM x\n")
    raw = load_env_yaml(tpl)
    cfg = load_env_config(env_dir)

    assert raw["spack"]["version"] == "1.1.1"  # nested wins, not bare's 0.0.0
    assert cfg.spack.version == "1.1.1"
    assert cfg.spack.env_name == "agree-env"
