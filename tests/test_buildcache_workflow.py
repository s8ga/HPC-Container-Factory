"""L0-L2 contracts for buildcache rendering, scripts, services, and CLI."""

from __future__ import annotations

import json
import subprocess
import threading
from contextlib import ExitStack, contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from hpc_cf.execution import ProjectLayout, SharedBuildcacheStore
from hpc_cf.template import build_context, render_template


ROOT = Path(__file__).resolve().parent.parent
PILOT = "cp2k_opensource-2025.2"
PILOT_TEMPLATE = ROOT / "spack-envs" / PILOT / "Dockerfile.j2"


def _render(policy: str, *, producer: bool = False) -> str:
    context = build_context(
        use_mirror=True,
        build_only=False,
        app_version=PILOT,
        template_path=PILOT_TEMPLATE,
        buildcache_policy=policy,
        buildcache_producer=producer,
    )
    return render_template(PILOT_TEMPLATE, context)


def test_pilot_has_installed_builder_and_final_stages() -> None:
    rendered = _render("auto", producer=True)
    assert "AS builder-installed" in rendered
    assert "FROM builder-installed AS builder" in rendered
    assert "FROM runtime AS final" in rendered
    assert rendered.index("AS builder-installed") < rendered.index(
        "FROM builder-installed AS builder"
    )
    assert rendered.index("FROM builder-installed AS builder") < rendered.index(
        " gc -y"
    )
    assert "COPY --from=builder " in rendered


def test_auto_consumer_mounts_both_read_only_stores_and_can_fallback() -> None:
    rendered = _render("auto")
    assert not any(line.startswith("&&") for line in rendered.splitlines())
    install = next(
        block for block in rendered.split("RUN ") if "--use-buildcache auto" in block
    )
    assert "source=assets/spack-buildcache,target=/opt/spack-buildcache,readonly" in install
    assert "source=assets/spack-mirror,target=/opt/spack-mirror,readonly" in install
    assert "mirror add --scope env:cp2k-env --unsigned binary-cache" in install
    assert "install --only-concrete" in install
    assert "--only-concrete" in install
    assert install.index("mirror add") < install.index("install --only-concrete")


def test_only_consumer_has_no_source_mount_or_source_install_branch() -> None:
    rendered = _render("only")
    assert not any(line.startswith("&&") for line in rendered.splitlines())
    install = next(
        block for block in rendered.split("RUN ") if "--use-buildcache only" in block
    )
    assert "source=assets/spack-buildcache,target=/opt/spack-buildcache,readonly" in install
    assert "spack-mirror" not in install
    assert "libint" not in install
    assert "mirror add --scope env:cp2k-env --unsigned binary-cache" in install
    assert install.index("mirror add") < install.index("install --only-concrete")
    assert "--only-concrete" in install


def test_producer_auto_install_reuses_buildcache_with_padding() -> None:
    """Producer keeps padded_length but may extract published hashes."""
    rendered = _render("auto", producer=True)
    assert "padded_length:128" in rendered
    assert not any(line.startswith("&&") for line in rendered.splitlines())
    install = next(
        block for block in rendered.split("RUN ") if "--use-buildcache auto" in block
    )
    assert "source=assets/spack-buildcache,target=/opt/spack-buildcache,readonly" in install
    assert "source=assets/spack-mirror,target=/opt/spack-mirror,readonly" in install
    assert "--use-buildcache never" not in install
    assert "libint" not in install


def test_buildcache_build_renders_producer_with_auto_policy(
    tmp_path: Path,
) -> None:
    from hpc_cf.environment import BuildcachePolicy
    from hpc_cf.workflows import BuildcacheRequest, BuildcacheService

    layout, _, resolved = _producer_service_inputs(tmp_path)
    captured: dict[str, object] = {}

    def fake_generate(**kwargs: object) -> Path:
        captured.update(kwargs)
        return tmp_path / "Dockerfile"

    with (
        _producer_patches(resolved),
        patch("hpc_cf.template.generate_dockerfile", side_effect=fake_generate),
        patch("hpc_cf.sif.build_docker_stage"),
        patch(
            "hpc_cf.buildcache.publish",
            return_value=(subprocess.CompletedProcess([], 0, stdout=""), 1),
        ),
        patch("hpc_cf.buildcache.promote_producer_image"),
        patch(
            "hpc_cf.buildcache.inspect_image_digest",
            return_value="sha256:installed",
        ),
        patch("hpc_cf.buildcache.remove_temporary_image"),
    ):
        assert BuildcacheService(layout).run(
            BuildcacheRequest(action="build", env=PILOT)
        ) == 0

    assert captured["buildcache_policy"] == BuildcachePolicy.AUTO.value
    assert captured["buildcache_producer"] is True


@pytest.mark.parametrize("spack_version", ("1.1.0", "1.1.1", "1.2.0"))
def test_buildcache_register_uses_explicit_named_environment_scope(
    spack_version: str,
) -> None:
    partial = ROOT / "templates" / "partials" / "spack_buildcache_register.j2"
    rendered = render_template(
        partial,
        {
            "spack_env_name": "cp2k-env",
            "spack_version": spack_version,
        },
    )
    assert rendered.strip() == (
        "spack -e cp2k-env mirror add --scope env:cp2k-env "
        "--unsigned binary-cache \\\n"
        "        file:///opt/spack-buildcache"
    )
    assert "--scope env " not in rendered


def test_publisher_script_pushes_indexes_then_checks_explicit_non_external_hashes() -> None:
    from hpc_cf.buildcache_ops import build_publish_script
    from hpc_cf.config import IMAGE_SPACK_ROOT, IMAGE_SPACK_SETUP_SCRIPT

    script = build_publish_script(
        env_name="cp2k-env",
        store_path="/work/assets/spack-buildcache",
    )
    assert IMAGE_SPACK_ROOT == "/opt/spack-exe"
    assert IMAGE_SPACK_SETUP_SCRIPT == (
        "/opt/spack-exe/share/spack/setup-env.sh"
    )
    assert script.startswith(f"set -e\nset -o pipefail\nsource {IMAGE_SPACK_SETUP_SCRIPT}\n")
    push = script.index("buildcache push")
    index = script.index("buildcache update-index")
    check = script.index("buildcache check")
    assert push < index < check
    assert "--force" not in script
    assert "spec.external" in script
    assert "spec.installed" in script
    assert '"${installed_hashes[@]}"' in script
    assert '"${spec_hashes[@]}"' in script
    assert "HPC_CF_PUSHED_SPEC_COUNT=" in script
    assert "HPC_CF_PARTIAL_PUBLISH=1" in script
    assert (
        "spack buildcache update-index "
        "file:///work/assets/spack-buildcache"
    ) in script
    assert "--mirror-url file:///work/assets/spack-buildcache" in script


def test_producer_install_soft_fails_to_keep_partial_image() -> None:
    rendered = _render("auto", producer=True)
    install = next(
        block for block in rendered.split("RUN ") if "--use-buildcache auto" in block
    )
    assert "HPC_CF_PARTIAL_INSTALL=1" in install
    assert "HPC_CF_INSTALLED_SPEC_COUNT=" in install
    assert "keeping image for partial buildcache push" in install


def test_consumer_install_still_hard_fails() -> None:
    rendered = _render("auto", producer=False)
    install = next(
        block for block in rendered.split("RUN ") if "--use-buildcache auto" in block
    )
    assert "HPC_CF_PARTIAL_INSTALL=1" not in install


