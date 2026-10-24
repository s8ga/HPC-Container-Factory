"""Lock-membership prune of the mirror's git snapshot cache (v1, git side).

Contract: ``spack mirror create`` only ever adds files, so the git cache
under ``_source-cache/git/`` accumulates one full-repo bundle per fetched
commit forever (a single py-torch pin costs ~4 GiB). The prune keeps only
what the current run's fetch specs identify:

  class A — git spec with a pinned commit: keep exactly the bundles whose
            filename stem matches an allowed commit (exact membership).
  class B — git spec on a floating branch (no commit in the recipe): keep
            bundles fetched during THIS run (mtime >= fresh_after); when
            none are fresh keep a single survivor (stagnant branch), the
            newest by (mtime, name).
  class C — a git dir matching no lock spec at all (py-torch submodule
            caches like cpuinfo/FXdiv): keep the whole directory.

The per-package symlink layer is repaired in step (delete links whose
target blob was deleted, drop emptied package dirs); the archive side and
``.hpc_cf`` are never touched.
"""

from __future__ import annotations

import os
from pathlib import Path

from hpc_cf.mirror_prune import (
    apply_git_prune_plan,
    plan_git_side_prune,
)

FRESH = 2_000_000.0
STALE = 0.0

TORCH_URL = "https://github.com/pytorch/pytorch.git"
KEEP_SHA = "7269437d655783a26cba32aa88195b741ff496aa"
OLD1_SHA = "0fabc3ba44823f257e70ce397d989c8de5e362c1"
OLD2_SHA = "ee1b6804381c57161c477caa380a840a84167676"


def torch_spec(commit: str | None) -> dict[str, object]:
    rec: dict[str, object] = {
        "name": "py-torch",
        "version": "2.12.1",
        "kind": "git",
        "url": TORCH_URL,
    }
    if commit is None:
        rec["commit"] = None
    else:
        rec["commit"] = commit
    return rec


def make_git_blob(tree: Path, repo: str, sha: str, *, size: int, mtime: float) -> Path:
    """Create a git bundle blob under ``_source-cache/git/<repo>/<sha>.tar.gz``."""
    blob = tree / "_source-cache" / "git" / repo / f"{sha}.tar.gz"
    blob.parent.mkdir(parents=True, exist_ok=True)
    blob.write_bytes(b"x" * size)
    os.utime(blob, (mtime, mtime))
    return blob


def make_link(tree: Path, package: str, link_name: str, target: Path) -> Path:
    pkg_dir = tree / package
    pkg_dir.mkdir(parents=True, exist_ok=True)
    link = pkg_dir / link_name
    link.symlink_to(os.path.relpath(target, pkg_dir))
    return link


def rel(tree: Path, path: Path) -> Path:
    """Plans carry tree-relative paths; absolute inputs also accepted."""
    return path if not path.is_absolute() else path.relative_to(tree)


def test_class_a_pinned_commit_keeps_exact_membership(tmp_path: Path) -> None:
    tree = tmp_path / "mirror"
    keep = make_git_blob(tree, "pytorch/pytorch.git", KEEP_SHA, size=100, mtime=STALE)
    gone1 = make_git_blob(tree, "pytorch/pytorch.git", OLD1_SHA, size=300, mtime=STALE)
    gone2 = make_git_blob(tree, "pytorch/pytorch.git", OLD2_SHA, size=500, mtime=STALE)

    plan = plan_git_side_prune(
        tree, [torch_spec(KEEP_SHA)], fresh_after=FRESH
    )

    assert sorted(rel(tree, p) for p in plan.delete_blobs) == sorted(
        [
            Path("_source-cache/git/pytorch/pytorch.git") / f"{OLD1_SHA}.tar.gz",
            Path("_source-cache/git/pytorch/pytorch.git") / f"{OLD2_SHA}.tar.gz",
        ]
    )
    assert plan.freed_bytes == 800
    assert rel(tree, keep) not in plan.delete_blobs
    assert rel(tree, gone1) in plan.delete_blobs
    assert rel(tree, gone2) in plan.delete_blobs


def test_class_a_union_of_commits_for_same_repo(tmp_path: Path) -> None:
    tree = tmp_path / "mirror"
    make_git_blob(tree, "LLNL/Umpire.git", KEEP_SHA, size=10, mtime=STALE)
    make_git_blob(tree, "LLNL/Umpire.git", OLD1_SHA, size=20, mtime=STALE)
    specs = [
        {
            "name": "umpire",
            "version": "2025.12.0",
            "kind": "git",
            "url": "https://github.com/LLNL/Umpire.git",
            "commit": KEEP_SHA,
        },
        {
            "name": "umpire",
            "version": "2025.03.0",
            "kind": "git",
            "url": "https://github.com/LLNL/Umpire",
            "commit": OLD1_SHA,
        },
    ]
    plan = plan_git_side_prune(tree, specs, fresh_after=FRESH)
    assert plan.delete_blobs == ()
    assert plan.freed_bytes == 0


