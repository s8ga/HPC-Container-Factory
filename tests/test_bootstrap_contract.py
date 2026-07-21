"""BootstrapContract: build-input and runtime verify share one file set."""
from __future__ import annotations

from pathlib import Path

import pytest

from hpc_cf.environment import load_environment_spec
from hpc_cf.execution import ProjectLayout
from hpc_cf.spack_ops import EXPECTED_BOOTSTRAP_BINARIES, EnvConfig, SpackConfig, SpackOps
from hpc_cf.validation import (
    BootstrapContract,
    ValidationProfile,
    ValidationSeverity,
    collect_spack_assets,
    validate_environment,
)


class _NoopRunner:
    def exec(self, *args, **kwargs):  # pragma: no cover - unused
        raise AssertionError("unexpected exec")

    def run_ephemeral(self, *args, **kwargs):  # pragma: no cover - unused
        raise AssertionError("unexpected run_ephemeral")


def _write_contract_bootstrap(bootstrap: Path, *, omit: str | None = None) -> None:
    for relative_path in BootstrapContract.required_relative_paths():
        if omit is not None and relative_path.as_posix() == omit:
            continue
        path = bootstrap / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("bootstrap: valid\n", encoding="utf-8")


def test_bootstrap_contract_includes_binary_package_json() -> None:
    rels = {p.as_posix() for p in BootstrapContract.required_relative_paths()}
    assert "metadata/sources/metadata.yaml" in rels
    assert "metadata/binaries/metadata.yaml" in rels
    for name in ("clingo", "gnupg", "patchelf"):
        assert f"metadata/binaries/{name}.json" in rels
    assert EXPECTED_BOOTSTRAP_BINARIES == BootstrapContract.BINARY_PACKAGES


def test_bootstrap_contract_is_complete_requires_all_files(tmp_path: Path) -> None:
    bootstrap = tmp_path / "bootstrap-1.1.1"
    _write_contract_bootstrap(bootstrap, omit="metadata/binaries/gnupg.json")
    assert BootstrapContract.invalid_paths(bootstrap)
    assert not BootstrapContract.is_complete(bootstrap)

    _write_contract_bootstrap(bootstrap)
    assert BootstrapContract.is_complete(bootstrap)


def test_verify_bootstrap_fail_closed_on_missing_binary_json(tmp_path: Path) -> None:
    bootstrap = tmp_path / "bootstrap-1.1.1"
    _write_contract_bootstrap(bootstrap, omit="metadata/binaries/patchelf.json")
    ops = SpackOps(
        EnvConfig(spack=SpackConfig(version="1.1.1", env_name="test-env")),
        _NoopRunner(),  # type: ignore[arg-type]
    )
    with pytest.raises(RuntimeError, match="patchelf\\.json"):
        ops._verify_bootstrap(bootstrap)
    assert ops._bootstrap_metadata_complete(bootstrap) is False


def test_verify_bootstrap_accepts_contract_complete_cache(tmp_path: Path) -> None:
    bootstrap = tmp_path / "bootstrap-1.1.1"
    _write_contract_bootstrap(bootstrap)
    ops = SpackOps(
        EnvConfig(spack=SpackConfig(version="1.1.1", env_name="test-env")),
        _NoopRunner(),  # type: ignore[arg-type]
    )
    ops._verify_bootstrap(bootstrap)
    assert ops._bootstrap_metadata_complete(bootstrap) is True


def test_build_input_and_verify_share_invalid_paths(tmp_path: Path) -> None:
    """Same missing file must fail both build-input and runtime verify."""
    env_dir = tmp_path / "env"
    env_dir.mkdir()
    (env_dir / "env.yaml").write_text(
        "schema_version: 1\nmethod: spack\n"
        "spack:\n  version: '1.1.1'\n  env_name: test-env\n",
        encoding="utf-8",
    )
    (env_dir / "Dockerfile.j2").write_text("FROM debian:trixie\n", encoding="utf-8")
    (env_dir / "spack.yaml").write_text("spack:\n  specs: [pkgconf]\n", encoding="utf-8")
    (env_dir / "spack.lock").write_text("{}\n", encoding="utf-8")

    layout = ProjectLayout(project_root=tmp_path)
    layout.assets_dir.mkdir()
    (layout.assets_dir / "spack-v1.1.1.tar.gz").write_bytes(b"x")
    bootstrap = layout.bootstrap_dir("1.1.1")
    _write_contract_bootstrap(bootstrap, omit="metadata/binaries/clingo.json")

    invalid = BootstrapContract.invalid_paths(bootstrap)
    assert any(p.name == "clingo.json" for p in invalid)

    findings = collect_spack_assets(
        load_environment_spec(env_dir),
        assets_dir=layout.assets_dir,
        require_bootstrap=True,
    )
    assert any(
        f.code == "spack_assets.bootstrap_missing"
        and f.severity is ValidationSeverity.ERROR
        for f in findings
    )

    report = validate_environment(
        env_dir, ValidationProfile.BUILD_INPUT, layout=layout
    )
    assert any(f.code == "spack_assets.bootstrap_missing" for f in report.errors())

    ops = SpackOps(
        EnvConfig(spack=SpackConfig(version="1.1.1", env_name="test-env")),
        _NoopRunner(),  # type: ignore[arg-type]
    )
    with pytest.raises(RuntimeError, match="clingo\\.json"):
        ops._verify_bootstrap(bootstrap)
