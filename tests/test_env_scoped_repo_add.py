"""Render guard: env-scoped ``spack repo add`` must use ``spack -e``.

When a Dockerfile registers a repo with ``--scope env:<name>``, the same
command fragment must activate the environment via ``spack -e`` (site-scoped
adds are out of scope for this guard). Checking per ``&&``/``;`` fragment
ensures a neighboring ``repo ls`` cannot falsely satisfy the rule.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from hpc_cf.config import SPACK_ENVS_DIR
from hpc_cf.environment import BuildMethod, load_environment_spec
from hpc_cf.template import build_context, render_template, select_template

# ``repo add`` with env scope must include ``spack -e`` on the same logical line.
_REPO_ADD_ENV_SCOPE = re.compile(
    r"repo\s+add\b.*--scope\s+env:",
    re.IGNORECASE,
)
_SPACK_E = re.compile(r"\bspack\s+-e\b")


def _logical_lines(text: str) -> list[str]:
    """Join Dockerfile backslash continuations into single logical lines."""
    joined = text.replace("\\\n", " ")
    return [ln.strip() for ln in joined.splitlines() if ln.strip()]


def env_scoped_repo_add_violations(rendered: str) -> list[str]:
    """Return ``repo add --scope env:`` statements that lack ``spack -e``.

    Dockerfile ``\\`` continuations are joined first, then each ``&&`` / ``;``
    fragment is checked so a later ``spack -e ... repo ls`` cannot satisfy an
    earlier bare ``spack repo add --scope env:...``.
    """
    bad: list[str] = []
    for logical in _logical_lines(rendered):
        for part in re.split(r"&&|;", logical):
            part = part.strip()
            if not part or not _REPO_ADD_ENV_SCOPE.search(part):
                continue
            if _SPACK_E.search(part):
                continue
            bad.append(part)
    return bad


def test_env_scoped_repo_add_helper_accepts_spack_e() -> None:
    good = (
        "RUN spack -e cp2k-env repo add --scope env:cp2k-env /opt/repos/ && \\\n"
        "    spack -e cp2k-env repo add --scope env:cp2k-env /opt/other/\n"
    )
    assert env_scoped_repo_add_violations(good) == []


def test_env_scoped_repo_add_helper_rejects_missing_e() -> None:
    # Neighboring ``spack -e ... repo ls`` must NOT count — the add itself needs -e.
    bad = (
        "RUN spack repo add --scope env:cp2k-env /opt/repos/ && \\\n"
        "    spack -e cp2k-env repo ls\n"
    )
    violations = env_scoped_repo_add_violations(bad)
    assert len(violations) == 1
    assert "repo add" in violations[0]
    assert "--scope env:cp2k-env" in violations[0]


def test_env_scoped_repo_add_helper_ignores_site_scope() -> None:
    site = (
        "RUN spack repo add --scope site /opt/repos/ && \\\n"
        "    spack -e vasp-env repo ls\n"
    )
    assert env_scoped_repo_add_violations(site) == []


def _spack_env_dirs() -> list[Path]:
    return [
        d
        for d in sorted(SPACK_ENVS_DIR.iterdir())
        if d.is_dir()
        and (
            (d / "spack-env-file" / "env.yaml").exists()
            or (d / "env.yaml").exists()
        )
    ]


@pytest.mark.parametrize("env_dir", _spack_env_dirs(), ids=lambda p: p.name)
def test_shipped_dockerfiles_env_scoped_repo_add_uses_spack_e(env_dir: Path) -> None:
    """Shipped renders: any ``repo add --scope env:`` must include ``spack -e``."""
    spec = load_environment_spec(env_dir)
    if spec.method is BuildMethod.NO_SPACK:
        pytest.skip("no_spack")
    if not (env_dir / "Dockerfile.j2").exists():
        pytest.skip("no per-env Dockerfile.j2")

    template_path = select_template(env_dir.name)
    context = build_context(
        use_mirror=False,
        build_only=False,
        app_version=env_dir.name,
        template_path=template_path,
    )
    rendered = render_template(template_path, context)
    violations = env_scoped_repo_add_violations(rendered)
    assert not violations, (
        f"{env_dir.name}: env-scoped repo add must use 'spack -e' on the same "
        f"command:\n  - " + "\n  - ".join(violations)
    )
