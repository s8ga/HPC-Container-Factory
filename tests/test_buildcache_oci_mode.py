"""OCI buildcache mode: schema, plan, backend resolution, and rendering.

Two-sided rendering contract:
- local mode (the default) must stay byte-equivalent to the pre-oci output:
  existing pins in test_buildcache_workflow are the regression line, and the
  sweep here additionally asserts zero oci traces in every local render.
- oci mode renders the registry mirror registration (with optional
  credential-variable flags and a build secret) and no buildcache bind mount.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from hpc_cf.environment import (
    BuildcacheMode,
    parse_environment_spec,
)
from hpc_cf.requests import build_request_from_args
from hpc_cf.spack_plan import build_spack_environment_plan, plan_context
from hpc_cf.template import build_context, render_template
from hpc_cf.workflows import resolve_buildcache_backend

ROOT = Path(__file__).resolve().parent.parent
PILOT = "cp2k_opensource-2025.2"
PILOT_TEMPLATE = ROOT / "spack-envs" / PILOT / "Dockerfile.j2"
OCI_URL = "oci://ghcr.io/example/hpc-cf-buildcache"


def _spec(buildcache: dict | None) -> object:
    spack: dict = {"version": "1.2.0", "env_name": "e"}
    if buildcache is not None:
        spack["buildcache"] = buildcache
    return parse_environment_spec({"schema_version": 1, "spack": spack})


def _render(
    policy: str,
    *,
    mode: str | None = None,
    url: str | None = None,
    producer: bool = False,
    context_mutate=None,
) -> str:
    context = build_context(
        use_mirror=True,
        build_only=False,
        app_version=PILOT,
        template_path=PILOT_TEMPLATE,
        buildcache_policy=policy,
        buildcache_producer=producer,
        buildcache_mode=mode,
        buildcache_url=url,
    )
    if context_mutate is not None:
        context_mutate(context)
    return render_template(PILOT_TEMPLATE, context)


# ── Schema ───────────────────────────────────────────────────────────────


def test_buildcache_mode_defaults_to_local() -> None:
    spec = _spec(None)
    assert spec.spack.buildcache.mode is BuildcacheMode.LOCAL
    assert spec.spack.buildcache.url is None
    assert spec.spack.buildcache.username_var is None
    assert spec.spack.buildcache.password_var is None


def test_buildcache_oci_mode_parses_url_and_credential_vars() -> None:
    spec = _spec(
        {
            "enabled": True,
            "policy": "only",
            "mode": "oci",
            "url": OCI_URL,
            "username_var": "OCI_USER",
            "password_var": "OCI_PASS",
        }
    )
    assert spec.spack.buildcache.mode is BuildcacheMode.OCI
    assert spec.spack.buildcache.url == OCI_URL
    assert spec.spack.buildcache.username_var == "OCI_USER"
    assert spec.spack.buildcache.password_var == "OCI_PASS"


def test_buildcache_oci_mode_roundtrips_through_as_dict() -> None:
    spec = _spec({"mode": "oci", "url": "oci+http://localhost:5000/hpccf"})
    assert spec.as_dict()["spack"]["buildcache"]["mode"] == "oci"
    assert (
        spec.as_dict()["spack"]["buildcache"]["url"]
        == "oci+http://localhost:5000/hpccf"
    )


@pytest.mark.parametrize(
    "buildcache,match",
    [
        # oci without url fails closed.
        ({"mode": "oci"}, "url is required"),
        # local mode must not carry oci-only fields (no silent ignoring).
        ({"url": OCI_URL}, "only valid with mode 'oci'"),
        ({"username_var": "A", "password_var": "B"}, "only valid with mode 'oci'"),
        # credentials come in pairs only.
        (
            {"mode": "oci", "url": OCI_URL, "username_var": "A"},
            "set together",
        ),
        # url scheme and shape are validated.
        ({"mode": "oci", "url": "https://ghcr.io/x"}, "must start with oci"),
        ({"mode": "oci", "url": "oci://"}, "repository path"),
        ({"mode": "oci", "url": "oci://ghcr.io/"}, "repository path"),
        # unknown enum value.
        ({"mode": "remote"}, "Unknown buildcache mode"),
    ],
)
def test_buildcache_mode_rejections(buildcache: dict, match: str) -> None:
    with pytest.raises(ValueError, match=match):
        _spec(buildcache)


# ── Plan / context ───────────────────────────────────────────────────────


def test_plan_context_carries_mode_url_and_credential_vars() -> None:
    spec = _spec(
        {
            "enabled": True,
            "mode": "oci",
            "url": OCI_URL,
            "username_var": "OCI_USER",
            "password_var": "OCI_PASS",
        }
    )
    plan = build_spack_environment_plan(spec)
    assert plan.buildcache.mode is BuildcacheMode.OCI
    context = plan_context(plan)
    assert context["spack_buildcache_mode"] == "oci"
    assert context["spack_buildcache_url"] == OCI_URL
    assert context["spack_buildcache_username_var"] == "OCI_USER"
    assert context["spack_buildcache_password_var"] == "OCI_PASS"


def test_plan_context_local_defaults() -> None:
    context = plan_context(build_spack_environment_plan(_spec(None)))
    assert context["spack_buildcache_mode"] == "local"
    assert context["spack_buildcache_url"] is None
    assert context["spack_buildcache_username_var"] is None
    assert context["spack_buildcache_password_var"] is None


# ── Backend resolution ────────────────────────────────────────────────────


def test_resolve_backend_defaults_to_local() -> None:
    backend = resolve_buildcache_backend(_spec(None))
    assert backend.mode is BuildcacheMode.LOCAL
    assert backend.url is None
    assert backend.username_var is None
    assert backend.password_var is None


def test_resolve_backend_cli_override_beats_env_yaml() -> None:
    # env url survives a local mode override (no creds configured)
    plain = _spec({"mode": "oci", "url": "oci://ghcr.io/a/b"})
    backend = resolve_buildcache_backend(
        plain, mode_override=BuildcacheMode.LOCAL, url_override=None
    )
    assert backend.mode is BuildcacheMode.LOCAL
    assert backend.url == "oci://ghcr.io/a/b"

    credentialed = _spec(
        {
            "mode": "oci",
            "url": "oci://ghcr.io/a/b",
            "username_var": "ENV_USER",
            "password_var": "ENV_PASS",
        }
    )
    # env creds make a local mode override contradictory — fail closed
    with pytest.raises(ValueError, match="only valid with mode 'oci'"):
        resolve_buildcache_backend(credentialed, mode_override=BuildcacheMode.LOCAL)

    backend = resolve_buildcache_backend(
        credentialed,
        username_var_override="CLI_USER",
        password_var_override="CLI_PASS",
    )
    # CLI credential-var overrides win over env.yaml in oci mode.
    assert (backend.username_var, backend.password_var) == ("CLI_USER", "CLI_PASS")


def test_resolve_backend_rejects_cred_vars_without_oci() -> None:
    with pytest.raises(ValueError, match="only valid with mode 'oci'"):
        resolve_buildcache_backend(
            _spec({"mode": "oci", "url": OCI_URL}),
            mode_override=BuildcacheMode.LOCAL,
            username_var_override="A",
            password_var_override="B",
        )
    with pytest.raises(ValueError, match="as a pair"):
        resolve_buildcache_backend(
            _spec({"mode": "oci", "url": OCI_URL}),
            username_var_override="A",
        )


def test_resolve_backend_oci_requires_url_fail_closed() -> None:
    with pytest.raises(ValueError, match="requires a mirror URL"):
        resolve_buildcache_backend(None, mode_override=BuildcacheMode.OCI)


def test_resolve_backend_env_oci_mode_uses_env_url() -> None:
    backend = resolve_buildcache_backend(
        _spec({"mode": "oci", "url": OCI_URL})
    )
    assert backend.mode is BuildcacheMode.OCI
    assert backend.url == OCI_URL


# ── Rendering: local stays clean, oci is explicit ─────────────────────────


def test_local_render_has_zero_oci_traces() -> None:
    rendered = _render("auto")
    assert "file:///opt/spack-buildcache" in rendered
    assert "source=assets/spack-buildcache,target=/opt/spack-buildcache,readonly" in rendered
    for leak in ("oci://", "oci+http://", "--oci-", "type=secret"):
        assert leak not in rendered, f"oci leak in local render: {leak}"


def test_oci_render_registers_registry_mirror_without_bind_mount() -> None:
    rendered = _render("only", mode="oci", url=OCI_URL)
    install = next(
        block for block in rendered.split("RUN ") if "--use-buildcache only" in block
    )
    assert f"binary-cache \\\n        {OCI_URL}" in install
    assert "source=assets/spack-buildcache" not in install
    assert "file:///opt/spack-buildcache" not in install
    assert "spack-mirror" not in install  # only policy never mounts sources
    assert "--oci-" not in install  # no credentials configured
    assert "--autopush" not in install  # consumers never push relocated trees


def test_oci_render_auto_keeps_source_mirror_fallback() -> None:
    rendered = _render("auto", mode="oci", url=OCI_URL)
    install = next(
        block for block in rendered.split("RUN ") if "--use-buildcache auto" in block
    )
    assert "source=assets/spack-mirror,target=/opt/spack-mirror,readonly" in install
    assert "source=assets/spack-buildcache" not in install


def test_oci_render_with_credentials_uses_secret_not_layers() -> None:
    def add_creds(context: dict) -> None:
        context["spack_buildcache_username_var"] = "OCI_USER"
        context["spack_buildcache_password_var"] = "OCI_PASS"

    rendered = _render("only", mode="oci", url=OCI_URL, context_mutate=add_creds)
    install = next(
        block for block in rendered.split("RUN ") if "--use-buildcache only" in block
    )
    assert "--mount=type=secret,id=buildcache-creds" in install
    assert ". /run/secrets/buildcache-creds" in install
    assert "--oci-username-variable OCI_USER" in install
    assert "--oci-password-variable OCI_PASS" in install
    # Credential VALUES must never appear: only variable names are rendered.
    assert "OCI_USER=" not in install.replace("OCI_USER ", "")
    assert "OCI_PASS=" not in install.replace("OCI_PASS ", "")


def test_oci_producer_render_keeps_soft_fail_and_counts() -> None:
    rendered = _render("auto", mode="oci", url=OCI_URL, producer=True)
    install = next(
        block for block in rendered.split("RUN ") if "--use-buildcache auto" in block
    )
    assert "HPC_CF_INSTALL_RC=" in install
    assert "HPC_CF_INSTALLED_SPEC_COUNT=" in install
    assert "HPC_CF_PARTIAL_INSTALL=1" in install
    # Producer-only autopush: a killed install keeps every completed
    # package in the registry, so retries converge strictly.
    assert "mirror add --scope env:cp2k-env --unsigned binary-cache \\\n        --autopush" in install


def test_all_env_local_renders_have_zero_oci_traces() -> None:
    """Inventory sweep: no env renders oci code without opting into the mode."""
    from hpc_cf.env import list_available_envs

    checked = 0
    for env_name in list_available_envs(layout=None):
        template = ROOT / "spack-envs" / env_name / "Dockerfile.j2"
        if not template.is_file():
            continue
        if "spack_buildcache_install" not in template.read_text(encoding="utf-8"):
            continue
        rendered = _render_for_env(env_name)
        for leak in ("oci://", "oci+http://", "--oci-", "type=secret"):
            assert leak not in rendered, (
                f"{env_name}: oci leak in local render: {leak}"
            )
        checked += 1
    assert checked >= 4, "expected the sweep to cover the opensource track"


def _render_for_env(env_name: str) -> str:
    template = ROOT / "spack-envs" / env_name / "Dockerfile.j2"
    context = build_context(
        use_mirror=True,
        build_only=False,
        app_version=env_name,
        template_path=template,
        buildcache_policy="auto",
    )
    return render_template(template, context)


# ── CLI request mapping ───────────────────────────────────────────────────


def test_build_request_maps_backend_overrides() -> None:
    args = argparse.Namespace(
        app_version="e",
        template=None,
        output=None,
        build_only=False,
        engine="podman",
        image=None,
        tag=None,
        network_host=False,
        build_arg=[],
        build_opt=[],
        allow_reconcretize=False,
        buildcache=None,
        buildcache_mode=BuildcacheMode.OCI,
        buildcache_url=OCI_URL,
    )
    request = build_request_from_args(args, use_mirror=True)
    assert request.buildcache_mode is BuildcacheMode.OCI
    assert request.buildcache_url == OCI_URL


# ── Producer leaves: oci publish script and publisher container ───────────

import hashlib  # noqa: E402
import json  # noqa: E402
import subprocess  # noqa: E402
from types import SimpleNamespace  # noqa: E402
from unittest.mock import MagicMock, patch  # noqa: E402

from hpc_cf.buildcache import publish_oci, run_in_installed_image  # noqa: E402
from hpc_cf.buildcache_ops import (  # noqa: E402
    build_publish_script,
    build_publish_script_oci,
)
from hpc_cf.execution import ProjectLayout  # noqa: E402


def test_oci_publish_script_pins_registry_flow() -> None:
    script = build_publish_script_oci(env_name="cp2k-env", mirror_url=OCI_URL)
    normalized = " ".join(script.split())
    assert (
        "spack -e cp2k-env mirror add --unsigned binary-cache "
        f"{OCI_URL}" in normalized
    )
    assert "buildcache push --unsigned --fail-fast binary-cache" in normalized
    # Idempotent registration: the producer install step (autopush) has
    # already registered binary-cache in the env scope, and that config
    # persists inside the built image — mirror add would fail with
    # "already exists" without the best-effort remove first.
    assert (
        "spack -e cp2k-env mirror remove binary-cache "
        ">/dev/null 2>&1 || true spack -e cp2k-env mirror add"
    ) in normalized
    assert "HPC_CF_PUSHED_SPEC_COUNT=" in script
    assert "HPC_CF_CHECKED_SPEC_COUNT=" in script
    assert "HPC_CF_BUILDCACHE_STEP=oci-count-check" in script
    # Commands that must never run against oci mirrors.
    assert "update-index" not in script
    assert "buildcache check" not in script


def test_oci_publish_script_credential_flags() -> None:
    script = build_publish_script_oci(
        env_name="e",
        mirror_url=OCI_URL,
        username_var="OCI_USER",
        password_var="OCI_PASS",
    )
    normalized = " ".join(script.split())
    assert (
        "mirror add --unsigned --oci-username-variable OCI_USER "
        "--oci-password-variable OCI_PASS binary-cache" in normalized
    )


def test_local_publish_script_has_zero_oci_traces() -> None:
    script = build_publish_script(
        env_name="e", store_path="/work/assets/spack-buildcache"
    )
    assert "oci" not in script.lower()
    assert "buildcache check --mirror-url file:///" in script


def test_run_in_installed_image_default_command_unchanged(
    tmp_path, monkeypatch
) -> None:
    captured: dict[str, object] = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, 0, stdout="")

    import hpc_cf.buildcache as bc

    monkeypatch.setattr(bc.subprocess, "run", fake_run)
    run_in_installed_image(
        engine="podman",
        image_ref="img",
        layout=ProjectLayout(project_root=tmp_path),
        script="true",
    )
    cmd = captured["cmd"]
    assert cmd[0] == "podman"
    assert "--network=host" not in cmd
    assert any(part == "-v" for part in cmd)


def test_run_in_installed_image_oci_variant_flags(tmp_path, monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, 0, stdout="")

    import hpc_cf.buildcache as bc

    monkeypatch.setattr(bc.subprocess, "run", fake_run)
    run_in_installed_image(
        engine="podman",
        image_ref="img",
        layout=ProjectLayout(project_root=tmp_path),
        script="true",
        env_extra={"OCI_USER": "u", "OCI_PASS": "p"},
        network_host=True,
        mount_buildcache=False,
    )
    cmd = captured["cmd"]
    assert "--network=host" in cmd
    assert "OCI_USER=u" in cmd and "OCI_PASS=p" in cmd
    assert not any(part == "-v" for part in cmd)


def test_publish_oci_parses_count_and_skips_local_mount(tmp_path, monkeypatch) -> None:
    calls: dict[str, object] = {}

    def fake_run(**kwargs):
        calls.update(kwargs)
        return subprocess.CompletedProcess(
            [], 0, stdout="HPC_CF_PUSHED_SPEC_COUNT=7\nHPC_CF_CHECKED_SPEC_COUNT=7\n"
        )

    import hpc_cf.buildcache as bc

    monkeypatch.setattr(bc, "run_in_installed_image", fake_run)
    result, count = publish_oci(
        engine="podman",
        image_ref="img",
        env_name="e",
        layout=ProjectLayout(project_root=tmp_path),
        mirror_url=OCI_URL,
        credentials={"OCI_USER": "u", "OCI_PASS": "p"},
    )
    assert count == 7
    assert result.returncode == 0
    assert calls["mount_buildcache"] is False
    assert calls["writable"] is False
    assert calls["env_extra"] == {"OCI_USER": "u", "OCI_PASS": "p"}
    assert f"binary-cache {OCI_URL}" in " ".join(str(calls["script"]).split())


def test_publish_oci_requires_explicit_count(tmp_path, monkeypatch) -> None:
    import hpc_cf.buildcache as bc

    monkeypatch.setattr(
        bc,
        "run_in_installed_image",
        lambda **kwargs: subprocess.CompletedProcess([], 0, stdout="no markers"),
    )
    with pytest.raises(RuntimeError, match="checked spec count"):
        publish_oci(
            engine="podman",
            image_ref="img",
            env_name="e",
            layout=ProjectLayout(project_root=tmp_path),
            mirror_url=OCI_URL,
        )


def _oci_spec() -> SimpleNamespace:
    return SimpleNamespace(
        spack=SimpleNamespace(
            version="1.2.0",
            env_name="cp2k-env",
            buildcache=SimpleNamespace(
                enabled=True,
                padded_length=128,
                mode=BuildcacheMode.OCI,
                url=OCI_URL,
                username_var=None,
                password_var=None,
            ),
        ),
        images=SimpleNamespace(builder="debian:13"),
    )


def test_verify_action_rejects_oci_mode(tmp_path) -> None:
    from hpc_cf.workflows import BuildcacheRequest, BuildcacheService

    layout = ProjectLayout(project_root=tmp_path)
    env_dir = layout.spack_envs_dir / PILOT / "spack-env-file"
    env_dir.mkdir(parents=True)
    resolved = SimpleNamespace(
        environment_dir=env_dir, environment_spec=_oci_spec()
    )
    with patch(
        "hpc_cf.template.resolve_build_input", return_value=resolved
    ):
        with pytest.raises(RuntimeError, match="local-mode only"):
            BuildcacheService(layout).run(
                BuildcacheRequest(action="verify", env=PILOT)
            )


def test_oci_producer_build_publishes_to_registry_and_marks_healthy(
    tmp_path,
) -> None:
    from hpc_cf.workflows import BuildcacheRequest, BuildcacheService

    layout = ProjectLayout(project_root=tmp_path)
    env_dir = layout.spack_envs_dir / PILOT / "spack-env-file"
    env_dir.mkdir(parents=True)
    (env_dir / "spack.lock").write_text('{"lock": true}\n', encoding="utf-8")
    lock_sha = hashlib.sha256(
        (env_dir / "spack.lock").read_bytes()
    ).hexdigest()
    resolved = SimpleNamespace(
        environment_dir=env_dir, environment_spec=_oci_spec()
    )
    images: dict[str, str] = {}
    publish_oci_kwargs: dict[str, object] = {}
    render_kwargs: dict[str, object] = {}

    def fake_build_stage(*, image_ref: str, **_: object) -> None:
        images[image_ref] = "sha256:producer"

    def fake_promote(*, temporary_ref: str, stable_ref: str, **_: object) -> None:
        images[stable_ref] = images[temporary_ref]

    def fake_remove(*, image_ref: str, **_: object) -> None:
        images.pop(image_ref, None)

    def fake_inspect(*, image_ref: str, **_: object) -> str:
        return images[image_ref]

    def fake_generate(**kwargs):
        render_kwargs.update(kwargs)
        return tmp_path / "Dockerfile"

    def fake_publish_oci(**kwargs):
        publish_oci_kwargs.update(kwargs)
        return (
            subprocess.CompletedProcess(
                [],
                0,
                stdout=(
                    "HPC_CF_PUSHED_SPEC_COUNT=3\n"
                    "HPC_CF_CHECKED_SPEC_COUNT=3\n"
                ),
            ),
            3,
        )

    local_publish = MagicMock()
    with (
        patch("hpc_cf.template.resolve_build_input", return_value=resolved),
        patch(
            "hpc_cf.template.resolve_image_and_tag",
            return_value=("cp2k", "2025.2"),
        ),
        patch("hpc_cf.env.run_static_checks"),
        patch("hpc_cf.buildcache.require_verified_source_mirror"),
        patch("hpc_cf.template.generate_dockerfile", side_effect=fake_generate),
        patch("hpc_cf.sif.build_docker_stage", side_effect=fake_build_stage),
        patch("hpc_cf.buildcache.promote_producer_image", side_effect=fake_promote),
        patch("hpc_cf.buildcache.remove_temporary_image", side_effect=fake_remove),
        patch("hpc_cf.buildcache.inspect_image_digest", side_effect=fake_inspect),
        patch(
            "hpc_cf.buildcache.inspect_image_lock_sha", return_value=lock_sha
        ),
        patch("hpc_cf.buildcache.publish", local_publish),
        patch("hpc_cf.buildcache.publish_oci", side_effect=fake_publish_oci),
    ):
        assert BuildcacheService(layout).run(
            BuildcacheRequest(action="build", env=PILOT)
        ) == 0

    local_publish.assert_not_called()
    assert publish_oci_kwargs["mirror_url"] == OCI_URL
    assert render_kwargs.get("buildcache_mode") == "oci"
    assert render_kwargs.get("buildcache_url") == OCI_URL
    assert render_kwargs.get("buildcache_producer") is True
    health = json.loads(layout.buildcache_health_path.read_text(encoding="utf-8"))
    assert health["healthy"] is True
    coverage = json.loads(
        (layout.buildcache_coverage_dir / f"{lock_sha}.json").read_text(
            encoding="utf-8"
        )
    )
    assert coverage["check_kind"] == "count"
    assert coverage["checked_spec_count"] == 3


# ── Consumer admission: oci variants ─────────────────────────────────────

from hpc_cf.buildcache import (  # noqa: E402
    coverage_path_for_lock,
    require_coverage,
    resolve_consumer_policy,
)
from hpc_cf.environment import BuildcachePolicy  # noqa: E402
from hpc_cf.execution import SharedBuildcacheStore  # noqa: E402


def _write_coverage_record(
    layout,
    lock_path,
    *,
    check_kind: str = "count",
    spack_version: str = "1.2.0",
    padded_length: int = 128,
    environment_provenance: dict | None = None,
) -> Path:
    path = coverage_path_for_lock(layout, lock_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "spack_version": spack_version,
                "builder_image_digest": "sha256:producer",
                "environment_provenance": environment_provenance or {},
                "padded_length": padded_length,
                "signing_policy": "unsigned",
                "check_returncode": 0,
                "checked_spec_count": 3,
                "check_kind": check_kind,
                "coverage": "non_external",
                "external_specs_excluded": True,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return path


def _admission_layout(tmp_path, *, record_kind: str | None):
    """tmp layout + lock; count/live coverage record written when asked."""
    layout = ProjectLayout(project_root=tmp_path)
    env_dir = layout.spack_envs_dir / PILOT / "spack-env-file"
    env_dir.mkdir(parents=True)
    lock_path = env_dir / "spack.lock"
    lock_path.write_text('{"lock": true}\n', encoding="utf-8")
    if record_kind is not None:
        path = _write_coverage_record(layout, lock_path, check_kind=record_kind)
        layout.buildcache_state_dir.mkdir(parents=True, exist_ok=True)
        layout.buildcache_health_path.write_text(
            json.dumps({"healthy": True, "coverage_path": str(path)}) + "\n",
            encoding="utf-8",
        )
    return layout, lock_path


def test_oci_policy_admits_on_count_record_without_local_store(tmp_path) -> None:
    layout, lock_path = _admission_layout(tmp_path, record_kind="count")
    store = SharedBuildcacheStore(layout)
    # No local buildcache directory exists at all — registry-only consumer.
    assert not layout.spack_buildcache_dir.is_dir()
    assert (
        resolve_consumer_policy(
            BuildcachePolicy.AUTO, store, lock_path=lock_path,
            backend_mode=BuildcacheMode.OCI,
        )
        is BuildcachePolicy.AUTO
    )
    assert (
        resolve_consumer_policy(
            BuildcachePolicy.ONLY, store, lock_path=lock_path,
            backend_mode=BuildcacheMode.OCI,
        )
        is BuildcachePolicy.ONLY
    )


def test_oci_policy_fails_closed_without_coverage(tmp_path) -> None:
    layout, lock_path = _admission_layout(tmp_path, record_kind=None)
    store = SharedBuildcacheStore(layout)
    assert (
        resolve_consumer_policy(
            BuildcachePolicy.AUTO, store, lock_path=lock_path,
            backend_mode=BuildcacheMode.OCI,
        )
        is BuildcachePolicy.NEVER
    )
    with pytest.raises(RuntimeError, match="no successful coverage record"):
        resolve_consumer_policy(
            BuildcachePolicy.ONLY, store, lock_path=lock_path,
            backend_mode=BuildcacheMode.OCI,
        )


def test_coverage_kind_never_crosses_backends(tmp_path) -> None:
    from hpc_cf.environment import BuildcachePolicy

    # A live (local-producer) record must not admit an oci-only consumer.
    layout, lock_path = _admission_layout(tmp_path, record_kind="live")
    layout.spack_buildcache_dir.mkdir(parents=True, exist_ok=True)
    store = SharedBuildcacheStore(layout)
    with pytest.raises(RuntimeError):
        resolve_consumer_policy(
            BuildcachePolicy.ONLY, store, lock_path=lock_path,
            backend_mode=BuildcacheMode.OCI,
        )
    # A count (registry) record must not admit a local-only consumer.
    layout2, lock2 = _admission_layout(tmp_path / "b", record_kind="count")
    layout2.spack_buildcache_dir.mkdir(parents=True, exist_ok=True)
    store2 = SharedBuildcacheStore(layout2)
    with pytest.raises(RuntimeError):
        resolve_consumer_policy(
            BuildcachePolicy.ONLY, store2, lock_path=lock2,
            backend_mode=BuildcacheMode.LOCAL,
        )


def test_require_coverage_oci_skips_digest_bind_but_keeps_gates(tmp_path) -> None:
    layout, lock_path = _admission_layout(tmp_path, record_kind="count")
    record = require_coverage(
        layout,
        lock_path,
        spack_version="1.2.0",
        builder_image=None,
        padded_length=128,
        environment_provenance={},
        backend_mode=BuildcacheMode.OCI,
    )
    assert record["check_kind"] == "count"

    # Kind mismatch: a live record cannot serve oci admission.
    with pytest.raises(RuntimeError, match="incompatible"):
        require_coverage(
            layout,
            lock_path,
            spack_version="1.2.0",
            builder_image=None,
            padded_length=128,
            environment_provenance={},
            backend_mode=BuildcacheMode.LOCAL,
        )


def test_only_build_skips_live_check_and_digest_bind_for_oci(tmp_path) -> None:
    from hpc_cf.buildcache import collect_environment_provenance
    from hpc_cf.workflows import BuildRequest, BuildService

    layout = ProjectLayout(project_root=tmp_path)
    env_dir = layout.spack_envs_dir / PILOT / "spack-env-file"
    env_dir.mkdir(parents=True)
    lock_path = env_dir / "spack.lock"
    lock_path.write_text('{"concrete_specs": {}}\n', encoding="utf-8")
    provenance = collect_environment_provenance(lock_path, env_dir)
    _write_coverage_record(
        layout, lock_path, check_kind="count", environment_provenance=provenance
    )
    layout.buildcache_state_dir.mkdir(parents=True, exist_ok=True)
    layout.buildcache_health_path.write_text(
        json.dumps(
            {
                "healthy": True,
                "coverage_path": str(coverage_path_for_lock(layout, lock_path)),
            }
        )
        + "\n",
        encoding="utf-8",
    )
    spec = SimpleNamespace(
        spack=SimpleNamespace(
            version="1.2.0",
            env_name="cp2k-env",
            buildcache=SimpleNamespace(
                enabled=True,
                padded_length=128,
                policy=None,
                mode=BuildcacheMode.OCI,
                url=OCI_URL,
                username_var=None,
                password_var=None,
            ),
        ),
    )
    resolved = SimpleNamespace(environment_dir=env_dir, environment_spec=spec)
    built: list[str] = []
    live_verify = MagicMock()
    digest_inspect = MagicMock()
    with (
        patch("hpc_cf.template.resolve_build_input", return_value=resolved),
        patch(
            "hpc_cf.template.resolve_image_and_tag",
            return_value=("cp2k", "2025.2"),
        ),
        patch("hpc_cf.env.run_static_checks"),
        patch("hpc_cf.template.generate_dockerfile", return_value=tmp_path / "D"),
        patch(
            "hpc_cf.sif.build_docker_like",
            side_effect=lambda **kwargs: built.append(kwargs.get("tag", "")),
        ),
        patch("hpc_cf.buildcache.verify", live_verify),
        patch("hpc_cf.buildcache.inspect_image_digest", digest_inspect),
    ):
        assert BuildService(layout).run(
            BuildRequest(
                app_version=PILOT,
                buildcache=BuildcachePolicy.ONLY,
                buildcache_mode=BuildcacheMode.OCI,
                buildcache_url=OCI_URL,
                use_mirror=False,
            )
        ) == 0
    assert built == ["2025.2"]
    live_verify.assert_not_called()
    digest_inspect.assert_not_called()




# ── Build secrets and credential plumbing ────────────────────────────────

from hpc_cf.sif import build_docker_like, build_docker_stage  # noqa: E402


def _captured_build_commands(tmp_path, monkeypatch, **kwargs) -> list[list[str]]:
    commands: list[list[str]] = []

    def fake_run_cmd(cmd, **_: object) -> None:
        commands.append(cmd)

    import hpc_cf.sif as sif_module

    monkeypatch.setattr(sif_module, "run_cmd", fake_run_cmd)
    dockerfile = tmp_path / "Dockerfile"
    dockerfile.write_text(
        "FROM debian:trixie AS builder-installed\nFROM debian:trixie AS builder\n",
        encoding="utf-8",
    )
    build_docker_like(
        dockerfile=dockerfile,
        image="cp2k",
        tag="t",
        engine="podman",
        network_host=True,
        **kwargs,
    )
    return commands


def test_build_secrets_rendered_on_every_stage(tmp_path, monkeypatch) -> None:
    commands = _captured_build_commands(
        tmp_path,
        monkeypatch,
        build_args=["A=1"],
        build_opts=["--no-cache"],
        build_secrets=["buildcache-creds=GHCR_CREDS"],
    )
    assert len(commands) == 3  # installed, builder, final
    for cmd in commands:
        assert "--network" in cmd and "host" in cmd
        assert "id=buildcache-creds,env=GHCR_CREDS" in cmd
        i = cmd.index("--secret")
        assert cmd[i + 1] == "id=buildcache-creds,env=GHCR_CREDS"


def test_build_secrets_default_keeps_command_unchanged(tmp_path, monkeypatch) -> None:
    commands = _captured_build_commands(tmp_path, monkeypatch)
    for cmd in commands:
        assert "--secret" not in cmd


def test_build_secret_malformed_entry_fails_closed(tmp_path, monkeypatch) -> None:
    with pytest.raises(ValueError, match="ID=ENVVAR"):
        _captured_build_commands(
            tmp_path, monkeypatch, build_secrets=["no-separator"]
        )


def test_build_docker_stage_secret_flag(tmp_path, monkeypatch) -> None:
    commands: list[list[str]] = []

    def fake_run_cmd(cmd, **_: object) -> None:
        commands.append(cmd)

    import hpc_cf.sif as sif_module

    monkeypatch.setattr(sif_module, "run_cmd", fake_run_cmd)
    build_docker_stage(
        dockerfile=tmp_path / "D",
        image_ref="img:tmp",
        target="builder-installed",
        engine="podman",
        network_host=False,
        build_secrets=["buildcache-creds=GHCR_CREDS"],
    )
    assert "id=buildcache-creds,env=GHCR_CREDS" in commands[0]


def test_cli_parses_secret_and_credential_args() -> None:
    from hpc_cf.cli import build_parser

    parser = build_parser()
    args = parser.parse_args(
        [
            "build",
            "--env",
            PILOT,
            "--buildcache-mode",
            "oci",
            "--buildcache-url",
            OCI_URL,
            "--buildcache-username-var",
            "OCI_USER",
            "--buildcache-password-var",
            "OCI_PASS",
            "--build-secret",
            "buildcache-creds=GHCR_CREDS",
            "--build-secret",
            "other=OTHER_VAR",
        ]
    )
    assert args.buildcache_mode is BuildcacheMode.OCI
    assert args.buildcache_url == OCI_URL
    assert args.buildcache_username_var == "OCI_USER"
    assert args.buildcache_password_var == "OCI_PASS"
    assert args.build_secret == [
        "buildcache-creds=GHCR_CREDS",
        "other=OTHER_VAR",
    ]


def test_cli_rejects_credential_args_without_oci_mode(capsys) -> None:
    from hpc_cf.cli import run_new_cli

    argv = [
        "build",
        "--env",
        PILOT,
        "--buildcache-username-var",
        "OCI_USER",
        "--buildcache-password-var",
        "OCI_PASS",
    ]
    with pytest.raises(SystemExit):
        run_new_cli(argv)
    assert "require" in capsys.readouterr().err


def test_render_honors_credential_var_overrides() -> None:
    def add_creds(context: dict) -> None:
        context["spack_buildcache_username_var"] = "CLI_USER"
        context["spack_buildcache_password_var"] = "CLI_PASS"

    rendered = _render(
        "only", mode="oci", url=OCI_URL, context_mutate=add_creds
    )
    install = next(
        block for block in rendered.split("RUN ") if "--use-buildcache only" in block
    )
    assert "--oci-username-variable CLI_USER" in install
    assert "--oci-password-variable CLI_PASS" in install
