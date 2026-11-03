"""GHCR package-version pruning: shared script executed against a fake gh.

The inline prune steps piped jq's ``--arg`` into ``gh api`` (no such
flag): every run since 2026-09-07 died with "unknown flag: --arg" inside
a process substitution — invisible to ``set -e`` — and the steps printed
"within budget" while deleting nothing. These tests run the real script
end to end: cross-page keep-N, sibling-tag immunity, keep-tag protection,
and loud failure when the query itself fails.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import textwrap
from pathlib import Path

from hpc_cf.config import PROJECT_ROOT

SCRIPT = PROJECT_ROOT / "scripts" / "ghcr_prune_versions.sh"
WORKFLOW = PROJECT_ROOT / ".github" / "workflows" / "pipeline-cp2k.yml"


def _version(vid: int, day: str, tags: list[str]) -> dict:
    return {
        "id": vid,
        "created_at": f"2026-{day}T10:00:00Z",
        "metadata": {"container": {"tags": tags}},
    }


# Two pages (GitHub paginates at 30): the env's dated versions span both,
# plus decoys that must never match — a sibling env, a fork image, an
# unrelated package tag.
PAGES = [
    [
        _version(101, "10-29", ["demo-20261029"]),
        _version(102, "10-30", ["demo-20261030"]),
        _version(103, "10-31", ["demo-20261031"]),
    ],
    [
        _version(104, "11-01", ["demo-20261101"]),
        _version(105, "11-02", ["demo-20261102", "demo-fork-alice-nightly-20261102"]),
        _version(106, "11-03", ["demo-20261103", "demo"]),
        _version(107, "11-04", ["siblingenv-20261104"]),
    ],
]
PATTERN = "^demo(-[0-9]{8})?$"

FAKE_GH = textwrap.dedent(
    """
    #!/usr/bin/env bash
    set -u
    if [ "$1" = api ] && [[ "$*" == *--paginate* ]]; then
      if [ -f "$FIXTURE_DIR/fail-fetch" ]; then
        echo "fake gh: fetch failed" >&2
        exit 7
      fi
      cat "$FIXTURE_DIR/pages.json"
      exit 0
    fi
    if [ "$1" = api ] && [ "$2" = -X ] && [ "$3" = DELETE ]; then
      echo "${4##*/}" >> "$FIXTURE_DIR/deletes.log"
      exit 0
    fi
    echo "fake gh: unexpected args: $*" >&2
    exit 64
    """
)


def _run_prune(
    tmp_path: Path,
    pages: list[list[dict]],
    pattern: str,
    keep_tag: str,
    keep_n: int,
    *,
    fail_fetch: bool = False,
) -> subprocess.CompletedProcess:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "gh").write_text(FAKE_GH, encoding="utf-8")
    (bin_dir / "gh").chmod(0o755)
    fixture = tmp_path / "fixture"
    fixture.mkdir()
    # gh api --paginate streams the raw page bodies back to back; jq -rs
    # in the script slurps the concatenated stream.
    (fixture / "pages.json").write_text(
        "".join(json.dumps(page) for page in pages), encoding="utf-8"
    )
    if fail_fetch:
        (fixture / "fail-fetch").write_text("", encoding="utf-8")
    env = {
        **os.environ,
        "PATH": f"{bin_dir}:/usr/bin:/bin",
        "FIXTURE_DIR": str(fixture),
    }
    return subprocess.run(
        [
            "bash",
            str(SCRIPT),
            "/user/packages/container/fake-pkg/versions",
            pattern,
            keep_tag,
            str(keep_n),
        ],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def _deleted(tmp_path: Path) -> list[str]:
    log = tmp_path / "fixture" / "deletes.log"
    if not log.is_file():
        return []
    return log.read_text(encoding="utf-8").split()


def test_script_exists_and_passes_bash_n() -> None:
    assert SCRIPT.is_file()
    subprocess.run(["bash", "-n", str(SCRIPT)], check=True)


def test_keep_n_counts_across_pages(tmp_path: Path) -> None:
    """6 matching versions split 3+3 over two pages, keep 3: the 3 oldest
    are deleted. gh's --paginate runs --jq per page, so the old inline
    version saw page 1 alone (3 items, nothing to prune) and never got to
    page 2."""
    proc = _run_prune(tmp_path, PAGES, PATTERN, "demo-20261103", 3)
    assert proc.returncode == 0, proc.stderr
    assert sorted(_deleted(tmp_path)) == ["101", "102", "103"]


def test_sibling_and_fork_tags_are_never_matched(tmp_path: Path) -> None:
    proc = _run_prune(tmp_path, PAGES, PATTERN, "demo-20261103", 0)
    # keep 0 (fork semantics): everything matching goes except this run's
    # tag — ids 101-105 are the SAME env's dated versions, while the
    # sibling (107) never matches the anchored pattern.
    assert proc.returncode == 0, proc.stderr
    assert sorted(_deleted(tmp_path)) == ["101", "102", "103", "104", "105"]


def test_keep_tag_protects_even_the_oldest_version(tmp_path: Path) -> None:
    pages = [
        [
            _version(201, "10-20", ["demo-20261020"]),  # carries this run's tag
            _version(202, "10-21", ["demo-20261021"]),
            _version(203, "10-22", ["demo-20261022"]),
        ],
        [
            _version(204, "10-23", ["demo-20261023"]),
            _version(205, "10-24", ["demo-20261024"]),
        ],
    ]
    proc = _run_prune(tmp_path, pages, PATTERN, "demo-20261020", 2)
    assert proc.returncode == 0, proc.stderr
    # newest 2 (205, 204) kept by budget; 201 spared by the keep tag.
    assert sorted(_deleted(tmp_path)) == ["202", "203"]


def test_within_budget_deletes_nothing(tmp_path: Path) -> None:
    pages = [[_version(301, "11-01", ["demo-20261101"])]]
    proc = _run_prune(tmp_path, pages, PATTERN, "demo-20261103", 3)
    assert proc.returncode == 0, proc.stderr
    assert _deleted(tmp_path) == []
    assert "nothing to delete" in proc.stdout


def test_fetch_failure_fails_loudly(tmp_path: Path) -> None:
    """A failed query must fail the step. The old code fetched inside
    <( ), so gh's exit status was swallowed and the step reported
    'within budget'."""
    proc = _run_prune(
        tmp_path, PAGES, PATTERN, "demo-20261103", 3, fail_fetch=True
    )
    assert proc.returncode != 0
    assert _deleted(tmp_path) == []


def test_workflow_calls_the_script_three_times() -> None:
    """The three prune steps (mirror keep-3, fork keep-1, runtime keep-7)
    all delegate to the shared script — no hand-rolled gh api queries left
    in the workflow."""
    text = WORKFLOW.read_text(encoding="utf-8")
    calls = [
        line
        for line in text.splitlines()
        if "ghcr_prune_versions.sh" in line and "run:" not in line
    ]
    assert len(calls) == 3, f"expected 3 script calls, found {len(calls)}"
    # The jq-flag footgun cannot come back: the version queries (with
    # their patterns) live only in the script, and --arg is never passed
    # to gh api.
    assert "gh api --paginate" not in text
    assert "--arg" not in text
    # The script itself pins the cross-page mechanism, and --arg only
    # ever reaches jq — never gh api (the original footgun).
    script_text = SCRIPT.read_text(encoding="utf-8")
    assert "--paginate" in script_text
    assert "jq -rs" in script_text
    exec_lines = [
        line for line in script_text.splitlines() if not line.lstrip().startswith("#")
    ]
    assert all("jq" in line for line in exec_lines if "--arg" in line)


def test_prune_budgets_are_pinned() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")
    calls = re.findall(
        r"ghcr_prune_versions\.sh \\\n\s+\"([^\"]+)\" \\\n\s+\"([^\"]+)\" "
        r"\\\n\s+\"([^\"]+)\" \\\n\s+([0-9]+)",
        text,
    )
    assert len(calls) == 3, calls
    by_repo = {}
    for repo, pattern, keep_tag, keep_n in calls:
        by_repo.setdefault(repo.split("/")[4], []).append((pattern, keep_tag, int(keep_n)))
    assert by_repo["hpc-cf-inputs"] == [
        ("^${ENV_NAME}(-[0-9]{8})?$", "${ENV_NAME}-${NIGHTLY_DATE}", 3)
    ]
    runtime = sorted(by_repo["${IMAGE_NAME}"], key=lambda c: c[2])
    assert runtime == [
        ("^${ENV_SUFFIX}-(nightly|stable|release)(-[0-9]{8})?$", "${ENV_SUFFIX}-nightly-${NIGHTLY_DATE}", 0),
        ("^${ENV_SUFFIX}-(nightly|stable|release)(-[0-9]{8})?$", "${ENV_SUFFIX}-${TAG_PREFIX}-${NIGHTLY_DATE}", 7),
    ]
