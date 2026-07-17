"""Regression guards for previously confirmed hpc_cf framework defects.

These tests document post-fix behavior and must stay green. They are
guardrails against regressions — not an “expected red” suite.
All fixtures are synthetic — no shipped application env is required.
"""

from __future__ import annotations

import json
import logging
import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from hpc_cf.execution import ProjectLayout
from hpc_cf.template import build_context, render_template, select_template


# ── Helpers ──────────────────────────────────────────────────────────────


def _write_spack_env(
    root: Path,
    name: str = "synth-env",
    *,
    env_yaml: str,
    spack_yaml: str | None = "spack:\n  specs: [pkgconf]\n",
    dockerfile: str | None = None,
    nested: bool = True,
) -> Path:
    """Create ``root/spack-envs/<name>/[spack-env-file/]`` and return env root."""
    env_root = root / "spack-envs" / name
    conf = env_root / "spack-env-file" if nested else env_root
    conf.mkdir(parents=True, exist_ok=True)
    env_root.mkdir(parents=True, exist_ok=True)
    (conf / "env.yaml").write_text(env_yaml, encoding="utf-8")
    if spack_yaml is not None:
        (conf / "spack.yaml").write_text(spack_yaml, encoding="utf-8")
    if dockerfile is not None:
        (env_root / "Dockerfile.j2").write_text(dockerfile, encoding="utf-8")
    return env_root


# ── 1. Mirror scope decoupled from repo scope ────────────────────────────


def test_mirror_scope_decoupled_from_repo_scope_when_use_mirror(tmp_path: Path) -> None:
    """``use_mirror=True`` must register the mirror under an independent site scope.

    Custom image ``repo_scope: env`` must not leak into ``spack mirror add --scope``.
    """
    env_yaml = (
        "schema_version: 1\n"
        "method: spack\n"
        "spack:\n"
        "  version: '1.1.1'\n"
        "  env_name: test-env\n"
        "  image:\n"
        "    repo_scope: env\n"
        "    update_builtin: true\n"
    )
    env_root = _write_spack_env(
        tmp_path,
        "synth-mirror-scope",
        env_yaml=env_yaml,
        dockerfile="{% include 'partials/spack_mirror.j2' %}\n",
        nested=False,
    )
    tpl = env_root / "Dockerfile.j2"
    ctx = build_context(
        use_mirror=True,
        build_only=False,
        app_version="synth-mirror-scope",
        template_path=tpl,
    )
    rendered = render_template(tpl, ctx)

    assert "local-mirror file:///opt/spack-mirror" in rendered
    # Repo registration may use env scope; mirror must stay site (or dedicated mirror scope).
    assert "spack mirror add --scope site " in rendered
    assert "spack mirror add --scope env:" not in rendered


# ── 2. find non-zero exit → probe failure (-1) ───────────────────────────


def test_count_broken_symlinks_maps_find_nonzero_returncode_to_minus_one(
    tmp_path: Path,
) -> None:
    """``find`` exit status != 0 must be treated as probe failure, not ``0 broken``."""
    from hpc_cf.container import _count_broken_symlinks

    failed = subprocess.CompletedProcess(
        args=["find", "-L", str(tmp_path), "-type", "l"],
        returncode=1,
        stdout="",
        stderr="find: ‘/tmp/x’: Permission denied\n",
    )
    with patch("subprocess.run", return_value=failed) as mock_run:
        assert _count_broken_symlinks(tmp_path) == -1
    mock_run.assert_called_once()


# ── 3. Host verify failure must not leave a success manifest ─────────────


