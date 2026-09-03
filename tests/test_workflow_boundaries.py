"""Phase 5: request/service boundaries, RunnerPort, ProjectLayout, mirror lock."""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from hpc_cf.execution import ProjectLayout, RunnerPort, SharedMirrorStore
from hpc_cf.workflows import (
    AssetsRequest,
    AssetsService,
    BuildRequest,
    BuildService,
    assets_request_from_args,
    build_request_from_args,
)


def test_assets_module_has_no_argparse_import() -> None:
    import hpc_cf.assets as assets_mod

    assert not hasattr(assets_mod, "argparse")
    source = Path(assets_mod.__file__).read_text(encoding="utf-8")
    assert "argparse" not in source
    assert "Namespace" not in source


def test_container_satisfies_runner_port() -> None:
    from hpc_cf.container import Container

    ctr: RunnerPort = Container(name="n", image="i")
    assert callable(ctr.exec)
    assert callable(ctr.run_ephemeral)


def test_project_layout_paths(tmp_path: Path) -> None:
    layout = ProjectLayout(project_root=tmp_path)
    assert layout.assets_dir == tmp_path / "assets"
    assert layout.spack_mirror_dir == tmp_path / "assets" / "spack-mirror"
    assert layout.spack_envs_dir == tmp_path / "spack-envs"
    assert layout.templates_dir == tmp_path / "templates"
    assert layout.artifacts_dir == tmp_path / "artifacts"
    assert layout.bootstrap_dir("1.2.0") == tmp_path / "assets" / "bootstrap-1.2.0"
    assert layout.mirror_lock_path.parent.name == ".hpc_cf"


def test_template_discovery_and_render_use_project_layout(tmp_path: Path) -> None:
    """Template select/render must resolve paths under injected ProjectLayout only."""
    from hpc_cf.config import PROJECT_ROOT
    from hpc_cf.template import (
        build_context,
        render_template,
        resolve_build_input,
        select_template,
    )

    layout = ProjectLayout(project_root=tmp_path)
    env_name = "tmp-layout-env"
    templates = tmp_path / "templates"
    templates.mkdir(parents=True)
    (templates / "Dockerfile.nospack.j2").write_text(
        "FROM {{ runtime_base_image }}\n",
        encoding="utf-8",
    )
    env_dir = tmp_path / "spack-envs" / env_name
    env_dir.mkdir(parents=True)
    (env_dir / "env.yaml").write_text(
        "schema_version: 1\nmethod: no_spack\n"
        "images:\n  builder: debian:trixie\n  runtime: debian:trixie-slim\n"
        "spack:\n  version: '1.1.1'\n  env_name: e\n"
        "script: echo hi\n",
        encoding="utf-8",
    )

    chosen = select_template(env_name, None, layout=layout)
    assert chosen == templates / "Dockerfile.nospack.j2"
    assert PROJECT_ROOT.resolve() not in chosen.resolve().parents

    resolved = resolve_build_input(env_name, None, layout=layout)
    ctx = build_context(
        use_mirror=False,
        build_only=True,
        app_version=env_name,
        template_path=chosen,
        resolved=resolved,
        layout=layout,
    )
    out = render_template(chosen, ctx, layout=layout)
    assert "FROM debian:trixie-slim" in out


def test_find_bootstrap_dir_uses_layout(
    tmp_path: Path,
) -> None:
    from hpc_cf.assets import find_bootstrap_dir

    assets = tmp_path / "assets"
    assets.mkdir()
    (assets / "bootstrap-1.0.0").mkdir()
    preferred = assets / "bootstrap-1.2.0"
    preferred.mkdir()
    layout = ProjectLayout(project_root=tmp_path)

    assert find_bootstrap_dir("1.2.0", layout=layout) == preferred
    assert find_bootstrap_dir(layout=layout) == assets / "bootstrap-1.0.0"


def test_assets_request_from_cli_args() -> None:
    from hpc_cf.cli import build_parser

    args = build_parser().parse_args(
        [
            "assets",
            "--env", "cp2k_opensource-2026.1-force-avx512",
            "--skip-verify",
            "--force-bootstrap",
            "--podman-opt=--dns=1.1.1.1",
        ]
    )
    req = assets_request_from_args(args)
    assert isinstance(req, AssetsRequest)
    assert req.env == "cp2k_opensource-2026.1-force-avx512"
    assert req.skip_verify is True
    assert req.force_bootstrap is True
    assert req.podman_opt == ("--dns=1.1.1.1",)


