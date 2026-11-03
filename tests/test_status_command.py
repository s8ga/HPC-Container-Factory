"""hpc_cf status: read-only aggregate over buildcache/mirror/env state."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from hpc_cf.execution import ProjectLayout
from hpc_cf.status import run_status_command


def _make_env(root: Path, name: str, *, lock: bool, manifest: dict | None = None) -> None:
    conf = root / "spack-envs" / name / "spack-env-file"
    conf.mkdir(parents=True)
    (conf / "env.yaml").write_text(
        "schema_version: 1\nmethod: spack\n"
        f"spack:\n  version: '1.2.0'\n  env_name: {name}\n",
        encoding="utf-8",
    )
    if lock:
        (conf / "spack.lock").write_text("{}\n", encoding="utf-8")
    if manifest is not None:
        (conf / "lock-manifest.json").write_text(
            json.dumps(manifest), encoding="utf-8"
        )


def test_status_sections_and_env_states(tmp_path, capsys) -> None:
    layout = ProjectLayout(project_root=tmp_path)
    (layout.buildcache_state_dir).mkdir(parents=True)
    (layout.buildcache_health_path).write_text(
        json.dumps({"state": "healthy", "timestamp": "2026-11-02T00:00:00Z"}),
        encoding="utf-8",
    )
    (layout.buildcache_coverage_dir).mkdir(parents=True)
    (layout.buildcache_coverage_dir / "aa.json").write_text("{}")
    (layout.spack_mirror_dir / ".hpc_cf").mkdir(parents=True)

    _make_env(tmp_path, "env-unknown", lock=True)  # lock, no manifest
    _make_env(tmp_path, "env-no-lock", lock=False)

    assert run_status_command(layout=layout) == 0
    out = capsys.readouterr().out
    assert "health: healthy" in out
    assert "coverage records: 1" in out
    assert "env-no-lock: no lock" in out
    # lock without a manifest → provenance unknown (backfill state)
    assert "env-unknown: lock, provenance unknown" in out


def test_status_unknown_env_fails(tmp_path, capsys) -> None:
    assert (
        run_status_command(env="nope", layout=ProjectLayout(project_root=tmp_path))
        == 1
    )


def test_status_floating_repo_with_resolved_tip_is_fresh(tmp_path, capsys) -> None:
    """status compares the view builds use (resolved-repos.yaml applied): a
    floating repo whose resolved tip the manifest recorded is fresh."""
    sha = "d0ee3f460a2543c05c693317c767652abf964db7"
    conf = tmp_path / "spack-envs" / "floaty" / "spack-env-file"
    conf.mkdir(parents=True)
    (conf / "env.yaml").write_text(
        "schema_version: 1\nmethod: spack\n"
        "spack:\n  version: '1.2.0'\n  env_name: floaty\n"
        "  custom_repos:\n"
        "    - url: https://example.com/r.git\n"
        "      branch: main\n"
        "      sparse_path: spack_repo/app\n"
        "      namespace: app\n",
        encoding="utf-8",
    )
    (conf / "spack.yaml").write_text("spack: {}\n", encoding="utf-8")
    (conf / "spack.lock").write_text("{}\n", encoding="utf-8")
    (conf / "resolved-repos.yaml").write_text(
        f"schema_version: 1\nrepos:\n  app:\n    commit: {sha}\n", encoding="utf-8"
    )
    (conf / "lock-manifest.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "spack_yaml_sha256": hashlib.sha256(b"spack: {}\n").hexdigest(),
                "custom_repos_pins": {"app": sha},
                "spack_version": "1.2.0",
            }
        ),
        encoding="utf-8",
    )

    assert run_status_command(layout=ProjectLayout(project_root=tmp_path)) == 0
    assert "floaty: fresh" in capsys.readouterr().out


def test_status_reads_nothing_when_absent(tmp_path, capsys) -> None:
    assert run_status_command(layout=ProjectLayout(project_root=tmp_path)) == 0
    out = capsys.readouterr().out
    assert "no state file" in out
    assert "absent" in out