def test_verify_script_uses_shipped_image_spack_setup_contract_by_default() -> None:
    from hpc_cf.buildcache_ops import build_verify_script
    from hpc_cf.config import IMAGE_SPACK_ROOT, IMAGE_SPACK_SETUP_SCRIPT

    script = build_verify_script(
        env_name="cp2k-env",
        store_path="/work/assets/spack-buildcache",
    )

    assert IMAGE_SPACK_SETUP_SCRIPT == f"{IMAGE_SPACK_ROOT}/share/spack/setup-env.sh"
    assert script.startswith(f"set -e\nset -o pipefail\nsource {IMAGE_SPACK_SETUP_SCRIPT}\n")


def test_spack_install_partial_matches_image_spack_root_contract() -> None:
    from hpc_cf.config import IMAGE_SPACK_ROOT

    partial = ROOT / "templates" / "partials" / "spack_install.j2"
    text = partial.read_text(encoding="utf-8")

    assert f"ENV SPACK_ROOT={IMAGE_SPACK_ROOT}" in text


@pytest.mark.parametrize(
    ("engine", "expects_keep_id"),
    [("podman", True), ("docker", False)],
)
def test_publisher_container_uses_engine_compatible_user_namespace(
    tmp_path: Path,
    engine: str,
    expects_keep_id: bool,
) -> None:
    from hpc_cf.buildcache import run_in_installed_image

    layout = ProjectLayout(project_root=tmp_path)
    layout.spack_buildcache_dir.mkdir(parents=True)
    completed = subprocess.CompletedProcess([], 0, stdout="")
    with patch("hpc_cf.buildcache.subprocess.run", return_value=completed) as run:
        run_in_installed_image(
            engine=engine,
            image_ref="image:installed",
            layout=layout,
            script="true",
            writable=True,
        )

    command = run.call_args.args[0]
    assert ("--userns=keep-id:uid=0,gid=0" in command) is expects_keep_id
    assert [
        command[index + 1]
        for index, argument in enumerate(command)
        if argument == "--env"
    ] == [
        "HOME=/root",
        "SPACK_USER_CACHE_PATH=/root/.spack",
        "SPACK_USER_CONFIG_PATH=/tmp/hpc-cf-spack-config",
    ]
    mounts = [
        command[index + 1]
        for index, argument in enumerate(command)
        if argument == "-v"
    ]
    assert mounts == [
        (
            f"{layout.spack_buildcache_dir}:"
            f"{layout.container_publisher_buildcache_dir()}:rw"
        )
    ]
    assert command[-3:] == [
        "bash",
        "-lc",
        (
            'mkdir -p "$SPACK_USER_CONFIG_PATH"\n'
            'test -w "$HOME"\n'
            'test -w "$SPACK_USER_CACHE_PATH"\n'
            'test -d "$SPACK_USER_CACHE_PATH/package_repos"\n'
            'test -w "$SPACK_USER_CONFIG_PATH"\n'
            "true"
        ),
    ]


def test_checker_container_keeps_buildcache_read_only(tmp_path: Path) -> None:
    from hpc_cf.buildcache import run_in_installed_image

    layout = ProjectLayout(project_root=tmp_path)
    layout.spack_buildcache_dir.mkdir(parents=True)
    completed = subprocess.CompletedProcess([], 0, stdout="")
    with patch("hpc_cf.buildcache.subprocess.run", return_value=completed) as run:
        run_in_installed_image(
            engine="podman",
            image_ref="image:installed",
            layout=layout,
            script="true",
            writable=False,
        )

    command = run.call_args.args[0]
    mounts = [
        command[index + 1]
        for index, argument in enumerate(command)
        if argument == "-v"
    ]
    assert mounts == [
        (
            f"{layout.spack_buildcache_dir}:"
            f"{layout.container_publisher_buildcache_dir()}:ro"
        )
    ]


def test_publisher_container_uses_configured_hpc_timeout(tmp_path: Path) -> None:
    from hpc_cf.buildcache import run_in_installed_image

    layout = ProjectLayout(project_root=tmp_path)
    layout.spack_buildcache_dir.mkdir(parents=True)
    completed = subprocess.CompletedProcess([], 0, stdout="")
    with patch("hpc_cf.buildcache.subprocess.run", return_value=completed) as run:
        run_in_installed_image(
            engine="podman",
            image_ref="image:installed",
            layout=layout,
            script="true",
            timeout_seconds=43200,
        )

    assert run.call_args.kwargs["timeout"] == 43200


def test_build_docker_like_builds_all_three_tags_and_preserves_builder_semantics(
    tmp_path: Path,
) -> None:
    from hpc_cf.sif import build_docker_like

    dockerfile = tmp_path / "Dockerfile"
    dockerfile.write_text(
        "FROM base AS builder-installed\n"
        "FROM builder-installed AS builder\n"
        "FROM builder AS final\n",
        encoding="utf-8",
    )
    commands: list[list[str]] = []
    with (
        patch("hpc_cf.sif.check_command_exists", return_value=True),
        patch("hpc_cf.sif.run_cmd", side_effect=lambda cmd, **_: commands.append(cmd)),
    ):
        build_docker_like(
            dockerfile=dockerfile,
            image="cp2k",
            tag="2025.2",
            engine="podman",
            network_host=True,
        )

    assert [cmd[cmd.index("--target") + 1] for cmd in commands[:2]] == [
        "builder-installed",
        "builder",
    ]
    assert "cp2k:2025.2-installed" in commands[0]
    assert "cp2k:2025.2-builder" in commands[1]
    assert "cp2k:2025.2" in commands[2]


def test_build_cli_preserves_unspecified_policy_and_rejects_explicit_only() -> None:
    from hpc_cf.cli import build_parser, run_new_cli
    from hpc_cf.environment import BuildcachePolicy
    from hpc_cf.workflows import BuildService

    args = build_parser().parse_args(["build", "--env", PILOT])
    assert args.buildcache is None
    dockerfile_args = build_parser().parse_args(
        ["dockerfile", "--env", PILOT, "--buildcache", "only"]
    )
    assert dockerfile_args.buildcache is BuildcachePolicy.ONLY

    with patch.object(BuildService, "run") as run:
        with pytest.raises(SystemExit):
            run_new_cli(
                [
                    "build",
                    "--env",
                    PILOT,
                    "--buildcache",
                    "only",
                    "--allow-reconcretize",
                ]
            )
    run.assert_not_called()


def test_buildcache_cli_dispatches_build_verify_resume_and_status() -> None:
    from hpc_cf.cli import run_new_cli
    from hpc_cf.workflows import BuildcacheRequest, BuildcacheService

    for action in ("build", "verify", "resume", "status"):
        with patch.object(BuildcacheService, "run", return_value=0) as run:
            argv = ["buildcache", action]
            if action != "status":
                argv += ["--env", PILOT]
            assert run_new_cli(argv) == 0
        request = run.call_args.args[0]
        assert isinstance(request, BuildcacheRequest)
        assert request.action == action
        assert request.env == (None if action == "status" else PILOT)


def test_buildcache_status_rejects_unused_build_options() -> None:
    from hpc_cf.cli import build_parser

    with pytest.raises(SystemExit):
        build_parser().parse_args(
            ["buildcache", "status", "--env", PILOT]
        )
    with pytest.raises(SystemExit):
        build_parser().parse_args(
            ["buildcache", "status", "--engine", "docker"]
        )


def test_buildcache_resume_rejects_arbitrary_image_reference() -> None:
    from hpc_cf.cli import build_parser

    with pytest.raises(SystemExit):
        build_parser().parse_args(
            ["buildcache", "resume", "--env", PILOT, "--image", "arbitrary"]
        )


