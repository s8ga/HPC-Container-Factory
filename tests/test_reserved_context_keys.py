"""template_vars must not shadow factory-computed build-context keys.

``build_context`` spreads ``**template_vars`` AFTER the computed defaults, so
an env.yaml variable named ``use_mirror`` / ``default_image_tag`` /
``spack_version`` … would silently clobber a CLI-derived value (audit 2.1,
round 2 P0). These tests pin the reserved-key rejection at parse time, its
surfacing through validate, and the drift contract between the reserved set
and what build_context/plan_context actually emit.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hpc_cf.config import PROJECT_ROOT
from hpc_cf.environment import (
    RESERVED_CONTEXT_KEYS,
    parse_environment_spec,
)
from hpc_cf.spack_plan import build_spack_environment_plan, plan_context
from hpc_cf.template import build_context, select_template
from hpc_cf.validation import (
    ValidationProfile,
    validate_environment,
)

NO_SPACK_PAYLOAD_KEYS = frozenset(
    {"script", "runtime_copy_dirs", "runtime_extra_pkgs"}
)


def _env_yaml(template_vars: dict[str, object]) -> dict[str, object]:
    return {
        "schema_version": 1,
        "method": "spack",
        "spack": {"version": "1.2.0", "env_name": "reserved-env"},
        "template_vars": template_vars,
    }


@pytest.mark.parametrize("key", sorted(RESERVED_CONTEXT_KEYS))
def test_parse_rejects_each_reserved_template_var(key: str) -> None:
    with pytest.raises(ValueError, match="shadow build-context keys"):
        parse_environment_spec(_env_yaml({key: "x"}))


def test_apt_mirror_is_the_documented_exception() -> None:
    """env.yaml apt_mirror is an allowed channel (CLI --apt-mirror wins)."""
    assert "apt_mirror" not in RESERVED_CONTEXT_KEYS
    spec = parse_environment_spec(_env_yaml({"apt_mirror": "mirrors.example.com"}))
    assert spec.template_vars["apt_mirror"] == "mirrors.example.com"


def test_validate_config_surfaces_reserved_key_as_schema_error(
    tmp_path: Path,
) -> None:
    env_dir = tmp_path / "spack-envs" / "reserved-env"
    env_dir.mkdir(parents=True)
    report = validate_environment(
        env_dir,
        ValidationProfile.CONFIG,
        env_config=_env_yaml({"use_mirror": "false"}),  # type: ignore[arg-type]
    )
    schema = [f for f in report.findings if f.code == "schema.invalid"]
    assert schema and all(f.severity.value == "error" for f in schema)
    assert "use_mirror" in schema[0].message
    assert not report.ok


def test_lock_source_commits_cannot_target_reserved_key() -> None:
    raw = _env_yaml({})
    raw["spack"] = {
        "version": "1.2.0",
        "env_name": "reserved-env",
        "lock_source_commits": {"cp2k": "spack_version"},
    }
    with pytest.raises(ValueError, match="reserved"):
        parse_environment_spec(raw)


def test_plan_context_keys_are_all_reserved() -> None:
    spec = parse_environment_spec(_env_yaml({}))
    keys = set(plan_context(build_spack_environment_plan(spec)))
    assert keys <= RESERVED_CONTEXT_KEYS, sorted(keys - RESERVED_CONTEXT_KEYS)


def test_build_context_emits_all_reserved_keys() -> None:
    """The reserved set must track what build_context really computes.

    A spack render covers every reserved key except the no_spack payload
    (script / runtime_*); those three are checked against a no_spack env.
    """
    pilot = "cp2k_opensource-2026.2-force-avx512"
    tpl = select_template(pilot)
    spack_ctx = build_context(
        use_mirror=True,
        build_only=False,
        app_version=pilot,
        template_path=tpl,
    )
    missing = (RESERVED_CONTEXT_KEYS - NO_SPACK_PAYLOAD_KEYS) - set(spack_ctx)
    assert not missing, sorted(missing)


def test_nospack_build_context_emits_payload_keys(tmp_path: Path) -> None:
    import shutil

    from hpc_cf.execution import ProjectLayout
    from hpc_cf.template import resolve_build_input

    layout = ProjectLayout(project_root=tmp_path)
    templates = tmp_path / "templates"
    (templates / "partials").mkdir(parents=True)
    shutil.copy2(
        PROJECT_ROOT / "templates" / "Dockerfile.nospack.j2",
        templates / "Dockerfile.nospack.j2",
    )
    shutil.copy2(
        PROJECT_ROOT / "templates" / "partials" / "locale_c_utf8.j2",
        templates / "partials" / "locale_c_utf8.j2",
    )
    env_dir = tmp_path / "spack-envs" / "nospack-x"
    env_dir.mkdir(parents=True)
    (env_dir / "env.yaml").write_text(
        "schema_version: 1\nmethod: no_spack\n"
        "spack:\n  version: '1.2.0'\n  env_name: nospack-env\n"
        "script: echo hi\n",
        encoding="utf-8",
    )
    resolved = resolve_build_input("nospack-x", None, layout=layout)
    ctx = build_context(
        use_mirror=False,
        build_only=False,
        app_version="nospack-x",
        template_path=resolved.render_template,
        resolved=resolved,
        layout=layout,
    )
    assert NO_SPACK_PAYLOAD_KEYS <= set(ctx)
