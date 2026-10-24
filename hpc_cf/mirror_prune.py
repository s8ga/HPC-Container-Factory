"""Lock-membership prune of the spack source-mirror cache.

``spack mirror create`` is fill-only: it never removes files, so the
cache under ``_source-cache/`` grows without bound — one full-repo
bundle per fetched git commit (each py-torch pin alone is ~4 GiB), one
tarball per retired version pin, yesterday's floating-master bundles
kept forever.

The prune keeps only what the current run's fetch specs identify.

Git side (``_source-cache/git/``):

  class A — git spec with a pinned commit: keep exactly the bundles whose
            filename stem matches an allowed commit (exact membership).
  class B — git spec on a floating branch (no commit in the recipe): keep
            bundles fetched during THIS run (mtime >= fresh_after); the
            freshness signal is only valid inside one run because the CI
            seed/publish cycle normalizes restored mtimes to epoch 0.
            When nothing is fresh (stagnant branch, cache hit from the
            seed) keep a single survivor — the newest by (mtime, name) —
            so a non-moving branch never churns. "Only one → keep it."
  class C — a git dir matching no fetch spec at all (legacy submodule
            caches like cpuinfo/FXdiv are mirrored as their own repos but
            are not lock nodes): keep the whole directory. Identity
            uncertainty always resolves to keep.

Archive side (``_source-cache/archive/``): blobs are content-addressed —
the filename IS the source checksum. The allowed checksum set comes from
the sidecar: recipe version-dict checksums (the lock node itself carries
none), patch checksums (recipe-level superset of the lock's node
``patches``) and fetched-resource checksums. Specs without any checksum
authorize nothing — identity unknown means keep.

The per-package symlink layer (``<package>/<file> -> _source-cache/...``)
is repaired to match: links whose target blob was deleted are removed, as
are package dirs emptied by that. ``.hpc_cf`` is never touched.

Pure planning + a thin applier, so the whole policy is L0-testable
without a container; ``fresh_after`` is captured by the caller right
before ``spack mirror create`` runs.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

SOURCE_CACHE_SUBPATH = Path("_source-cache")
GIT_CACHE_SUBPATH = SOURCE_CACHE_SUBPATH / "git"
ARCHIVE_CACHE_SUBPATH = SOURCE_CACHE_SUBPATH / "archive"
PRUNE_REPORT_FILENAME = "prune-report.json"
FETCH_SPECS_FILENAME = "fetch-specs.json"


@dataclass(frozen=True)
class MirrorPrunePlan:
    """Deterministic plan: relative paths under the mirror tree root."""

    delete_blobs: tuple[Path, ...] = ()
    delete_links: tuple[Path, ...] = ()
    remove_dirs: tuple[Path, ...] = ()
    guarded_dirs: tuple[str, ...] = ()
    archive_delete_blobs: tuple[Path, ...] = ()
    freed_bytes: int = 0
    summary: tuple[str, ...] = field(default=())

    @property
    def is_noop(self) -> bool:
        return not (self.delete_blobs or self.delete_links or self.archive_delete_blobs)


# v1 name kept: the plan type now covers both cache sides.
GitPrunePlan = MirrorPrunePlan


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
    """Bundle files are ``<sha>.tar.gz``; archive blobs ``<checksum>[.ext]``.

    Checksums are hex and never contain dots, so the stem is everything
    before the first dot — for extension-less names the name itself.
    """
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


def _iter_spec_dicts(
    fetch_specs: Sequence[Mapping[str, Any]],
) -> list[Mapping[str, Any]]:
    """All usable spec records (any kind — patches/resources ride on git
    records too, e.g. py-torch)."""
    return [s for s in fetch_specs if isinstance(s, Mapping)]


def _git_specs(fetch_specs: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    """Specs usable for git-side decisions: kind=git with a URL."""
    out = []
    for spec in _iter_spec_dicts(fetch_specs):
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


def _allowed_archive_checksums(
    specs: Sequence[Mapping[str, Any]],
) -> set[str]:
    """Checksums the current DAG authorizes on the archive side.

    Sources: recipe version-dict sha256/md5, patch checksums, fetched
    resources. A spec with none of these (floating git package) adds
    nothing — its archive blobs, if any, are guarded.
    """
    allowed: set[str] = set()
    for spec in _iter_spec_dicts(specs):
        for key in ("sha256", "md5"):
            val = spec.get(key)
            if isinstance(val, str) and val:
                allowed.add(val.lower())
        for key in ("patches", "resource_checksums"):
            vals = spec.get(key)
            if isinstance(vals, (list, tuple)):
                allowed.update(
                    v.lower() for v in vals if isinstance(v, str) and v
                )
    return allowed


def plan_mirror_prune(
    tree: Path,
    fetch_specs: Sequence[Mapping[str, Any]],
    *,
    fresh_after: float,
    include_archive: bool = True,
) -> MirrorPrunePlan:
    """Compute the deletion plan for both cache sides of *tree* (dry run)."""
    git_root = tree / GIT_CACHE_SUBPATH
    specs = _iter_spec_dicts(fetch_specs)

    # Reverse index: blob → its per-package link names (for the report).
    link_names_by_target: dict[Path, list[str]] = {}
    for link in _iter_package_links(tree):
        target = _link_target_rel(tree, link)
        if target is not None:
            link_names_by_target.setdefault(target, []).append(link.name)

    doomed: list[Path] = []  # absolute blob paths, any side
    guarded: list[str] = []
    per_repo_lines: list[str] = []

    # ── git side ────────────────────────────────────────────────────────
    git_specs = _git_specs(specs)
    allowed, floating = _classify(git_specs)
    if git_root.is_dir():
        for repo_dir in _iter_git_repos(git_root):
            rel_repo = repo_dir.relative_to(tree)
            tail = _url_tail(str(rel_repo.relative_to(GIT_CACHE_SUBPATH)))
            blobs = sorted(p for p in repo_dir.iterdir() if p.is_file())

            if tail in allowed or tail in floating:
                keep = _keep_set(
                    blobs, tail, allowed, floating, fresh_after=fresh_after
                )
                repo_doomed = [b for b in blobs if b not in keep]
                freed = sum(b.stat().st_size for b in repo_doomed)
                doomed.extend(repo_doomed)
                rel_blobs = [b.relative_to(tree) for b in blobs]
                rel_doomed = [b.relative_to(tree) for b in repo_doomed]
                per_repo_lines.append(
                    _repo_summary(
                        rel_repo, rel_blobs, rel_doomed, link_names_by_target, freed
                    )
                )
            else:
                guarded.append(str(rel_repo))

    # ── archive side ────────────────────────────────────────────────────
    archive_doomed: list[Path] = []
    archive_root = tree / ARCHIVE_CACHE_SUBPATH
    archive_no_identity = False
    if include_archive and archive_root.is_dir():
        allowed_checksums = _allowed_archive_checksums(specs)
        if not allowed_checksums:
            # No spec carries a checksum — identity unknown means keep;
            # an empty allowlist must not authorize deleting everything.
            archive_no_identity = True
        else:
            for dirpath, _dirnames, filenames in os.walk(archive_root):
                for name in sorted(filenames):
                    blob = Path(dirpath) / name
                    if _blob_stem(blob) not in allowed_checksums:
                        archive_doomed.append(blob)
            doomed.extend(archive_doomed)

    doomed_rel = sorted(p.relative_to(tree) for p in doomed)
    doomed_set = set(doomed_rel)
    archive_doomed_rel = sorted(p.relative_to(tree) for p in archive_doomed)

    # ── symlink-layer repair over the union of doomed blobs ────────────
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

    # ── report ──────────────────────────────────────────────────────────
    summary_lines: list[str] = [line for line in per_repo_lines if line]
    if archive_no_identity:
        summary_lines.append(
            "archive: no checksum identity in specs — kept whole"
        )
    if archive_doomed_rel:
        gone_names = sorted(
            name
            for b in archive_doomed_rel
            for name in link_names_by_target.get(b, [b.name])
        )
        archive_freed = sum(
            (tree / p).stat().st_size for p in archive_doomed_rel
        )
        summary_lines.append(
            f"archive: delete {len(archive_doomed_rel)} blob(s) "
            f"[{', '.join(gone_names)}] — free {archive_freed / 2**30:.2f} GiB"
        )
    if guarded:
        names = ", ".join(sorted(guarded))
        summary_lines.append(
            f"guard: kept {len(guarded)} repo dir(s) not in the lock "
            f"(submodule caches): {names}"
        )
    total = sum(p.stat().st_size for p in doomed)
    summary_lines.append(
        f"prune plan: delete {len(doomed_rel)} blob(s) / "
        f"{len(delete_links)} link(s), free {total / 2**30:.2f} GiB"
    )

    return MirrorPrunePlan(
        delete_blobs=tuple(doomed_rel),
        delete_links=tuple(sorted(delete_links)),
        remove_dirs=tuple(sorted(emptied)),
        guarded_dirs=tuple(sorted(guarded)),
        archive_delete_blobs=tuple(archive_doomed_rel),
        freed_bytes=total,
        summary=tuple(summary_lines),
    )


def plan_git_side_prune(
    tree: Path,
    fetch_specs: Sequence[Mapping[str, Any]],
    *,
    fresh_after: float,
) -> MirrorPrunePlan:
    """v1 entry: git side only, archive side left untouched."""
    return plan_mirror_prune(
        tree, fetch_specs, fresh_after=fresh_after, include_archive=False
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
        if entry.name == SOURCE_CACHE_SUBPATH.name:
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


def apply_git_prune_plan(tree: Path, plan: MirrorPrunePlan) -> None:
    """Execute a plan: unlink blobs (both sides), links, emptied dirs."""
    for rel_blob in (*plan.delete_blobs, *plan.archive_delete_blobs):
        (tree / rel_blob).unlink(missing_ok=True)
    for rel_link in plan.delete_links:
        (tree / rel_link).unlink(missing_ok=True)
    for rel_dir in plan.remove_dirs:
        dir_path = tree / rel_dir
        if dir_path.is_dir():
            dir_path.rmdir()