def test_buildcache_service_rejects_unknown_action_before_resolution(
    tmp_path: Path,
) -> None:
    from hpc_cf.workflows import BuildcacheRequest, BuildcacheService

    with (
        patch("hpc_cf.template.resolve_build_input") as resolve,
        pytest.raises(ValueError, match="Unsupported buildcache action"),
    ):
        BuildcacheService(ProjectLayout(project_root=tmp_path)).run(
            BuildcacheRequest(action="typo", env=PILOT)
        )
    resolve.assert_not_called()


def test_buildcache_service_resolves_image_digest_under_publisher_lock(
    tmp_path: Path,
) -> None:
    from hpc_cf.workflows import BuildcacheRequest, BuildcacheService

    layout = ProjectLayout(project_root=tmp_path)
    env_dir = layout.spack_envs_dir / PILOT / "spack-env-file"
    env_dir.mkdir(parents=True)
    (env_dir / "spack.lock").write_text('{"lock": true}\n', encoding="utf-8")
    spec = SimpleNamespace(
        spack=SimpleNamespace(
            buildcache=SimpleNamespace(enabled=True, padded_length=128),
            version="1.1.0",
            env_name="cp2k-env",
        ),
        images=SimpleNamespace(builder="debian:13"),
    )
    resolved = SimpleNamespace(environment_dir=env_dir, environment_spec=spec)

    def inspect_under_lock(**_: object) -> str:
        probe = subprocess.run(
            [
                "python3",
                "-c",
                (
                    "import fcntl;"
                    f"f=open({str(layout.buildcache_lock_path)!r},'a+');"
                    "fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB)"
                ),
            ],
            check=False,
        )
        assert probe.returncode != 0
        return "sha256:installed"

    completed = subprocess.CompletedProcess([], 0, stdout="")
    with (
        patch("hpc_cf.template.resolve_build_input", return_value=resolved),
        patch(
            "hpc_cf.template.resolve_image_and_tag",
            return_value=("cp2k", "2025.2"),
        ),
        patch(
            "hpc_cf.buildcache.inspect_image_digest",
            side_effect=inspect_under_lock,
        ),
        patch(
            "hpc_cf.buildcache.verify",
            return_value=(completed, 1),
        ),
    ):
        assert (
            BuildcacheService(layout).run(
                BuildcacheRequest(action="verify", env=PILOT)
            )
            == 0
        )


def test_buildcache_build_runs_build_input_preflight_before_image_build(
    tmp_path: Path,
) -> None:
    from hpc_cf.workflows import BuildcacheRequest, BuildcacheService

    layout = ProjectLayout(project_root=tmp_path)
    env_dir = layout.spack_envs_dir / PILOT / "spack-env-file"
    env_dir.mkdir(parents=True)
    (env_dir / "spack.lock").write_text('{"lock": true}\n', encoding="utf-8")
    spec = SimpleNamespace(
        spack=SimpleNamespace(
            buildcache=SimpleNamespace(enabled=True),
        ),
    )
    resolved = SimpleNamespace(environment_dir=env_dir, environment_spec=spec)

    with (
        patch("hpc_cf.template.resolve_build_input", return_value=resolved),
        patch(
            "hpc_cf.template.resolve_image_and_tag",
            return_value=("cp2k", "2025.2"),
        ),
        patch(
            "hpc_cf.env.run_static_checks",
            side_effect=RuntimeError("preflight failed"),
        ) as preflight,
        patch("hpc_cf.template.generate_dockerfile") as generate,
        pytest.raises(RuntimeError, match="preflight failed"),
    ):
        BuildcacheService(layout).run(
            BuildcacheRequest(action="build", env=PILOT)
        )

    preflight.assert_called_once()
    generate.assert_not_called()


def test_buildcache_build_requires_verified_source_mirror_before_image_build(
    tmp_path: Path,
) -> None:
    from hpc_cf.workflows import BuildcacheRequest, BuildcacheService

    layout = ProjectLayout(project_root=tmp_path)
    env_dir = layout.spack_envs_dir / PILOT / "spack-env-file"
    env_dir.mkdir(parents=True)
    (env_dir / "spack.lock").write_text('{"lock": true}\n', encoding="utf-8")
    spec = SimpleNamespace(
        spack=SimpleNamespace(
            buildcache=SimpleNamespace(enabled=True),
            version="1.1.0",
        ),
    )
    resolved = SimpleNamespace(environment_dir=env_dir, environment_spec=spec)

    with (
        patch("hpc_cf.template.resolve_build_input", return_value=resolved),
        patch(
            "hpc_cf.template.resolve_image_and_tag",
            return_value=("cp2k", "2025.2"),
        ),
        patch("hpc_cf.env.run_static_checks"),
        patch("hpc_cf.template.generate_dockerfile") as generate,
        patch("hpc_cf.sif.build_docker_stage") as build_stage,
        pytest.raises(RuntimeError, match="source mirror is missing"),
    ):
        BuildcacheService(layout).run(
            BuildcacheRequest(action="build", env=PILOT)
        )

    generate.assert_not_called()
    build_stage.assert_not_called()


def test_source_mirror_gate_accepts_matching_verify_manifest(
    tmp_path: Path,
) -> None:
    import hashlib

    from hpc_cf.buildcache import require_verified_source_mirror

    layout = ProjectLayout(project_root=tmp_path)
    lock = tmp_path / "spack.lock"
    lock.write_text('{"lock": true}\n', encoding="utf-8")
    payload = layout.spack_mirror_dir / "pkg" / "archive.tar.gz"
    payload.parent.mkdir(parents=True)
    payload.write_bytes(b"source")
    run_dir = layout.mirror_runs_dir / "run"
    run_dir.mkdir(parents=True)
    (run_dir / "mirror-verify.log").write_text("verified\n", encoding="utf-8")
    manifest = run_dir / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "env": PILOT,
                "spack_version": "1.1.0",
                "lock_hash": hashlib.sha256(lock.read_bytes()).hexdigest(),
                "status": "success",
                "stats": {"failed": 0},
            }
        ),
        encoding="utf-8",
    )

    assert require_verified_source_mirror(
        layout,
        env_name=PILOT,
        lock_path=lock,
        spack_version="1.1.0",
    ) == manifest
    lock.write_text('{"lock": "changed"}\n', encoding="utf-8")
    with pytest.raises(RuntimeError, match="exact environment.*lock SHA"):
        require_verified_source_mirror(
            layout,
            env_name=PILOT,
            lock_path=lock,
            spack_version="1.1.0",
        )


def test_source_mirror_gate_rejects_newer_failed_matching_run(
    tmp_path: Path,
) -> None:
    import hashlib

    from hpc_cf.buildcache import require_verified_source_mirror

    layout = ProjectLayout(project_root=tmp_path)
    lock = tmp_path / "spack.lock"
    lock.write_text('{"lock": true}\n', encoding="utf-8")
    payload = layout.spack_mirror_dir / "pkg" / "archive.tar.gz"
    payload.parent.mkdir(parents=True)
    payload.write_bytes(b"source")
    base = {
        "env": PILOT,
        "spack_version": "1.1.0",
        "lock_hash": hashlib.sha256(lock.read_bytes()).hexdigest(),
        "stats": {"failed": 0},
    }
    for name, status in (("001-success", "success"), ("002-failed", "failed")):
        run_dir = layout.mirror_runs_dir / name
        run_dir.mkdir(parents=True)
        (run_dir / "mirror-verify.log").write_text("verify\n")
        (run_dir / "manifest.json").write_text(
            json.dumps({**base, "status": status}),
            encoding="utf-8",
        )

    with pytest.raises(RuntimeError, match="latest matching assets run"):
        require_verified_source_mirror(
            layout,
            env_name=PILOT,
            lock_path=lock,
            spack_version="1.1.0",
        )


