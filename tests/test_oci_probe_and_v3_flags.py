"""Unit tests for v3 Commit 5: --lock-only, --no-mirror, single-pass verify, and oci_probe."""

from __future__ import annotations

import hashlib
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from hpc_cf.assets import run_assets, run_lock, run_mirror
from hpc_cf.buildcache import require_verified_source_mirror
from hpc_cf.buildcache_workflow import BuildcacheService
from hpc_cf.cli import build_parser
from hpc_cf.environment import BuildcacheMode, parse_environment_spec
from hpc_cf.execution import ProjectLayout
from hpc_cf.oci_probe import (
    expected_oci_tags_from_lock,
    parse_oci_url,
    probe_oci_lock,
)
from hpc_cf.requests import (
    AssetsRequest,
    BuildcacheRequest,
    assets_request_from_args,
    buildcache_request_from_args,
)


def test_parse_oci_url_and_expected_tags(tmp_path: Path) -> None:
    t1 = parse_oci_url("oci://ghcr.io/s8ga/hpc-cf-buildcache")
    assert (t1.http_scheme, t1.registry, t1.repository) == (
        "https",
        "ghcr.io",
        "s8ga/hpc-cf-buildcache",
    )
    t2 = parse_oci_url("oci+http://127.0.0.1:5000/cache")
    assert (t2.http_scheme, t2.registry, t2.repository) == (
        "http",
        "127.0.0.1:5000",
        "cache",
    )
    with pytest.raises(ValueError, match="Unsupported OCI"):
        parse_oci_url("https://ghcr.io/foo")
    with pytest.raises(ValueError, match="host and repository"):
        parse_oci_url("oci://ghcr.io")

    lock = tmp_path / "spack.lock"
    lock.write_text(
        json.dumps(
            {
                "concrete_specs": {
                    "ext1": {"name": "glibc", "version": "2.44", "external": {"path": "/usr"}},
                    "vasp1": {"name": "vasp", "version": "6.6.1"},
                    "cp2k1": {"name": "cp2k", "version": "master"},
                    "libint1": {"name": "libint", "version": "2.14.1-cp2k-lmax-7"},
                }
            }
        ),
        encoding="utf-8",
    )
    tags = expected_oci_tags_from_lock(lock, exclude_specs=("vasp",))
    assert tags == [
        "cp2k-master-cp2k1.spack",
        "libint-2.14.1-cp2k-lmax-7-libint1.spack",
    ]


def test_probe_oci_lock_with_bearer_auth_against_local_http_server(tmp_path: Path) -> None:
    lock = tmp_path / "spack.lock"
    lock.write_text(
        json.dumps(
            {
                "concrete_specs": {
                    "h1": {"name": "pkg-a", "version": "1.0"},
                    "h2": {"name": "pkg-b", "version": "2.0"},
                }
            }
        ),
        encoding="utf-8",
    )
    present_tag = "pkg-a-1.0-h1.spack"
    seen_tokens: list[str] = []

    class _Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:  # noqa: A002
            return

        def do_GET(self) -> None:  # noqa: N802
            if self.path.startswith("/token"):
                auth = self.headers.get("Authorization", "")
                if not auth.startswith("Basic "):
                    self.send_response(401)
                    self.end_headers()
                    return
                payload = json.dumps({"token": "tok-123"}).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
                return
            self.send_response(404)
            self.end_headers()

        def do_HEAD(self) -> None:  # noqa: N802
            port = self.server.server_address[1]
            auth = self.headers.get("Authorization", "")
            if auth != "Bearer tok-123":
                self.send_response(401)
                self.send_header(
                    "WWW-Authenticate",
                    f'Bearer realm="http://127.0.0.1:{port}/token",service="reg",scope="repository:org/cache:pull"',
                )
                self.end_headers()
                return
            seen_tokens.append(auth)
            if self.path == f"/v2/org/cache/manifests/{present_tag}":
                self.send_response(200)
            else:
                self.send_response(404)
            self.end_headers()

    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        report = probe_oci_lock(
            lock_path=lock,
            mirror_url=f"oci+http://127.0.0.1:{port}/org/cache",
            username="u",
            password="p",
            concurrency=4,
        )
    finally:
        server.shutdown()
        server.server_close()

    assert report.total == 2
    assert report.present == 1
    assert report.missing == 1
    assert report.present_tags == (present_tag,)
    assert report.missing_tags == ("pkg-b-2.0-h2.spack",)
    assert len(seen_tokens) == 2


