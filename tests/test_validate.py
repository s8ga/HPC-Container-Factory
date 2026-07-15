"""Negative-path coverage for env static validators."""
from __future__ import annotations

from pathlib import Path

import pytest

from hpc_cf.env import (
    validate_branch_consistency,
    validate_manual_packages,
    validate_spack_assets,
)


def test_validate_manual_packages_sha256_mismatch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from hpc_cf.execution import ProjectLayout

    pkg = tmp_path / "manual.tgz"
    pkg.write_bytes(b"content-a")
    monkeypatch.setattr(
        "hpc_cf.env.ProjectLayout.default",
        classmethod(lambda cls: ProjectLayout(project_root=tmp_path)),
    )

    with pytest.raises(ValueError, match="sha256 mismatch"):
        validate_manual_packages(
            {
                "method": "no_spack",
                "manual_packages": [
                    {
                        "file": "manual.tgz",
                        "sha256": "0" * 64,
                    }
                ]
            }
        )


def test_validate_spack_assets_missing_tarball(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from hpc_cf.execution import ProjectLayout

    (tmp_path / "assets").mkdir()
    monkeypatch.setattr(
        "hpc_cf.env.ProjectLayout.default",
        classmethod(lambda cls: ProjectLayout(project_root=tmp_path)),
    )

    with pytest.raises(FileNotFoundError, match="Spack tarball"):
        validate_spack_assets(
            {"method": "spack", "spack": {"version": "9.9.9", "env_name": "e"}}
        )


def test_validate_branch_consistency_skips_non_cp2k(tmp_path: Path) -> None:
    """VASP/ABACUS-style envs must not trip CP2K branch checks."""
    env_dir = tmp_path / "env"
    env_dir.mkdir()
    (env_dir / "env.yaml").write_text(
        "schema_version: 1\n"
        "spack:\n  version: '1.1.1'\n  env_name: vasp-env\n",
        encoding="utf-8",
    )
    (env_dir / "Dockerfile.j2").write_text(
        "FROM debian:trixie\nRUN echo no-cp2k-clone\n",
        encoding="utf-8",
    )
    validate_branch_consistency(env_dir)  # no raise


def test_validate_branch_consistency_rejects_hardcoded_clone(tmp_path: Path) -> None:
    env_dir = tmp_path / "env"
    env_dir.mkdir()
    (env_dir / "env.yaml").write_text(
        "template_vars:\n  cp2k_branch: support/v2026.1\n",
        encoding="utf-8",
    )
    (env_dir / "Dockerfile.j2").write_text(
        "RUN git clone --filter=blob:none -b support/v2026.1 \\\n"
        "    https://github.com/cp2k/cp2k.git /opt/cp2k\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="hardcodes a branch"):
        validate_branch_consistency(env_dir)


def test_validate_branch_consistency_requires_template_vars(tmp_path: Path) -> None:
    env_dir = tmp_path / "env"
    env_dir.mkdir()
    (env_dir / "env.yaml").write_text("method: spack\n", encoding="utf-8")
    (env_dir / "Dockerfile.j2").write_text(
        "RUN git clone -b {{ cp2k_branch }} https://github.com/cp2k/cp2k.git /opt/cp2k\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="does not declare"):
        validate_branch_consistency(env_dir)