def test_class_b_moving_day_keeps_fresh_deletes_stale(tmp_path: Path) -> None:
    tree = tmp_path / "mirror"
    make_git_blob(tree, "cp2k/cp2k.git", KEEP_SHA, size=400, mtime=FRESH + 10)
    make_git_blob(tree, "cp2k/cp2k.git", OLD1_SHA, size=300, mtime=STALE)

    plan = plan_git_side_prune(
        tree,
        [torch_spec(None) | {"name": "cp2k", "url": "https://github.com/cp2k/cp2k.git"}],
        fresh_after=FRESH,
    )
    assert [rel(tree, p) for p in plan.delete_blobs] == [
        Path(f"_source-cache/git/cp2k/cp2k.git/{OLD1_SHA}.tar.gz")
    ]
    assert plan.freed_bytes == 300


def test_class_b_stagnant_single_blob_kept(tmp_path: Path) -> None:
    tree = tmp_path / "mirror"
    make_git_blob(tree, "cp2k/cp2k.git", KEEP_SHA, size=400, mtime=STALE)
    plan = plan_git_side_prune(
        tree,
        [torch_spec(None) | {"name": "cp2k", "url": "https://github.com/cp2k/cp2k.git"}],
        fresh_after=FRESH,
    )
    assert plan.delete_blobs == ()
    assert plan.freed_bytes == 0


def test_class_b_stagnant_multiple_keeps_newest(tmp_path: Path) -> None:
    tree = tmp_path / "mirror"
    # Equal mtimes (the CI seed restores everything at epoch 0): the
    # deterministic tie-break is the file name, descending.
    make_git_blob(tree, "cp2k/cp2k.git", OLD1_SHA, size=100, mtime=STALE)
    make_git_blob(tree, "cp2k/cp2k.git", KEEP_SHA, size=100, mtime=STALE)
    plan = plan_git_side_prune(
        tree,
        [torch_spec(None) | {"name": "cp2k", "url": "https://github.com/cp2k/cp2k.git"}],
        fresh_after=FRESH,
    )
    assert [rel(tree, p) for p in plan.delete_blobs] == [
        Path(f"_source-cache/git/cp2k/cp2k.git/{OLD1_SHA}.tar.gz")
    ]


def test_class_b_newest_mtime_beats_name(tmp_path: Path) -> None:
    tree = tmp_path / "mirror"
    make_git_blob(tree, "cp2k/cp2k.git", OLD1_SHA, size=100, mtime=1000.0)
    make_git_blob(tree, "cp2k/cp2k.git", KEEP_SHA, size=100, mtime=999.0)
    plan = plan_git_side_prune(
        tree,
        [torch_spec(None) | {"name": "cp2k", "url": "https://github.com/cp2k/cp2k.git"}],
        fresh_after=FRESH,
    )
    # mtime dominates: the newer blob survives even though its name sorts
    # lower.
    assert [rel(tree, p) for p in plan.delete_blobs] == [
        Path(f"_source-cache/git/cp2k/cp2k.git/{KEEP_SHA}.tar.gz")
    ]


def test_class_c_unmatched_dir_kept_whole(tmp_path: Path) -> None:
    tree = tmp_path / "mirror"
    blob = make_git_blob(tree, "pytorch/cpuinfo.git", OLD1_SHA, size=31, mtime=STALE)
    plan = plan_git_side_prune(tree, [torch_spec(KEEP_SHA)], fresh_after=FRESH)
    assert plan.delete_blobs == ()
    assert plan.freed_bytes == 0
    assert any("cpuinfo" in line for line in plan.summary)
    assert blob.exists()


def test_symlink_layer_repaired_and_emptied_dir_removed(tmp_path: Path) -> None:
    tree = tmp_path / "mirror"
    keep = make_git_blob(tree, "pytorch/pytorch.git", KEEP_SHA, size=100, mtime=STALE)
    gone = make_git_blob(tree, "pytorch/pytorch.git", OLD1_SHA, size=300, mtime=STALE)
    keep_link = make_link(tree, "py-torch", "py-torch-2.12.1.tar.gz", keep)
    gone_link = make_link(tree, "py-torch", "py-torch-2.4.1.tar.gz", gone)
    # A second stale package dir whose links all die → the dir goes too.
    gone2 = make_git_blob(tree, "wavefunction91/GauXC.git", OLD2_SHA, size=50, mtime=STALE)
    make_link(tree, "gauxc", "gauxc-dev20260717.tar.gz", gone2)

    plan = plan_git_side_prune(
        tree,
        [
            torch_spec(KEEP_SHA),
            {
                "name": "gauxc",
                "version": "dev20260717",
                "kind": "git",
                "url": "https://github.com/wavefunction91/GauXC.git",
                "commit": KEEP_SHA,
            },
        ],
        fresh_after=FRESH,
    )

    assert sorted(rel(tree, p) for p in plan.delete_links) == sorted(
        [
            Path("py-torch/py-torch-2.4.1.tar.gz"),
            Path("gauxc/gauxc-dev20260717.tar.gz"),
        ]
    )
    assert plan.remove_dirs == (Path("gauxc"),)
    assert keep_link.exists() and gone_link.exists()  # dry-run changes nothing


