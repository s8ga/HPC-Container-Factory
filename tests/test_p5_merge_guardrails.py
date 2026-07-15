"""P5 merge guardrails: synthetic fixtures for A–D contracts.

Covers scope / mirror / phase / StrictUndefined, plus the optional
``spack_image_repos`` partial — without touching shipped application envs.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hpc_cf.environment import parse_environment_spec
from hpc_cf.spack_plan import build_spack_environment_plan, plan_context
from hpc_cf.template import build_context, render_template


def _write_synth_env(
    root: Path,
    *,
    env_yaml: str,
    dockerfile: str,
    name: str = "p5-synth",
) -> Path:
    env_root = root / "spack-envs" / name
    conf = env_root / "spack-env-file"
    conf.mkdir(parents=True)
    (conf / "env.yaml").write_text(env_yaml, encoding="utf-8")
    (conf / "spack.yaml").write_text("spack:\n  specs: [pkgconf]\n", encoding="utf-8")
    (env_root / "Dockerfile.j2").write_text(dockerfile, encoding="utf-8")
    return env_root


def test_unknown_phase_and_scope_fail_closed() -> None:
    """Unknown custom_repos phases / repo_scope must raise (Stream B)."""
    with pytest.raises(ValueError, match=r"phases|Unknown"):
        parse_environment_spec(
            {
                "schema_version": 1,
                "spack": {
                    "version": "1.1.1",
                    "env_name": "e",
                    "custom_repos": [
                        {"path": "repos", "namespace": "x", "phases": "build"}
                    ],
                },
            }
        )
    with pytest.raises(ValueError, match=r"repo_scope|Unknown"):
        parse_environment_spec(
            {
                "schema_version": 1,
                "spack": {
                    "version": "1.1.1",
                    "env_name": "e",
                    "image": {"repo_scope": "user"},
                },
            }
        )


def test_synthetic_scope_mirror_phase_and_strict_undefined(tmp_path: Path) -> None:
    """One synthetic env exercises mirror/repo scope, image-only phase, StrictUndefined."""
    env_yaml = (
        "schema_version: 1\n"
        "method: spack\n"
        "spack:\n"
        "  version: '1.1.1'\n"
        "  env_name: synth-env\n"
        "  image:\n"
        "    repo_scope: env\n"
        "    update_builtin: true\n"
        "  custom_repos:\n"
        "    - path: repos\n"
        "      namespace: both-repo\n"
        "      phases: both\n"
        "    - path: image-only\n"
        "      namespace: image-repo\n"
        "      phases: image\n"
        "template_vars:\n"
        "  required_token: ok\n"
    )
    dockerfile = (
        "FROM debian:trixie\n"
        "TOKEN={{ required_token }}\n"
        "{% include 'partials/spack_mirror.j2' %}\n"
        "{% include 'partials/spack_image_repos.j2' %}\n"
    )
    env_root = _write_synth_env(tmp_path, env_yaml=env_yaml, dockerfile=dockerfile)
    tpl = env_root / "Dockerfile.j2"

    # Missing template_vars entry → StrictUndefined failure.
    with pytest.raises((RuntimeError, Exception), match="required_token"):
        bad_ctx = build_context(
            use_mirror=True,
            build_only=False,
            app_version="p5-synth",
            template_path=tpl,
        )
        bad_ctx.pop("required_token", None)
        render_template(tpl, bad_ctx)

    ctx = build_context(
        use_mirror=True,
        build_only=False,
        app_version="p5-synth",
        template_path=tpl,
    )
    rendered = render_template(tpl, ctx)

    assert "TOKEN=ok" in rendered
    assert "spack mirror add --scope site " in rendered
    assert "spack mirror add --scope env:" not in rendered
    assert "spack -e synth-env repo add --scope env:synth-env" in rendered
    assert "/opt/spack-env-file/repos" in rendered
    assert "/opt/spack-env-file/image-only" in rendered

    from hpc_cf.environment import load_environment_spec

    plan = build_spack_environment_plan(load_environment_spec(env_root))
    assert [r.namespace for r in plan.assets.repos] == ["both-repo"]
    assert [r.namespace for r in plan.image.repos] == ["both-repo", "image-repo"]
    assert plan.mirror_scope_flag() == "site"
    assert plan.image.scope_flag() == "env:synth-env"


def test_spack_image_repos_partial_from_plan_context(tmp_path: Path) -> None:
    """Optional partial: register image repos from plan_context only (no app wiring)."""
    spec = parse_environment_spec(
        {
            "schema_version": 1,
            "spack": {
                "version": "1.1.1",
                "env_name": "demo-env",
                "image": {"repo_scope": "site", "update_builtin": False},
                "custom_repos": [
                    {
                        "path": "repos",
                        "namespace": "local-a",
                        "phases": "both",
                    },
                    {
                        "url": "https://example.com/r.git",
                        "namespace": "git-b",
                        "sparse_path": "tools/spack/spack_repo/git_b",
                        "phases": "image",
                        "image_path": "/opt/custom/git_b",
                    },
                ],
            },
        }
    )
    plan = build_spack_environment_plan(spec)
    ctx = {
        "use_mirror": False,
        "build_only": False,
        **plan_context(plan),
    }
    tpl = tmp_path / "Dockerfile.j2"
    tpl.write_text("{% include 'partials/spack_image_repos.j2' %}\n", encoding="utf-8")
    rendered = render_template(tpl, ctx)

    assert "spack -e demo-env repo add --scope site /opt/spack-env-file/repos" in rendered
    assert "spack -e demo-env repo add --scope site /opt/custom/git_b" in rendered
    # assets-only repos must not appear; both + image do, in YAML order.
    assert rendered.index("/opt/spack-env-file/repos") < rendered.index("/opt/custom/git_b")
