"""P0-A: Apptainer install pin/SHA256 + sif artifact path confinement."""

from __future__ import annotations

import hashlib
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from hpc_cf import config as config_mod
from hpc_cf import sif as sif_mod
from hpc_cf.execution import ProjectLayout


def test_apptainer_install_url_is_pinned_not_main() -> None:
    url = config_mod.APPTAINER_INSTALL_SCRIPT_URL
    assert "/main/" not in url
    assert config_mod.APPTAINER_INSTALL_SCRIPT_REF in url
    assert url.endswith("/tools/install-unprivileged.sh")
    assert len(config_mod.APPTAINER_INSTALL_SCRIPT_SHA256) == 64


def test_fetch_and_verify_rejects_sha256_mismatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    tools = tmp_path / "tools"
    tools.mkdir()
    dest = tools / "install-unprivileged.sh"
    monkeypatch.setattr(sif_mod, "TOOLS_DIR", tools)
    monkeypatch.setattr(sif_mod, "APPTAINER_INSTALL_SCRIPT", dest)
    monkeypatch.setattr(
        sif_mod,
        "APPTAINER_INSTALL_SCRIPT_SHA256",
        "0" * 64,
    )

    def fake_curl(cmd: list[str], **_kwargs: object) -> MagicMock:
        assert cmd[0] == "curl"
        assert sif_mod.APPTAINER_INSTALL_SCRIPT_URL in cmd
        assert "/main/" not in sif_mod.APPTAINER_INSTALL_SCRIPT_URL
        dest.write_bytes(b"tampered-install-script")
        return MagicMock(returncode=0)

    with patch.object(sif_mod.subprocess, "run", side_effect=fake_curl):
        with pytest.raises(RuntimeError, match="SHA256 mismatch"):
            sif_mod._fetch_and_verify_install_script()

    assert not dest.exists(), "tampered script must be removed fail-closed"


