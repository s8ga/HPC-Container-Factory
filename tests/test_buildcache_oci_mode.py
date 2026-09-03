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
    mode, url = resolve_buildcache_backend(_spec(None))
    assert mode is BuildcacheMode.LOCAL
    assert url is None


def test_resolve_backend_cli_override_beats_env_yaml() -> None:
    spec = _spec({"mode": "oci", "url": "oci://ghcr.io/a/b"})
    mode, url = resolve_buildcache_backend(
        spec,
        mode_override=BuildcacheMode.LOCAL,
        url_override=None,
    )
    assert mode is BuildcacheMode.LOCAL
    assert url == "oci://ghcr.io/a/b"  # env url survives a local override


def test_resolve_backend_oci_requires_url_fail_closed() -> None:
    with pytest.raises(ValueError, match="requires a mirror URL"):
        resolve_buildcache_backend(None, mode_override=BuildcacheMode.OCI)


def test_resolve_backend_env_oci_mode_uses_env_url() -> None:
    mode, url = resolve_buildcache_backend(_spec({"mode": "oci", "url": OCI_URL}))
    assert mode is BuildcacheMode.OCI
    assert url == OCI_URL


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
