"""Workflow hardening contracts (permissions / timeouts / concurrency).

Round-2 audit 2.6 + 2.2: pipeline jobs inherited the workflow-level
contents:write ceiling, ci-tests lacked read-only permissions and a
timeout, the pipeline concurrency group collapsed fork dispatches into
the upstream group, and stable-release happily ran env families the
pipeline does not serve. These tests pin the hardened shapes.
"""

from __future__ import annotations

import re

import yaml

from hpc_cf.config import PROJECT_ROOT

WORKFLOWS_DIR = PROJECT_ROOT / ".github" / "workflows"
ALL_WORKFLOWS = (
    "ci-tests.yml",
    "nightly-auto-retry.yml",
    "nightly-cp2k.yml",
    "pipeline-cp2k.yml",
    "regen-lock.yml",
    "stable-release.yml",
)


def _doc(name: str) -> dict:
    return yaml.safe_load((WORKFLOWS_DIR / name).read_text(encoding="utf-8"))


def test_every_normal_job_declares_timeout() -> None:
    """Reusable-workflow caller jobs cannot carry timeout-minutes; every
    job that runs steps itself must declare one."""
    for name in ALL_WORKFLOWS:
        for job_id, job in _doc(name)["jobs"].items():
            if "uses" in job:
                continue
            assert "timeout-minutes" in job, f"{name}:{job_id}"


def test_ci_tests_is_read_only() -> None:
    assert _doc("ci-tests.yml")["permissions"] == {"contents": "read"}


def test_pipeline_jobs_downgrade_from_the_write_ceiling() -> None:
    """Only sif needs contents:write; GHCR-pushing jobs carry exactly
    contents:read + packages:write."""
    jobs = _doc("pipeline-cp2k.yml")["jobs"]
    for job_id in ("prepare", "binaries", "runtime"):
        assert jobs[job_id]["permissions"] == {
            "contents": "read",
            "packages": "write",
        }, job_id
    sif = jobs["sif"]["permissions"]
    assert sif["contents"] == "write"


def test_stable_release_grants_packages_only_at_the_call_site() -> None:
    doc = _doc("stable-release.yml")
    assert doc["permissions"] == {"contents": "write"}
    pipeline = doc["jobs"]["pipeline"]
    assert "uses" in pipeline
    assert pipeline["permissions"] == {"contents": "write", "packages": "write"}


def test_pipeline_concurrency_keys_on_all_three_dimensions() -> None:
    text = (WORKFLOWS_DIR / "pipeline-cp2k.yml").read_text(encoding="utf-8")
    match = re.search(r"^  group: (hpc-cf-pipeline-.+)$", text, re.MULTILINE)
    assert match, "pipeline concurrency group not found"
    group = match.group(1)
    for dimension in ("inputs.env_name", "inputs.fork_url", "inputs.fork_branch"):
        assert dimension in group, group
    # Three independent keys, not the old fallback chain that collapsed
    # fork dispatches onto a single axis.
    assert group.count("${{") == 3, group


def test_regen_lock_serializes_with_the_upstream_variant() -> None:
    """regen-lock must land in the same concurrency family as a nightly of
    the same env (upstream-default variant of the 3-dimension group)."""
    text = (WORKFLOWS_DIR / "regen-lock.yml").read_text(encoding="utf-8")
    assert "hpc-cf-pipeline-${{ inputs.env_name }}-upstream-default" in text


def test_stable_release_whitelists_env_families() -> None:
    """Non-opensource families (VASP/MKL/ROCm) must fail the resolve step
    in seconds instead of building for hours (2026-10-29 misroute)."""
    text = (WORKFLOWS_DIR / "stable-release.yml").read_text(encoding="utf-8")
    assert "cp2k_opensource-*|abacus_opensource-*" in text