def test_archive_side_and_hpc_cf_untouched(tmp_path: Path) -> None:
    tree = tmp_path / "mirror"
    arch = tree / "_source-cache" / "archive" / "f9" / "f9bd86f.tar.bz2"
    arch.parent.mkdir(parents=True)
    arch.write_bytes(b"y" * 700)
    os.utime(arch, (STALE, STALE))
    make_link(tree, "cp2k", "cp2k-2026.2.tar.bz2", arch)
    manifest = tree / ".hpc_cf" / "runs" / "r1" / "manifest.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text("{}")
    make_git_blob(tree, "pytorch/pytorch.git", OLD1_SHA, size=100, mtime=STALE)

    plan = plan_git_side_prune(tree, [torch_spec(KEEP_SHA)], fresh_after=FRESH)
    assert all(
        "_source-cache/archive" not in str(p) and ".hpc_cf" not in str(p)
        for p in plan.delete_blobs + plan.delete_links
    )
    assert Path("cp2k/cp2k-2026.2.tar.bz2") not in [
        rel(tree, p) for p in plan.delete_links
    ]


def test_no_git_cache_is_noop(tmp_path: Path) -> None:
    tree = tmp_path / "mirror"
    tree.mkdir()
    plan = plan_git_side_prune(tree, [torch_spec(KEEP_SHA)], fresh_after=FRESH)
    assert plan.delete_blobs == () and plan.delete_links == ()
    assert plan.freed_bytes == 0


def test_missing_tree_is_noop(tmp_path: Path) -> None:
    plan = plan_git_side_prune(
        tmp_path / "absent", [torch_spec(KEEP_SHA)], fresh_after=FRESH
    )
    assert plan.delete_blobs == () and plan.delete_links == ()
    assert plan.freed_bytes == 0


def test_malformed_specs_ignored_as_guard(tmp_path: Path) -> None:
    tree = tmp_path / "mirror"
    make_git_blob(tree, "pytorch/pytorch.git", OLD1_SHA, size=100, mtime=STALE)
    plan = plan_git_side_prune(
        tree,
        [{"name": "py-torch"}],  # missing kind/url/commit — no usable identity
        fresh_after=FRESH,
    )
    assert plan.delete_blobs == ()


def test_summary_names_files_by_package(tmp_path: Path) -> None:
    tree = tmp_path / "mirror"
    keep = make_git_blob(tree, "pytorch/pytorch.git", KEEP_SHA, size=100, mtime=STALE)
    gone = make_git_blob(tree, "pytorch/pytorch.git", OLD1_SHA, size=300, mtime=STALE)
    make_link(tree, "py-torch", "py-torch-2.12.1.tar.gz", keep)
    make_link(tree, "py-torch", "py-torch-2.4.1.tar.gz", gone)

    plan = plan_git_side_prune(tree, [torch_spec(KEEP_SHA)], fresh_after=FRESH)
    joined = "\n".join(plan.summary)
    assert "py-torch-2.12.1.tar.gz" in joined
    assert "py-torch-2.4.1.tar.gz" in joined
    assert "pytorch/pytorch.git" in joined


def test_apply_removes_blobs_links_and_dirs(tmp_path: Path) -> None:
    tree = tmp_path / "mirror"
    keep = make_git_blob(tree, "pytorch/pytorch.git", KEEP_SHA, size=100, mtime=STALE)
    gone = make_git_blob(tree, "pytorch/pytorch.git", OLD1_SHA, size=300, mtime=STALE)
    keep_link = make_link(tree, "py-torch", "py-torch-2.12.1.tar.gz", keep)
    gone_link = make_link(tree, "py-torch", "py-torch-2.4.1.tar.gz", gone)
    gone2 = make_git_blob(tree, "wavefunction91/GauXC.git", OLD2_SHA, size=50, mtime=STALE)
    make_link(tree, "gauxc", "gauxc-dev.tar.gz", gone2)

    plan = plan_git_side_prune(
        tree,
        [
            torch_spec(KEEP_SHA),
            {
                "name": "gauxc",
                "version": "dev",
                "kind": "git",
                "url": "https://github.com/wavefunction91/GauXC.git",
                "commit": KEEP_SHA,
            },
        ],
        fresh_after=FRESH,
    )
    apply_git_prune_plan(tree, plan)

    assert keep.exists() and keep_link.exists()
    assert not gone.exists() and not gone_link.exists()
    assert not gone2.exists()
    assert not (tree / "gauxc").exists()
    assert (tree / "py-torch").is_dir()


def test_url_tail_normalization() -> None:
    from hpc_cf.mirror_prune import _url_tail

    assert _url_tail("https://github.com/pytorch/pytorch.git") == "pytorch/pytorch"
    assert _url_tail("https://github.com/pytorch/pytorch") == "pytorch/pytorch"
    assert _url_tail("https://gitlab.com/group/sub/repo.git") == "sub/repo"
    assert _url_tail("https://github.com/LLNL/Umpire.git") == "llnl/umpire"
    # Two-component URL (host/repo, no owner): the host IS the tail's
    # first component — matching whatever dir layout the mirror used.
    assert _url_tail("https://example.com/repo") == "example.com/repo"