def test_run_verify_host_failure_does_not_leave_success_manifest(
    tmp_path: Path,
) -> None:
    """If host-side verify fails, no success-looking manifest may remain on disk."""
    from hpc_cf.assets import run_verify

    layout = ProjectLayout(project_root=tmp_path)
    env_host = (
        tmp_path / "spack-envs" / "demo" / "spack-env-file"
    )
    env_host.mkdir(parents=True)
    (env_host / "spack.yaml").write_text("spack: {}\n", encoding="utf-8")
    (env_host / "spack.lock").write_text('{"hash":"x"}', encoding="utf-8")
    (env_host / "env.yaml").write_text(
        "schema_version: 1\n"
        "spack:\n  version: '1.1.1'\n  env_name: demo-env\n",
        encoding="utf-8",
    )

    ops = MagicMock()
    ops.env.spack.version = "1.1.1"
    ops.run_verify_pipeline.return_value = {
        "present": 1,
        "added": 0,
        "failed": 0,
    }

    with (
        patch("hpc_cf.assets._make_spack_ops", return_value=(ops.env, ops)),
        patch(
            "hpc_cf.assets.resolve_env_paths",
            return_value=(env_host, Path("/work/spack-envs/demo/spack-env-file")),
        ),
        patch(
            "hpc_cf.assets._verify_host_side",
            side_effect=RuntimeError("Broken symlinks found in mirror"),
        ),
        pytest.raises(RuntimeError, match="Broken symlink"),
    ):
        run_verify(MagicMock(), "demo", layout=layout)

    runs_dir = layout.spack_mirror_dir / ".hpc_cf" / "runs"
    manifests = list(runs_dir.glob("*/manifest.json")) if runs_dir.exists() else []
    for path in manifests:
        payload = json.loads(path.read_text(encoding="utf-8"))
        # Success manifests (missing status, or status == success) are forbidden.
        assert payload.get("status") == "failed", (
            f"host verify failed but found non-failed manifest: {payload}"
        )


# ── 4. EnvironmentSpec fail-closed ───────────────────────────────────────


def test_yaml_non_mapping_root_fails_closed(tmp_path: Path) -> None:
    """YAML document root must be a mapping; ``[]`` must not coerce to defaults."""
    from hpc_cf.environment import load_environment_spec

    env_dir = tmp_path / "bad-root"
    env_dir.mkdir()
    (env_dir / "env.yaml").write_text("[]\n", encoding="utf-8")

    with pytest.raises(ValueError, match="mapping"):
        load_environment_spec(env_dir)


def test_unknown_top_level_field_fails_closed() -> None:
    """Misspelled / unknown keys (e.g. ``methd``) must not be silently ignored."""
    from hpc_cf.environment import parse_environment_spec

    with pytest.raises(ValueError, match=r"methd|unknown|Unexpected"):
        parse_environment_spec(
            {
                "schema_version": 1,
                "methd": "spack",
                "spack": {"version": "1.1.1", "env_name": "e"},
            }
        )


def test_string_coerced_to_char_list_fails_closed() -> None:
    """``system_pkgs: git`` must not become ``['g','i','t']`` via ``list(str)``."""
    from hpc_cf.environment import parse_environment_spec

    with pytest.raises(ValueError, match=r"system_pkgs|list"):
        parse_environment_spec(
            {
                "schema_version": 1,
                "spack": {"version": "1.1.1", "env_name": "e"},
                "mirror_builder": {"system_pkgs": "git"},
            }
        )


def test_schema_version_bool_and_float_fail_closed() -> None:
    """``schema_version`` must be a strict JSON/YAML integer, not bool/float."""
    from hpc_cf.environment import parse_environment_spec

    for bad in (True, 1.0, "1"):
        with pytest.raises(ValueError, match="schema_version"):
            parse_environment_spec(
                {
                    "schema_version": bad,
                    "spack": {"version": "1.1.1", "env_name": "e"},
                }
            )


# ── 5. Nonexistent --template must not validate successfully ─────────────


