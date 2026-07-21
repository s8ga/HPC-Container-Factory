#!/usr/bin/env python3
"""Fail when dual-written env.yaml fields drift from their authoritative copies.

Checks:
- CP2K: ``template_vars.cp2k_branch`` / ``cp2k_dev_repo_path`` vs CP2K git
  ``custom_repos`` (and Dockerfile usage).
- CP2K force-avx512: when ``template_vars.force_avx512_repo_path`` is set,
  require a matching s8ga ``custom_repos`` entry whose ``sparse_path`` and
  ``image_path`` agree (Dockerfile sparse-clones via the template var;
  2026.1 / 2026.2 register via ``spack_image_repos`` / ``image_path``).
- s8ga: when either ``template_vars.s8ga_repo_commit`` or any s8ga git
  ``custom_repos[].commit`` is present, both sides must exist and match.
  Envs with s8ga repos but no commit pin on either side are skipped (Track B
  may land pins later).
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path


DUAL_WRITE_FIELDS = (
    ("cp2k_branch", "branch"),
    ("cp2k_dev_repo_path", "sparse_path"),
)

S8GA_REPO_COMMIT_KEY = "s8ga_repo_commit"
FORCE_AVX512_REPO_PATH_KEY = "force_avx512_repo_path"
S8GA_IMAGE_ROOT = "/opt/s8ga-spack-packages"


def _environment_dirs(project_root: Path) -> list[Path]:
    envs_root = project_root / "spack-envs"
    if not envs_root.is_dir():
        return []
    return [
        path
        for path in sorted(envs_root.iterdir())
        if path.is_dir()
        and (
            (path / "env.yaml").is_file()
            or (path / "spack-env-file" / "env.yaml").is_file()
        )
    ]


def _cp2k_git_repo(spec: object) -> tuple[object | None, str | None]:
    git_repos = [repo for repo in spec.spack.custom_repos if repo.type == "git"]
    matches = [
        repo
        for repo in git_repos
        if (repo.namespace or "").lower().startswith("cp2k")
        or "cp2k" in (repo.url or "").lower()
    ]
    if len(matches) == 1:
        return matches[0], None
    if len(matches) > 1:
        namespaces = [repo.namespace or "" for repo in matches]
        if all(namespace.lower().startswith("cp2k") for namespace in namespaces):
            detail = f"by namespace: {', '.join(namespaces)}"
        else:
            identities = [
                f"{repo.namespace or '<no namespace>'}={repo.url or '<no URL>'}"
                for repo in matches
            ]
            detail = f"by namespace/URL: {', '.join(identities)}"
        return None, f"multiple CP2K git custom_repos match {detail}"
    return None, None


def _s8ga_git_repos(spec: object) -> list[object]:
    return [
        repo
        for repo in spec.spack.custom_repos
        if repo.type == "git" and "s8ga" in (repo.url or "").lower()
    ]


def _dockerfile_text(env_dir: Path) -> str:
    dockerfile = env_dir / "Dockerfile.j2"
    if not dockerfile.is_file():
        return ""
    return dockerfile.read_text(encoding="utf-8")


def _is_cp2k_dockerfile(text: str) -> bool:
    lowered = text.lower()
    return "github.com/cp2k/cp2k" in lowered or "/opt/cp2k" in lowered


def _strip_hash_comments(text: str) -> str:
    """Drop ``#`` comments so template-var presence checks are not hollow."""
    return "\n".join(line.partition("#")[0] for line in text.splitlines())


def _uses_template_var(text: str, key: str) -> bool:
    return (
        re.search(
            r"{{\s*" + re.escape(key) + r"\b",
            _strip_hash_comments(text),
        )
        is not None
    )


def _check_cp2k_dual_write(
    *,
    env_name: str,
    spec: object,
    dockerfile_text: str,
) -> list[str]:
    errors: list[str] = []
    repo, repo_error = _cp2k_git_repo(spec)
    if repo is None and not _is_cp2k_dockerfile(dockerfile_text):
        return errors
    if repo_error:
        errors.append(f"{env_name}: {repo_error}")
        return errors

    for key, _attribute in DUAL_WRITE_FIELDS:
        if key not in spec.template_vars:
            errors.append(
                f"{env_name}: CP2K environment is missing template_vars.{key}"
            )

    if repo is None:
        errors.append(
            f"{env_name}: CP2K Dockerfile has no matching CP2K git custom_repo"
        )

    for key, attribute in DUAL_WRITE_FIELDS:
        if key not in spec.template_vars:
            continue
        if not _uses_template_var(dockerfile_text, key):
            errors.append(
                f"{env_name}: Dockerfile.j2 does not use template_vars.{key}"
            )
        if repo is None:
            continue
        template_value = spec.template_vars[key]
        repo_value = getattr(repo, attribute)
        if template_value != repo_value:
            errors.append(
                f"{env_name}: template_vars.{key}={template_value!r} "
                f"!= custom_repos.{attribute}={repo_value!r}"
            )
        if (
            key == "cp2k_dev_repo_path"
            and isinstance(repo_value, str)
            and repo_value
            and repo_value in dockerfile_text
        ):
            errors.append(
                f"{env_name}: Dockerfile.j2 hard-codes CP2K repo path "
                f"{repo_value!r}; use {{{{ cp2k_dev_repo_path }}}}"
            )
    return errors


def _normalize_repo_path(path: str) -> str:
    return path.strip().rstrip("/")


def _expected_s8ga_image_path(force_path: str) -> str:
    return f"{S8GA_IMAGE_ROOT}/{_normalize_repo_path(force_path).lstrip('/')}"