def test_buildcache_check_oci_cli_and_service_return_codes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    parser = build_parser()
    args = parser.parse_args(
        [
            "buildcache",
            "check-oci",
            "--env",
            "demo",
            "--buildcache-url",
            "oci://ghcr.io/s8ga/cache",
            "--format",
            "json",
            "--concurrency",
            "8",
        ]
    )
    req = buildcache_request_from_args(args)
    assert req.action == "check-oci"
    assert req.concurrency == 8

    layout = ProjectLayout(project_root=tmp_path)
    conf = layout.spack_envs_dir / "demo" / "spack-env-file"
    conf.mkdir(parents=True)
    (conf / "spack.lock").write_text('{"concrete_specs": {"h": {"name": "x", "version": "1"}}}', encoding="utf-8")
    spec = parse_environment_spec(
        {
            "schema_version": 1,
            "spack": {
                "version": "1.2.2",
                "env_name": "e",
                "buildcache": {
                    "enabled": True,
                    "mode": "oci",
                    "url": "oci://ghcr.io/s8ga/cache",
                },
            },
        }
    )
    resolved = SimpleNamespace(environment_dir=layout.spack_envs_dir / "demo", environment_spec=spec)

    with (
        patch("hpc_cf.template.resolve_build_input", return_value=resolved),
        patch(
            "hpc_cf.oci_probe.probe_oci_lock",
            return_value=SimpleNamespace(
                mirror_url="oci://ghcr.io/s8ga/cache",
                total=1,
                present=1,
                missing=0,
                to_dict=lambda: {"total": 1, "present": 1, "missing": 0},
            ),
        ),
    ):
        rc = BuildcacheService(layout).run(
            BuildcacheRequest(action="check-oci", env="demo", output_format="json")
        )
    assert rc == 0
    assert json.loads(capsys.readouterr().out)["missing"] == 0

    with (
        patch("hpc_cf.template.resolve_build_input", return_value=resolved),
        patch(
            "hpc_cf.oci_probe.probe_oci_lock",
            return_value=SimpleNamespace(
                mirror_url="oci://ghcr.io/s8ga/cache",
                total=2,
                present=1,
                missing=1,
                to_dict=lambda: {"total": 2, "present": 1, "missing": 1},
            ),
        ),
    ):
        rc2 = BuildcacheService(layout).run(
            BuildcacheRequest(action="check-oci", env="demo", output_format="text")
        )
    assert rc2 == 2
    assert "1/2 present, 1 missing" in capsys.readouterr().out


def test_assets_lock_only_skips_mirror_and_requires_allow_concretize_when_missing(
    tmp_path: Path,
) -> None:
    parser = build_parser()
    args = parser.parse_args(["assets", "--env", "demo", "--lock-only", "--allow-concretize"])
    req = assets_request_from_args(args)
    assert req.lock_only is True
    assert req.allow_concretize is True

    layout = ProjectLayout(project_root=tmp_path)
    conf = layout.spack_envs_dir / "demo" / "spack-env-file"
    conf.mkdir(parents=True)
    (conf / "spack.yaml").write_text("spack: {}\n", encoding="utf-8")
    (conf / "env.yaml").write_text(
        "schema_version: 1\nspack:\n  version: '1.2.2'\n  env_name: demo\n",
        encoding="utf-8",
    )

    with pytest.raises(FileNotFoundError, match="--lock-only --allow-concretize"):
        run_lock(MagicMock(), "demo", layout=layout, allow_concretize=False)

    with (
        patch("hpc_cf.assets.ensure_image"),
        patch("hpc_cf.assets._make_container", return_value=MagicMock()),
        patch("hpc_cf.assets.run_bootstrap") as boot_mock,
        patch("hpc_cf.assets.run_lock") as lock_mock,
        patch("hpc_cf.assets.run_mirror") as mirror_mock,
        patch("hpc_cf.assets.run_verify") as verify_mock,
        patch("hpc_cf.assets.spack_version_for_env", return_value="1.2.2"),
    ):
        run_assets(
            AssetsRequest(env="demo", lock_only=True, allow_concretize=True),
            layout=layout,
        )
    boot_mock.assert_called_once()
    lock_mock.assert_called_once()
    mirror_mock.assert_not_called()
    verify_mock.assert_not_called()