def test_validate_nonexistent_template_does_not_succeed(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``validate --template`` pointing at a missing file must fail (rc != 0)."""
    from hpc_cf.cli import run_new_cli

    # Valid env.yaml beside a missing Dockerfile — current CLI skips existence check.
    (tmp_path / "env.yaml").write_text(
        "schema_version: 1\n"
        "method: no_spack\n"
        "spack:\n  version: '1.1.1'\n  env_name: e\n",
        encoding="utf-8",
    )
    missing = tmp_path / "Dockerfile.j2"
    assert not missing.exists()

    rc = run_new_cli(
        [
            "validate",
            "--template",
            str(missing),
            "--profile",
            "config",
            "--format",
            "json",
        ]
    )
    assert rc != 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False


# ── 6. assets status vs download-mirror profile split ────────────────────


def test_assets_status_and_download_mirror_use_different_profiles(
    tmp_path: Path,
) -> None:
    """``status`` uses config-level checks; ``download-mirror`` needs assets inputs."""
    from hpc_cf.validation import ValidationProfile
    from hpc_cf.workflows import AssetsRequest, AssetsService

    layout = ProjectLayout(project_root=tmp_path)
    env_host = tmp_path / "spack-envs" / "demo" / "spack-env-file"
    env_host.mkdir(parents=True)
    (env_host / "env.yaml").write_text(
        "schema_version: 1\n"
        "method: spack\n"
        "spack:\n  version: '1.1.1'\n  env_name: demo-env\n",
        encoding="utf-8",
    )
    (env_host / "spack.yaml").write_text(
        "spack:\n  specs: [pkgconf]\n",
        encoding="utf-8",
    )
    (env_host / "spack.lock").write_text('{"concrete": true}\n', encoding="utf-8")
    layout.assets_dir.mkdir(parents=True)

    seen: list[ValidationProfile] = []

    def _capture(*args: Any, **kwargs: Any) -> None:
        # run_static_checks(..., profile=...) vs assert_valid(env_dir, profile, ...)
        profile = kwargs.get("profile")
        if profile is None and len(args) >= 2:
            maybe = args[1]
            if isinstance(maybe, ValidationProfile) or (
                isinstance(maybe, str)
                and maybe
                in {p.value for p in ValidationProfile} | {"template", "config/template"}
            ):
                profile = maybe
        if profile is not None:
            seen.append(ValidationProfile.parse(profile))

    paths = (env_host, Path("/work/spack-envs/demo/spack-env-file"))
    with (
        patch("hpc_cf.spack_ops.resolve_env_paths", return_value=paths),
        patch("hpc_cf.assets.resolve_env_paths", return_value=paths),
        # Imported inside AssetsService.run / run_assets — patch the source modules.
        patch("hpc_cf.env.run_static_checks", side_effect=_capture),
        patch("hpc_cf.validation.assert_valid", side_effect=_capture),
        patch("hpc_cf.assets._make_container", return_value=MagicMock()),
        patch("hpc_cf.assets.run_status"),
        patch("hpc_cf.assets.ensure_image"),
        patch("hpc_cf.assets.run_mirror"),
    ):
        svc = AssetsService(layout=layout)
        svc.run(AssetsRequest(env="demo", status=True))
        status_profiles = list(seen)
        seen.clear()
        svc.run(AssetsRequest(env="demo", download_mirror=True))
        download_profiles = list(seen)

    assert ValidationProfile.CONFIG in status_profiles, (
        f"status should use config profile; saw {status_profiles}"
    )
    assert ValidationProfile.ASSETS in download_profiles, (
        f"download-mirror should use assets profile; saw {download_profiles}"
    )
    assert ValidationProfile.ASSETS not in status_profiles


# ── 7. no_spack / legacy template fallback ───────────────────────────────


def test_select_template_falls_back_to_shared_nospack(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``method: no_spack`` without per-env Dockerfile.j2 uses shared nospack template."""
    import shutil

    from hpc_cf.config import PROJECT_ROOT

    name = "nospack-synth"
    env_root = _write_spack_env(
        tmp_path,
        name,
        env_yaml=(
            "schema_version: 1\n"
            "method: no_spack\n"
            "images:\n"
            "  builder: debian:trixie\n"
            "  runtime: debian:trixie-slim\n"
            "spack:\n  version: '1.1.1'\n  env_name: e\n"
            "script: echo hi\n"
        ),
        spack_yaml=None,
        dockerfile=None,
        nested=False,
    )
    assert not (env_root / "Dockerfile.j2").exists()

    # Shared method template lives under layout.templates_dir.
    templates = tmp_path / "templates"
    templates.mkdir()
    shutil.copy2(
        PROJECT_ROOT / "templates" / "Dockerfile.nospack.j2",
        templates / "Dockerfile.nospack.j2",
    )
    layout = ProjectLayout(project_root=tmp_path)

    chosen = select_template(name, None, layout=layout)
    assert chosen.name == "Dockerfile.nospack.j2"
    assert chosen.is_file()
    assert chosen.parent == templates


def test_legacy_template_without_env_yaml_warns_compatibility(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Legacy ``templates/Dockerfile-*.j2`` without env.yaml → compatibility + warning."""
    templates = tmp_path / "templates"
    templates.mkdir()
    tpl = templates / "Dockerfile-legacy-demo.j2"
    tpl.write_text("FROM debian:trixie\n", encoding="utf-8")

    with caplog.at_level(logging.WARNING):
        build_context(
            use_mirror=False,
            build_only=True,
            app_version="legacy-demo",
            template_path=tpl,
            layout=ProjectLayout(project_root=tmp_path),
        )

    text = caplog.text.lower()
    assert "compat" in text or "compatibility" in text


# ── 8. ProjectLayout alternate root stays isolated ───────────────────────


def test_resolve_env_paths_honors_project_layout(tmp_path: Path) -> None:
    """Alternate ``ProjectLayout`` must resolve envs under that root only."""
    from hpc_cf.config import PROJECT_ROOT
    from hpc_cf.spack_ops import resolve_env_paths

    layout = ProjectLayout(project_root=tmp_path)
    conf = tmp_path / "spack-envs" / "layout-only" / "spack-env-file"
    conf.mkdir(parents=True)
    (conf / "env.yaml").write_text(
        "schema_version: 1\n"
        "spack:\n  version: '1.1.1'\n  env_name: e\n",
        encoding="utf-8",
    )

    host, _container = resolve_env_paths("layout-only", layout=layout)
    host = host.resolve()
    assert host == conf.resolve()
    assert PROJECT_ROOT.resolve() not in host.parents
    assert host != PROJECT_ROOT.resolve()
    assert not str(host).startswith(str(PROJECT_ROOT.resolve() / "spack-envs"))


def test_list_available_envs_honors_project_layout(tmp_path: Path) -> None:
    """Env inventory for an alternate layout must not list the real checkout."""
    from hpc_cf.env import list_available_envs

    layout = ProjectLayout(project_root=tmp_path)
    _write_spack_env(
        tmp_path,
        "synthetic-only",
        env_yaml=(
            "schema_version: 1\n"
            "spack:\n  version: '1.1.1'\n  env_name: e\n"
        ),
        nested=False,
    )

    names = list_available_envs(layout=layout)
    assert names == ["synthetic-only"]
    assert not any(n.startswith("cp2k_") for n in names)


# ── 9. SIF relative output path ──────────────────────────────────────────


def test_build_sif_resolves_relative_output_before_stat(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Relative ``--output`` must be absolutized (wrt process cwd) before ``stat``.

    ``apptainer build`` may run with ``cwd=artifacts/``; post-build ``stat`` must
    still find the SIF when the caller passed a relative path.
    """
    from hpc_cf import sif as sif_mod

    monkeypatch.setattr(sif_mod, "PROJECT_ROOT", tmp_path)
    monkeypatch.chdir(tmp_path)

    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    (artifacts / "demo_tag.tar").write_bytes(b"oci-tar")

    def fake_run(
        cmd: list[str],
        cwd: Path | None = None,
        **kwargs: object,
    ) -> MagicMock:
        for arg in cmd:
            if str(arg).endswith(".sif"):
                out = Path(arg)
                if not out.is_absolute() and cwd is not None:
                    out = Path(cwd) / out
                out.parent.mkdir(parents=True, exist_ok=True)
                out.write_bytes(b"sif-bytes")
        return MagicMock(returncode=0)

    with (
        patch.object(sif_mod, "ensure_apptainer", return_value="/usr/bin/apptainer"),
        patch.object(sif_mod, "check_command_exists", return_value=True),
        patch.object(sif_mod, "run_cmd", side_effect=fake_run),
        patch.object(sif_mod, "_find_def_template", return_value=None),
    ):
        sif_mod.build_sif(
            docker_image="demo",
            docker_tag="tag",
            output=Path("out/rel.sif"),
            yes=True,
        )

    # Process-cwd-relative absolute resolution (not artifacts-cwd-only).
    expected = (tmp_path / "out" / "rel.sif").resolve()
    assert expected.is_file(), f"expected SIF at {expected}"


def test_build_sif_mock_contract_uses_archive_and_requested_options(
    tmp_path: Path,
) -> None:
    """SIF conversion stays mockable and passes the exact archive contract."""
    from hpc_cf import sif as sif_mod
    from hpc_cf.execution import ProjectLayout

    layout = ProjectLayout(project_root=tmp_path)
    layout.artifacts_dir.mkdir()
    tar_path = layout.artifacts_dir / "demo_tag.tar"
    tar_path.write_bytes(b"oci-tar")
    output = tmp_path / "result.sif"
    calls: list[tuple[list[str], Path | None]] = []

    def fake_run(
        cmd: list[str],
        *,
        cwd: Path | None = None,
        **_kwargs: object,
    ) -> None:
        calls.append((list(cmd), cwd))
        if cmd[0] == "/usr/bin/apptainer":
            output.write_bytes(b"sif")

    with (
        patch.object(sif_mod, "ensure_apptainer", return_value="/usr/bin/apptainer"),
        patch.object(sif_mod, "check_command_exists", side_effect=lambda c: c == "podman"),
        patch.object(sif_mod, "run_cmd", side_effect=fake_run),
        patch.object(sif_mod, "_find_def_template", return_value=None),
    ):
        sif_mod.build_sif(
            docker_image="registry.example/demo",
            docker_tag="tag",
            output=output,
            mksquashfs_args="-comp gzip",
            yes=True,
            layout=layout,
        )

    assert calls == [
        (
            [
                "/usr/bin/apptainer",
                "build",
                "--force",
                "--mksquashfs-args",
                "-comp gzip",
                str(output),
                "docker-archive://demo_tag.tar",
            ],
            layout.artifacts_dir,
        )
    ]


def test_build_docker_like_builds_builder_before_final(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Builder stage must be tagged before the final image build.

    Runtime-stage failures should leave ``:tag-builder`` available for debug.
    """
    from hpc_cf import sif as sif_mod

    dockerfile = tmp_path / "Dockerfile"
    dockerfile.write_text(
        "FROM debian:trixie AS builder\nFROM builder AS runtime\nFROM runtime AS final\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)

    calls: list[list[str]] = []

    def fake_run_cmd(cmd: list[str], **kwargs: object) -> MagicMock:
        calls.append(list(cmd))
        return MagicMock(returncode=0)

    with (
        patch.object(sif_mod, "check_command_exists", return_value=True),
        patch.object(sif_mod, "run_cmd", side_effect=fake_run_cmd),
    ):
        sif_mod.build_docker_like(
            dockerfile=dockerfile,
            image="cp2k_opensource",
            tag="2026.1",
            engine="podman",
            network_host=True,
        )

    assert len(calls) == 2
    builder_cmd, final_cmd = calls
    assert builder_cmd[:2] == ["podman", "build"]
    assert "--target" in builder_cmd
    assert builder_cmd[builder_cmd.index("--target") + 1] == "builder"
    assert "-t" in builder_cmd
    assert builder_cmd[builder_cmd.index("-t") + 1] == "cp2k_opensource:2026.1-builder"
    assert "--target" not in final_cmd
    assert final_cmd[final_cmd.index("-t") + 1] == "cp2k_opensource:2026.1"


def test_build_docker_like_keeps_builder_tag_if_final_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Final-stage failure must not prevent the builder target build from running."""
    from hpc_cf import sif as sif_mod

    dockerfile = tmp_path / "Dockerfile"
    dockerfile.write_text("FROM debian:trixie AS builder\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    calls: list[list[str]] = []

    def fake_run_cmd(cmd: list[str], **kwargs: object) -> MagicMock:
        calls.append(list(cmd))
        if "--target" not in cmd:
            raise subprocess.CalledProcessError(1, cmd)
        return MagicMock(returncode=0)

    with (
        patch.object(sif_mod, "check_command_exists", return_value=True),
        patch.object(sif_mod, "run_cmd", side_effect=fake_run_cmd),
        pytest.raises(subprocess.CalledProcessError),
    ):
        sif_mod.build_docker_like(
            dockerfile=dockerfile,
            image="demo",
            tag="t",
            engine="podman",
            network_host=False,
        )

    assert len(calls) == 2
    assert "--target" in calls[0]
    assert calls[0][calls[0].index("-t") + 1] == "demo:t-builder"
    assert "--target" not in calls[1]
