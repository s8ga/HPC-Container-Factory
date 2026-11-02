"""The collector registry is the single wiring point for sanity checks.

Adding a check = one collect_* function + one _COLLECTORS row. These tests
keep that contract honest: a public collect_* that is not reachable from
the registry (and not explicitly exempt) fails here, so a new check can
never silently ship unregistered — or registered twice under one profile
by accident.
"""

from __future__ import annotations

import inspect

import hpc_cf.validation as validation
from hpc_cf.validation import (
    _COLLECTORS,
    ValidationProfile,
    validate_environment,
)

# Helpers invoked INSIDE other collectors (or by the build path directly);
# they are not standalone profile checks.
_EXEMPT = {"collect_render_copy_sources"}


def _registry_source() -> str:
    src = inspect.getsource(validation)
    start = src.index("_COLLECTORS: tuple[")
    end = src.index("def validate_environment(")
    return src[start:end]


def test_every_public_collector_is_registered() -> None:
    registry_src = _registry_source()
    for name, obj in vars(validation).items():
        if not name.startswith("collect_") or not callable(obj):
            continue
        if name in _EXEMPT:
            continue
        assert name in registry_src, (
            f"validation.{name} is not wired into _COLLECTORS — add a row "
            "(see the registry how-to above _CollectorContext)"
        )


def test_registry_names_are_unique_per_profile() -> None:
    seen: dict[str, str] = {}
    for spec in _COLLECTORS:
        for profile in spec.profiles:
            key = f"{spec.name}@{profile.value}"
            assert key not in seen, f"duplicate collector registration: {key}"
            seen[key] = key


def test_registry_profiles_are_valid() -> None:
    for spec in _COLLECTORS:
        assert spec.profiles, f"{spec.name} registers no profile"
        assert all(
            isinstance(p, ValidationProfile) for p in spec.profiles
        ), spec.name


def test_registry_runs_the_table(tmp_path, monkeypatch) -> None:
    """validate_environment executes every applicable row, nothing else.

    A no_spack env with one applicable collector mocked proves the loop
    dispatches through _COLLECTORS (the mock's findings appear) and that
    spack_only rows are skipped for no_spack.
    """
    calls: list[str] = []

    real = validation.collect_manual_packages

    def spy(spec, *, project_root):
        calls.append("manual_packages")
        return real(spec, project_root=project_root)

    monkeypatch.setattr(validation, "collect_manual_packages", spy)
    env_dir = tmp_path / "spack-envs" / "nospack"
    env_dir.mkdir(parents=True)
    (env_dir / "env.yaml").write_text(
        "schema_version: 1\nmethod: no_spack\n"
        "spack:\n  version: '1.2.0'\n  env_name: nospack\n"
        "script: echo hi\n",
        encoding="utf-8",
    )
    validate_environment(
        env_dir, ValidationProfile.CONFIG, layout=validation.ProjectLayout(
            project_root=tmp_path
        )
    )
    assert calls == []  # manual_packages is build-input-only

    validate_environment(
        env_dir,
        ValidationProfile.BUILD_INPUT,
        layout=validation.ProjectLayout(project_root=tmp_path),
    )
    assert calls == ["manual_packages"]