def test_fetch_and_verify_accepts_matching_sha256(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    tools = tmp_path / "tools"
    tools.mkdir()
    dest = tools / "install-unprivileged.sh"
    payload = b"#!/bin/bash\necho ok\n"
    expected = hashlib.sha256(payload).hexdigest()

    monkeypatch.setattr(sif_mod, "TOOLS_DIR", tools)
    monkeypatch.setattr(sif_mod, "APPTAINER_INSTALL_SCRIPT", dest)
    monkeypatch.setattr(sif_mod, "APPTAINER_INSTALL_SCRIPT_SHA256", expected)

    def fake_curl(cmd: list[str], **_kwargs: object) -> MagicMock:
        dest.write_bytes(payload)
        return MagicMock(returncode=0)

    with patch.object(sif_mod.subprocess, "run", side_effect=fake_curl):
        path = sif_mod._fetch_and_verify_install_script()

    assert path == dest
    assert dest.is_file()
    assert dest.stat().st_mode & 0o111  # executable bit set


def test_ensure_apptainer_verifies_before_bash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    tools = tmp_path / "tools"
    prefix = tools / "apptainer"
    tools.mkdir()
    dest = tools / "install-unprivileged.sh"
    payload = b"#!/bin/bash\n"
    expected = hashlib.sha256(payload).hexdigest()

    monkeypatch.setattr(sif_mod, "TOOLS_DIR", tools)
    monkeypatch.setattr(sif_mod, "APPTAINER_INSTALL_SCRIPT", dest)
    monkeypatch.setattr(sif_mod, "APPTAINER_LOCAL_PREFIX", prefix)
    monkeypatch.setattr(sif_mod, "APPTAINER_INSTALL_SCRIPT_SHA256", expected)
    monkeypatch.setattr(sif_mod, "check_command_exists", lambda _c: True)
    # No system apptainer on PATH during the install probe.
    monkeypatch.setattr(sif_mod.shutil, "which", lambda _cmd: None)

    calls: list[list[str]] = []

    def fake_run(cmd: list[str], **_kwargs: object) -> MagicMock:
        calls.append(list(cmd))
        if cmd[0] == "curl":
            dest.write_bytes(payload)
        elif cmd[0] == "bash":
            # Simulate successful local install under the pinned prefix.
            bin_dir = prefix / "bin"
            bin_dir.mkdir(parents=True)
            apptainer = bin_dir / "apptainer"
            apptainer.write_text("#!/bin/sh\n", encoding="utf-8")
            apptainer.chmod(0o755)
        return MagicMock(returncode=0)

    with patch.object(sif_mod.subprocess, "run", side_effect=fake_run):
        result = sif_mod.ensure_apptainer(auto_confirm=True)

    assert result == str(prefix / "bin" / "apptainer")
    assert any(c[0] == "curl" for c in calls)
    bash_calls = [c for c in calls if c[0] == "bash"]
    assert len(bash_calls) == 1
    assert bash_calls[0][1] == str(dest)


def test_safe_filename_component_strips_path_separators() -> None:
    assert sif_mod._safe_filename_component("../../evil") == ".._.._evil"
    assert sif_mod._safe_filename_component("good-tag.1") == "good-tag.1"
    with pytest.raises(ValueError, match="unsafe filename"):
        sif_mod._safe_filename_component("...")
    with pytest.raises(ValueError, match="unsafe filename"):
        sif_mod._safe_filename_component("..")


def test_build_sif_sanitizes_docker_tag_under_artifacts(
    tmp_path: Path,
) -> None:
    """Traversal-like docker_tag must not escape artifacts/."""
    layout = ProjectLayout(project_root=tmp_path)
    layout.artifacts_dir.mkdir()
    calls: list[tuple[list[str], Path | None]] = []
    evil_tag = "../../outside"

    def fake_run(
        cmd: list[str],
        *,
        cwd: Path | None = None,
        **_kwargs: object,
    ) -> None:
        calls.append((list(cmd), cwd))
        if cmd[0] == "podman" and "save" in cmd:
            out = Path(cmd[cmd.index("-o") + 1])
            assert out.is_relative_to(layout.artifacts_dir.resolve())
            out.write_bytes(b"oci-tar")
        if cmd[0] == "/usr/bin/apptainer":
            sif_out = Path(cmd[-2])
            sif_out.write_bytes(b"sif")

    with (
        patch.object(sif_mod, "ensure_apptainer", return_value="/usr/bin/apptainer"),
        patch.object(
            sif_mod, "check_command_exists", side_effect=lambda c: c == "podman"
        ),
        patch.object(sif_mod, "run_cmd", side_effect=fake_run),
        patch.object(sif_mod, "_find_def_template", return_value=None),
    ):
        sif_mod.build_sif(
            docker_image="demo",
            docker_tag=evil_tag,
            yes=True,
            layout=layout,
        )

    safe = sif_mod._safe_filename_component(evil_tag)
    tar_path = layout.artifacts_dir / f"demo_{safe}.tar"
    sif_path = layout.artifacts_dir / f"demo_{safe}.sif"
    assert tar_path.is_file()
    assert sif_path.is_file()
    assert not (tmp_path / "outside.tar").exists()
    assert calls[-1][1] == layout.artifacts_dir
    assert f"docker-archive://demo_{safe}.tar" in calls[-1][0]


def test_build_sif_rejects_output_outside_project_root(
    tmp_path: Path,
) -> None:
    layout = ProjectLayout(project_root=tmp_path)
    layout.artifacts_dir.mkdir()
    (layout.artifacts_dir / "demo_tag.tar").write_bytes(b"oci-tar")
    outside = tmp_path.parent / "escape.sif"

    with (
        patch.object(sif_mod, "ensure_apptainer", return_value="/usr/bin/apptainer"),
        patch.object(
            sif_mod, "check_command_exists", side_effect=lambda c: c == "podman"
        ),
        patch.object(sif_mod, "run_cmd"),
        patch.object(sif_mod, "_find_def_template", return_value=None),
        pytest.raises(ValueError, match="escapes allowed root"),
    ):
        sif_mod.build_sif(
            docker_image="demo",
            docker_tag="tag",
            output=outside,
            yes=True,
            layout=layout,
        )