def test_build_request_from_cli_args() -> None:
    from hpc_cf.cli import build_parser

    args = build_parser().parse_args(
        [
            "build",
            "--app-version", "cp2k_opensource-2026.1-force-avx512",
            "--engine", "docker",
            "--network-host",
            "--build-arg", "SPACK_MAKE_JOBS=4",
            "--no-mirror",
        ]
    )
    req = build_request_from_args(args, use_mirror=False)
    assert isinstance(req, BuildRequest)
    assert req.app_version == "cp2k_opensource-2026.1-force-avx512"
    assert req.engine == "docker"
    assert req.network_host is True
    assert req.build_args == ("SPACK_MAKE_JOBS=4",)
    assert req.use_mirror is False
    assert req.render_only is False


def test_dockerfile_request_is_render_only() -> None:
    from hpc_cf.cli import build_parser

    args = build_parser().parse_args(
        ["dockerfile", "--app-version", "cp2k_opensource-2026.1-force-avx512"]
    )
    req = build_request_from_args(args, use_mirror=True, render_only=True)
    assert req.render_only is True


def test_cli_assets_dispatches_to_service() -> None:
    from hpc_cf.cli import run_new_cli

    with patch.object(AssetsService, "run") as mock_run:
        rc = run_new_cli(["assets", "--env"])
    assert rc == 0
    mock_run.assert_called_once()
    req = mock_run.call_args.args[0]
    assert isinstance(req, AssetsRequest)
    assert req.env == "__LIST__"


def test_cli_build_dispatches_to_build_service() -> None:
    from hpc_cf.cli import run_new_cli

    with patch.object(BuildService, "run", return_value=0) as mock_run:
        rc = run_new_cli(
            ["dockerfile", "--app-version", "cp2k_opensource-2026.1-force-avx512"]
        )
    assert rc == 0
    mock_run.assert_called_once()
    req = mock_run.call_args.args[0]
    assert isinstance(req, BuildRequest)
    assert req.render_only is True


def test_cli_build_command_dispatches_non_render_request() -> None:
    from hpc_cf.cli import run_new_cli

    with patch.object(BuildService, "run", return_value=0) as mock_run:
        rc = run_new_cli(
            [
                "build",
                "--app-version",
                "cp2k_opensource-2026.1-force-avx512",
                "--engine",
                "docker",
            ]
        )
    assert rc == 0
    mock_run.assert_called_once()
    req = mock_run.call_args.args[0]
    assert isinstance(req, BuildRequest)
    assert req.render_only is False
    assert req.engine == "docker"


def _write_assets_service_env(layout: ProjectLayout, *, with_lock: bool) -> None:
    env_dir = layout.spack_envs_dir / "demo" / "spack-env-file"
    env_dir.mkdir(parents=True)
    (env_dir / "env.yaml").write_text(
        "schema_version: 1\nmethod: spack\n"
        "spack:\n  version: '1.1.1'\n  env_name: demo-env\n",
        encoding="utf-8",
    )
    (env_dir / "spack.yaml").write_text(
        "spack:\n  specs: [pkgconf]\n",
        encoding="utf-8",
    )
    if with_lock:
        (env_dir / "spack.lock").write_text('{"concrete": true}\n', encoding="utf-8")
    layout.assets_dir.mkdir(parents=True)
    (layout.assets_dir / "spack-v1.1.1.tar.gz").write_bytes(b"spack")


def test_assets_service_fails_before_container_without_lock(tmp_path: Path) -> None:
    layout = ProjectLayout(project_root=tmp_path)
    _write_assets_service_env(layout, with_lock=False)

    with (
        patch("hpc_cf.assets.run_assets") as mock_run,
        pytest.raises(FileNotFoundError, match="spack.lock"),
    ):
        AssetsService(layout=layout).run(AssetsRequest(env="demo"))

    mock_run.assert_not_called()