def test_single_pass_mirror_verify_checks_host_symlinks_and_accepts_create_log(
    tmp_path: Path,
) -> None:
    layout = ProjectLayout(project_root=tmp_path)
    conf = layout.spack_envs_dir / "demo" / "spack-env-file"
    conf.mkdir(parents=True)
    (conf / "spack.yaml").write_text("spack: {}\n", encoding="utf-8")
    (conf / "env.yaml").write_text(
        "schema_version: 1\nspack:\n  version: '1.2.2'\n  env_name: demo\n",
        encoding="utf-8",
    )
    lock = conf / "spack.lock"
    lock.write_text('{"lock": 1}\n', encoding="utf-8")

    # Default run_assets must not call run_verify a second time.
    with (
        patch("hpc_cf.assets.ensure_image"),
        patch("hpc_cf.assets._make_container", return_value=MagicMock()),
        patch("hpc_cf.assets.run_bootstrap"),
        patch("hpc_cf.assets.run_mirror") as mirror_mock,
        patch("hpc_cf.assets.run_verify") as verify_mock,
        patch("hpc_cf.assets.spack_version_for_env", return_value="1.2.2"),
    ):
        run_assets(AssetsRequest(env="demo"), layout=layout)
    mirror_mock.assert_called_once()
    verify_mock.assert_not_called()

    # run_mirror itself runs _verify_host_side before writing manifest.
    fake_ops = MagicMock()
    fake_ops.run_mirror_pipeline.return_value = {"present": 1, "added": 2, "failed": 0}
    env_spec = parse_environment_spec(
        {"schema_version": 1, "spack": {"version": "1.2.2", "env_name": "demo"}}
    )
    payload = layout.spack_mirror_dir / "pkg" / "src.tar.gz"
    payload.parent.mkdir(parents=True)
    payload.write_bytes(b"src")

    with (
        patch("hpc_cf.assets._make_spack_ops", return_value=(env_spec, fake_ops)),
        patch("hpc_cf.assets._verify_host_side") as host_verify,
    ):
        run_mirror(MagicMock(), "demo", layout=layout)
    host_verify.assert_called_once()

    # Single-pass manifest with mirror-create.log satisfies require_verified_source_mirror.
    run_dir = next(layout.mirror_runs_dir.iterdir())
    (run_dir / "mirror-create.log").write_text("1 present / 2 added / 0 failed\n", encoding="utf-8")
    manifest = require_verified_source_mirror(
        layout, env_name="demo", lock_path=lock, spack_version="1.2.2"
    )
    assert json.loads(manifest.read_text(encoding="utf-8"))["lock_hash"] == hashlib.sha256(
        lock.read_bytes()
    ).hexdigest()


def test_buildcache_build_no_mirror_skips_source_mirror_gate(tmp_path: Path) -> None:
    parser = build_parser()
    args = parser.parse_args(["buildcache", "build", "--env", "demo", "--no-mirror"])
    req = buildcache_request_from_args(args)
    assert req.use_mirror is False

    layout = ProjectLayout(project_root=tmp_path)
    conf = layout.spack_envs_dir / "demo" / "spack-env-file"
    conf.mkdir(parents=True)
    (conf / "spack.lock").write_text('{"lock": true}\n', encoding="utf-8")
    spec = SimpleNamespace(
        spack=SimpleNamespace(
            buildcache=SimpleNamespace(
                enabled=True,
                padded_length=128,
                mode=BuildcacheMode.OCI,
                url="oci://ghcr.io/s8ga/cache",
                username_var=None,
                password_var=None,
                exclude=(),
            ),
            version="1.2.2",
            env_name="demo",
            custom_repos=[],
        ),
        images=SimpleNamespace(builder="fedora:45"),
        template_vars={},
        manual_packages=[],
    )
    resolved = SimpleNamespace(environment_dir=layout.spack_envs_dir / "demo", environment_spec=spec)
    gen_kwargs: dict[str, object] = {}

    def fake_gen(**kw: object) -> Path:
        gen_kwargs.update(kw)
        return tmp_path / "Dockerfile"

    with (
        patch("hpc_cf.template.resolve_build_input", return_value=resolved),
        patch("hpc_cf.template.resolve_image_and_tag", return_value=("demo", "v1")),
        patch("hpc_cf.template.generate_dockerfile", side_effect=fake_gen),
        patch("hpc_cf.env.run_static_checks"),
        patch("hpc_cf.buildcache.require_verified_source_mirror") as gate_mock,
        patch("hpc_cf.sif.build_docker_stage"),
        patch("hpc_cf.buildcache.inspect_image_digest", return_value="sha256:d"),
        patch("hpc_cf.buildcache.promote_producer_image"),
        patch("hpc_cf.buildcache.require_matching_image_lock"),
        patch("hpc_cf.buildcache.publish_oci", return_value=(SimpleNamespace(stdout=""), 5)),
        patch("hpc_cf.buildcache.remove_temporary_image"),
    ):
        rc = BuildcacheService(layout).run(
            BuildcacheRequest(action="build", env="demo", use_mirror=False)
        )
    assert rc == 0
    gate_mock.assert_not_called()
    assert gen_kwargs["use_mirror"] is False


