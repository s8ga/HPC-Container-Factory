"""Atomic repo-pin updates across the dual-write copies (``hpc_cf pin set``).

Bumping a monorepo pin used to mean hand-editing env.yaml 3+ times per env
(custom_repos entries + template_vars copies) across many envs and trusting
the author to run the guard. One command now: match repos by namespace or
URL substring, replace the old sha everywhere it appears in env.yaml,
re-verify with the same guard validate runs, and surface the lock-staleness
consequence of the assets-produces / build-consumes doctrine.

Editing is surgical text replacement (not YAML round-trip) so comments and
layout survive; the replacement is verified by re-parsing and by refusing
pre-drifted envs instead of blessing them.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

from hpc_cf.env import find_env_yaml, list_available_envs
from hpc_cf.environment import load_environment_spec
from hpc_cf.execution import ProjectLayout
from hpc_cf.lock_manifest import LOCK_MANIFEST_FILENAME, read_lock_manifest
from hpc_cf.repo_pins import check_env_repo_pins, expand_partial_includes
from hpc_cf.requests import PinSetRequest

logger = logging.getLogger(__name__)

_SHA40_RE = re.compile(r"[0-9a-fA-F]{40}")


def _match_repos(spec, selector: str) -> list:
    """Git custom_repos matching by exact namespace or URL substring."""
    needle = selector.lower()
    return [
        repo
        for repo in spec.spack.custom_repos
        if repo.type == "git"
        and (
            (repo.namespace or "").lower() == needle
            or needle in (repo.url or "").lower()
        )
    ]


def _plan_env_edit(
    env_dir: Path,
    selector: str,
) -> tuple[Path | None, str, str | None, int, int]:
    """Resolve one env's edit plan.

    Returns ``(env_yaml, reason, old_commit, repo_sites, var_sites)``;
    ``env_yaml is None`` means skip/refuse with ``reason`` set (empty
    reason on success). ``repo_sites``/``var_sites`` count the dual-write
    copies the old pin must appear in (matched custom_repos entries and
    template_vars carrying the same sha).
    """
    try:
        env_yaml = find_env_yaml(env_dir)
    except FileNotFoundError:
        return None, f"no env.yaml under {env_dir}", None, 0, 0
    spec = load_environment_spec(env_dir)

    matches = _match_repos(spec, selector)
    if not matches:
        return None, f"no git custom_repos match {selector!r}", None, 0, 0

    urls = {repo.url for repo in matches}
    if len(urls) > 1:
        return (
            None,
            f"selector {selector!r} matches repos with different URLs: "
            + ", ".join(sorted(u or "<none>" for u in urls)),
            None,
            0,
            0,
        )
    floating = [repo.namespace for repo in matches if not repo.commit]
    if floating:
        return (
            None,
            "floating repos cannot be pin-bumped (no old commit): "
            + ", ".join(floating),
            None,
            0,
            0,
        )
    old_commits = {repo.commit for repo in matches}
    if len(old_commits) > 1:
        return (
            None,
            "matched repos have inconsistent commits "
            "(fix drift first, see scripts/check-dual-write.py): "
            + ", ".join(sorted(c or "" for c in old_commits)),
            None,
            0,
            0,
        )
    old_commit = next(iter(old_commits))

    var_sites = [
        key
        for key, value in spec.template_vars.items()
        if isinstance(value, str) and value.lower() == old_commit.lower()
    ]
    return env_yaml, "", old_commit, len(matches), len(var_sites)


def run_pin_set(
    request: PinSetRequest,
    *,
    layout: ProjectLayout | None = None,
) -> int:
    """Execute ``pin set``; returns a process exit code (0 ok / 1 failure)."""
    layout = layout or ProjectLayout.default()
    if not _SHA40_RE.fullmatch(request.commit):
        logger.error("--commit must be a 40-hex git sha: %r", request.commit)
        return 1

    if request.all_envs:
        env_names = list_available_envs(layout=layout)
        if not env_names:
            logger.error("no environments found under %s", layout.spack_envs_dir)
            return 1
    else:
        env_names = list(request.envs)

    rc = 0
    changed = 0
    for name in env_names:
        env_dir = layout.spack_envs_dir / name
        plan = _plan_env_edit(env_dir, request.repo)
        if plan[0] is None:
            reason = plan[1]
            if request.all_envs:
                logger.info("%s: skipped (%s)", name, reason)
                continue
            logger.error("%s: %s", name, reason)
            rc = 1
            continue
        env_yaml, _reason, old_commit, repo_sites, var_sites = plan

        if old_commit.lower() == request.commit.lower():
            logger.info("%s: already at %s", name, request.commit)
            continue

        if not _apply_pin(
            env_dir,
            env_yaml,
            old_commit,
            request.commit,
            repo_sites,
            var_sites,
            dry_run=request.dry_run,
        ):
            rc = 1
            continue
        changed += 1

    if changed:
        logger.info(
            "%d env(s) %s; locks recorded against the old pin are now stale — "
            "run `python -m hpc_cf assets --env <name> --allow-concretize` "
            "(delete the stale spack.lock first) to re-concretize.",
            changed,
            "would change" if request.dry_run else "changed",
        )
    return rc


def _apply_pin(
    env_dir: Path,
    env_yaml: Path,
    old_commit: str,
    new_commit: str,
    repo_sites: int,
    var_sites: int,
    *,
    dry_run: bool,
) -> bool:
    text = env_yaml.read_text(encoding="utf-8")
    occurrences = text.count(old_commit)
    if occurrences < repo_sites + var_sites:
        logger.error(
            "%s: old pin %s appears %d time(s) but %d site(s) reference it "
            "(custom_repos=%d, template_vars=%d) — drifted config; fix with "
            "scripts/check-dual-write.py first",
            env_dir.name,
            old_commit,
            occurrences,
            repo_sites + var_sites,
            repo_sites,
            var_sites,
        )
        return False

    logger.info(
        "%s: %s -> %s (%d custom_repos, %d template_vars, %d occurrence(s) total)",
        env_dir.name,
        old_commit[:12],
        new_commit[:12],
        repo_sites,
        var_sites,
        occurrences,
    )
    if dry_run:
        return True

    original = text
    text = text.replace(old_commit, new_commit)
    env_yaml.write_text(text, encoding="utf-8")

    # Post-edit verification: the file must re-parse into a consistent env
    # that passes the same guard validate runs; otherwise restore and fail.
    try:
        spec = load_environment_spec(env_dir)
        drifted = [
            repo
            for repo in spec.spack.custom_repos
            if repo.type == "git" and repo.commit == new_commit
        ]
        if len(drifted) != repo_sites:
            raise ValueError(
                f"re-parse found {len(drifted)} repo(s) at the new pin, "
                f"expected {repo_sites}"
            )
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
    except (ValueError, OSError) as exc:
        env_yaml.write_text(original, encoding="utf-8")
        logger.error("%s: reverted edit (%s)", env_dir.name, exc)
        return False

    _report_lock_staleness(env_dir)
    return True


def _report_lock_staleness(env_dir: Path) -> None:
    config_dir = env_dir / "spack-env-file"
    lock = config_dir / "spack.lock"
    if not lock.is_file():
        return
    try:
        manifest = read_lock_manifest(config_dir)
    except ValueError:
        manifest = None
    if manifest is None:
        logger.info(
            "%s: lock present without %s (provenance unknown) — regenerating "
            "the lock will record the new pin",
            env_dir.name,
            LOCK_MANIFEST_FILENAME,
        )