def test_buildcache_service_preserves_failed_operation_output(
    tmp_path: Path,
) -> None:
    from hpc_cf.workflows import BuildcacheRequest, BuildcacheService

    layout = ProjectLayout(project_root=tmp_path)
    env_dir = layout.spack_envs_dir / PILOT / "spack-env-file"
    env_dir.mkdir(parents=True)
    (env_dir / "spack.lock").write_text('{"lock": true}\n', encoding="utf-8")
    spec = SimpleNamespace(
        spack=SimpleNamespace(
            buildcache=SimpleNamespace(enabled=True, padded_length=128),
            version="1.1.0",
            env_name="cp2k-env",
        ),
        images=SimpleNamespace(builder="debian:13"),
    )
    resolved = SimpleNamespace(environment_dir=env_dir, environment_spec=spec)
    failure = subprocess.CalledProcessError(
        1,
        ["spack", "buildcache", "check"],
        output="explicit check failed\n",
    )

    with (
        patch("hpc_cf.template.resolve_build_input", return_value=resolved),
        patch(
            "hpc_cf.template.resolve_image_and_tag",
            return_value=("cp2k", "2025.2"),
        ),
        patch(
            "hpc_cf.buildcache.inspect_image_digest",
            return_value="sha256:installed",
        ),
        patch("hpc_cf.buildcache.verify", side_effect=failure),
        pytest.raises(subprocess.CalledProcessError),
    ):
        BuildcacheService(layout).run(
            BuildcacheRequest(action="verify", env=PILOT)
        )

    runs = list(layout.buildcache_runs_dir.iterdir())
    assert len(runs) == 1
    assert (runs[0] / "verify.log").read_text(encoding="utf-8") == (
        "explicit check failed\n"
    )
    assert SharedBuildcacheStore(layout).read_health()["healthy"] is False


def test_publisher_timeout_releases_lock_marks_unhealthy_and_keeps_log(
    tmp_path: Path,
) -> None:
    from hpc_cf.workflows import BuildcacheRequest, BuildcacheService

    layout = ProjectLayout(project_root=tmp_path)
    env_dir = layout.spack_envs_dir / PILOT / "spack-env-file"
    env_dir.mkdir(parents=True)
    (env_dir / "spack.lock").write_text('{"lock": true}\n', encoding="utf-8")
    spec = SimpleNamespace(
        spack=SimpleNamespace(
            buildcache=SimpleNamespace(enabled=True, padded_length=128),
            version="1.1.0",
            env_name="cp2k-env",
        ),
    )
    resolved = SimpleNamespace(environment_dir=env_dir, environment_spec=spec)
    timeout = subprocess.TimeoutExpired(
        ["podman", "run"], 99, output="long push timed out\n"
    )

    with (
        patch("hpc_cf.template.resolve_build_input", return_value=resolved),
        patch(
            "hpc_cf.template.resolve_image_and_tag",
            return_value=("cp2k", "2025.2"),
        ),
        patch(
            "hpc_cf.buildcache.inspect_image_digest",
            return_value="sha256:installed",
        ),
        patch("hpc_cf.buildcache.verify", side_effect=timeout) as verify,
        pytest.raises(subprocess.TimeoutExpired),
    ):
        BuildcacheService(layout).run(
            BuildcacheRequest(
                action="verify",
                env=PILOT,
                operation_timeout_seconds=99,
            )
        )

    assert verify.call_args.kwargs["timeout_seconds"] == 99
    run_dir = next(layout.buildcache_runs_dir.iterdir())
    assert run_dir.joinpath("verify.log").read_text(encoding="utf-8") == (
        "long push timed out\n"
    )
    assert SharedBuildcacheStore(layout).read_health()["healthy"] is False
    probe = subprocess.run(
        [
            "python3",
            "-c",
            (
                "import fcntl;"
                f"f=open({str(layout.buildcache_lock_path)!r},'a+');"
                "fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB)"
            ),
        ],
        check=False,
    )
    assert probe.returncode == 0


def test_auto_missing_or_unhealthy_store_uses_source_but_only_fails(
    tmp_path: Path,
) -> None:
    from hpc_cf.buildcache import resolve_consumer_policy
    from hpc_cf.environment import BuildcachePolicy

    layout = ProjectLayout(project_root=tmp_path)
    store = SharedBuildcacheStore(layout)
    assert resolve_consumer_policy(BuildcachePolicy.AUTO, store) is BuildcachePolicy.NEVER
    with pytest.raises(RuntimeError, match="buildcache"):
        resolve_consumer_policy(BuildcachePolicy.ONLY, store)

    store.ensure_store_root()
    store.mark_unhealthy(run_id="r", failed_step="check", error="bad")
    assert resolve_consumer_policy(BuildcachePolicy.AUTO, store) is BuildcachePolicy.NEVER
    with pytest.raises(RuntimeError, match="unhealthy"):
        resolve_consumer_policy(BuildcachePolicy.ONLY, store)

    coverage = layout.buildcache_coverage_dir / "record.json"
    coverage.parent.mkdir(parents=True, exist_ok=True)
    coverage.write_text("{}\n", encoding="utf-8")
    store.mark_healthy(run_id="r2", coverage_path=coverage)
    assert resolve_consumer_policy(BuildcachePolicy.AUTO, store) is BuildcachePolicy.AUTO
    assert resolve_consumer_policy(BuildcachePolicy.ONLY, store) is BuildcachePolicy.ONLY


def test_disabled_environment_never_uses_global_buildcache(tmp_path: Path) -> None:
    from hpc_cf.buildcache import resolve_consumer_policy
    from hpc_cf.environment import BuildcachePolicy

    layout = ProjectLayout(project_root=tmp_path)
    store = SharedBuildcacheStore(layout)
    store.ensure_store_root()
    coverage = layout.buildcache_coverage_dir / "record.json"
    coverage.parent.mkdir(parents=True)
    coverage.write_text("{}\n", encoding="utf-8")
    store.mark_healthy(run_id="r", coverage_path=coverage)

    assert (
        resolve_consumer_policy(BuildcachePolicy.AUTO, store, enabled=False)
        is BuildcachePolicy.NEVER
    )
    with pytest.raises(RuntimeError, match="not enabled"):
        resolve_consumer_policy(BuildcachePolicy.ONLY, store, enabled=False)


@pytest.mark.parametrize(
    ("env_policy", "cli_policy", "expected"),
    [
        ("never", None, "never"),
        ("only", None, "only"),
        ("never", "auto", "auto"),
        ("only", "never", "never"),
    ],
)
def test_environment_policy_is_default_and_cli_is_override(
    tmp_path: Path,
    env_policy: str,
    cli_policy: str | None,
    expected: str,
) -> None:
    from hpc_cf.environment import BuildcachePolicy
    from hpc_cf.workflows import BuildRequest, BuildService

    layout = ProjectLayout(project_root=tmp_path)
    store = SharedBuildcacheStore(layout)
    store.ensure_store_root()
    coverage = layout.buildcache_coverage_dir / "record.json"
    coverage.parent.mkdir(parents=True)
    coverage.write_text("{}\n", encoding="utf-8")
    store.mark_healthy(run_id="r", coverage_path=coverage)
    rendered: list[str] = []

    with (
        patch("hpc_cf.env.run_static_checks"),
        patch("hpc_cf.template.resolve_build_input") as resolved,
        patch(
            "hpc_cf.template.generate_dockerfile",
            side_effect=lambda **kwargs: rendered.append(
                str(kwargs["buildcache_policy"])
            ),
        ),
    ):
        resolved.return_value.environment_dir = tmp_path
        resolved.return_value.environment_spec = SimpleNamespace(
            spack=SimpleNamespace(
                buildcache=SimpleNamespace(
                    enabled=True,
                    policy=BuildcachePolicy.parse(env_policy),
                ),
            )
        )
        BuildService(layout).run(
            BuildRequest(
                app_version=PILOT,
                render_only=True,
                buildcache=(
                    BuildcachePolicy.parse(cli_policy)
                    if cli_policy is not None
                    else None
                ),
            )
        )

    assert rendered == [expected]


