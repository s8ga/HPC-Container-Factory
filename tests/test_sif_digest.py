"""P1-A: OCI image Id gate before reusing SIF export tars."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from hpc_cf import sif as sif_mod
from hpc_cf.execution import ProjectLayout


def test_select_engine_with_image_prefers_engine_that_has_image() -> None:
    """Skip engines that are installed but do not hold the image."""

    def fake_exists(cmd: str) -> bool:
        return cmd in {"podman", "docker"}

    def fake_inspect(engine: str, image_ref: str, *, cwd: Path | None = None) -> str | None:
        assert image_ref == "demo:tag"
        if engine == "podman":
            return None
        if engine == "docker":
            return "sha256:docker-only"
        return None

    with (
        patch.object(sif_mod, "check_command_exists", side_effect=fake_exists),
        patch.object(sif_mod, "inspect_local_image_id", side_effect=fake_inspect),
    ):
        engine, image_id = sif_mod.select_engine_with_image("demo:tag")

    assert engine == "docker"
    assert image_id == "sha256:docker-only"


def test_select_engine_with_image_fails_when_neither_has_image() -> None:
    with (
        patch.object(
            sif_mod, "check_command_exists", side_effect=lambda c: c in {"podman", "docker"}
        ),
        patch.object(sif_mod, "inspect_local_image_id", return_value=None),
        pytest.raises(RuntimeError, match="not found locally"),
    ):
        sif_mod.select_engine_with_image("missing:latest")


def test_build_sif_reuses_tar_when_image_id_matches(tmp_path: Path) -> None:
    layout = ProjectLayout(project_root=tmp_path)
    layout.artifacts_dir.mkdir()
    image_id = "sha256:reuse-me"
    tar_path = layout.artifacts_dir / "demo_tag.tar"
    tar_path.write_bytes(b"oci-tar")
    (layout.artifacts_dir / "demo_tag.tar.id").write_text(
        f"{image_id}\n", encoding="utf-8"
    )
    output = tmp_path / "result.sif"
    calls: list[list[str]] = []

    def fake_run(cmd: list[str], **_kwargs: object) -> None:
        calls.append(list(cmd))
        if cmd[0] == "/usr/bin/apptainer":
            output.write_bytes(b"sif")

    with (
        patch.object(sif_mod, "ensure_apptainer", return_value="/usr/bin/apptainer"),
        patch.object(
            sif_mod,
            "select_engine_with_image",
            return_value=("podman", image_id),
        ),
        patch.object(sif_mod, "run_cmd", side_effect=fake_run),
        patch.object(sif_mod, "_find_def_template", return_value=None),
    ):
        sif_mod.build_sif(
            docker_image="demo",
            docker_tag="tag",
            output=output,
            yes=True,
            layout=layout,
        )

    assert all("save" not in c for c in calls)
    assert tar_path.read_bytes() == b"oci-tar"
    assert output.is_file()


def test_build_sif_reexports_when_image_id_mismatches(tmp_path: Path) -> None:
    layout = ProjectLayout(project_root=tmp_path)
    layout.artifacts_dir.mkdir()
    tar_path = layout.artifacts_dir / "demo_tag.tar"
    tar_path.write_bytes(b"stale-oci-tar")
    (layout.artifacts_dir / "demo_tag.tar.id").write_text(
        "sha256:old\n", encoding="utf-8"
    )
    current_id = "sha256:new"
    output = tmp_path / "result.sif"
    calls: list[list[str]] = []

    def fake_run(cmd: list[str], **_kwargs: object) -> None:
        calls.append(list(cmd))
        if cmd[0] == "podman" and "save" in cmd:
            out = Path(cmd[cmd.index("-o") + 1])
            out.write_bytes(b"fresh-oci-tar")
        if cmd[0] == "/usr/bin/apptainer":
            output.write_bytes(b"sif")

    with (
        patch.object(sif_mod, "ensure_apptainer", return_value="/usr/bin/apptainer"),
        patch.object(
            sif_mod,
            "select_engine_with_image",
            return_value=("podman", current_id),
        ),
        patch.object(sif_mod, "run_cmd", side_effect=fake_run),
        patch.object(sif_mod, "_find_def_template", return_value=None),
    ):
        sif_mod.build_sif(
            docker_image="demo",
            docker_tag="tag",
            output=output,
            yes=True,
            layout=layout,
        )

    save_calls = [c for c in calls if c and c[0] == "podman" and "save" in c]
    assert len(save_calls) == 1
    assert tar_path.read_bytes() == b"fresh-oci-tar"
    assert (layout.artifacts_dir / "demo_tag.tar.id").read_text(
        encoding="utf-8"
    ).strip() == current_id


def test_build_sif_reexports_when_sidecar_missing(tmp_path: Path) -> None:
    layout = ProjectLayout(project_root=tmp_path)
    layout.artifacts_dir.mkdir()
    tar_path = layout.artifacts_dir / "demo_tag.tar"
    tar_path.write_bytes(b"orphan-tar")
    current_id = "sha256:no-sidecar"
    output = tmp_path / "result.sif"
    calls: list[list[str]] = []

    def fake_run(cmd: list[str], **_kwargs: object) -> None:
        calls.append(list(cmd))
        if cmd[0] == "podman" and "save" in cmd:
            Path(cmd[cmd.index("-o") + 1]).write_bytes(b"fresh")
        if cmd[0] == "/usr/bin/apptainer":
            output.write_bytes(b"sif")

    with (
        patch.object(sif_mod, "ensure_apptainer", return_value="/usr/bin/apptainer"),
        patch.object(
            sif_mod,
            "select_engine_with_image",
            return_value=("podman", current_id),
        ),
        patch.object(sif_mod, "run_cmd", side_effect=fake_run),
        patch.object(sif_mod, "_find_def_template", return_value=None),
    ):
        sif_mod.build_sif(
            docker_image="demo",
            docker_tag="tag",
            output=output,
            yes=True,
            layout=layout,
        )

    assert any(c[0] == "podman" and "save" in c for c in calls)
    assert tar_path.read_bytes() == b"fresh"
    assert (layout.artifacts_dir / "demo_tag.tar.id").read_text(
        encoding="utf-8"
    ).strip() == current_id


def test_inspect_local_image_id_returns_none_on_failure() -> None:
    with patch.object(
        sif_mod.subprocess,
        "run",
        side_effect=sif_mod.subprocess.CalledProcessError(1, ["podman"]),
    ):
        assert sif_mod.inspect_local_image_id("podman", "missing:tag") is None
