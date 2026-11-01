"""Documentation drift guard: env tables must match CLI discovery.

The audit found three env tables stale at 10 envs while the CLI discovered
13. Tables are hand-maintained summaries; this test fails with regeneration
instructions whenever they drift from list_available_envs().
"""

from __future__ import annotations

import re
from pathlib import Path

from hpc_cf.config import PROJECT_ROOT
from hpc_cf.env import list_available_envs

_TABLE_DOCS = (
    "README.md",
    "docs/README.md",
    "docs/TEMPLATE_MATRIX.md",
)

_ENV_CELL_RE = re.compile(r"^\|\s*`([a-z0-9._-]+)`", re.IGNORECASE)


def _doc_env_names(path: Path) -> set[str]:
    names: set[str] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.startswith("|"):
            continue
        match = _ENV_CELL_RE.match(line)
        name = match.group(1) if match else ""
        # Skip header cells like `--app-version`; env names never start
        # with a dash and always carry a version separator.
        if name and not name.startswith("-") and "-" in name:
            names.add(name)
    return names


def test_env_tables_match_discovered_environments() -> None:
    discovered = set(list_available_envs())
    assert discovered, "expected at least one env under spack-envs/"
    for rel in _TABLE_DOCS:
        path = PROJECT_ROOT / rel
        documented = _doc_env_names(path)
        missing = discovered - documented
        extra = documented - discovered
        assert not missing and not extra, (
            f"{rel} env table drifted: missing {sorted(missing)}, "
            f"unknown {sorted(extra)} — regenerate the table from "
            "`python -m hpc_cf dockerfile --app-version` output"
        )


def test_quickstart_has_no_hardcoded_env_count() -> None:
    """Counts rot silently; the live list command is the source."""
    text = (PROJECT_ROOT / "QUICKSTART.md").read_text(encoding="utf-8")
    assert not re.search(r"\d+\s*个环境", text), (
        "QUICKSTART.md hardcodes an env count; point readers at "
        "`python -m hpc_cf dockerfile --app-version` instead"
    )


# Version column of the README env tables (docs drifted 3 rows stale once:
# 2025.2/2025.2fa listed 1.1.0, 2026.1fa 1.1.1 while env.yaml said 1.2.0).
_VERSION_ROW_RE = re.compile(
    r"^\|\s*`(?P<env>[a-z0-9._-]+)`\s*\|(?P<rest>[^|]*)\|\s*(?P<ver>\d+\.\d+\.\d+)\s*\|",
    re.IGNORECASE,
)


def test_readme_spack_versions_match_env_yaml() -> None:
    from hpc_cf.environment import load_environment_spec

    for rel in ("README.md", "docs/README.md"):
        for line in (PROJECT_ROOT / rel).read_text(encoding="utf-8").splitlines():
            match = _VERSION_ROW_RE.match(line)
            if not match:
                continue
            env = match.group("env")
            env_dir = PROJECT_ROOT / "spack-envs" / env
            assert env_dir.is_dir(), f"{rel}: unknown env row {env}"
            actual = load_environment_spec(env_dir).spack.version
            assert match.group("ver") == actual, (
                f"{rel}: {env} listed as Spack {match.group('ver')} but "
                f"env.yaml pins {actual}; regenerate the table row"
            )


_RETIRED_VAR_NAMES = (
    "force_avx512_repo_path",
    "force_avx512_repo_url",
    "force_avx512_repo_branch",
    "s8ga_repo_url",
    "s8ga_repo_commit",
    "s8ga_abacus_repo_path",
    "s8ga_vasp_repo_path",
    "s8ga_overrides_repo_path",
)


def test_docs_do_not_teach_retired_repo_vars() -> None:
    """Docs that instruct declaring retired dual-write vars send readers
    straight into a guard error (round-2 audit 2.16)."""
    for rel in ("docs/ADD_NEW_ENV.md", "docs/ASSETS_GUIDE.md"):
        text = (PROJECT_ROOT / rel).read_text(encoding="utf-8")
        for name in _RETIRED_VAR_NAMES:
            assert name not in text, (
                f"{rel} still references retired template var {name}; "
                "staging is plan-driven (spack_staging_clones.j2)"
            )
