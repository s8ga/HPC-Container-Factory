"""Atomic app-version bump across one env (``hpc_cf env set-version``).

Bumping cp2k 2026.2 → 2026.3 used to mean ~13 hand edits per env
(env.yaml branch/vars, spack.yaml spec, Dockerfile.j2 titles/labels/
paths, *.def.j2 headers) and trusting the author to catch them all —
the round-2 audit counted exactly this friction. One command now:
replace the old version token across the env's version-bearing files,
re-parse both YAML files, re-run the repo-pin guard, revert everything
on failure, and handle the stale-lock consequence explicitly.

Editing is surgical text replacement (not YAML round-trip) so comments
and layout survive, same doctrine as ``pin set``.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

import yaml

from hpc_cf.env import find_env_yaml
from hpc_cf.environment import load_environment_spec
from hpc_cf.execution import ProjectLayout
from hpc_cf.repo_pins import check_env_repo_pins, expand_partial_includes
from hpc_cf.requests import EnvSetVersionRequest

logger = logging.getLogger(__name__)

# Version tokens sit next to -, ., @, / — never inside a longer
# alphanumeric run (so "2026.2" matches "release-2026.2" and
# "cp2k@2026.2" but neither "2026.2.1" nor "v2026.2x").
_BOUNDARY = r"(?<![A-Za-z0-9_.]){}(?![A-Za-z0-9_.])"


def _version_files(env_dir: Path) -> list[Path]:
    """The env's version-bearing text files (existing ones only)."""
    candidates = [
        env_dir / "Dockerfile.j2",
        env_dir / "README.md",
        *sorted(env_dir.glob("*.def.j2")),
    ]
    spack_yaml = next(
        (
            p
            for p in (
                env_dir / "spack-env-file" / "spack.yaml",
                env_dir / "spack.yaml",
            )
            if p.is_file()
        ),
        None,
    )
    try:
        candidates.insert(1, find_env_yaml(env_dir))
    except FileNotFoundError:
        pass
    if spack_yaml is not None:
        candidates.append(spack_yaml)
    return [p for p in candidates if p.is_file()]


def run_env_set_version(
    request: EnvSetVersionRequest,
    *,
    layout: ProjectLayout | None = None,
) -> int:
    """Execute ``env set-version``; returns a process exit code."""
    layout = layout or ProjectLayout.default()
    if not request.env:
        logger.error("env set-version requires --env <name>")
        return 1
    if not request.old_version or not request.new_version:
        logger.error("env set-version requires --from and --to versions")
        return 1
    if request.old_version == request.new_version:
        logger.error("--from and --to are identical: %s", request.new_version)
        return 1

    env_dir = layout.spack_envs_dir / request.env
    try:
        find_env_yaml(env_dir)
    except FileNotFoundError:
        logger.error("unknown env %r (no env.yaml under %s)", request.env, env_dir)
        return 1

    lock = env_dir / "spack-env-file" / "spack.lock"
    if not lock.is_file():
        lock = env_dir / "spack.lock"
    if lock.is_file() and not request.delete_lock and not request.dry_run:
        logger.error(
            "%s: %s exists — a version bump invalidates the concretized "
            "spec. Re-run with --delete-lock (the lock is regenerated via "
            "`gh workflow run regen-lock.yml -f env_name=%s` after "
            "committing) or delete it yourself first.",
            request.env,
            lock.name,
            request.env,
        )
        return 1

    pattern = re.compile(
        _BOUNDARY.format(re.escape(request.old_version))
    )
    files = _version_files(env_dir)
    plan: list[tuple[Path, str, str, int]] = []
    total = 0
    for path in files:
        text = path.read_text(encoding="utf-8")
        count = len(pattern.findall(text))
        if count:
            plan.append((path, text, pattern.sub(request.new_version, text), count))
            total += count
            logger.info(
                "%s: %s -> %s (%d occurrence(s) in %s)",
                request.env,
                request.old_version,
                request.new_version,
                count,
                path.name,
            )
    if not total:
        logger.error(
            "%s: no occurrence of %r in env.yaml/spack.yaml/Dockerfile.j2/"
            "*.def.j2 — wrong --from?",
            request.env,
            request.old_version,
        )
        return 1
    if request.dry_run:
        logger.info("%s: dry-run; %d occurrence(s) would change", request.env, total)
        return 0

    for path, _original, new_text, _count in plan:
        path.write_text(new_text, encoding="utf-8")

    # Post-edit verification: both YAML files must re-parse, and the env
    # must pass the same repo-pin guard validate runs. Anything failing
    # restores every touched file.
    try:
        spec = load_environment_spec(env_dir)
        spack_yaml = next(
            (
                p
                for p in (
                    env_dir / "spack-env-file" / "spack.yaml",
                    env_dir / "spack.yaml",
                )
                if p.is_file()
            ),
            None,
        )
        if spack_yaml is not None:
            parsed = yaml.safe_load(spack_yaml.read_text(encoding="utf-8"))
            if not isinstance(parsed, dict):
                raise ValueError(f"{spack_yaml.name} no longer parses to a mapping")
        dockerfile = env_dir / "Dockerfile.j2"
        dockerfile_text = (
            dockerfile.read_text(encoding="utf-8") if dockerfile.is_file() else ""
        )
        project_root = (
            env_dir.parent.parent
            if env_dir.parent.name == "spack-envs"
            else ProjectLayout.default().project_root
        )
        dockerfile_text = expand_partial_includes(dockerfile_text, project_root)
        guard_errors = check_env_repo_pins(env_dir.name, spec, dockerfile_text)
        if guard_errors:
            raise ValueError(
                "guard failed after edit:\n- " + "\n- ".join(guard_errors)
            )
    except (ValueError, OSError, yaml.YAMLError) as exc:
        for path, original, _new, _count in plan:
            path.write_text(original, encoding="utf-8")
        logger.error("%s: reverted all edits (%s)", request.env, exc)
        return 1

    if lock.is_file():
        lock.unlink()
        logger.info(
            "%s: deleted %s (stale against the new version); regenerate via "
            "`git rm` + `gh workflow run regen-lock.yml -f env_name=%s` "
            "then commit the artifact",
            request.env,
            lock.name,
            request.env,
        )
    if spec.method.runs_spack_validations:
        logger.info(
            "%s: next: validate (`python -m hpc_cf validate --app-version %s`) "
            "and re-render before building",
            request.env,
            request.env,
        )
    if spec.manual_packages:
        logger.warning(
            "%s: manual_packages present — update the tarball file and its "
            "sha256 for %s by hand (private sources are outside this "
            "command's reach)",
            request.env,
            request.new_version,
        )
    return 0
