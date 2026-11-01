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
