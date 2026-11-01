"""Key-name-agnostic repo-pin invariants (hpc_cf.repo_pins).

The app-shaped CP2K/s8ga checks are covered end-to-end by
``tests/test_merge_guardrails.py`` (which runs the wrapper script). These
tests target the generic invariants that must hold for ANY app: monorepo
pin unity, ``<name>_repo_commit`` var matching, the staging path contract,
and value-matched staging var wiring.
"""

from __future__ import annotations

from pathlib import Path

from hpc_cf.config import PROJECT_ROOT
from hpc_cf.environment import parse_environment_spec
from hpc_cf.repo_pins import check_env_repo_pins, check_project
from hpc_cf.validation import (
    ValidationProfile,
    validate_environment,
)

PIN = "d0ee3f460a2543c05c693317c767652abf964db7"
OTHER_PIN = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
S8GA_URL = "https://github.com/s8ga/s8ga-spack-packages.git"


def _spec(custom_repos: list[dict], template_vars: dict | None = None):
    return parse_environment_spec(
        {
            "schema_version": 1,
            "method": "spack",
            "spack": {
                "version": "1.2.0",
                "env_name": "synth-env",
                "custom_repos": custom_repos,
            },
            "template_vars": template_vars or {},
        }
    )


def _s8ga_repo(
    namespace: str,
    sparse_path: str,
    *,
    commit: str | None = PIN,
    image_path: str | None = None,
    url: str = S8GA_URL,
) -> dict:
    entry: dict = {
        "url": url,
        "branch": "master",
        "sparse_path": sparse_path,
        "namespace": namespace,
    }
    if commit is not None:
        entry["commit"] = commit
    if image_path is not None:
        entry["image_path"] = image_path
    return entry


# --- monorepo pin unity ----------------------------------------------------


def test_monorepo_pin_unity_rejects_same_url_commit_drift() -> None:
    spec = _spec(
        [
            _s8ga_repo("app", "spack_repo/app"),
            _s8ga_repo("s8_overrides", "spack_repo/s8_overrides", commit=OTHER_PIN),
        ],
        {"s8ga_repo_commit": PIN},
    )
    errors = check_env_repo_pins("synth", spec, "FROM debian:trixie\n")
    assert any("inconsistent commit pins" in e for e in errors)
    assert any("s8_overrides" in e for e in errors)


def test_monorepo_pin_unity_rejects_mixed_pinned_and_floating() -> None:
    spec = _spec(
        [
            _s8ga_repo("app", "spack_repo/app"),
            _s8ga_repo("s8_overrides", "spack_repo/s8_overrides", commit=None),
        ],
        {"s8ga_repo_commit": PIN},
    )
    errors = check_env_repo_pins("synth", spec, "FROM debian:trixie\n")
    assert any(
        "inconsistent commit pins" in e and "<floating>" in e for e in errors
    )


def test_monorepo_pin_unity_passes_when_all_floating() -> None:
    spec = _spec(
        [
            _s8ga_repo("app", "spack_repo/app", commit=None),
            _s8ga_repo("s8_overrides", "spack_repo/s8_overrides", commit=None),
        ],
    )
    errors = check_env_repo_pins("synth", spec, "FROM debian:trixie\n")
    assert not errors


# --- <name>_repo_commit vars -----------------------------------------------


def test_repo_commit_var_matching_pin_passes() -> None:
    spec = _spec(
        [_s8ga_repo("app", "spack_repo/app")],
        {"s8ga_repo_commit": PIN},
    )
    errors = check_env_repo_pins("synth", spec, "FROM debian:trixie\n")
    assert not errors


def test_repo_commit_var_orphan_is_rejected() -> None:
    spec = _spec(
        [_s8ga_repo("app", "spack_repo/app", commit=OTHER_PIN)],
        {"s8ga_repo_commit": PIN},
    )
    errors = check_env_repo_pins("synth", spec, "FROM debian:trixie\n")
    assert any(
        "template_vars.s8ga_repo_commit" in e and "matches no git custom_repos"
        in e
        for e in errors
    )


def test_repo_commit_var_non_sha_is_rejected() -> None:
    spec = _spec(
        [_s8ga_repo("app", "spack_repo/app")],
        {"my_repo_commit": "main"},
    )
    errors = check_env_repo_pins("synth", spec, "FROM debian:trixie\n")
    assert any("must be a 40-hex git commit" in e for e in errors)


def test_repo_commit_var_floating_fallback_is_allowed() -> None:
    """Floating repo's <namespace>_repo_commit var is the static fallback."""
    spec = _spec(
        [_s8ga_repo("app", "spack_repo/app", commit=None)],
        {"app_repo_commit": PIN},
    )
    errors = check_env_repo_pins("synth", spec, "FROM debian:trixie\n")
    assert not any("orphan repo-pin copy" in e for e in errors)


def test_repo_commit_var_orphan_despite_floating_repo() -> None:
    """A fallback-named var for a PINNED repo still has to match the pin."""
    spec = _spec(
        [_s8ga_repo("app", "spack_repo/app", commit=OTHER_PIN)],
        {"app_repo_commit": PIN},
    )
    errors = check_env_repo_pins("synth", spec, "FROM debian:trixie\n")
    assert any("orphan repo-pin copy" in e for e in errors)


# --- staging path contract --------------------------------------------------


