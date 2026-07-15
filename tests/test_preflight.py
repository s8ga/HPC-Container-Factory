"""Preflight correctness: sentinels, mirror log isolation, validate gates, CLI."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from hpc_cf.assets import _verify_host_side, find_bootstrap_dir
from hpc_cf.env import run_static_checks, validate_spack_assets
from hpc_cf.spack_ops import (
    MIRROR_CREATE_LOG,
    MIRROR_VERIFY_LOG,
    EnvConfig,
    SpackConfig,
    SpackOps,
)


def _ops(ctr=None) -> SpackOps:
    env = EnvConfig(spack=SpackConfig(version="1.1.1", env_name="cp2k-env"))
    return SpackOps(env, ctr or MagicMock())


# ── Broken-symlink sentinel ─────────────────────────────────────────────


def test_verify_host_side_treats_minus_one_as_check_failure(
    tmp_path: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    mirror = tmp_path / "spack-mirror"
    mirror.mkdir()
    with (
        patch("hpc_cf.container._count_broken_symlinks", return_value=-1),
        patch("hpc_cf.assets.find_bootstrap_dir", return_value=None),
        caplog.at_level("WARNING"),
    ):
        _verify_host_side(mirror_dir_host=mirror)

    assert "Broken symlinks found" not in caplog.text
    assert "check failed" in caplog.text.lower() or "could not" in caplog.text.lower()


def test_verify_host_side_reports_positive_broken_count(
    tmp_path: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    mirror = tmp_path / "spack-mirror"
    mirror.mkdir()
    with (
        patch("hpc_cf.container._count_broken_symlinks", return_value=3),
        patch("hpc_cf.assets.find_bootstrap_dir", return_value=None),
        caplog.at_level("ERROR"),
    ):
        _verify_host_side(mirror_dir_host=mirror)

    assert "Broken symlinks found" in caplog.text


# ── Mirror log isolation ────────────────────────────────────────────────


def test_mirror_create_script_truncates_its_own_log() -> None:
    script = _ops()._build_mirror_create_script("/work/mirror")
    assert f": > {MIRROR_CREATE_LOG}" in script
    assert MIRROR_CREATE_LOG in script
    assert MIRROR_VERIFY_LOG not in script


def test_mirror_verify_script_truncates_its_own_log() -> None:
    script = _ops()._build_mirror_verify_script("/work/mirror")
    assert f": > {MIRROR_VERIFY_LOG}" in script
    assert MIRROR_VERIFY_LOG in script


def test_parse_mirror_stats_reads_only_requested_log() -> None:
    """Must not cat both logs — stale verify output must not pollute create."""
    captured: list[str] = []

    class FakeCtr:
        def exec(self, script: str, *, capture: bool = False):
            captured.append(script)
            return SimpleNamespace(stdout="==> 1 already present\n==> 0 added\n==> 0 failed\n")

    ops = _ops(FakeCtr())
    stats = ops._parse_mirror_stats(MIRROR_CREATE_LOG)

    assert stats["failed"] == 0
    assert len(captured) == 1
    assert MIRROR_CREATE_LOG in captured[0]
    assert MIRROR_VERIFY_LOG not in captured[0]


def test_mirror_create_parses_create_log_only() -> None:
    class FakeCtr:
        def __init__(self) -> None:
            self.scripts: list[str] = []

        def exec(self, script: str, *, capture: bool = False):
            self.scripts.append(script)
            if capture:
                return SimpleNamespace(
                    stdout="==> 0 already present\n==> 2 added\n==> 0 failed\n",
                )
            return SimpleNamespace(stdout="")

    ctr = FakeCtr()
    _ops(ctr).mirror_create("/work/mirror")

    # Last exec is the stats read; must target create log only.
    assert MIRROR_CREATE_LOG in ctr.scripts[-1]
    assert MIRROR_VERIFY_LOG not in ctr.scripts[-1]


# ── validate_spack_assets method gate ───────────────────────────────────


def test_validate_spack_assets_skips_no_spack(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assets = tmp_path / "assets"
    assets.mkdir()
    monkeypatch.setattr("hpc_cf.config.ASSETS_DIR", assets)
    # Missing tarball would raise if gated incorrectly.
    validate_spack_assets({"method": "no_spack", "spack": {"version": "1.1.1"}})


def test_validate_spack_assets_requires_tarball_for_spack(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    assets = tmp_path / "assets"
    assets.mkdir()
    monkeypatch.setattr("hpc_cf.config.ASSETS_DIR", assets)
    with pytest.raises(FileNotFoundError, match="Spack tarball"):
        validate_spack_assets({"method": "spack", "spack": {"version": "1.1.1"}})


def test_cli_dockerfile_requires_explicit_app_version(
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    from hpc_cf.cli import run_new_cli

    with caplog.at_level("ERROR"):
        rc = run_new_cli(["dockerfile"])

    assert rc == 1
    assert "Specify --app-version" in caplog.text
    listed = capsys.readouterr().out
    assert "abacus_" in listed or "cp2k_" in listed


def test_cli_accepts_env_alias_for_app_version() -> None:
    from hpc_cf.cli import build_parser

    args = build_parser().parse_args(
        ["dockerfile", "--env", "cp2k_opensource-2026.1-force-avx512"]
    )
    assert args.app_version == "cp2k_opensource-2026.1-force-avx512"


def test_find_bootstrap_dir_prefers_version_match(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    assets = tmp_path / "assets"
    assets.mkdir()
    (assets / "bootstrap-1.0.0").mkdir()
    preferred = assets / "bootstrap-1.2.0"
    preferred.mkdir()
    monkeypatch.setattr("hpc_cf.assets.PROJECT_ROOT", tmp_path)

    assert find_bootstrap_dir("1.2.0") == preferred
    # Without a version, first sorted match wins.
    assert find_bootstrap_dir() == assets / "bootstrap-1.0.0"


def test_run_static_checks_calls_all_validators(tmp_path: Path) -> None:
    env_dir = tmp_path / "env"
    env_dir.mkdir()
    (env_dir / "env.yaml").write_text("method: no_spack\n")
    (env_dir / "Dockerfile.j2").write_text("FROM x\n")

    with (
        patch("hpc_cf.env.validate_manual_packages") as v_mp,
        patch("hpc_cf.env.validate_spack_assets") as v_sa,
        patch("hpc_cf.env.validate_branch_consistency") as v_br,
        patch("hpc_cf.env.validate_spack_yaml") as v_sy,
    ):
        run_static_checks(env_dir, {"method": "no_spack"})

    v_mp.assert_called_once()
    v_sa.assert_called_once()
    v_br.assert_called_once_with(env_dir)
    v_sy.assert_called_once_with(env_dir)