def test_buildcache_check_oci_write_state_enables_only_coverage(tmp_path: Path) -> None:
    from hpc_cf.buildcache import (
        BuildcacheMode,
        collect_environment_provenance,
        coverage_path_for_lock,
        require_coverage,
    )
    from hpc_cf.execution import SharedBuildcacheStore

    args = build_parser().parse_args(
        ["buildcache", "check-oci", "--env", "demo", "--write-state"]
    )
    req = buildcache_request_from_args(args)
    assert req.write_state is True

    layout = ProjectLayout(project_root=tmp_path)
    conf = layout.spack_envs_dir / "demo" / "spack-env-file"
    conf.mkdir(parents=True)
    lock = conf / "spack.lock"
    lock.write_text('{"concrete_specs": {"h": {"name": "x", "version": "1"}}}', encoding="utf-8")
    spec = parse_environment_spec(
        {
            "schema_version": 1,
            "spack": {
                "version": "1.2.2",
                "env_name": "e",
                "buildcache": {
                    "enabled": True,
                    "mode": "oci",
                    "url": "oci://ghcr.io/s8ga/cache",
                },
            },
        }
    )
    resolved = SimpleNamespace(environment_dir=layout.spack_envs_dir / "demo", environment_spec=spec)

    # Incomplete probe (missing > 0) must NOT record healthy coverage state.
    with (
        patch("hpc_cf.template.resolve_build_input", return_value=resolved),
        patch(
            "hpc_cf.oci_probe.probe_oci_lock",
            return_value=SimpleNamespace(
                mirror_url="oci://ghcr.io/s8ga/cache",
                total=2,
                present=1,
                missing=1,
                to_dict=lambda: {"total": 2, "present": 1, "missing": 1},
            ),
        ),
    ):
        rc_miss = BuildcacheService(layout).run(
            BuildcacheRequest(action="check-oci", env="demo", write_state=True)
        )
    assert rc_miss == 2
    store = SharedBuildcacheStore(layout)
    assert not coverage_path_for_lock(layout, lock).exists()

    # Complete probe (missing == 0) writes count coverage + healthy state so
    # `--buildcache only` passes without running `binaries`.
    with (
        patch("hpc_cf.template.resolve_build_input", return_value=resolved),
        patch(
            "hpc_cf.oci_probe.probe_oci_lock",
            return_value=SimpleNamespace(
                mirror_url="oci://ghcr.io/s8ga/cache",
                total=2,
                present=2,
                missing=0,
                to_dict=lambda: {"total": 2, "present": 2, "missing": 0},
            ),
        ),
    ):
        rc_ok = BuildcacheService(layout).run(
            BuildcacheRequest(action="check-oci", env="demo", write_state=True)
        )
    assert rc_ok == 0
    assert store.read_health()["healthy"] is True
    require_coverage(
        layout,
        lock,
        spack_version="1.2.2",
        builder_image=None,
        padded_length=spec.spack.buildcache.padded_length,
        environment_provenance=collect_environment_provenance(
            lock, layout.spack_envs_dir / "demo"
        ),
        backend_mode=BuildcacheMode.OCI,
    )
