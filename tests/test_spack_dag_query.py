"""Unit tests for scripts/spack_dag_query.py."""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

import spack_dag_query  # noqa: E402


@dataclass
class _FakeSpec:
    name: str
    _hash: str
    installed: bool = False
    external: bool = False

    def dag_hash(self) -> str:
        return self._hash


def test_parse_excludes_splits_commas_and_repeat_flags() -> None:
    excluded = spack_dag_query.parse_excludes(["vasp, mkl", "  ", "intel-oneapi-mkl"])
    assert excluded == frozenset({"vasp", "mkl", "intel-oneapi-mkl"})


def test_select_hashes_filters_external_excluded_and_uninstalled() -> None:
    specs = [
        _FakeSpec("glibc", "h0", installed=True, external=True),
        _FakeSpec("vasp", "h1", installed=True, external=False),
        _FakeSpec("cp2k", "h2", installed=True, external=False),
        _FakeSpec("libint", "h3", installed=False, external=False),
    ]
    exclude = frozenset({"vasp"})
    assert spack_dag_query.select_hashes(
        specs, installed_only=True, exclude=exclude
    ) == ["/h2"]
    assert spack_dag_query.select_hashes(
        specs, installed_only=False, exclude=exclude
    ) == ["/h2", "/h3"]


def test_build_summary_complete_and_partial() -> None:
    complete_specs = [
        _FakeSpec("glibc", "h0", installed=True, external=True),
        _FakeSpec("cp2k", "h1", installed=True, external=False),
    ]
    summary = spack_dag_query.build_summary(complete_specs, install_rc=0)
    assert summary["installed_count"] == 1
    assert summary["planned_count"] == 1
    assert summary["checked_count"] == 1
    assert summary["complete"] is True
    assert summary["partial_install"] is False

    partial_specs = [
        *complete_specs,
        _FakeSpec("libint", "h2", installed=False, external=False),
    ]
    partial = spack_dag_query.build_summary(partial_specs, install_rc=1)
    assert partial["installed_count"] == 1
    assert partial["planned_count"] == 2
    assert partial["complete"] is False
    assert partial["partial_install"] is True
    assert partial["install_rc"] == 1


def test_resolve_container_ident_commit_version_and_fallback(tmp_path: Path) -> None:
    lock = tmp_path / "spack.lock"
    lock.write_text(
        json.dumps(
            {
                "concrete_specs": {
                    "abc": {
                        "name": "cp2k",
                        "version": "master",
                        "parameters": {"commit": "0123456789abcdef" * 2 + "01234567"},
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    assert (
        spack_dag_query.resolve_container_ident(
            lock, spec_name="cp2k", branch="master", fallback="cp2k:latest"
        )
        == "master@0123456789ab"
    )
    assert (
        spack_dag_query.resolve_container_ident(
            lock, spec_name="missing", branch="master", fallback="cp2k:latest"
        )
        == "cp2k:latest"
    )
