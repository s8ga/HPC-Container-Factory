"""EnvironmentSpec v1 schema, BuildMethod policy, and parser contract."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from hpc_cf.environment import (
    SUPPORTED_SCHEMA_VERSION,
    BuildMethod,
    EnvironmentSpec,
    load_environment_spec,
    parse_environment_spec,
)
from hpc_cf.env import find_env_yaml, load_env_yaml
from hpc_cf.spack_ops import load_env_config


MINIMAL_V1 = """\
schema_version: 1
images:
  builder: debian:trixie
  runtime: debian:trixie-slim
spack:
  version: "1.1.1"
  env_name: test-env
"""


def _write_env(tmp_path: Path, body: str, *, nested: bool = True) -> Path:
    env_dir = tmp_path / "my-env"
    env_dir.mkdir()
    target = env_dir / "spack-env-file" if nested else env_dir
    if nested:
        target.mkdir()
    (target / "env.yaml").write_text(body, encoding="utf-8")
    return env_dir


def test_build_method_spack_policy() -> None:
    m = BuildMethod.SPACK
    assert m.requires_spack_assets is True
    assert m.default_template is None
    assert m.allows_mirror is True
    assert m.runs_spack_validations is True


def test_build_method_no_spack_policy() -> None:
    m = BuildMethod.NO_SPACK
    assert m.requires_spack_assets is False
    assert m.default_template == "Dockerfile.nospack.j2"
    assert m.allows_mirror is False
    assert m.runs_spack_validations is False


def test_parse_schema_version_1() -> None:
    spec = parse_environment_spec(
        {
            "schema_version": 1,
            "method": "spack",
            "images": {"builder": "a", "runtime": "b"},
            "spack": {"version": "1.1.1", "env_name": "e"},
            "template_vars": {"k": "v"},
        }
    )
    assert spec.schema_version == SUPPORTED_SCHEMA_VERSION
    assert spec.method is BuildMethod.SPACK
    assert spec.images.builder == "a"
    assert spec.spack.version == "1.1.1"
    assert spec.template_vars == {"k": "v"}


def test_missing_schema_version_treated_as_v1(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING):
        spec = parse_environment_spec(
            {
                "images": {"builder": "a", "runtime": "b"},
                "spack": {"version": "1.1.1", "env_name": "e"},
            },
            source="<memory>",
        )
    assert spec.schema_version == 1
    assert "schema_version" in caplog.text
    assert "migration" in caplog.text.lower() or "schema_version: 1" in caplog.text


def test_unknown_schema_version_rejected() -> None:
    with pytest.raises(ValueError, match="Unsupported schema_version"):
        parse_environment_spec({"schema_version": 99, "spack": {"version": "1.1.1"}})


def test_parse_full_fields() -> None:
    raw = {
        "schema_version": 1,
        "method": "no_spack",
        "images": {
            "builder": "img:build",
            "runtime": "img:run",
            "output_name": "out",
            "output_tag": "t1",
        },
        "spack": {
            "version": "1.1.1",
            "env_name": "x-env",
            "custom_repos": [
                {
                    "url": "https://example.com/r.git",
                    "branch": "main",
                    "sparse_path": "tools/spack",
                    "namespace": "git-ns",
                },
                {"path": "repos", "namespace": "local-ns"},
            ],
        },
        "mirror_builder": {
            "system_pkgs": ["git"],
            "pkg_mirror_setup": "echo hi",
            "pkg_install_cmd": "apt-get install -y",
        },
        "manual_packages": [
            {"file": "assets/x.tgz", "dest": "/opt/x/", "sha256": "abc"},
        ],
        "runtime": {"copy_dirs": ["/opt/app"], "extra_pkgs": ["libgomp1"]},
        "script": "echo build",
        "template_vars": {"cp2k_branch": "v1"},
    }
    spec = parse_environment_spec(raw)
    assert spec.method is BuildMethod.NO_SPACK
    assert spec.images.output_name == "out"
    assert len(spec.spack.custom_repos) == 2
    assert spec.spack.custom_repos[0].type == "git"
    assert spec.spack.custom_repos[1].type == "local"
    assert spec.mirror_builder.system_pkgs == ["git"]
    assert spec.manual_packages[0].file == "assets/x.tgz"
    assert spec.runtime.copy_dirs == ["/opt/app"]
    assert spec.script == "echo build"
    assert spec.template_vars["cp2k_branch"] == "v1"


def test_invalid_method_rejected() -> None:
    with pytest.raises(ValueError, match="method"):
        parse_environment_spec({"schema_version": 1, "method": "magic"})


def test_load_environment_spec_from_dir(tmp_path: Path) -> None:
    env_dir = _write_env(tmp_path, MINIMAL_V1)
    spec = load_environment_spec(env_dir)
    assert isinstance(spec, EnvironmentSpec)
    assert spec.spack.env_name == "test-env"
    assert spec.source_path == find_env_yaml(env_dir)


def test_load_env_yaml_deprecated_wrapper(
    tmp_path: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    env_dir = _write_env(tmp_path, MINIMAL_V1)
    tpl = env_dir / "Dockerfile.j2"
    tpl.write_text("FROM x\n", encoding="utf-8")
    with caplog.at_level(logging.WARNING):
        raw = load_env_yaml(tpl)
    assert raw["spack"]["env_name"] == "test-env"
    assert raw["schema_version"] == 1
    assert "deprecated" in caplog.text.lower()


def test_load_env_config_deprecated_wrapper(
    tmp_path: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    env_dir = _write_env(tmp_path, MINIMAL_V1)
    with caplog.at_level(logging.WARNING):
        cfg = load_env_config(env_dir)
    assert cfg.spack.version == "1.1.1"
    assert cfg.spack.env_name == "test-env"
    assert "deprecated" in caplog.text.lower()


def test_as_dict_roundtrip_keys() -> None:
    spec = parse_environment_spec(
        {
            "schema_version": 1,
            "method": "spack",
            "images": {"builder": "a", "runtime": "b"},
            "spack": {"version": "1.1.1", "env_name": "e"},
            "manual_packages": [{"file": "f.tgz"}],
        }
    )
    d = spec.as_dict()
    assert d["schema_version"] == 1
    assert d["method"] == "spack"
    assert d["images"]["builder"] == "a"
    assert d["manual_packages"][0]["file"] == "f.tgz"


def test_shipped_envs_declare_schema_version_1() -> None:
    root = Path(__file__).resolve().parent.parent / "spack-envs"
    env_dirs = [
        d
        for d in sorted(root.iterdir())
        if d.is_dir()
        and (
            (d / "spack-env-file" / "env.yaml").exists()
            or (d / "env.yaml").exists()
        )
    ]
    assert env_dirs, "expected at least one env with env.yaml"
    for env_dir in env_dirs:
        loaded = load_environment_spec(env_dir)
        assert loaded.schema_version == 1, env_dir.name
        assert loaded.source_path is not None
        text = loaded.source_path.read_text(encoding="utf-8")
        assert "schema_version: 1" in text, env_dir.name
