#!/usr/bin/env python3
"""Query concrete Spack environment specs inside image builds without inline -c.

Intended to be invoked via ``spack -e <env> python /usr/local/bin/spack_dag_query.py``
inside builder/publisher containers, while keeping pure filtering and summary
helpers directly unit-testable with plain Python stdlib.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any


def parse_excludes(values: Sequence[str] | None) -> frozenset[str]:
    """Parse repeatable or comma-separated package names to exclude."""
    excluded: set[str] = set()
    for raw in values or ():
        for part in raw.split(","):
            name = part.strip()
            if name:
                excluded.add(name)
    return frozenset(excluded)


def select_hashes(
    specs: Iterable[Any],
    *,
    installed_only: bool,
    exclude: frozenset[str] = frozenset(),
) -> list[str]:
    """Return ``/<dag_hash>`` strings for non-external concrete specs."""
    out: list[str] = []
    seen: set[str] = set()
    for spec in specs:
        if getattr(spec, "external", False):
            continue
        if exclude and getattr(spec, "name", "") in exclude:
            continue
        if installed_only and not getattr(spec, "installed", False):
            continue
        dag_hash = spec.dag_hash() if callable(getattr(spec, "dag_hash", None)) else str(spec.dag_hash)
        ref = f"/{dag_hash}"
        if ref not in seen:
            seen.add(ref)
            out.append(ref)
    return out


def build_summary(
    specs: Iterable[Any],
    *,
    exclude: frozenset[str] = frozenset(),
    install_rc: int = 0,
) -> dict[str, Any]:
    """Summarize installed vs planned non-external concrete specs."""
    spec_list = list(specs)
    installed = select_hashes(spec_list, installed_only=True, exclude=exclude)
    planned = select_hashes(spec_list, installed_only=False, exclude=exclude)
    complete = len(planned) > 0 and len(installed) == len(planned)
    return {
        "installed_hashes": installed,
        "planned_hashes": planned,
        "installed_count": len(installed),
        "planned_count": len(planned),
        "checked_count": len(planned),
        "complete": complete,
        "partial_install": (not complete) or install_rc != 0,
        "install_rc": install_rc,
    }


def resolve_container_ident(
    lock_path: Path,
    *,
    spec_name: str,
    branch: str | None = None,
    fallback: str = "",
) -> str:
    """Resolve ``<branch>@<short_sha>`` or concrete version from ``spack.lock``."""
    try:
        lock = json.loads(lock_path.read_text(encoding="utf-8"))
        concrete = lock.get("concrete_specs", {})
        node = next(
            (s for s in concrete.values() if s.get("name") == spec_name),
            None,
        )
        if node is None:
            return fallback
        params = node.get("parameters", {})
        commit = params.get("commit")
        if commit and branch:
            return f"{branch}@{str(commit)[:12]}"
        if commit:
            return str(commit)[:12]
        version = node.get("version")
        return str(version) if version else fallback
    except Exception:
        return fallback


def _load_active_specs() -> list[Any]:
    import spack.environment as ev

    env = ev.active_environment()
    if env is None:
        raise RuntimeError("No active Spack environment (run with spack -e <env> python)")
    return list(env.all_specs())


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Spack DAG query helper")
    sub = parser.add_subparsers(dest="command", required=True)

    for cmd_name in ("installed-hashes", "planned-hashes"):
        p = sub.add_parser(cmd_name)
        p.add_argument("--exclude", action="append", default=[])
        p.add_argument("--count", action="store_true")

    summary_p = sub.add_parser("summary")
    summary_p.add_argument("--exclude", action="append", default=[])
    summary_p.add_argument("--install-rc", type=int, default=0)
    summary_p.add_argument("--output", type=Path, default=None)

    ident_p = sub.add_parser("container-ident")
    ident_p.add_argument("--lock", type=Path, required=True)
    ident_p.add_argument("--spec", required=True)
    ident_p.add_argument("--branch", default=None)
    ident_p.add_argument("--fallback", default="")
    ident_p.add_argument("--output", type=Path, default=None)

    args = parser.parse_args(argv)
    if args.command == "container-ident":
        ident = resolve_container_ident(
            args.lock,
            spec_name=args.spec,
            branch=args.branch,
            fallback=args.fallback,
        )
        if args.output is not None:
            args.output.write_text(ident, encoding="utf-8")
        else:
            print(ident)
        return 0

    specs = _load_active_specs()
    exclude = parse_excludes(args.exclude)
    if args.command == "installed-hashes":
        hashes = select_hashes(specs, installed_only=True, exclude=exclude)
        print(len(hashes) if args.count else "\n".join(hashes))
        return 0
    if args.command == "planned-hashes":
        hashes = select_hashes(specs, installed_only=False, exclude=exclude)
        print(len(hashes) if args.count else "\n".join(hashes))
        return 0
    if args.command == "summary":
        payload = build_summary(specs, exclude=exclude, install_rc=args.install_rc)
        rendered = json.dumps(payload, indent=2, sort_keys=True) + "\n"
        if args.output is not None:
            args.output.write_text(rendered, encoding="utf-8")
        else:
            sys.stdout.write(rendered)
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
