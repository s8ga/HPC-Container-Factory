"""Wave 4: ban libint ``--use-buildcache never`` on opensource CP2K Dockerfiles."""

from __future__ import annotations

from pathlib import Path

import pytest

from hpc_cf.template import build_context, render_template

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SPACK_ENVS = PROJECT_ROOT / "spack-envs"

OPENSOURCE_ENVS = [
    d.name
    for d in sorted(SPACK_ENVS.iterdir())
    if d.is_dir()
    and d.name.startswith("cp2k_opensource-")
    and (d / "Dockerfile.j2").is_file()
]


def _non_comment_lines(text: str) -> list[tuple[int, str]]:
    out: list[tuple[int, str]] = []
    for i, line in enumerate(text.splitlines(), 1):
        stripped = line.lstrip()
        if stripped.startswith("#"):
            continue
        out.append((i, line))
    return out


@pytest.mark.parametrize("env_name", OPENSOURCE_ENVS)
def test_opensource_dockerfile_source_bans_use_buildcache_never(env_name: str) -> None:
    tpl = SPACK_ENVS / env_name / "Dockerfile.j2"
    for lineno, line in _non_comment_lines(tpl.read_text(encoding="utf-8")):
        assert "--use-buildcache never" not in line, (
            f"{tpl}:{lineno}: opensource CP2K must not pin libint/install with "
            f"--use-buildcache never (shared authority hash reuse)"
        )


@pytest.mark.parametrize("env_name", OPENSOURCE_ENVS)
@pytest.mark.parametrize("policy", ["auto", "only", "never"])
def test_opensource_rendered_install_bans_use_buildcache_never(
    env_name: str, policy: str
) -> None:
    tpl = SPACK_ENVS / env_name / "Dockerfile.j2"
    ctx = build_context(
        use_mirror=True,
        build_only=False,
        app_version=env_name,
        template_path=tpl,
        buildcache_policy=policy,
    )
    rendered = render_template(tpl, ctx)
    for lineno, line in _non_comment_lines(rendered):
        assert "--use-buildcache never" not in line, (
            f"{env_name} policy={policy} line {lineno}: rendered Dockerfile "
            f"must not emit --use-buildcache never"
        )
