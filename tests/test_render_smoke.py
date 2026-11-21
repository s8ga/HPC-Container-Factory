"""L1 smoke: every env's Dockerfile.j2 renders with its own env.yaml.

Catches template breakage (undefined vars, bad Jinja, broken {{ cp2k_branch }},
inconsistent parametrization) across ALL envs in one test. This is the
highest-ROI test in the suite — it protects every refactor that touches
templates or build_context.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from hpc_cf.template import (
    _extract_available_versions,
    build_context,
    render_template,
    select_template,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SPACK_ENVS = PROJECT_ROOT / "spack-envs"

ENV_NAMES = _extract_available_versions()


@pytest.mark.parametrize("env_name", ENV_NAMES)
def test_env_renders_without_error(env_name: str) -> None:
    tpl = select_template(env_name)
    ctx = build_context(
        use_mirror=True,
        build_only=False,
        app_version=env_name,
        template_path=tpl,
    )
    out = render_template(tpl, ctx)
    assert "FROM" in out
    # No unrendered Jinja variables left behind.
    assert "{{" not in out
    assert "{%" not in out


@pytest.mark.parametrize("env_name", ENV_NAMES)
def test_env_renders_build_only(env_name: str) -> None:
    """--build-only path must also render cleanly (runtime stage skipped)."""
    tpl = select_template(env_name)
    ctx = build_context(
        use_mirror=False,
        build_only=True,
        app_version=env_name,
        template_path=tpl,
    )
    out = render_template(tpl, ctx)
    assert "FROM" in out
    assert "{{" not in out