def _staging_dockerfile() -> str:
    return (
        "RUN git clone --filter=blob:none --sparse --no-checkout "
        "{{ s8ga_repo_url }} /opt/s8ga-spack-packages && \\\n"
        "    git fetch --depth 1 origin "
        "{{ s8ga_repo_commit }} && \\\n"
        '    git sparse-checkout set "{{ s8ga_app_repo_path }}" '
        '"{{ s8ga_overrides_repo_path }}" && \\\n'
        '    git checkout "{{ s8ga_repo_commit }}"\n'
    )


def test_staging_path_contract_passes_with_shared_root() -> None:
    spec = _spec(
        [
            _s8ga_repo(
                "app",
                "spack_repo/app",
                image_path="/opt/s8ga-spack-packages/spack_repo/app",
            ),
            _s8ga_repo(
                "s8_overrides",
                "spack_repo/s8_overrides",
                image_path=(
                    "/opt/s8ga-spack-packages/spack_repo/s8_overrides"
                ),
            ),
        ],
        {
            "s8ga_repo_url": S8GA_URL,
            "s8ga_repo_commit": PIN,
            "s8ga_app_repo_path": "spack_repo/app",
            "s8ga_overrides_repo_path": "spack_repo/s8_overrides",
        },
    )
    errors = check_env_repo_pins("synth", spec, _staging_dockerfile())
    assert not errors


def test_staging_path_contract_rejects_suffix_mismatch() -> None:
    spec = _spec(
        [
            _s8ga_repo(
                "app",
                "spack_repo/app",
                image_path="/opt/elsewhere/app",
            ),
        ],
        {
            "s8ga_repo_url": S8GA_URL,
            "s8ga_repo_commit": PIN,
        },
    )
    errors = check_env_repo_pins("synth", spec, _staging_dockerfile())
    assert any(
        "does not end with '/spack_repo/app'" in e for e in errors
    )


def test_staging_path_contract_rejects_root_drift_within_url() -> None:
    spec = _spec(
        [
            _s8ga_repo(
                "app",
                "spack_repo/app",
                image_path="/opt/s8ga-spack-packages/spack_repo/app",
            ),
            _s8ga_repo(
                "s8_overrides",
                "spack_repo/s8_overrides",
                image_path="/opt/other-root/spack_repo/s8_overrides",
            ),
        ],
        {
            "s8ga_repo_url": S8GA_URL,
            "s8ga_repo_commit": PIN,
        },
    )
    errors = check_env_repo_pins("synth", spec, _staging_dockerfile())
    assert any("different clone roots" in e for e in errors)


def test_staging_path_contract_skips_unstaged_urls() -> None:
    """Repos whose URL the Dockerfile never clones are out of scope."""
    spec = _spec(
        [
            _s8ga_repo(
                "app",
                "spack_repo/app",
                commit=None,
                image_path="/opt/elsewhere/app",
            ),
        ],
    )
    errors = check_env_repo_pins("synth", spec, "FROM debian:trixie\n")
    assert not errors


# --- staging var wiring -----------------------------------------------------


def test_staging_var_wiring_rejects_declared_but_unused() -> None:
    spec = _spec(
        [_s8ga_repo("app", "spack_repo/app")],
        {
            "s8ga_repo_commit": PIN,
            "s8ga_app_repo_path": "spack_repo/app",
        },
    )
    # Dockerfile uses the commit var but never the sparse-path var.
    dockerfile = (
        "RUN git fetch --depth 1 origin {{ s8ga_repo_commit }}\n"
    )
    errors = check_env_repo_pins("synth", spec, dockerfile)
    assert any(
        "mirrors custom_repos['app'].sparse_path" in e
        and "does not use it" in e
        for e in errors
    )


def test_url_var_wiring_rejects_declared_but_unused() -> None:
    spec = _spec(
        [_s8ga_repo("app", "spack_repo/app")],
        {
            "s8ga_repo_url": S8GA_URL,
            "s8ga_repo_commit": PIN,
        },
    )
    dockerfile = "RUN git fetch --depth 1 origin {{ s8ga_repo_commit }}\n"
    errors = check_env_repo_pins("synth", spec, dockerfile)
    assert any(
        "mirrors custom_repos['app'].url" in e and "does not use it" in e
        for e in errors
    )


# --- validate wiring + whole-repo parity ------------------------------------


def test_validate_build_input_surfaces_repo_pin_drift(tmp_path: Path) -> None:
    conf = tmp_path / "spack-envs" / "drift-env" / "spack-env-file"
    conf.mkdir(parents=True)
    (conf / "env.yaml").write_text(
        "schema_version: 1\nmethod: spack\n"
        "spack:\n"
        "  version: '1.2.0'\n"
        "  env_name: drift-env\n"
        "  custom_repos:\n"
        f"    - url: {S8GA_URL}\n"
        "      branch: master\n"
        f"      commit: {OTHER_PIN}\n"
        "      sparse_path: spack_repo/app\n"
        "      namespace: app\n"
        "template_vars:\n"
        f"  s8ga_repo_commit: {PIN}\n",
        encoding="utf-8",
    )
    report = validate_environment(
        tmp_path / "spack-envs" / "drift-env",
        ValidationProfile.BUILD_INPUT,
    )
    drift = [f for f in report.findings if f.code == "repo_pins.inconsistent"]
    assert drift, report.to_dict()
    assert all(f.severity.value == "error" for f in drift)


def test_check_project_clean_on_shipped_envs() -> None:
    assert check_project(PROJECT_ROOT) == []
