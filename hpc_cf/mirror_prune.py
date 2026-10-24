"""Lock-membership prune of the mirror's git snapshot cache (v1).

``spack mirror create`` is fill-only: it never removes files, so the git
cache under ``_source-cache/git/`` grows without bound — one full-repo
bundle per fetched commit (each py-torch pin alone is ~4 GiB), with
yesterday's floating-master bundles and retired pins kept forever.

The prune keeps only what the current run's fetch specs identify:

  class A — git spec with a pinned commit: keep exactly the bundles whose
            filename stem matches an allowed commit (exact membership).
  class B — git spec on a floating branch (no commit in the recipe): keep
            bundles fetched during THIS run (mtime >= fresh_after); the
            freshness signal is only valid inside one run because the CI
            seed/publish cycle normalizes restored mtimes to epoch 0.
            When nothing is fresh (stagnant branch, cache hit from the
            seed) keep a single survivor — the newest by (mtime, name) —
            so a non-moving branch never churns. "Only one → keep it."
  class C — a git dir matching no fetch spec at all (py-torch's submodule
            caches like cpuinfo/FXdiv are mirrored as their own repos but
            are not lock nodes): keep the whole directory. Identity
            uncertainty always resolves to keep.

The per-package symlink layer (``<package>/<file> -> _source-cache/...``)
is repaired to match: links whose target blob was deleted are removed, as
are package dirs emptied by that. The archive side (v2 candidate — patch
checksums do live in the lock) and ``.hpc_cf`` are never touched.

Pure planning + a thin applier, so the whole policy is L0-testable
without a container; ``fresh_after`` is captured by the caller right
before ``spack mirror create`` runs.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

GIT_CACHE_SUBPATH = Path("_source-cache") / "git"
PRUNE_REPORT_FILENAME = "prune-report.json"
FETCH_SPECS_FILENAME = "fetch-specs.json"


@dataclass(frozen=True)
class GitPrunePlan:
    """Deterministic plan: relative paths under the mirror tree root."""

    delete_blobs: tuple[Path, ...] = ()
    delete_links: tuple[Path, ...] = ()
    remove_dirs: tuple[Path, ...] = ()
    guarded_dirs: tuple[str, ...] = ()
    freed_bytes: int = 0
    summary: tuple[str, ...] = field(default=())

    @property
    def is_noop(self) -> bool:
        return not (self.delete_blobs or self.delete_links)


def _url_tail(url: str) -> str:
    """Identity key of a git URL: last two path components, lowercase.

    Host-agnostic on purpose: the mirror layout may key a repo dir with
    or without its host component depending on the git remote, and the
    ``.git`` suffix is optional on both sides.
    """
    no_scheme = url.split("://", 1)[-1]
    parts = [p for p in no_scheme.split("/") if p]
    tail = parts[-2:] if len(parts) >= 2 else parts
    return "/".join(p.lower().removesuffix(".git") for p in tail)


def _blob_stem(blob: Path) -> str:
    """Bundle files are ``<commit-sha>.tar.gz``; the stem is the sha."""
    return blob.name.split(".", 1)[0].lower()


def _iter_git_repos(git_root: Path) -> list[Path]:
    """Leaf dirs of the git cache (each holds one repo's bundle files)."""
    if not git_root.is_dir():
        return []
    repos: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(git_root):
        if not dirnames and filenames:
            repos.append(Path(dirpath))
    return sorted(repos)


def _git_specs(fetch_specs: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    """Specs usable for git-side decisions: kind=git with a URL."""
    out = []
    for spec in fetch_specs:
        if not isinstance(spec, Mapping):
            continue
        if spec.get("kind") != "git":
            continue
        url = spec.get("url")
        if not isinstance(url, str) or not url:
            continue
        out.append(spec)
    return out


def _classify(
    specs: Sequence[Mapping[str, Any]],
) -> tuple[dict[str, set[str]], set[str]]:
    """Split git specs into pinned-commit allowlists and floating repos."""
    allowed: dict[str, set[str]] = {}
    floating: set[str] = set()
    for spec in specs:
        tail = _url_tail(spec["url"])
        commit = spec.get("commit")
        if isinstance(commit, str) and commit:
            allowed.setdefault(tail, set()).add(commit.lower())
        else:
            floating.add(tail)
    return allowed, floating


def plan_git_side_prune(
    tree: Path,
    fetch_specs: Sequence[Mapping[str, Any]],
    *,
    fresh_after: float,
) -> GitPrunePlan:
    """Compute the deletion plan for the git side of *tree* (dry run)."""
    git_root = tree / GIT_CACHE_SUBPATH
    if not git_root.is_dir():
        return GitPrunePlan()

    specs = _git_specs(fetch_specs)
    allowed, floating = _classify(specs)

    # Reverse index: blob → its per-package link names (for the report).
    link_names_by_target: dict[Path, list[str]] = {}
    for link in _iter_package_links(tree):
        target = _link_target_rel(tree, link)
        if target is not None:
            link_names_by_target.setdefault(target, []).append(link.name)

    delete_blobs: list[Path] = []
    guarded: list[str] = []
    per_repo_lines: list[str] = []

    for repo_dir in _iter_git_repos(git_root):
        rel_repo = repo_dir.relative_to(tree)
        tail = _url_tail(str(rel_repo.relative_to(GIT_CACHE_SUBPATH)))
        blobs = sorted(p for p in repo_dir.iterdir() if p.is_file())

        if tail in allowed or tail in floating:
            keep = _keep_set(
                blobs, tail, allowed, floating, fresh_after=fresh_after
            )
            doomed = [b for b in blobs if b not in keep]
            freed = sum(b.stat().st_size for b in doomed)
            delete_blobs.extend(doomed)
            rel_blobs = [b.relative_to(tree) for b in blobs]
            rel_doomed = [b.relative_to(tree) for b in doomed]
            per_repo_lines.append(
                _repo_summary(rel_repo, rel_blobs, rel_doomed, link_names_by_target, freed)
            )
        else:
            guarded.append(str(rel_repo))

    delete_blobs_rel = sorted(p.relative_to(tree) for p in delete_blobs)
    doomed_set = set(delete_blobs_rel)

    delete_links: list[Path] = []
    emptied: list[Path] = []
    pkg_link_count: dict[Path, int] = {}
    for link in _iter_package_links(tree):
        rel_link = link.relative_to(tree)
        pkg_dir = rel_link.parent
        pkg_link_count[pkg_dir] = pkg_link_count.get(pkg_dir, 0) + 1
        target = _link_target_rel(tree, link)
        if target is not None and target in doomed_set:
            delete_links.append(rel_link)
            pkg_link_count[pkg_dir] -= 1
    for pkg_dir, remaining in sorted(pkg_link_count.items()):
        if remaining == 0 and not _dir_has_other_content(tree / pkg_dir):
            emptied.append(pkg_dir)

    summary_lines: list[str] = []
    for line in per_repo_lines:
        if line:
            summary_lines.append(line)
    total = sum(p.stat().st_size for p in delete_blobs)
    if guarded:
        names = ", ".join(sorted(guarded))
        summary_lines.append(
            f"guard: kept {len(guarded)} repo dir(s) not in the lock "
            f"(submodule caches): {names}"
        )
    summary_lines.append(
        f"prune plan: delete {len(delete_blobs_rel)} blob(s) / "
        f"{len(delete_links)} link(s), free {total / 2**30:.2f} GiB"
    )

    return GitPrunePlan(
        delete_blobs=tuple(delete_blobs_rel),
        delete_links=tuple(sorted(delete_links)),
        remove_dirs=tuple(sorted(emptied)),
        guarded_dirs=tuple(sorted(guarded)),
        freed_bytes=total,
        summary=tuple(summary_lines),
    )


def _keep_set(
    blobs: Sequence[Path],
    tail: str,
    allowed: Mapping[str, set[str]],
    floating: set[str],
    *,
    fresh_after: float,
) -> set[Path]:
    """Apply class A/B to one repo dir's blobs."""
    keep: set[Path] = set()
    if tail in allowed:
        wanted = allowed[tail]
        keep.update(b for b in blobs if _blob_stem(b) in wanted)
    if tail in floating:
        fresh = [b for b in blobs if b.stat().st_mtime >= fresh_after]
        if fresh:
            keep.update(fresh)
        elif len(blobs) == 1:
            keep.update(blobs)
        else:
            # Stagnant branch with a dirty history (first prune against an
            # accumulated dir): keep exactly one deterministic survivor.
            keep.add(max(blobs, key=lambda b: (b.stat().st_mtime, b.name)))
    return keep


def _repo_summary(
    rel_repo: Path,
    blobs: Sequence[Path],
    doomed: Sequence[Path],
    link_names_by_target: Mapping[Path, list[str]],
    freed: int,
) -> str:
    if not doomed:
        return ""
    kept_names = sorted(
        name
        for b in blobs
        if b not in doomed
        for name in link_names_by_target.get(b, [b.name])
    )
    gone_names = sorted(
        name for b in doomed for name in link_names_by_target.get(b, [b.name])
    )
    return (
        f"{rel_repo.relative_to(GIT_CACHE_SUBPATH)}: keep "
        f"[{', '.join(kept_names) or '-'}], delete "
        f"[{', '.join(gone_names)}] — free {freed / 2**30:.2f} GiB"
    )


def _iter_package_links(tree: Path) -> list[Path]:
    """Symlinks in the per-package layer (top level, not _source-cache)."""
    if not tree.is_dir():
        return []
    links: list[Path] = []
    for entry in sorted(tree.iterdir()):
        if not entry.is_dir() or entry.name.startswith("."):
            continue
        if entry.name == "_source-cache":
            continue
        for child in sorted(entry.iterdir()):
            if child.is_symlink():
                links.append(child)
    return links


def _link_target_rel(tree: Path, link: Path) -> Path | None:
    """Resolve a package link to its path relative to *tree* (no follow)."""
    raw = os.readlink(link)
    target = os.path.normpath(link.parent / raw)
    try:
        return Path(target).relative_to(tree)
    except ValueError:
        return None


def _dir_has_other_content(pkg_dir: Path) -> bool:
    """True when non-link content remains (stray files, kept links)."""
    return any(not child.is_symlink() for child in pkg_dir.iterdir())


def apply_git_prune_plan(tree: Path, plan: GitPrunePlan) -> None:
    """Execute a plan: unlink blobs, their links, then emptied dirs."""
    for rel_blob in plan.delete_blobs:
        (tree / rel_blob).unlink(missing_ok=True)
    for rel_link in plan.delete_links:
        (tree / rel_link).unlink(missing_ok=True)
    for rel_dir in plan.remove_dirs:
        dir_path = tree / rel_dir
        if dir_path.is_dir():
            dir_path.rmdir()