def _s8ga_repos_for_force_path(spec: object, force_path: str) -> list[object]:
    """Prefer sparse_path match; fall back to s8_overrides identity."""
    s8ga_repos = _s8ga_git_repos(spec)
    exact = [
        repo
        for repo in s8ga_repos
        if _normalize_repo_path(repo.sparse_path or "")
        == _normalize_repo_path(force_path)
    ]
    if exact:
        return exact
    return [
        repo
        for repo in s8ga_repos
        if (repo.namespace or "").lower() == "s8_overrides"
        or _normalize_repo_path(repo.sparse_path or "").endswith("s8_overrides")
    ]


def _check_force_avx512_image_path(
    *,
    env_name: str,
    spec: object,
    dockerfile_text: str,
) -> list[str]:
    """Keep force_avx512_repo_path dual-write aligned with plan image_path.

    CP2K force-avx512 Dockerfiles sparse-clone via
    ``{{ force_avx512_repo_path }}``; ``custom_repos[].image_path`` is the
    plan-side SoT for ``spack_image_repos`` registration (2026.1 / 2026.2)
    and must stay aligned so staging and registration cannot drift.
    """
    if FORCE_AVX512_REPO_PATH_KEY not in spec.template_vars:
        return []

    errors: list[str] = []
    force_path = spec.template_vars[FORCE_AVX512_REPO_PATH_KEY]
    if not isinstance(force_path, str) or not force_path.strip():
        errors.append(
            f"{env_name}: template_vars.{FORCE_AVX512_REPO_PATH_KEY} must be "
            "a non-empty string"
        )
        return errors

    if not _uses_template_var(dockerfile_text, FORCE_AVX512_REPO_PATH_KEY):
        errors.append(
            f"{env_name}: Dockerfile.j2 does not use "
            f"template_vars.{FORCE_AVX512_REPO_PATH_KEY}"
        )

    matches = _s8ga_repos_for_force_path(spec, force_path)
    if not matches:
        errors.append(
            f"{env_name}: template_vars.{FORCE_AVX512_REPO_PATH_KEY}="
            f"{force_path!r} but no matching s8ga git custom_repos entry found"
        )
        return errors

    expected_image = _expected_s8ga_image_path(force_path)
    for repo in matches:
        identity = repo.namespace or repo.url or "<unknown>"
        sparse = repo.sparse_path or ""
        if _normalize_repo_path(sparse) != _normalize_repo_path(force_path):
            errors.append(
                f"{env_name}: template_vars.{FORCE_AVX512_REPO_PATH_KEY}="
                f"{force_path!r} != custom_repos[{identity!r}].sparse_path="
                f"{sparse!r}"
            )
        if not repo.image_path:
            errors.append(
                f"{env_name}: custom_repos[{identity!r}] missing image_path "
                f"(expected {expected_image!r} from "
                f"template_vars.{FORCE_AVX512_REPO_PATH_KEY})"
            )
            continue
        if _normalize_repo_path(repo.image_path) != _normalize_repo_path(
            expected_image
        ):
            errors.append(
                f"{env_name}: custom_repos[{identity!r}].image_path="
                f"{repo.image_path!r} != expected {expected_image!r} from "
                f"template_vars.{FORCE_AVX512_REPO_PATH_KEY}"
            )
    return errors


def _check_s8ga_repo_commit(*, env_name: str, spec: object) -> list[str]:
    """When either side pins s8ga, require both sides present and equal."""
    s8ga_repos = _s8ga_git_repos(spec)
    template_present = S8GA_REPO_COMMIT_KEY in spec.template_vars
    template_commit = (
        spec.template_vars[S8GA_REPO_COMMIT_KEY] if template_present else None
    )
    repo_commits = [repo.commit for repo in s8ga_repos if repo.commit]
    if not template_present and not repo_commits:
        return []

    if template_present and not s8ga_repos:
        return [
            f"{env_name}: template_vars.{S8GA_REPO_COMMIT_KEY}={template_commit!r} "
            "but no s8ga git custom_repos entry found"
        ]

    errors: list[str] = []
    if not template_present:
        identities = ", ".join(
            repr(repo.namespace or repo.url or "<unknown>")
            for repo in s8ga_repos
            if repo.commit
        )
        errors.append(
            f"{env_name}: s8ga custom_repos have commit ({identities}) "
            f"but template_vars.{S8GA_REPO_COMMIT_KEY} is missing"
        )
        return errors

    for repo in s8ga_repos:
        identity = repo.namespace or repo.url or "<unknown>"
        if not repo.commit:
            errors.append(
                f"{env_name}: s8ga custom_repos[{identity!r}] missing commit "
                f"while template_vars.{S8GA_REPO_COMMIT_KEY}={template_commit!r}"
            )
            continue
        if repo.commit != template_commit:
            errors.append(
                f"{env_name}: template_vars.{S8GA_REPO_COMMIT_KEY}="
                f"{template_commit!r} != custom_repos[{identity!r}].commit="
                f"{repo.commit!r}"
            )
    return errors


def check_project(project_root: Path) -> list[str]:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from hpc_cf.environment import load_environment_spec

    errors: list[str] = []
    for env_dir in _environment_dirs(project_root):
        spec = load_environment_spec(env_dir)
        dockerfile_text = _dockerfile_text(env_dir)
        errors.extend(
            _check_cp2k_dual_write(
                env_name=env_dir.name,
                spec=spec,
                dockerfile_text=dockerfile_text,
            )
        )
        errors.extend(
            _check_force_avx512_image_path(
                env_name=env_dir.name,
                spec=spec,
                dockerfile_text=dockerfile_text,
            )
        )
        errors.extend(_check_s8ga_repo_commit(env_name=env_dir.name, spec=spec))
    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--project-root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
    )
    args = parser.parse_args(argv)
    errors = check_project(args.project_root.resolve())
    if errors:
        for error in errors:
            print(error)
        return 1
    print("dual-write guard passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