def test_disabled_environment_default_never_but_cli_only_still_rejected(
    tmp_path: Path,
) -> None:
    from hpc_cf.environment import BuildcachePolicy
    from hpc_cf.workflows import BuildRequest, BuildService

    layout = ProjectLayout(project_root=tmp_path)
    resolved = SimpleNamespace(
        environment_dir=tmp_path,
        environment_spec=SimpleNamespace(
            spack=SimpleNamespace(
                buildcache=SimpleNamespace(
                    enabled=False,
                    policy=BuildcachePolicy.NEVER,
                ),
            )
        ),
    )
    with (
        patch("hpc_cf.template.resolve_build_input", return_value=resolved),
        patch("hpc_cf.env.run_static_checks"),
        patch("hpc_cf.template.generate_dockerfile"),
    ):
        assert BuildService(layout).run(
            BuildRequest(app_version="disabled", render_only=True)
        ) == 0
        with pytest.raises(RuntimeError, match="not enabled"):
            BuildService(layout).run(
                BuildRequest(
                    app_version="disabled",
                    render_only=True,
                    buildcache=BuildcachePolicy.ONLY,
                )
            )


def test_only_coverage_matches_lock_spack_padding_and_image_digest(
    tmp_path: Path,
) -> None:
    from hpc_cf.buildcache import (
        collect_environment_provenance,
        coverage_path_for_lock,
        require_coverage,
    )

    layout = ProjectLayout(project_root=tmp_path)
    lock = tmp_path / "spack.lock"
    lock.write_text(
        json.dumps(
            {
                "concrete_specs": {
                    "abc": {
                        "arch": {
                            "platform_os": "debian13",
                            "target": {"name": "x86_64_v3"},
                        },
                        "compiler": {"name": "gcc", "version": "14.2.0"},
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "spack.yaml").write_text(
        "spack:\n  repos:\n    builtin:\n      commit: abc123\n",
        encoding="utf-8",
    )
    provenance = collect_environment_provenance(lock, tmp_path)
    assert provenance == {
        "operating_systems": ["debian13"],
        "targets": ["x86_64_v3"],
        "compilers": ["gcc@14.2.0"],
        "repo_commits": {"builtin": "abc123"},
    }
    record = {
        "schema_version": 2,
        "check_returncode": 0,
        "coverage": "non_external",
        "external_specs_excluded": True,
        "spack_version": "1.1.0",
        "builder_image_digest": "sha256:good",
        "padded_length": 128,
        "environment_provenance": provenance,
    }
    path = coverage_path_for_lock(layout, lock)
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(record), encoding="utf-8")

    assert require_coverage(
        layout,
        lock,
        spack_version="1.1.0",
        builder_image="sha256:good",
        padded_length=128,
        environment_provenance=provenance,
    ) == record
    with pytest.raises(RuntimeError, match="incompatible"):
        require_coverage(
            layout,
            lock,
            spack_version="1.1.0",
            builder_image="sha256:other",
            padded_length=128,
            environment_provenance=provenance,
        )
    with pytest.raises(RuntimeError, match="incompatible"):
        require_coverage(
            layout,
            lock,
            spack_version="1.1.0",
            builder_image="sha256:good",
            padded_length=128,
            environment_provenance={**provenance, "targets": ["x86_64_v4"]},
        )


def test_provenance_models_unavailable_fields_as_unknown(tmp_path: Path) -> None:
    from hpc_cf.buildcache import collect_environment_provenance

    lock = tmp_path / "spack.lock"
    lock.write_text('{"concrete_specs": {"abc": {"name": "pkg"}}}\n')

    assert collect_environment_provenance(lock, tmp_path) == {
        "operating_systems": None,
        "targets": None,
        "compilers": None,
        "repo_commits": None,
    }


def test_build_service_holds_shared_lock_across_consumer_build(tmp_path: Path) -> None:
    from hpc_cf.environment import BuildcachePolicy
    from hpc_cf.workflows import BuildRequest, BuildService

    layout = ProjectLayout(project_root=tmp_path)
    store = SharedBuildcacheStore(layout)
    store.ensure_store_root()
    coverage = layout.buildcache_coverage_dir / "lock.json"
    coverage.parent.mkdir(parents=True)
    coverage.write_text("{}\n", encoding="utf-8")
    store.mark_healthy(run_id="r", coverage_path=coverage)

    entered = False

    def fake_build(**_: object) -> None:
        nonlocal entered
        entered = True
        lock_probe = subprocess.run(
            [
                "python3",
                "-c",
                (
                    "import fcntl,sys;"
                    f"f=open({str(layout.buildcache_lock_path)!r},'a+');"
                    "fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB)"
                ),
            ],
            check=False,
        )
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
                buildcache=SimpleNamespace(enabled=True),
            )
        )
        BuildService(layout=layout).run(
            BuildRequest(
                app_version=PILOT,
                buildcache=BuildcachePolicy.AUTO,
            )
        )
    assert entered


def test_build_service_rechecks_health_after_acquiring_consumer_lock(
    tmp_path: Path,
) -> None:
    from contextlib import contextmanager

    from hpc_cf.environment import BuildcachePolicy
    from hpc_cf.workflows import BuildRequest, BuildService

    layout = ProjectLayout(project_root=tmp_path)
    store = SharedBuildcacheStore(layout)
    store.ensure_store_root()
    coverage = layout.buildcache_coverage_dir / "lock.json"
    coverage.parent.mkdir(parents=True)
    coverage.write_text("{}\n", encoding="utf-8")
    store.mark_healthy(run_id="r", coverage_path=coverage)
    rendered_policies: list[str] = []

    @contextmanager
    def publisher_won_race():
        store.mark_unhealthy(run_id="r2", failed_step="push", error="failed")
        yield

    def fake_generate(**kwargs: object) -> Path:
        rendered_policies.append(str(kwargs["buildcache_policy"]))
        return Path("Dockerfile")

    with (
        patch("hpc_cf.env.run_static_checks"),
        patch("hpc_cf.template.resolve_build_input") as resolved,
        patch("hpc_cf.template.resolve_image_and_tag", return_value=("img", "tag")),
        patch("hpc_cf.template.generate_dockerfile", side_effect=fake_generate),
        patch("hpc_cf.sif.build_docker_like"),
        patch.object(store, "consumer_lock", side_effect=publisher_won_race),
        patch("hpc_cf.workflows.SharedBuildcacheStore", return_value=store),
    ):
        resolved.return_value.environment_dir = tmp_path
        resolved.return_value.environment_spec = SimpleNamespace(
            spack=SimpleNamespace(
                buildcache=SimpleNamespace(enabled=True),
            )
        )
        BuildService(layout=layout).run(
            BuildRequest(
                app_version=PILOT,
                buildcache=BuildcachePolicy.AUTO,
            )
        )

    assert rendered_policies == ["never"]


def test_producer_auto_only_lifecycle_preserves_producer_digest(
    tmp_path: Path,
) -> None:
    from hpc_cf.environment import BuildcachePolicy
    from hpc_cf.workflows import (
        BuildcacheRequest,
        BuildcacheService,
        BuildRequest,
        BuildService,
    )

    layout = ProjectLayout(project_root=tmp_path)
    env_dir = layout.spack_envs_dir / PILOT / "spack-env-file"
    env_dir.mkdir(parents=True)
    (env_dir / "spack.lock").write_text('{"lock": true}\n', encoding="utf-8")
    spec = SimpleNamespace(
        spack=SimpleNamespace(
            buildcache=SimpleNamespace(enabled=True, padded_length=128),
            version="1.1.0",
            env_name="cp2k-env",
        ),
        images=SimpleNamespace(builder="debian:13"),
    )
    resolved = SimpleNamespace(environment_dir=env_dir, environment_spec=spec)
    images: dict[str, str] = {}
    inspected_refs: list[str] = []
    verified_refs: list[str] = []
    producer_ref = "cp2k:2025.2-buildcache-producer"
    installed_ref = "cp2k:2025.2-installed"

    def fake_build_stage(*, image_ref: str, **_: object) -> None:
        images[image_ref] = "sha256:producer"

    def fake_promote(*, temporary_ref: str, stable_ref: str, **_: object) -> None:
        images[stable_ref] = images[temporary_ref]

    def fake_remove(*, image_ref: str, **_: object) -> None:
        images.pop(image_ref, None)

    def fake_build_consumer(*, image: str, tag: str, **_: object) -> None:
        images[f"{image}:{tag}-installed"] = "sha256:consumer"

    def fake_inspect(*, image_ref: str, **_: object) -> str:
        inspected_refs.append(image_ref)
        return images[image_ref]

    def fake_verify(*, image_ref: str, **_: object):
        verified_refs.append(image_ref)
        return subprocess.CompletedProcess([], 0, stdout=""), 1

    with (
        patch("hpc_cf.template.resolve_build_input", return_value=resolved),
        patch(
            "hpc_cf.template.resolve_image_and_tag",
            return_value=("cp2k", "2025.2"),
        ),
        patch("hpc_cf.env.run_static_checks"),
        patch("hpc_cf.buildcache.require_verified_source_mirror"),
        patch(
            "hpc_cf.template.generate_dockerfile",
            return_value=tmp_path / "Dockerfile",
        ),
        patch("hpc_cf.sif.build_docker_stage", side_effect=fake_build_stage),
        patch(
            "hpc_cf.buildcache.promote_producer_image",
            side_effect=fake_promote,
        ),
        patch(
            "hpc_cf.buildcache.remove_temporary_image",
            side_effect=fake_remove,
        ),
        patch("hpc_cf.sif.build_docker_like", side_effect=fake_build_consumer),
        patch("hpc_cf.buildcache.inspect_image_digest", side_effect=fake_inspect),
        patch(
            "hpc_cf.buildcache.publish",
            return_value=(subprocess.CompletedProcess([], 0, stdout=""), 1),
        ),
        patch("hpc_cf.buildcache.verify", side_effect=fake_verify),
    ):
        assert BuildcacheService(layout).run(
            BuildcacheRequest(action="build", env=PILOT)
        ) == 0
        producer_digest = images[producer_ref]

        assert BuildService(layout).run(
            BuildRequest(
                app_version=PILOT,
                buildcache=BuildcachePolicy.AUTO,
            )
        ) == 0
        assert images[installed_ref] == "sha256:consumer"
        assert images[producer_ref] == producer_digest

        assert BuildService(layout).run(
            BuildRequest(
                app_version=PILOT,
                buildcache=BuildcachePolicy.ONLY,
            )
        ) == 0

    assert len(inspected_refs) == 3
    assert inspected_refs[0].startswith(
        "cp2k:2025.2-buildcache-producer-"
    )
    assert inspected_refs[1:] == [producer_ref, producer_ref]
    assert verified_refs[-1] == producer_ref


def test_interleaved_producers_use_unique_temporary_tags_and_serial_promotions(
    tmp_path: Path,
) -> None:
    from hpc_cf.workflows import BuildcacheRequest, BuildcacheService

    layout = ProjectLayout(project_root=tmp_path)
    env_dir = layout.spack_envs_dir / PILOT / "spack-env-file"
    env_dir.mkdir(parents=True)
    (env_dir / "spack.lock").write_text('{"lock": true}\n', encoding="utf-8")
    spec = SimpleNamespace(
        spack=SimpleNamespace(
            buildcache=SimpleNamespace(enabled=True, padded_length=128),
            version="1.1.0",
            env_name="cp2k-env",
        ),
    )
    resolved = SimpleNamespace(environment_dir=env_dir, environment_spec=spec)
    images: dict[str, str] = {}
    built_refs: list[str] = []
    promoted_refs: list[str] = []
    barrier = threading.Barrier(2)
    failures: list[BaseException] = []

    def fake_build(*, image_ref: str, **_: object) -> None:
        built_refs.append(image_ref)
        images[image_ref] = f"sha256:{len(built_refs)}"
        barrier.wait(timeout=2)

    def fake_promote(*, temporary_ref: str, stable_ref: str, **_: object) -> None:
        promoted_refs.append(temporary_ref)
        images[stable_ref] = images[temporary_ref]

    def run() -> None:
        try:
            BuildcacheService(layout).run(
                BuildcacheRequest(action="build", env=PILOT)
            )
        except BaseException as exc:
            failures.append(exc)

    completed = subprocess.CompletedProcess([], 0, stdout="")
    with (
        patch("hpc_cf.template.resolve_build_input", return_value=resolved),
        patch(
            "hpc_cf.template.resolve_image_and_tag",
            return_value=("cp2k", "2025.2"),
        ),
        patch("hpc_cf.env.run_static_checks"),
        patch("hpc_cf.buildcache.require_verified_source_mirror"),
        patch(
            "hpc_cf.template.generate_dockerfile",
            return_value=tmp_path / "Dockerfile",
        ),
        patch("hpc_cf.sif.build_docker_stage", side_effect=fake_build),
        patch(
            "hpc_cf.buildcache.promote_producer_image",
            side_effect=fake_promote,
        ),
        patch(
            "hpc_cf.buildcache.inspect_image_digest",
            side_effect=lambda *, image_ref, **_: images[image_ref],
        ),
        patch(
            "hpc_cf.buildcache.publish",
            return_value=(completed, 1),
        ),
        patch(
            "hpc_cf.buildcache.remove_temporary_image",
            side_effect=lambda *, image_ref, **_: images.pop(image_ref, None),
        ),
    ):
        threads = [threading.Thread(target=run) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)

    assert failures == []
    assert len(set(built_refs)) == 2
    assert set(promoted_refs) == set(built_refs)
    assert all(ref not in images for ref in built_refs)
    assert "cp2k:2025.2-buildcache-producer" in images


def test_failed_interleaved_producer_cleans_only_its_temporary_tag(
    tmp_path: Path,
) -> None:
    from hpc_cf.workflows import BuildcacheRequest, BuildcacheService

    layout = ProjectLayout(project_root=tmp_path)
    env_dir = layout.spack_envs_dir / PILOT / "spack-env-file"
    env_dir.mkdir(parents=True)
    (env_dir / "spack.lock").write_text('{"lock": true}\n', encoding="utf-8")
    spec = SimpleNamespace(
        spack=SimpleNamespace(
            buildcache=SimpleNamespace(enabled=True, padded_length=128),
            version="1.1.0",
            env_name="cp2k-env",
        ),
    )
    resolved = SimpleNamespace(environment_dir=env_dir, environment_spec=spec)
    images: dict[str, str] = {}
    first_built = threading.Event()
    second_built = threading.Event()
    first_failed = threading.Event()
    cleaned: list[str] = []
    results: list[str] = []

    def fake_build(*, image_ref: str, **_: object) -> None:
        if not first_built.is_set():
            images[image_ref] = "sha256:first"
            first_built.set()
            second_built.wait(timeout=2)
        else:
            images[image_ref] = "sha256:second"
            second_built.set()
            first_failed.wait(timeout=2)

    def fake_promote(*, temporary_ref: str, stable_ref: str, **_: object) -> None:
        images[stable_ref] = images[temporary_ref]

    def fake_publish(*, image_ref: str, **_: object):
        if images[image_ref] == "sha256:first":
            first_failed.set()
            raise RuntimeError("first publish failed")
        return subprocess.CompletedProcess([], 0, stdout=""), 1

    def run(label: str) -> None:
        try:
            BuildcacheService(layout).run(
                BuildcacheRequest(action="build", env=PILOT)
            )
        except RuntimeError:
            results.append(f"{label}:failed")
        else:
            results.append(f"{label}:ok")

    with (
        patch("hpc_cf.template.resolve_build_input", return_value=resolved),
        patch(
            "hpc_cf.template.resolve_image_and_tag",
            return_value=("cp2k", "2025.2"),
        ),
        patch("hpc_cf.env.run_static_checks"),
        patch("hpc_cf.buildcache.require_verified_source_mirror"),
        patch(
            "hpc_cf.template.generate_dockerfile",
            return_value=tmp_path / "Dockerfile",
        ),
        patch("hpc_cf.sif.build_docker_stage", side_effect=fake_build),
        patch(
            "hpc_cf.buildcache.promote_producer_image",
            side_effect=fake_promote,
        ),
        patch(
            "hpc_cf.buildcache.inspect_image_digest",
            side_effect=lambda *, image_ref, **_: images[image_ref],
        ),
        patch("hpc_cf.buildcache.publish", side_effect=fake_publish),
        patch(
            "hpc_cf.buildcache.remove_temporary_image",
            side_effect=lambda *, image_ref, **_: (
                cleaned.append(image_ref),
                images.pop(image_ref, None),
            ),
        ),
    ):
        first = threading.Thread(target=run, args=("first",))
        second = threading.Thread(target=run, args=("second",))
        first.start()
        assert first_built.wait(timeout=2)
        second.start()
        first.join(timeout=5)
        second.join(timeout=5)

    assert sorted(item.rsplit(":", 1)[1] for item in results) == ["failed", "ok"]
    assert len(set(cleaned)) == 1
    failed_ref = next(
        ref for ref, digest in images.items() if digest == "sha256:first"
    )
    assert failed_ref not in cleaned
    assert all(ref not in images for ref in cleaned)
    assert images["cp2k:2025.2-buildcache-producer"] == "sha256:second"


def _producer_service_inputs(tmp_path: Path):
    layout = ProjectLayout(project_root=tmp_path)
    env_dir = layout.spack_envs_dir / PILOT / "spack-env-file"
    env_dir.mkdir(parents=True)
    lock_path = env_dir / "spack.lock"
    lock_path.write_text('{"lock": true}\n', encoding="utf-8")
    spec = SimpleNamespace(
        spack=SimpleNamespace(
            buildcache=SimpleNamespace(enabled=True, padded_length=128),
            version="1.1.0",
            env_name="cp2k-env",
        ),
    )
    resolved = SimpleNamespace(environment_dir=env_dir, environment_spec=spec)
    return layout, lock_path, resolved


@contextmanager
def _producer_patches(resolved, *, digest: str = "sha256:installed"):
    completed = subprocess.CompletedProcess([], 0, stdout="")
    patchers = (
        patch("hpc_cf.template.resolve_build_input", return_value=resolved),
        patch(
            "hpc_cf.template.resolve_image_and_tag",
            return_value=("cp2k", "2025.2"),
        ),
        patch("hpc_cf.env.run_static_checks"),
        patch("hpc_cf.buildcache.require_verified_source_mirror"),
        patch(
            "hpc_cf.template.generate_dockerfile",
            return_value=Path("Dockerfile"),
        ),
        patch("hpc_cf.sif.build_docker_stage"),
        patch("hpc_cf.buildcache.inspect_image_digest", return_value=digest),
        patch("hpc_cf.buildcache.publish", return_value=(completed, 1)),
        patch("hpc_cf.buildcache.promote_producer_image"),
    )
    with ExitStack() as stack:
        for patcher in patchers:
            stack.enter_context(patcher)
        yield


def test_publish_failure_preserves_recoverable_temporary_image(
    tmp_path: Path,
) -> None:
    from hpc_cf.workflows import BuildcacheRequest, BuildcacheService

    layout, lock_path, resolved = _producer_service_inputs(tmp_path)
    removed: list[str] = []
    with (
        _producer_patches(resolved),
        patch("hpc_cf.buildcache.publish", side_effect=RuntimeError("push failed")),
        patch(
            "hpc_cf.buildcache.remove_temporary_image",
            side_effect=lambda *, image_ref, **_: removed.append(image_ref),
        ),
        pytest.raises(RuntimeError, match="push failed"),
    ):
        BuildcacheService(layout).run(
            BuildcacheRequest(action="build", env=PILOT)
        )

    assert removed == []
    health = SharedBuildcacheStore(layout).read_health()
    assert health["healthy"] is False
    assert health["failed_step"] == "publish"
    assert health["recovery_image_ref"].startswith(
        "cp2k:2025.2-buildcache-producer-"
    )
    assert health["recovery_image_digest"] == "sha256:installed"
    assert health["env"] == PILOT
    assert health["spack_version"] == "1.1.0"
    assert health["lock_sha256"] == __import__("hashlib").sha256(
        lock_path.read_bytes()
    ).hexdigest()
    provenance = json.loads(
        next(layout.buildcache_runs_dir.iterdir())
        .joinpath("provenance.json")
        .read_text(encoding="utf-8")
    )
    assert provenance["failed_step"] == "publish"
    assert provenance["recovery_image_ref"] == health["recovery_image_ref"]


def test_success_removes_temporary_image_only_after_healthy_state(
    tmp_path: Path,
) -> None:
    from hpc_cf.workflows import BuildcacheRequest, BuildcacheService

    layout, _, resolved = _producer_service_inputs(tmp_path)
    removed: list[str] = []

    def remove_after_healthy(*, image_ref: str, **_: object) -> None:
        assert SharedBuildcacheStore(layout).read_health()["healthy"] is True
        removed.append(image_ref)

    with (
        _producer_patches(resolved),
        patch(
            "hpc_cf.buildcache.remove_temporary_image",
            side_effect=remove_after_healthy,
        ),
    ):
        assert BuildcacheService(layout).run(
            BuildcacheRequest(action="build", env=PILOT)
        ) == 0

    assert len(removed) == 1


def test_docker_stage_failure_cleans_partial_tag_without_recovery(
    tmp_path: Path,
) -> None:
    from hpc_cf.workflows import BuildcacheRequest, BuildcacheService

    layout, _, resolved = _producer_service_inputs(tmp_path)
    removed: list[str] = []
    with (
        _producer_patches(resolved),
        patch(
            "hpc_cf.sif.build_docker_stage",
            side_effect=RuntimeError("docker build failed"),
        ),
        patch(
            "hpc_cf.buildcache.inspect_image_digest",
            side_effect=RuntimeError("image missing"),
        ),
        patch(
            "hpc_cf.buildcache.remove_temporary_image",
            side_effect=lambda *, image_ref, **_: removed.append(image_ref),
        ),
        pytest.raises(RuntimeError, match="docker build failed"),
    ):
        BuildcacheService(layout).run(
            BuildcacheRequest(action="build", env=PILOT)
        )

    assert len(removed) == 1
    health = SharedBuildcacheStore(layout).read_health()
    assert health["failed_step"] == "docker-build"
    assert "recovery_image_ref" not in health


def test_docker_stage_failure_with_image_attempts_partial_publish(
    tmp_path: Path,
) -> None:
    from hpc_cf.workflows import BuildcacheRequest, BuildcacheService

    layout, _, resolved = _producer_service_inputs(tmp_path)
    publish_stdout = (
        "HPC_CF_BUILDCACHE_STEP=publish\n"
        "HPC_CF_PUSHED_SPEC_COUNT=3\n"
        "HPC_CF_CHECKED_SPEC_COUNT=10\n"
        "HPC_CF_PARTIAL_PUBLISH=1\n"
        "HPC_CF_BUILDCACHE_STEP=update-index\n"
        "HPC_CF_BUILDCACHE_STEP=check\n"
    )
    publish_exc = subprocess.CalledProcessError(
        1, ["podman"], output=publish_stdout
    )
    removed: list[str] = []
    with (
        _producer_patches(resolved),
        patch(
            "hpc_cf.sif.build_docker_stage",
            side_effect=RuntimeError("docker build failed"),
        ),
        patch(
            "hpc_cf.buildcache.publish",
            side_effect=publish_exc,
        ),
        patch(
            "hpc_cf.buildcache.remove_temporary_image",
            side_effect=lambda *, image_ref, **_: removed.append(image_ref),
        ),
        pytest.raises(subprocess.CalledProcessError),
    ):
        BuildcacheService(layout).run(
            BuildcacheRequest(action="build", env=PILOT)
        )

    assert removed == []
    health = SharedBuildcacheStore(layout).read_health()
    assert health["healthy"] is False
    assert health["failed_step"] == "check"
    assert health["partial_publish"] is True
    assert health["pushed_spec_count"] == 3
    assert health["recovery_image_ref"].startswith(
        "cp2k:2025.2-buildcache-producer-"
    )


def _write_recovery_health(
    layout: ProjectLayout,
    lock_path: Path,
    *,
    env: str = PILOT,
    spack_version: str = "1.1.0",
    image_ref: str = "cp2k:2025.2-buildcache-producer-recovery",
    digest: str = "sha256:installed",
) -> None:
    SharedBuildcacheStore(layout).mark_unhealthy(
        run_id="failed-run",
        failed_step="publish",
        error="push failed",
        recovery={
            "recoverable": True,
            "env": env,
            "spack_version": spack_version,
            "lock_sha256": __import__("hashlib").sha256(
                lock_path.read_bytes()
            ).hexdigest(),
            "recovery_image_ref": image_ref,
            "recovery_image_digest": digest,
            "stable_image_ref": "cp2k:2025.2-buildcache-producer",
        },
    )


def test_resume_success_skips_docker_build_and_cleans_recovery_image(
    tmp_path: Path,
) -> None:
    from hpc_cf.workflows import BuildcacheRequest, BuildcacheService

    layout, lock_path, resolved = _producer_service_inputs(tmp_path)
    _write_recovery_health(layout, lock_path)
    removed: list[str] = []
    build_stage = MagicMock(side_effect=AssertionError("resume rebuilt image"))
    with (
        _producer_patches(resolved),
        patch("hpc_cf.sif.build_docker_stage", new=build_stage),
        patch(
            "hpc_cf.buildcache.remove_temporary_image",
            side_effect=lambda *, image_ref, **_: removed.append(image_ref),
        ),
    ):
        assert BuildcacheService(layout).run(
            BuildcacheRequest(action="resume", env=PILOT)
        ) == 0

    build_stage.assert_not_called()
    assert removed == ["cp2k:2025.2-buildcache-producer-recovery"]
    assert SharedBuildcacheStore(layout).read_health()["healthy"] is True


@pytest.mark.parametrize(
    ("override", "message"),
    [
        ({"env": "other"}, "environment"),
        ({"spack_version": "9.9.9"}, "Spack version"),
        ({"digest": "sha256:other"}, "digest"),
    ],
)
def test_resume_rejects_recovery_identity_mismatch(
    tmp_path: Path,
    override: dict[str, str],
    message: str,
) -> None:
    from hpc_cf.workflows import BuildcacheRequest, BuildcacheService

    layout, lock_path, resolved = _producer_service_inputs(tmp_path)
    state_kwargs = {
        key: value for key, value in override.items() if key != "digest"
    }
    _write_recovery_health(layout, lock_path, **state_kwargs)
    inspected = override.get("digest", "sha256:installed")
    publish = MagicMock()
    with (
        _producer_patches(resolved, digest=inspected),
        patch("hpc_cf.buildcache.publish", new=publish),
        pytest.raises(RuntimeError, match=message),
    ):
        BuildcacheService(layout).run(
            BuildcacheRequest(action="resume", env=PILOT)
        )
    publish.assert_not_called()


def test_resume_rejects_changed_lock_and_missing_image(tmp_path: Path) -> None:
    from hpc_cf.workflows import BuildcacheRequest, BuildcacheService

    layout, lock_path, resolved = _producer_service_inputs(tmp_path)
    _write_recovery_health(layout, lock_path)
    lock_path.write_text('{"lock": "changed"}\n', encoding="utf-8")
    with (
        _producer_patches(resolved),
        pytest.raises(RuntimeError, match="lock SHA"),
    ):
        BuildcacheService(layout).run(
            BuildcacheRequest(action="resume", env=PILOT)
        )

    _write_recovery_health(layout, lock_path)
    with (
        _producer_patches(resolved),
        patch(
            "hpc_cf.buildcache.inspect_image_digest",
            side_effect=subprocess.CalledProcessError(1, ["inspect"]),
        ),
        pytest.raises(RuntimeError, match="does not exist"),
    ):
        BuildcacheService(layout).run(
            BuildcacheRequest(action="resume", env=PILOT)
        )


def test_resume_failure_keeps_image_and_updates_recovery_state(
    tmp_path: Path,
) -> None:
    from hpc_cf.workflows import BuildcacheRequest, BuildcacheService

    layout, lock_path, resolved = _producer_service_inputs(tmp_path)
    _write_recovery_health(layout, lock_path)
    remove = MagicMock()
    with (
        _producer_patches(resolved),
        patch("hpc_cf.buildcache.publish", side_effect=RuntimeError("again")),
        patch("hpc_cf.buildcache.remove_temporary_image", new=remove),
        pytest.raises(RuntimeError, match="again"),
    ):
        BuildcacheService(layout).run(
            BuildcacheRequest(action="resume", env=PILOT)
        )
    remove.assert_not_called()
    health = SharedBuildcacheStore(layout).read_health()
    assert health["failed_step"] == "publish"
    assert health["recovery_image_ref"].endswith("-recovery")


def test_status_text_and_json_show_recovery_image(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from hpc_cf.workflows import BuildcacheRequest, BuildcacheService

    layout, lock_path, _ = _producer_service_inputs(tmp_path)
    _write_recovery_health(layout, lock_path)
    service = BuildcacheService(layout)

    assert service.run(BuildcacheRequest(action="status")) == 1
    text = capsys.readouterr().out
    assert "Recovery image: cp2k:2025.2-buildcache-producer-recovery" in text
    assert "Recovery digest: sha256:installed" in text

    assert service.run(
        BuildcacheRequest(action="status", output_format="json")
    ) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["recovery_image_ref"].endswith("-recovery")
