"""GHCR/Release cleanup filters must exact-match this env's own tags.

``startswith("${ENV_NAME}-")`` cross-matches ``-force-avx512`` sibling envs
and ``-fork-*`` images (round-2 audit 2.1a): running the base env counted
siblings into its keep-N budget and pruned their images. These tests
extract the live patterns from pipeline-cp2k.yml and pin the match sets.
"""

from __future__ import annotations

import re

import yaml

from hpc_cf.config import PROJECT_ROOT

WORKFLOW = PROJECT_ROOT / ".github" / "workflows" / "pipeline-cp2k.yml"
ENV = "cp2k_opensource-2025.2"
SFX_FA = "2026.2-force-avx512"
SFX_BASE = "2025.2"


def _text() -> str:
    return WORKFLOW.read_text(encoding="utf-8")


def _substitute(pattern: str, **values: str) -> str:
    for key, value in values.items():
        pattern = pattern.replace("${" + key + "}", value)
    assert "${" not in pattern, f"unexpanded var in {pattern!r}"
    return pattern


def _matches(pattern: str, tag: str) -> bool:
    # jq test() is an unanchored search; the extracted patterns carry
    # their own ^...$ anchors, so search() mirrors the live semantics.
    return re.search(pattern, tag) is not None


def _extract_patterns() -> dict[str, str]:
    text = _text()
    calls = re.findall(
        r"ghcr_prune_versions\.sh \\\n\s+\"([^\"]+)\" \\\n\s+\"([^\"]+)\" "
        r"\\\n\s+\"([^\"]+)\" \\\n\s+([0-9]+)",
        text,
    )
    assert len(calls) == 3, f"expected 3 ghcr_prune_versions.sh calls, got {calls}"
    pats = [pattern for _repo, pattern, _keep, _n in calls]
    mirror = [p for p in pats if "${ENV_NAME}" in p]
    runtime = {p for p in pats if "${ENV_SUFFIX}" in p}
    assert len(mirror) == 1 and len(runtime) == 1
    return {"mirror": mirror[0], "runtime": runtime.pop()}


def test_workflow_yaml_parses() -> None:
    assert isinstance(yaml.safe_load(_text()), dict)


def test_no_prefix_startswith_filters_remain() -> None:
    assert "startswith($p)" not in _text()
    assert 'startswith(\\"stable-' not in _text()


def test_mirror_filter_matches_only_own_env_tags() -> None:
    pat = _substitute(_extract_patterns()["mirror"], ENV_NAME=ENV)
    assert _matches(pat, f"{ENV}-20261101")
    assert _matches(pat, f"{ENV}-20251031")
    assert _matches(pat, ENV)  # crane float tag
    assert not _matches(pat, f"{ENV}-force-avx512-20261101")
    assert not _matches(pat, f"{ENV}-fork-alice-feat-20261101")
    assert not _matches(pat, "cp2k_opensource-2026.2-20261101")


def test_runtime_filter_matches_only_own_channel_tags() -> None:
    pat = _substitute(_extract_patterns()["runtime"], ENV_SUFFIX=SFX_FA)
    assert _matches(pat, f"{SFX_FA}-nightly-20261101")
    assert _matches(pat, f"{SFX_FA}-stable-20261101")
    assert _matches(pat, f"{SFX_FA}-nightly")  # latest-attempt float
    assert _matches(pat, f"{SFX_FA}-release")  # verified float (sif job)
    # this env's fork images are NOT this env's budget
    assert not _matches(pat, f"{SFX_FA}-fork-alice-feat-nightly-20261101")
    assert not _matches(pat, "2026.2-nightly-20261101")

    base_pat = _substitute(_extract_patterns()["runtime"], ENV_SUFFIX=SFX_BASE)
    assert _matches(base_pat, f"{SFX_BASE}-nightly-20261101")
    assert not _matches(base_pat, f"{SFX_BASE}-force-avx512-nightly-20261101")


def test_runtime_filter_covers_every_caller_channel() -> None:
    """Dated runtime tags read ${ENV_SUFFIX}-${TAG_PREFIX}-<date> and
    TAG_PREFIX is the channel name, so each channel a caller passes must
    match — a filter blind to `-stable-<date>` left stable images unpruned."""
    workflows = WORKFLOW.parent
    channels = set()
    for name in ("nightly-cp2k.yml", "stable-release.yml", "regen-lock.yml"):
        text = (workflows / name).read_text(encoding="utf-8")
        channels |= set(re.findall(r"^\s*channel:\s*(\w+)\s*$", text, re.MULTILINE))
    assert channels == {"nightly", "stable"}, channels
    assert '${ENV_SUFFIX}-${TAG_PREFIX}-${NIGHTLY_DATE}' in _text()
    pat = _substitute(_extract_patterns()["runtime"], ENV_SUFFIX=SFX_FA)
    for channel in sorted(channels):
        assert _matches(pat, f"{SFX_FA}-{channel}-20261101"), channel


def test_stable_release_filter_is_exact() -> None:
    text = _text()
    literal = 'test(\\"^stable-${ENV_NAME}-[0-9]{8}$\\")'
    assert literal in text, "stable-release prune must anchor the tag exactly"
    pat = _substitute("^stable-${ENV_NAME}-[0-9]{8}$", ENV_NAME=ENV)
    assert _matches(pat, f"stable-{ENV}-20261101")
    assert not _matches(pat, f"stable-{ENV}-force-avx512-20261101")
    assert not _matches(pat, f"stable-{ENV}-fork-alice-feat-20261101")
