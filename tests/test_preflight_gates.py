"""Preflight gates: empty-mirror warning + build-sif loud unknown env.

Audit 2.8: a configured-but-empty source mirror silently broke the
offline promise. Audit 2.7: build-sif swallowed an unknown --app-version
and packed hpc-cp2k:latest instead of failing like the dockerfile command.
"""

from __future__ import annotations

from pathlib import Path

from hpc_cf.execution import ProjectLayout
from hpc_cf.validation import (
    ValidationProfile,
    collect_mirror_content,
    validate_environment,
)


def _env(tmp_path: Path) -> Path:
    env_root = tmp_path / "spack-envs" / "demo"
    conf = env_root / "spack-env-file"
    conf.mkdir(parents=True)
    (conf / "env.yaml").write_text(
        "schema_version: 1\nmethod: spack\n"
        "spack:\n"
        "  version: '1.1.1'\n"
        "  env_name: demo-env\n",
        encoding="utf-8",
    )
    return env_root


# --- mirror.empty -------------------------------------------------------------


def test_missing_mirror_dir_warns(tmp_path: Path) -> None:
    _env(tmp_path)
    layout = ProjectLayout(project_root=tmp_path)
    findings = collect_mirror_content(layout)
    assert len(findings) == 1
    assert findings[0].code == "mirror.empty"
    assert findings[0].severity.value == "warning"


def test_metadata_only_mirror_warns(tmp_path: Path) -> None:
    _env(tmp_path)
    layout = ProjectLayout(project_root=tmp_path)
    layout.spack_mirror_dir.mkdir(parents=True)
    (layout.spack_mirror_dir / ".hpc_cf").mkdir()
    (layout.spack_mirror_dir / ".hpc_cf" / "runs").mkdir()
    findings = collect_mirror_content(layout)
    assert len(findings) == 1
    assert "no source content" in findings[0].message


def test_populated_mirror_is_clean(tmp_path: Path) -> None:
    _env(tmp_path)
    layout = ProjectLayout(project_root=tmp_path)
    layout.spack_mirror_dir.mkdir(parents=True)
    (layout.spack_mirror_dir / "some-source.tar.gz").write_text("x", encoding="utf-8")
    assert collect_mirror_content(layout) == []


def test_build_input_profile_carries_mirror_warning(tmp_path: Path) -> None:
    from hpc_cf.validation import BootstrapContract

    env_root = _env(tmp_path)
    conf = env_root / "spack-env-file"
    (conf / "spack.yaml").write_text(
        "spack:\n"
        "  specs: [pkgconf]\n"
        "  repos:\n"
        "    builtin:\n"
        "      commit: 1234567890abcdef1234567890abcdef12345678\n",
        encoding="utf-8",
    )
    (conf / "spack.lock").write_text('{"_meta": {}}', encoding="utf-8")
    # Spack tarball + bootstrap present so spack_assets stays quiet and the
    # only finding left is the mirror one.
    layout = ProjectLayout(project_root=tmp_path)
    layout.assets_dir.mkdir(parents=True)
    (layout.assets_dir / "spack-v1.1.1.tar.gz").write_text("x", encoding="utf-8")
    bootstrap = layout.bootstrap_dir("1.1.1")
    for relative in BootstrapContract.required_relative_paths():
        path = bootstrap / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("bootstrap: valid\n", encoding="utf-8")

    report = validate_environment(
        env_root, ValidationProfile.BUILD_INPUT, layout=layout
    )
    assert any(f.code == "mirror.empty" for f in report.findings)
    assert report.ok  # warning only — offline fallback is a choice


# --- build-sif unknown env ------------------------------------------------------


def test_build_sif_unknown_app_version_fails_loudly() -> None:
    """Unknown --app-version must not fall back to hpc-cp2k:latest."""
    import pytest

    from hpc_cf.cli import run_new_cli

    with pytest.raises(FileNotFoundError, match="No template found"):
        run_new_cli(["build-sif", "--app-version", "definitely-not-an-env"])


def test_build_sif_error_lists_available_envs() -> None:
    import pytest

    from hpc_cf.cli import run_new_cli

    with pytest.raises(FileNotFoundError) as excinfo:
        run_new_cli(["build-sif", "--app-version", "definitely-not-an-env"])
    assert "Available versions:" in str(excinfo.value)
