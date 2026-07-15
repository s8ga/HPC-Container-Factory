"""Validation profiles, findings, and CLI report formats."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from hpc_cf.execution import ProjectLayout
from hpc_cf.validation import (
    ValidationFinding,
    ValidationProfile,
    ValidationReport,
    ValidationSeverity,
    validate_environment,
)


def _write_minimal_env(env_dir: Path, *, method: str = "spack") -> None:
    env_dir.mkdir(parents=True, exist_ok=True)
    (env_dir / "env.yaml").write_text(
        f"schema_version: 1\nmethod: {method}\n"
        "spack:\n  version: '1.1.1'\n  env_name: test-env\n",
        encoding="utf-8",
    )
    (env_dir / "Dockerfile.j2").write_text("FROM debian:trixie\n", encoding="utf-8")
    if method == "spack":
        (env_dir / "spack.yaml").write_text(
            "spack:\n  specs: [pkgconf]\n",
            encoding="utf-8",
        )


def test_config_profile_skips_missing_spack_tarball(tmp_path: Path) -> None:
    env_dir = tmp_path / "env"
    _write_minimal_env(env_dir)
    layout = ProjectLayout(project_root=tmp_path)
    (layout.assets_dir).mkdir()

    report = validate_environment(
        env_dir, ValidationProfile.CONFIG, layout=layout
    )
    assert report.ok
    assert not any(f.code.startswith("spack_assets.") for f in report.findings)


def test_build_input_requires_spack_tarball(tmp_path: Path) -> None:
    env_dir = tmp_path / "env"
    _write_minimal_env(env_dir)
    layout = ProjectLayout(project_root=tmp_path)
    layout.assets_dir.mkdir()

    report = validate_environment(
        env_dir, ValidationProfile.BUILD_INPUT, layout=layout
    )
    assert not report.ok
    codes = {f.code for f in report.errors()}
    assert "spack_assets.tarball_missing" in codes


def test_assets_profile_requires_tarball_not_manual_packages(tmp_path: Path) -> None:
    env_dir = tmp_path / "env"
    env_dir.mkdir()
    (env_dir / "env.yaml").write_text(
        "schema_version: 1\nmethod: spack\n"
        "spack:\n  version: '1.1.1'\n  env_name: test-env\n"
        "manual_packages:\n  - file: missing.tgz\n",
        encoding="utf-8",
    )
    (env_dir / "spack.yaml").write_text(
        "spack:\n  specs: [pkgconf]\n",
        encoding="utf-8",
    )
    layout = ProjectLayout(project_root=tmp_path)
    layout.assets_dir.mkdir()

    assets_report = validate_environment(
        env_dir, ValidationProfile.ASSETS, layout=layout
    )
    assert any(f.code == "spack_assets.tarball_missing" for f in assets_report.findings)
    assert not any(f.code.startswith("manual_packages.") for f in assets_report.findings)

    build_report = validate_environment(
        env_dir, ValidationProfile.BUILD_INPUT, layout=layout
    )
    assert any(f.code == "manual_packages.missing" for f in build_report.findings)


def test_no_spack_skips_asset_checks(tmp_path: Path) -> None:
    env_dir = tmp_path / "env"
    _write_minimal_env(env_dir, method="no_spack")
    layout = ProjectLayout(project_root=tmp_path)

    for profile in (
        ValidationProfile.CONFIG,
        ValidationProfile.BUILD_INPUT,
        ValidationProfile.ASSETS,
    ):
        report = validate_environment(env_dir, profile, layout=layout)
        assert report.ok, profile
        assert not any(f.code.startswith("spack_assets.") for f in report.findings)


def test_report_json_and_text_roundtrip() -> None:
    report = ValidationReport(profile="config", env_name="demo")
    report.add(
        ValidationFinding(
            code="schema.invalid",
            severity=ValidationSeverity.ERROR,
            message="bad",
            path="/tmp/env.yaml",
            fix_hint="fix it",
        )
    )
    data = json.loads(report.format_json())
    assert data["ok"] is False
    assert data["profile"] == "config"
    assert data["findings"][0]["code"] == "schema.invalid"
    assert data["findings"][0]["fix_hint"] == "fix it"
    text = report.format_text()
    assert "schema.invalid" in text
    assert "FAILED" in text


def test_profile_alias_template() -> None:
    assert ValidationProfile.parse("template") is ValidationProfile.CONFIG
    assert ValidationProfile.parse("config/template") is ValidationProfile.CONFIG


def test_cli_validate_json_format(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    from hpc_cf.cli import run_new_cli

    env_name = "valtest_nospack"
    env_dir = tmp_path / "spack-envs" / env_name
    _write_minimal_env(env_dir, method="no_spack")
    monkeypatch.setattr(
        "hpc_cf.execution.ProjectLayout.default",
        classmethod(lambda cls: ProjectLayout(project_root=tmp_path)),
    )

    rc = run_new_cli(
        [
            "validate",
            "--app-version", env_name,
            "--profile", "config",
            "--format", "json",
        ]
    )
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["profile"] == "config"


def test_all_envs_pass_config_profile() -> None:
    """Contract: every discovered env loads and passes config validation."""
    from hpc_cf.config import PROJECT_ROOT
    from hpc_cf.env import list_available_envs

    layout = ProjectLayout(project_root=PROJECT_ROOT)
    envs = list_available_envs(layout=layout)
    assert envs, "expected at least one env under spack-envs/"
    for name in envs:
        env_dir = layout.spack_envs_dir / name
        report = validate_environment(
            env_dir, ValidationProfile.CONFIG, layout=layout
        )
        assert report.ok, f"{name}: {report.format_text()}"
