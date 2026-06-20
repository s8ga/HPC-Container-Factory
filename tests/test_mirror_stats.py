"""L4: mirror-stat parsing must NOT silently report success on failure.

The bug (spack_ops._parse_mirror_stats): ``except Exception: pass`` returned
``failed=0`` for ANY parse problem, while callers only raised when ``failed>0``.
Result: a broken/incomplete mirror was reported as success.

A3 extracts a pure ``_parse_mirror_stats_from_text`` and makes it return
``failed=-1`` (sentinel for "could not determine") when it cannot parse,
so callers can distinguish "0 failures" from "couldn't tell".
"""
from __future__ import annotations

import pytest

from hpc_cf.spack_ops import _parse_mirror_stats_from_text

GOOD = """
==> Warning: something
==> 12 already present
==> 34 added
==> 0 failed
"""
GOOD_WITH_FAILURES = """
==> 1 already present
==> 2 added
==> 5 failed
"""


def test_parses_good_output() -> None:
    stats = _parse_mirror_stats_from_text(GOOD)
    assert stats == {"present": 12, "added": 34, "failed": 0}


def test_parses_failures() -> None:
    stats = _parse_mirror_stats_from_text(GOOD_WITH_FAILURES)
    assert stats["failed"] == 5
    assert stats["added"] == 2


def test_empty_text_is_unknown_not_success() -> None:
    """The core regression: empty/garbage must NOT read as failed=0."""
    stats = _parse_mirror_stats_from_text("")
    assert stats["failed"] < 0  # sentinel: "could not determine"


def test_garbage_text_is_unknown_not_success() -> None:
    stats = _parse_mirror_stats_from_text("totally unrelated spack noise\n")
    assert stats["failed"] < 0


@pytest.mark.parametrize(
    "text",
    ["", "   \n  \n", "no numbers here", "Spack internal error trace"],
)
def test_never_reports_zero_failures_on_garbage(text: str) -> None:
    stats = _parse_mirror_stats_from_text(text)
    assert stats["failed"] != 0, f"garbage must not parse as failed=0: {text!r}"
