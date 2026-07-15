"""Fail-closed spack.lock checks for build-input and Dockerfile rendering."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from hpc_cf.execution import ProjectLayout
from hpc_cf.template import build_context, render_template
from hpc_cf.validation import ValidationProfile, validate_environment


def _write_spack_env(env_dir: Path, *, with_lock: bool = True) -> None:
    env_dir.mkdir(parents=True, exist_ok=True)
    (env_dir / "env.yaml").write_text(
        "schema_version: 1\nmethod: spack\n"
        "spack:\n  version: '1.1.1'\n  env_name: test-env\n",
        encoding="utf-8",
    )
    (env_dir / "Dockerfile.j2").write_text(
        "{% include 'partials/spack_env_create.j2' %}\n",
        encoding="utf-8",
    )
    (env_dir / "spack.yaml").write_text(
        "spack:\n  specs: [pkgconf]\n",
        encoding="utf-8",
    )
    if with_lock:
        (env_dir / "spack.lock").write_text('{"concrete":true}\n', encoding="utf-8")


def test_build_input_errors_without_lock(tmp_path: Path) -> None:
    env_dir = tmp_path / "env"
    _write_spack_env(env_dir, with_lock=False)
    layout = ProjectLayout(project_root=tmp_path)
    layout.assets_dir.mkdir()
    (layout.assets_dir / "spack-v1.1.1.tar.gz").write_bytes(b"x")

    report = validate_environment(
        env_dir, ValidationProfile.BUILD_INPUT, layout=layout
    )
    assert not report.ok
    assert any(f.code == "spack_lock.missing" for f in report.errors())


def test_build_input_ok_with_nonempty_lock(tmp_path: Path) -> None:
    env_dir = tmp_path / "env"
    _write_spack_env(env_dir, with_lock=True)
    layout = ProjectLayout(project_root=tmp_path)
    layout.assets_dir.mkdir()
    (layout.assets_dir / "spack-v1.1.1.tar.gz").write_bytes(b"x")

    report = validate_environment(
        env_dir, ValidationProfile.BUILD_INPUT, layout=layout
    )
    assert not any(f.code == "spack_lock.missing" for f in report.findings)


def test_allow_reconcretize_downgrades_missing_lock(tmp_path: Path) -> None:
    env_dir = tmp_path / "env"
    _write_spack_env(env_dir, with_lock=False)
    layout = ProjectLayout(project_root=tmp_path)
    layout.assets_dir.mkdir()
    (layout.assets_dir / "spack-v1.1.1.tar.gz").write_bytes(b"x")

    report = validate_environment(
        env_dir,
        ValidationProfile.BUILD_INPUT,
        layout=layout,
        allow_reconcretize=True,
    )
    assert report.ok
    warns = [f for f in report.warnings() if f.code == "spack_lock.missing"]
    assert len(warns) == 1


def test_env_create_partial_fail_closed_without_allow(tmp_path: Path) -> None:
    env_dir = tmp_path / "spack-envs" / "demo"
    _write_spack_env(env_dir, with_lock=False)
    tpl = env_dir / "Dockerfile.j2"
    ctx = build_context(
        use_mirror=False,
        build_only=True,
        app_version="demo",
        template_path=tpl,
        allow_reconcretize=False,
    )
    out = render_template(tpl, ctx)
    assert "refuse to concretize during image build" in out
    assert "exit 1" in out
    assert "install step will concretize" not in out


def test_env_create_partial_allows_reconcretize_escape(tmp_path: Path) -> None:
    env_dir = tmp_path / "spack-envs" / "demo"
    _write_spack_env(env_dir, with_lock=False)
    tpl = env_dir / "Dockerfile.j2"
    ctx = build_context(
        use_mirror=False,
        build_only=True,
        app_version="demo",
        template_path=tpl,
        allow_reconcretize=True,
    )
    out = render_template(tpl, ctx)
    assert "--allow-reconcretize" in out
    assert "refuse to concretize" not in out


def test_run_mirror_fails_closed_without_lock(tmp_path: Path) -> None:
    import json

    from hpc_cf.assets import run_mirror

    layout = ProjectLayout(project_root=tmp_path)
    env_host = tmp_path / "spack-envs" / "demo" / "spack-env-file"
    env_host.mkdir(parents=True)
    (env_host / "spack.yaml").write_text("spack: {}\n", encoding="utf-8")
    (env_host / "env.yaml").write_text(
        "schema_version: 1\nspack:\n  version: '1.1.1'\n  env_name: demo-env\n",
        encoding="utf-8",
    )

    ops = MagicMock()
    ops.env.spack.version = "1.1.1"

    with (
        patch("hpc_cf.assets._make_spack_ops", return_value=(ops.env, ops)),
        patch(
            "hpc_cf.assets.resolve_env_paths",
            return_value=(env_host, Path("/work/spack-envs/demo/spack-env-file")),
        ),
        pytest.raises(FileNotFoundError, match="spack.lock"),
    ):
        run_mirror(MagicMock(), "demo", layout=layout)

    ops.run_all_pipeline.assert_not_called()
    ops.run_mirror_pipeline.assert_not_called()
    manifests = list(
        (layout.spack_mirror_dir / ".hpc_cf" / "runs").glob("*/manifest.json")
    )
    assert manifests, "missing lock should still write a failed manifest"
    payload = json.loads(manifests[0].read_text(encoding="utf-8"))
    assert payload["status"] == "failed"


def test_run_mirror_allow_concretize_uses_all_pipeline(tmp_path: Path) -> None:
    from hpc_cf.assets import run_mirror

    layout = ProjectLayout(project_root=tmp_path)
    env_host = tmp_path / "spack-envs" / "demo" / "spack-env-file"
    env_host.mkdir(parents=True)
    (env_host / "spack.yaml").write_text("spack: {}\n", encoding="utf-8")
    (env_host / "env.yaml").write_text(
        "schema_version: 1\nspack:\n  version: '1.1.1'\n  env_name: demo-env\n",
        encoding="utf-8",
    )

    stats = {"present": 0, "added": 1, "failed": 0}
    ops = MagicMock()
    ops.env.spack.version = "1.1.1"
    ops.run_all_pipeline.return_value = stats

    with (
        patch("hpc_cf.assets._make_spack_ops", return_value=(ops.env, ops)),
        patch(
            "hpc_cf.assets.resolve_env_paths",
            return_value=(env_host, Path("/work/spack-envs/demo/spack-env-file")),
        ),
    ):
        # Simulate all-pipeline writing the lock before manifest.
        def _all(*_a, **_k):
            (env_host / "spack.lock").write_text('{"hash":"x"}', encoding="utf-8")
            return stats

        ops.run_all_pipeline.side_effect = _all
        out = run_mirror(
            MagicMock(), "demo", layout=layout, allow_concretize=True
        )

    assert out == stats
    ops.run_all_pipeline.assert_called_once()


def test_run_mirror_pipeline_failure_writes_failed_manifest(tmp_path: Path) -> None:
    import json

    from hpc_cf.assets import run_mirror

    layout = ProjectLayout(project_root=tmp_path)
    env_host = tmp_path / "spack-envs" / "demo" / "spack-env-file"
    env_host.mkdir(parents=True)
    (env_host / "spack.yaml").write_text("spack: {}\n", encoding="utf-8")
    (env_host / "spack.lock").write_text('{"hash":"x"}', encoding="utf-8")
    (env_host / "env.yaml").write_text(
        "schema_version: 1\nspack:\n  version: '1.1.1'\n  env_name: demo-env\n",
        encoding="utf-8",
    )

    ops = MagicMock()
    ops.env.spack.version = "1.1.1"
    ops.run_mirror_pipeline.side_effect = RuntimeError("mirror boom")

    with (
        patch("hpc_cf.assets._make_spack_ops", return_value=(ops.env, ops)),
        patch(
            "hpc_cf.assets.resolve_env_paths",
            return_value=(env_host, Path("/work/spack-envs/demo/spack-env-file")),
        ),
        pytest.raises(RuntimeError, match="mirror boom"),
    ):
        run_mirror(MagicMock(), "demo", layout=layout)

    manifests = list(
        (layout.spack_mirror_dir / ".hpc_cf" / "runs").glob("*/manifest.json")
    )
    assert len(manifests) == 1
    payload = json.loads(manifests[0].read_text(encoding="utf-8"))
    assert payload["status"] == "failed"
    assert "mirror boom" in payload.get("error", "")


def test_assets_service_aborts_when_env_yaml_missing(tmp_path: Path) -> None:
    from hpc_cf.workflows import AssetsRequest, AssetsService

    layout = ProjectLayout(project_root=tmp_path)
    env_host = tmp_path / "spack-envs" / "demo" / "spack-env-file"
    env_host.mkdir(parents=True)
    # No env.yaml — preflight must abort before run_assets.
    with (
        patch(
            "hpc_cf.spack_ops.resolve_env_paths",
            return_value=(env_host, Path("/work/x")),
        ),
        patch("hpc_cf.assets.run_assets") as mock_run,
        pytest.raises(FileNotFoundError, match="preflight aborted"),
    ):
        AssetsService(layout=layout).run(AssetsRequest(env="demo", status=True))
    mock_run.assert_not_called()


def test_dockerfile_render_only_fails_without_lock(tmp_path: Path) -> None:
    """dockerfile CLI (render_only) must fail-closed on missing lock — same as build."""
    from hpc_cf.workflows import BuildRequest, BuildService

    env_dir = tmp_path / "spack-envs" / "demo"
    _write_spack_env(env_dir, with_lock=False)
    layout = ProjectLayout(project_root=tmp_path)
    layout.assets_dir.mkdir()
    (layout.assets_dir / "spack-v1.1.1.tar.gz").write_bytes(b"x")

    with (
        patch("hpc_cf.template.generate_dockerfile") as mock_gen,
        pytest.raises(FileNotFoundError, match="spack.lock"),
    ):
        BuildService(layout=layout).run(
            BuildRequest(
                app_version="demo",
                render_only=True,
                output=tmp_path / "out.Dockerfile",
            )
        )
    mock_gen.assert_not_called()


def test_dockerfile_render_only_allow_reconcretize_without_lock(
    tmp_path: Path,
) -> None:
    from hpc_cf.workflows import BuildRequest, BuildService

    env_dir = tmp_path / "spack-envs" / "demo"
    _write_spack_env(env_dir, with_lock=False)
    layout = ProjectLayout(project_root=tmp_path)
    layout.assets_dir.mkdir()
    (layout.assets_dir / "spack-v1.1.1.tar.gz").write_bytes(b"x")
    out = tmp_path / "out.Dockerfile"

    with patch(
        "hpc_cf.template.generate_dockerfile", return_value=out
    ) as mock_gen:
        rc = BuildService(layout=layout).run(
            BuildRequest(
                app_version="demo",
                render_only=True,
                allow_reconcretize=True,
                output=out,
                use_mirror=False,
                build_only=True,
            )
        )
    assert rc == 0
    mock_gen.assert_called_once()
    assert mock_gen.call_args.kwargs.get("allow_reconcretize") is True


def test_dockerfile_cli_wires_allow_reconcretize() -> None:
    from hpc_cf.cli import build_parser
    from hpc_cf.workflows import build_request_from_args

    args = build_parser().parse_args(
        [
            "dockerfile",
            "--app-version",
            "demo",
            "--allow-reconcretize",
        ]
    )
    req = build_request_from_args(args, use_mirror=True, render_only=True)
    assert req.render_only is True
    assert req.allow_reconcretize is True


def test_run_verify_fails_on_empty_lock(tmp_path: Path) -> None:
    from hpc_cf.assets import run_verify

    layout = ProjectLayout(project_root=tmp_path)
    env_host = tmp_path / "spack-envs" / "demo" / "spack-env-file"
    env_host.mkdir(parents=True)
    (env_host / "spack.yaml").write_text("spack: {}\n", encoding="utf-8")
    (env_host / "spack.lock").write_text("", encoding="utf-8")
    (env_host / "env.yaml").write_text(
        "schema_version: 1\nspack:\n  version: '1.1.1'\n  env_name: demo-env\n",
        encoding="utf-8",
    )

    ops = MagicMock()
    ops.env.spack.version = "1.1.1"

    with (
        patch("hpc_cf.assets._make_spack_ops", return_value=(ops.env, ops)),
        patch(
            "hpc_cf.assets.resolve_env_paths",
            return_value=(env_host, Path("/work/spack-envs/demo/spack-env-file")),
        ),
        pytest.raises(FileNotFoundError, match="empty|spack.lock"),
    ):
        run_verify(MagicMock(), "demo", layout=layout)

    ops.run_verify_pipeline.assert_not_called()
