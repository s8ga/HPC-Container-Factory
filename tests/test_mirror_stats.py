"""Mirror-stat parsing and raise-on-unknown/failure behaviour.

``_parse_mirror_stats_from_text`` returns ``failed=-1`` when it cannot parse,
so callers can distinguish "0 failures" from "couldn't tell". ``mirror_create``
/ ``mirror_verify`` must raise on ``failed < 0`` and ``failed > 0``.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from hpc_cf.spack_ops import (
    EnvConfig,
    SpackConfig,
    SpackOps,
    _parse_mirror_stats_from_text,
)

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
    """Empty/garbage must NOT read as failed=0."""
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


class _CapturingContainer:
    """Minimal stand-in: records exec scripts, returns fixed stdout for capture."""

    def __init__(self, stats_stdout: str) -> None:
        self.stats_stdout = stats_stdout
        self.scripts: list[str] = []

    def exec(self, script: str, *, capture: bool = False, check: bool = True):
        self.scripts.append(script)
        if capture:
            return SimpleNamespace(stdout=self.stats_stdout, returncode=0)
        return SimpleNamespace(stdout="", returncode=0)


def _ops(stats_stdout: str) -> SpackOps:
    env = EnvConfig(spack=SpackConfig(version="1.1.1", env_name="cp2k-env"))
    return SpackOps(env, _CapturingContainer(stats_stdout))  # type: ignore[arg-type]


def test_mirror_create_raises_on_unparseable_stats() -> None:
    with pytest.raises(RuntimeError, match="Could not determine mirror status"):
        _ops("garbage noise without stats\n").mirror_create("/work/mirror")


def test_mirror_create_raises_on_positive_failures() -> None:
    with pytest.raises(RuntimeError, match="failed to fetch"):
        _ops(GOOD_WITH_FAILURES).mirror_create("/work/mirror")


def test_mirror_verify_raises_on_unparseable_stats() -> None:
    with pytest.raises(RuntimeError, match="Could not determine mirror status"):
        _ops("").mirror_verify("/work/mirror")


def test_mirror_verify_raises_on_positive_failures() -> None:
    with pytest.raises(RuntimeError, match="still missing"):
        _ops(GOOD_WITH_FAILURES).mirror_verify("/work/mirror")