def test_assets_service_allows_concretize_to_produce_lock(tmp_path: Path) -> None:
    layout = ProjectLayout(project_root=tmp_path)
    _write_assets_service_env(layout, with_lock=False)

    def fake_run(_request: AssetsRequest, *, layout: ProjectLayout) -> None:
        lock = layout.spack_envs_dir / "demo" / "spack-env-file" / "spack.lock"
        lock.write_text('{"concrete": true}\n', encoding="utf-8")

    with patch("hpc_cf.assets.run_assets", side_effect=fake_run) as mock_run:
        AssetsService(layout=layout).run(
            AssetsRequest(env="demo", allow_concretize=True)
        )

    mock_run.assert_called_once()
    lock = layout.spack_envs_dir / "demo" / "spack-env-file" / "spack.lock"
    assert lock.stat().st_size > 0


def test_shared_mirror_lock_serializes_writers(tmp_path: Path) -> None:
    layout = ProjectLayout(project_root=tmp_path)
    store = SharedMirrorStore(layout)
    order: list[str] = []
    barrier = threading.Barrier(2)

    def worker(label: str) -> None:
        barrier.wait()
        with store.exclusive_write():
            order.append(f"{label}-enter")
            time.sleep(0.05)
            order.append(f"{label}-exit")

    t1 = threading.Thread(target=worker, args=("a",))
    t2 = threading.Thread(target=worker, args=("b",))
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    # Critical sections must not interleave.
    assert order in (
        ["a-enter", "a-exit", "b-enter", "b-exit"],
        ["b-enter", "b-exit", "a-enter", "a-exit"],
    )


