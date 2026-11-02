"""Atomic sidecar/state writes — a crash can never tear the target file.

Round-2 audit 2.12d: lock-manifest, resolved-repos, mirror manifests,
prune reports, OCI-id sidecars, and the rendered def file were written
in place; every consumer parses them fail-closed, so an interrupt left a
corrupt file that aborted the NEXT run. All writes now go through
hpc_cf.atomic_io (unique temp + os.replace).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hpc_cf.atomic_io import atomic_write_json, atomic_write_text


def test_write_creates_and_overwrites(tmp_path: Path) -> None:
    target = tmp_path / "sidecar.json"
    atomic_write_json(target, {"a": 1})
    assert json.loads(target.read_text(encoding="utf-8")) == {"a": 1}
    atomic_write_json(target, {"b": 2})
    assert json.loads(target.read_text(encoding="utf-8")) == {"b": 2}


def test_failed_write_keeps_old_content_and_leaves_no_temp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "state.json"
    atomic_write_json(target, {"good": True})

    def broken_replace(src: str, dst: str) -> None:
        raise OSError("simulated crash before rename")

    monkeypatch.setattr("hpc_cf.atomic_io.os.replace", broken_replace)
    with pytest.raises(OSError):
        atomic_write_json(target, {"torn": True})

    # The old complete file survived; no temp siblings linger.
    assert json.loads(target.read_text(encoding="utf-8")) == {"good": True}
    leftovers = [p for p in tmp_path.iterdir() if p.name != "state.json"]
    assert leftovers == []


def test_failed_write_on_missing_target_leaves_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken_replace(src: str, dst: str) -> None:
        raise OSError("simulated crash before rename")

    monkeypatch.setattr("hpc_cf.atomic_io.os.replace", broken_replace)
    target = tmp_path / "fresh.json"
    with pytest.raises(OSError):
        atomic_write_json(target, {"torn": True})
    assert not target.exists()
    assert list(tmp_path.iterdir()) == []


def test_text_write_roundtrip(tmp_path: Path) -> None:
    target = tmp_path / "nested" / "def.txt"
    atomic_write_text(target, "Bootstrap: docker-archive\n")
    assert target.read_text(encoding="utf-8") == "Bootstrap: docker-archive\n"


def test_fetch_specs_script_stages_to_tmp() -> None:
    """The container-side fetch-specs redirect must stage to .tmp + mv."""
    from tests.test_mirror_prune_wiring import _NoopContainer, _env_config

    from hpc_cf.spack_ops import SpackOps

    ops = SpackOps(_env_config(), _NoopContainer())
    script = ops._build_fetch_specs_script("/opt/x/fetch-specs.json")
    assert "> /opt/x/fetch-specs.json.tmp" in script
    assert "mv -f /opt/x/fetch-specs.json.tmp /opt/x/fetch-specs.json" in script
    # Direct redirect into the final path is the torn-file hazard.
    assert "> /opt/x/fetch-specs.json\n" not in script


def test_site_writers_use_atomic_helpers() -> None:
    """No writer may reintroduce in-place text writes of sidecar/state
    files (the six audited call sites stay migrated)."""
    import re

    from hpc_cf import assets, execution, lock_manifest, sif

    for module in (assets, execution, lock_manifest, sif):
        src = Path(module.__file__).read_text(encoding="utf-8")
        offenders = re.findall(
            r"^\s*(?!#)(.*\.(?:write_text)\(.*)$", src, re.MULTILINE
        )
        assert offenders == [], (
            f"{module.__name__} writes in place: {offenders} — use "
            "hpc_cf.atomic_io"
        )
