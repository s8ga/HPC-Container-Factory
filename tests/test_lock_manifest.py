"""Lock input manifest: provenance sidecar + stale-input gate (audit 2.2).

spack.lock stores no environment hash, so drift between the lock and the
spack.yaml / custom_repos pins that produced it was invisible to every
preflight. These tests pin the new doctrine: assets records inputs at
concretize time, validate compares and fails closed.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml

from hpc_cf.assets import run_mirror
from hpc_cf.environment import parse_environment_spec
from hpc_cf.execution import ProjectLayout
from hpc_cf.lock_manifest import (
    LOCK_MANIFEST_FILENAME,
    compare_lock_inputs,
    read_lock_manifest,
    write_lock_manifest,
)
from hpc_cf.validation import (
    ValidationProfile,
    validate_environment,
)

PIN = "d0ee3f460a2543c05c693317c767652abf964db7"
OTHER_PIN = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"


def _write_env(tmp_path: Path) -> Path:
    env_root = tmp_path / "spack-envs" / "demo"
    conf = env_root / "spack-env-file"
    conf.mkdir(parents=True)
    (conf / "spack.yaml").write_text("spack:\n  specs: [pkgconf]\n", encoding="utf-8")
    (conf / "env.yaml").write_text(
        "schema_version: 1\nmethod: spack\n"
        "spack:\n"
        "  version: '1.1.1'\n"
        "  env_name: demo-env\n",
        encoding="utf-8",
    )
    return env_root


def _spec(spack_extra: str = ""):
    extra = yaml.safe_load(spack_extra) or {} if spack_extra else {}
    return parse_environment_spec(
        {
            "schema_version": 1,
            "method": "spack",
            "spack": {
                "version": "1.1.1",
                "env_name": "demo-env",
                **extra,
            },
        }
    )


def _with_lock(env_root: Path) -> Path:
    lock = env_root / "spack-env-file" / "spack.lock"
    lock.write_text('{"_meta": {"lockfile-version": 6}}', encoding="utf-8")
    return lock


# --- sidecar mechanics -------------------------------------------------------


def test_write_and_read_roundtrip(tmp_path: Path) -> None:
    conf = tmp_path / "conf"
    conf.mkdir()
    (conf / "spack.yaml").write_text("spack: {}\n", encoding="utf-8")
    spec = _spec(
        "  custom_repos:\n"
        "    - url: https://example.com/r.git\n"
        f"      commit: {PIN}\n"
        "      sparse_path: spack_repo/app\n"
        "      namespace: app\n"
    )
    path = write_lock_manifest(spec, conf)
    assert path.name == LOCK_MANIFEST_FILENAME
    manifest = read_lock_manifest(conf)
    assert manifest is not None
    assert manifest["custom_repos_pins"] == {"app": PIN}
    assert manifest["spack_version"] == "1.1.1"
    assert len(manifest["spack_yaml_sha256"]) == 64


def test_read_missing_manifest_is_none(tmp_path: Path) -> None:
    assert read_lock_manifest(tmp_path) is None


def test_read_malformed_manifest_raises(tmp_path: Path) -> None:
    (tmp_path / LOCK_MANIFEST_FILENAME).write_text("{nope", encoding="utf-8")
    with pytest.raises(ValueError, match="malformed"):
        read_lock_manifest(tmp_path)


# --- compare ------------------------------------------------------------------


def test_compare_clean_when_inputs_unchanged(tmp_path: Path) -> None:
    env_root = _write_env(tmp_path)
    conf = env_root / "spack-env-file"
    repo_yaml = (
        "  custom_repos:\n"
        "    - url: https://example.com/r.git\n"
        f"      commit: {PIN}\n"
        "      sparse_path: spack_repo/app\n"
        "      namespace: app\n"
    )
    spec = _spec(repo_yaml)
    write_lock_manifest(spec, conf)
    manifest = read_lock_manifest(conf)
    assert compare_lock_inputs(manifest, spec, conf) == []


def test_compare_detects_spack_yaml_drift(tmp_path: Path) -> None:
    env_root = _write_env(tmp_path)
    conf = env_root / "spack-env-file"
    spec = _spec()
    write_lock_manifest(spec, conf)
    manifest = read_lock_manifest(conf)
    (conf / "spack.yaml").write_text("spack:\n  specs: [zlib]\n", encoding="utf-8")
    drift = compare_lock_inputs(manifest, spec, conf)
    assert any("spack.yaml changed" in line for line in drift)


def test_compare_detects_pin_drift(tmp_path: Path) -> None:
    env_root = _write_env(tmp_path)
    conf = env_root / "spack-env-file"
    repo_yaml = (
        "  custom_repos:\n"
        "    - url: https://example.com/r.git\n"
        f"      commit: {PIN}\n"
        "      sparse_path: spack_repo/app\n"
        "      namespace: app\n"
    )
    spec = _spec(repo_yaml)
    write_lock_manifest(spec, conf)
    manifest = read_lock_manifest(conf)
    bumped = _spec(repo_yaml.replace(PIN, OTHER_PIN))
    drift = compare_lock_inputs(manifest, bumped, conf)
    assert any("'app'" in line and PIN in line and OTHER_PIN in line for line in drift)


def test_compare_detects_unpin(tmp_path: Path) -> None:
    env_root = _write_env(tmp_path)
    conf = env_root / "spack-env-file"
    repo_yaml = (
        "  custom_repos:\n"
        "    - url: https://example.com/r.git\n"
        f"      commit: {PIN}\n"
        "      sparse_path: spack_repo/app\n"
        "      namespace: app\n"
    )
    spec = _spec(repo_yaml)
    write_lock_manifest(spec, conf)
    manifest = read_lock_manifest(conf)
    unpinned = _spec(
        "  custom_repos:\n"
        "    - url: https://example.com/r.git\n"
        "      branch: main\n"
        "      sparse_path: spack_repo/app\n"
        "      namespace: app\n"
    )
    drift = compare_lock_inputs(manifest, unpinned, conf)
    assert any("pin changed" in line for line in drift)


# --- validate wiring ----------------------------------------------------------


def test_validate_warns_when_manifest_missing(tmp_path: Path) -> None:
    env_root = _write_env(tmp_path)
    _with_lock(env_root)
    report = validate_environment(env_root, ValidationProfile.BUILD_INPUT)
    finding = next(
        (f for f in report.findings if f.code == "lock.provenance_unknown"), None
    )
    assert finding is not None
    assert finding.severity.value == "warning"
    assert report.ok  # warning must not fail the profile


def test_validate_errors_on_stale_inputs(tmp_path: Path) -> None:
    env_root = _write_env(tmp_path)
    conf = env_root / "spack-env-file"
    repo_yaml = (
        "  custom_repos:\n"
        "    - url: https://example.com/r.git\n"
        f"      commit: {PIN}\n"
        "      sparse_path: spack_repo/app\n"
        "      namespace: app\n"
    )
    spec = _spec(repo_yaml)
    write_lock_manifest(spec, conf)
    _with_lock(env_root)
    # Rewrite env.yaml with the bumped pin (spec-level drift like pin set).
    (conf / "env.yaml").write_text(
        "schema_version: 1\nmethod: spack\n"
        "spack:\n"
        "  version: '1.1.1'\n"
        "  env_name: demo-env\n"
        "  custom_repos:\n"
        "    - url: https://example.com/r.git\n"
        f"      commit: {OTHER_PIN}\n"
        "      sparse_path: spack_repo/app\n"
        "      namespace: app\n",
        encoding="utf-8",
    )
    report = validate_environment(env_root, ValidationProfile.BUILD_INPUT)
    stale = [f for f in report.findings if f.code == "lock.stale_input"]
    assert stale and stale[0].severity.value == "error"
    assert not report.ok

    softened = validate_environment(
        env_root, ValidationProfile.BUILD_INPUT, allow_reconcretize=True
    )
    stale_soft = [f for f in softened.findings if f.code == "lock.stale_input"]
    assert stale_soft and stale_soft[0].severity.value == "warning"
    assert softened.ok


def test_validate_errors_on_malformed_manifest(tmp_path: Path) -> None:
    env_root = _write_env(tmp_path)
    conf = env_root / "spack-env-file"
    _with_lock(env_root)
    (conf / LOCK_MANIFEST_FILENAME).write_text("{nope", encoding="utf-8")
    report = validate_environment(env_root, ValidationProfile.BUILD_INPUT)
    assert any(f.code == "lock.manifest_invalid" for f in report.findings)


# --- assets wiring -------------------------------------------------------------


def _mirror_layout(tmp_path: Path) -> tuple[ProjectLayout, Path]:
    layout = ProjectLayout(project_root=tmp_path)
    env_host = tmp_path / "spack-envs" / "demo" / "spack-env-file"
    env_host.mkdir(parents=True, exist_ok=True)
    (env_host / "spack.yaml").write_text("spack: {}\n", encoding="utf-8")
    (env_host / "env.yaml").write_text(
        "schema_version: 1\nmethod: spack\n"
        "spack:\n"
        "  version: '1.1.1'\n"
        "  env_name: demo-env\n",
        encoding="utf-8",
    )
    return layout, env_host


def test_run_mirror_concretize_writes_lock_manifest(tmp_path: Path) -> None:
    layout, env_host = _mirror_layout(tmp_path)
    stats = {"present": 0, "added": 1, "failed": 0}
    ops = MagicMock()
    ops.env.spack.version = "1.1.1"
    ops.resolved_repo_pins = {}

    def _all(*_a, **_k):
        (env_host / "spack.lock").write_text('{"hash": "x"}', encoding="utf-8")
        return stats

    ops.run_all_pipeline.side_effect = _all

    with (
        patch("hpc_cf.assets._make_spack_ops", return_value=(ops.env, ops)),
        patch(
            "hpc_cf.assets.resolve_env_paths",
            return_value=(env_host, Path("/work/spack-envs/demo/spack-env-file")),
        ),
    ):
        run_mirror(MagicMock(), "demo", layout=layout, allow_concretize=True)

    manifest_path = env_host / LOCK_MANIFEST_FILENAME
    assert manifest_path.is_file()
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert payload["schema_version"] == 1


def test_run_mirror_existing_lock_does_not_rewrite_manifest(
    tmp_path: Path,
) -> None:
    layout, env_host = _mirror_layout(tmp_path)
    (env_host / "spack.lock").write_text('{"hash": "x"}', encoding="utf-8")
    ops = MagicMock()
    ops.env.spack.version = "1.1.1"
    ops.resolved_repo_pins = {}
    ops.run_mirror_pipeline.return_value = {"present": 1, "added": 0, "failed": 0}

    with (
        patch("hpc_cf.assets._make_spack_ops", return_value=(ops.env, ops)),
        patch(
            "hpc_cf.assets.resolve_env_paths",
            return_value=(env_host, Path("/work/spack-envs/demo/spack-env-file")),
        ),
    ):
        run_mirror(MagicMock(), "demo", layout=layout)

    # A pre-existing lock must not be blessed with current-input hashes:
    # its provenance stays unknown until the next real concretize.
    assert not (env_host / LOCK_MANIFEST_FILENAME).exists()