def test_exclusive_write_logs_while_waiting(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Blocked writers emit wait logs (interval patched short for the test)."""
    import hpc_cf.execution as execution

    monkeypatch.setattr(execution, "MIRROR_LOCK_WAIT_LOG_INTERVAL_S", 0.05)
    monkeypatch.setattr(execution, "MIRROR_LOCK_POLL_INTERVAL_S", 0.01)

    layout = ProjectLayout(project_root=tmp_path)
    store = SharedMirrorStore(layout)
    held = threading.Event()
    release = threading.Event()

    def holder() -> None:
        with store.exclusive_write():
            held.set()
            release.wait(timeout=5)

    t = threading.Thread(target=holder)
    t.start()
    assert held.wait(timeout=2)

    def delayed_release() -> None:
        # Hold long enough for the initial wait log plus one periodic log.
        time.sleep(0.12)
        release.set()

    threading.Thread(target=delayed_release, daemon=True).start()

    with caplog.at_level("INFO", logger="hpc_cf.execution"):
        with store.exclusive_write():
            pass
    t.join(timeout=2)
    assert not t.is_alive()

    text = caplog.text
    assert "Shared mirror write lock busy" in text
    assert "Still waiting for shared mirror write lock" in text
    assert "Acquired shared mirror write lock after" in text


def test_shared_mirror_read_lock_blocks_writer(tmp_path: Path) -> None:
    """Readers holding LOCK_SH must block exclusive writers."""
    layout = ProjectLayout(project_root=tmp_path)
    store = SharedMirrorStore(layout)
    reader_held = threading.Event()
    writer_entered = threading.Event()
    release_reader = threading.Event()

    def reader() -> None:
        with store.shared_read():
            reader_held.set()
            release_reader.wait(timeout=5)

    def writer() -> None:
        assert reader_held.wait(timeout=2)
        with store.exclusive_write():
            writer_entered.set()

    t_reader = threading.Thread(target=reader)
    t_writer = threading.Thread(target=writer)
    t_reader.start()
    t_writer.start()
    assert reader_held.wait(timeout=2)
    # Writer must stay blocked while the shared read lock is held.
    assert not writer_entered.wait(timeout=0.2)
    release_reader.set()
    t_writer.join(timeout=2)
    t_reader.join(timeout=2)
    assert writer_entered.is_set()
    assert not t_writer.is_alive()
    assert not t_reader.is_alive()


def test_build_service_holds_mirror_read_lock_when_use_mirror(
    tmp_path: Path,
) -> None:
    """OCI builds with use_mirror must hold LOCK_SH for the source mirror."""
    import subprocess
    from types import SimpleNamespace

    from hpc_cf.environment import BuildcacheMode, BuildcachePolicy

    layout = ProjectLayout(project_root=tmp_path)
    entered = False

    def fake_build(**_: object) -> None:
        nonlocal entered
        entered = True
        lock_probe = subprocess.run(
            [
                "python3",
                "-c",
                (
                    "import fcntl;"
                    f"f=open({str(layout.mirror_lock_path)!r},'a+');"
                    "fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB)"
                ),
            ],
            check=False,
        )
        # Exclusive acquire must fail while BuildService holds LOCK_SH.
        assert lock_probe.returncode != 0

    with (
        patch("hpc_cf.env.run_static_checks"),
        patch("hpc_cf.template.resolve_build_input") as resolved,
        patch("hpc_cf.template.resolve_image_and_tag", return_value=("img", "tag")),
        patch("hpc_cf.template.generate_dockerfile", return_value=Path("Dockerfile")),
        patch("hpc_cf.sif.build_docker_like", side_effect=fake_build),
    ):
        resolved.return_value.environment_dir = tmp_path
        resolved.return_value.environment_spec = SimpleNamespace(
            spack=SimpleNamespace(
                buildcache=SimpleNamespace(enabled=False, policy=BuildcachePolicy.NEVER, mode=BuildcacheMode.LOCAL, url=None),
        )
        )
        BuildService(layout=layout).run(
            BuildRequest(
                app_version="demo",
                use_mirror=True,
                buildcache=BuildcachePolicy.NEVER,
            )
        )
    assert entered


def test_build_service_skips_mirror_read_lock_without_mirror(
    tmp_path: Path,
) -> None:
    """use_mirror=False must not acquire SharedMirrorStore.shared_read."""
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    from hpc_cf.environment import BuildcacheMode, BuildcachePolicy

    layout = ProjectLayout(project_root=tmp_path)
    built = False

    def fake_build(**_: object) -> None:
        nonlocal built
        built = True

    with (
        patch("hpc_cf.env.run_static_checks"),
        patch("hpc_cf.template.resolve_build_input") as resolved,
        patch("hpc_cf.template.resolve_image_and_tag", return_value=("img", "tag")),
        patch("hpc_cf.template.generate_dockerfile", return_value=Path("Dockerfile")),
        patch("hpc_cf.sif.build_docker_like", side_effect=fake_build),
        patch.object(
            SharedMirrorStore,
            "shared_read",
            side_effect=AssertionError(
                "shared_read must not run when use_mirror=False"
            ),
        ) as mock_shared_read,
    ):
        resolved.return_value = MagicMock(
            environment_dir=tmp_path,
            environment_spec=SimpleNamespace(
                spack=SimpleNamespace(
                    buildcache=SimpleNamespace(
                        enabled=False, policy=BuildcachePolicy.NEVER, mode=BuildcacheMode.LOCAL, url=None,
                    )
                ),
            )
        )
        BuildService(layout=layout).run(
            BuildRequest(
                app_version="demo",
                use_mirror=False,
                buildcache=BuildcachePolicy.NEVER,
            )
        )
    assert built
    mock_shared_read.assert_not_called()


def test_shared_mirror_readers_are_concurrent(tmp_path: Path) -> None:
    """Multiple shared_read holders may overlap."""
    layout = ProjectLayout(project_root=tmp_path)
    store = SharedMirrorStore(layout)
    both_inside = threading.Barrier(2)
    release = threading.Event()
    inside = 0
    lock = threading.Lock()
    max_inside = 0

    def reader() -> None:
        nonlocal inside, max_inside
        with store.shared_read():
            with lock:
                inside += 1
                max_inside = max(max_inside, inside)
            both_inside.wait(timeout=2)
            release.wait(timeout=5)
            with lock:
                inside -= 1

    t1 = threading.Thread(target=reader)
    t2 = threading.Thread(target=reader)
    t1.start()
    t2.start()
    # If readers serialize incorrectly, the barrier times out.
    time.sleep(0.05)
    release.set()
    t1.join(timeout=2)
    t2.join(timeout=2)
    assert max_inside == 2


def test_shared_mirror_run_dir_and_manifest(tmp_path: Path) -> None:
    layout = ProjectLayout(project_root=tmp_path)
    store = SharedMirrorStore(layout)
    run = store.begin_run("cp2k_demo")
    assert run.host_dir.is_dir()
    assert run.host_dir.parent.name == "runs"
    assert (layout.spack_mirror_dir / ".hpc_cf" / "runs").is_dir()

    lock_file = tmp_path / "spack.lock"
    lock_file.write_text('{"concrete": true}', encoding="utf-8")
    path = store.write_manifest(
        run,
        env_name="cp2k_demo",
        spack_version="1.1.1",
        lock_path=lock_file,
        stats={"present": 1, "added": 2, "failed": 0},
    )
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["env"] == "cp2k_demo"
    assert data["spack_version"] == "1.1.1"
    assert data["stats"] == {"present": 1, "added": 2, "failed": 0}
    assert data["status"] == "success"
    assert len(data["lock_hash"]) == 64
    assert data["run_id"] == run.run_id


def test_shared_mirror_cleanup_keeps_newest_runs(tmp_path: Path) -> None:
    layout = ProjectLayout(project_root=tmp_path)
    store = SharedMirrorStore(layout)
    for i in range(5):
        (layout.mirror_runs_dir / f"2026010{i}T000000Z-env-deadbeef").mkdir(parents=True)
    removed = store.cleanup_runs(keep=2)
    assert removed == 3
    remaining = sorted(p.name for p in layout.mirror_runs_dir.iterdir())
    assert remaining == [
        "20260103T000000Z-env-deadbeef",
        "20260104T000000Z-env-deadbeef",
    ]


def test_run_mirror_acquires_lock_and_writes_manifest(tmp_path: Path) -> None:
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

    stats = {"present": 3, "added": 1, "failed": 0}
    ops = MagicMock()
    ops.env.spack.version = "1.1.1"
    ops.run_mirror_pipeline.return_value = stats

    with (
        patch("hpc_cf.assets._make_spack_ops", return_value=(ops.env, ops)),
        patch("hpc_cf.assets.resolve_env_paths", return_value=(
            env_host, Path("/work/spack-envs/demo/spack-env-file"),
        )),
    ):
        run_mirror(MagicMock(), "demo", layout=layout)

    ops.run_mirror_pipeline.assert_called_once()
    runs = list((layout.spack_mirror_dir / ".hpc_cf" / "runs").iterdir())
    assert len(runs) == 1
    manifest = runs[0] / "manifest.json"
    assert manifest.is_file()
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    assert payload["env"] == "demo"
    assert payload["stats"]["added"] == 1


def test_assets_service_list_envs_skips_container() -> None:
    svc = AssetsService(layout=ProjectLayout(project_root=Path("/tmp/unused")))
    with patch("hpc_cf.env.list_available_envs", return_value=["env-a", "env-b"]):
        with patch("hpc_cf.assets.Container") as mock_ctr:
            svc.run(AssetsRequest(env="__LIST__"))
            mock_ctr.assert_not_called()


def test_template_select_and_render_use_injected_layout(tmp_path: Path) -> None:
    """Template discovery/render must honor *layout*, not global config paths."""
    from hpc_cf import config as config_mod
    from hpc_cf.template import (
        build_context,
        render_template,
        resolve_build_input,
        select_template,
    )

    layout = ProjectLayout(project_root=tmp_path)
    templates = layout.templates_dir
    partials = templates / "partials"
    partials.mkdir(parents=True)
    (partials / "marker.j2").write_text("MARKER_FROM_TMP_LAYOUT\n", encoding="utf-8")

    env_dir = layout.spack_envs_dir / "layout-template-demo"
    env_dir.mkdir(parents=True)
    (env_dir / "env.yaml").write_text(
        "schema_version: 1\n"
        "method: spack\n"
        "spack:\n  version: '1.1.1'\n  env_name: demo-env\n",
        encoding="utf-8",
    )
    (env_dir / "spack.yaml").write_text("spack:\n  specs: [pkgconf]\n", encoding="utf-8")
    (env_dir / "Dockerfile.j2").write_text(
        "FROM debian:trixie\n{% include 'partials/marker.j2' %}\n",
        encoding="utf-8",
    )

    resolved = resolve_build_input("layout-template-demo", layout=layout)
    chosen = select_template("layout-template-demo", layout=layout)
    assert chosen == env_dir / "Dockerfile.j2"
    assert resolved.render_template == chosen
    assert chosen.is_relative_to(tmp_path)
    assert not chosen.is_relative_to(config_mod.PROJECT_ROOT / "spack-envs")

    ctx = build_context(
        use_mirror=False,
        build_only=True,
        app_version="layout-template-demo",
        template_path=chosen,
        resolved=resolved,
        layout=layout,
    )
    rendered = render_template(chosen, ctx, layout=layout)
    assert "MARKER_FROM_TMP_LAYOUT" in rendered
    # Must not have fallen back to the real checkout templates/ tree.
    assert config_mod.TEMPLATES_DIR.resolve() != templates.resolve()
