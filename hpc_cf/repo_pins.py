"""Repo-pin consistency invariants, shared by validate and the guard script.

The factory pins custom recipe repos in env.yaml ``custom_repos`` and, for
image-side staging, historically mirrors parts of that data into
``template_vars``. Drift between the copies concretizes against one revision
and clones another — usually silently. These checks keep the copies honest.

Two layers:

- App-shaped checks (CP2K branch/path dual-write, force-avx512 triangle,
  s8ga commit pin) mirror the historical guard semantics verbatim.
- Key-name-agnostic invariants derived from ``custom_repos`` VALUES so new
  apps get the same protection without adding per-app key names:

  - monorepo pin unity: git repos sharing a URL share the commit pin
    (a half-updated pair must fail, not silently diverge);
  - ``<name>_repo_commit`` template vars (the factory's injection naming
    convention) must be 40-hex and match a pinned git repo commit;
  - staging path contract: when the Dockerfile stages a URL, every repo of
    that URL with explicit ``sparse_path`` + ``image_path`` must satisfy
    ``image_path == <clone_root>/<sparse_path>`` with one shared root;
  - staging var wiring: template vars whose value equals a repo URL or
    sparse_path exist for Dockerfile wiring — unused means dead config.
"""

from __future__ import annotations

import re
from pathlib import Path

from hpc_cf.environment import EnvironmentSpec, load_environment_spec

# App-shaped legacy contract fields (CP2K). Kept verbatim from the original
# scripts/check-dual-write.py so existing guard semantics do not change.
DUAL_WRITE_FIELDS = (
    ("cp2k_branch", "branch"),
    ("cp2k_dev_repo_path", "sparse_path"),
)

S8GA_REPO_COMMIT_KEY = "s8ga_repo_commit"
FORCE_AVX512_REPO_PATH_KEY = "force_avx512_repo_path"
S8GA_IMAGE_ROOT = "/opt/s8ga-spack-packages"

_REPO_COMMIT_VAR_SUFFIX = "_repo_commit"
_SHA40_RE = re.compile(r"[0-9a-fA-F]{40}")


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


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


def _dockerfile_text(env_dir: Path) -> str:
    dockerfile = env_dir / "Dockerfile.j2"
    if not dockerfile.is_file():
        return ""
    return dockerfile.read_text(encoding="utf-8")


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


def _git_repos(spec: EnvironmentSpec) -> list[object]:
    return [repo for repo in spec.spack.custom_repos if repo.type == "git"]


def _repo_identity(repo: object) -> str:
    namespace = getattr(repo, "namespace", None)
    return namespace or getattr(repo, "url", None) or "<unknown>"


# ---------------------------------------------------------------------------
# App-shaped legacy checks (verbatim from the original guard script)
# ---------------------------------------------------------------------------


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


def _is_cp2k_dockerfile(text: str) -> bool:
    lowered = text.lower()
    return "github.com/cp2k/cp2k" in lowered or "/opt/cp2k" in lowered


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
        identity = _repo_identity(repo)
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
            repr(_repo_identity(repo)) for repo in s8ga_repos if repo.commit
        )
        errors.append(
            f"{env_name}: s8ga custom_repos have commit ({identities}) "
            f"but template_vars.{S8GA_REPO_COMMIT_KEY} is missing"
        )
        return errors

    for repo in s8ga_repos:
        identity = _repo_identity(repo)
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


# ---------------------------------------------------------------------------
# Key-name-agnostic invariants
# ---------------------------------------------------------------------------


def _check_monorepo_pin_unity(*, env_name: str, spec: EnvironmentSpec) -> list[str]:
    """Git custom_repos sharing a URL must share the commit pin.

    Same-URL entries are one monorepo staged with a single clone; a pair
    pinned at different revisions (or half pinned / half floating) means
    assets and image can disagree about which recipes are in play.
    """
    groups: dict[str, list[object]] = {}
    for repo in _git_repos(spec):
        if repo.url:
            groups.setdefault(repo.url, []).append(repo)

    errors: list[str] = []
    for url, repos in sorted(groups.items()):
        pins = {repo.commit for repo in repos if repo.commit}
        floating = [repo for repo in repos if not repo.commit]
        if len(pins) <= 1 and not (pins and floating):
            continue
        identities = ", ".join(
            f"{_repo_identity(repo)}@{repo.commit or '<floating>'}" for repo in repos
        )
        errors.append(
            f"{env_name}: git custom_repos sharing URL {url!r} have "
            f"inconsistent commit pins ({identities}); pin the monorepo "
            "at one revision"
        )
    return errors


def _check_repo_commit_vars(*, env_name: str, spec: EnvironmentSpec) -> list[str]:
    """``<name>_repo_commit`` vars must be 40-hex and match a repo pin.

    The suffix is the factory's naming convention (env.yaml declarations and
    the resolved-repos sidecar injection both use it), so any var carrying it
    is a repo-pin copy and must resolve to a pinned git repo in the same env.
    Exception: for a floating repo, the env.yaml-declared
    ``<namespace>_repo_commit`` var is the documented static fallback used
    when the sidecar is missing — its sha need not match anything.
    """
    pinned_commits = {
        (repo.commit or "").lower() for repo in _git_repos(spec) if repo.commit
    }
    floating_fallback_keys = {
        f"{repo.namespace}{_REPO_COMMIT_VAR_SUFFIX}"
        for repo in _git_repos(spec)
        if not repo.commit and repo.namespace
    }
    errors: list[str] = []
    for key, value in spec.template_vars.items():
        if not key.endswith(_REPO_COMMIT_VAR_SUFFIX):
            continue
        if not (isinstance(value, str) and _SHA40_RE.fullmatch(value)):
            errors.append(
                f"{env_name}: template_vars.{key}={value!r} must be a "
                "40-hex git commit"
            )
            continue
        if value.lower() not in pinned_commits and key not in floating_fallback_keys:
            errors.append(
                f"{env_name}: template_vars.{key}={value} matches no git "
                "custom_repos commit in this env (orphan repo-pin copy)"
            )
    return errors


def _url_staged_in_dockerfile(
    url: str,
    spec: EnvironmentSpec,
    dockerfile_text: str,
) -> bool:
    """True when the Dockerfile clones *url* (literal or via a template var)."""
    if url and url.lower() in dockerfile_text.lower():
        return True
    for key, value in spec.template_vars.items():
        if (
            isinstance(value, str)
            and value == url
            and _uses_template_var(dockerfile_text, key)
        ):
            return True
    return False


def _check_staging_path_contract(
    *,
    env_name: str,
    spec: EnvironmentSpec,
    dockerfile_text: str,
) -> list[str]:
    """Staged image_path must equal <clone_root>/<sparse_path>, one root per URL.

    The image stages each URL with a single clone + sparse-checkout, so the
    registered ``image_path`` must be reachable as the clone root joined with
    ``sparse_path``; a second repo from the same URL must share that root.
    Repos without an explicit ``image_path`` (plan-derived default) and URLs
    the Dockerfile does not stage are out of scope here.
    """
    groups: dict[str, list[object]] = {}
    for repo in _git_repos(spec):
        if repo.url and repo.sparse_path and repo.image_path:
            groups.setdefault(repo.url, []).append(repo)

    errors: list[str] = []
    for url, repos in sorted(groups.items()):
        staged = any(
            _url_staged_in_dockerfile(url, spec, dockerfile_text) for _ in repos
        )
        if not staged:
            continue
        roots: dict[str, list[str]] = {}
        for repo in repos:
            identity = _repo_identity(repo)
            sparse = _normalize_repo_path(repo.sparse_path or "")
            image = _normalize_repo_path(repo.image_path or "")
            suffix = f"/{sparse}"
            if not image.endswith(suffix):
                errors.append(
                    f"{env_name}: custom_repos[{identity!r}].image_path="
                    f"{repo.image_path!r} does not end with {suffix!r} "
                    "(staging produces <clone-root>/<sparse_path>)"
                )
                continue
            roots.setdefault(image[: -len(suffix)], []).append(identity)
        if len(roots) > 1:
            detail = "; ".join(
                f"root {root!r}: {', '.join(names)}"
                for root, names in sorted(roots.items())
            )
            errors.append(
                f"{env_name}: git custom_repos sharing URL {url!r} resolve "
                f"to different clone roots ({detail})"
            )
    return errors


def _check_staging_var_wiring(
    *,
    env_name: str,
    spec: EnvironmentSpec,
    dockerfile_text: str,
) -> list[str]:
    """URL/sparse_path-valued template vars exist for wiring — unused is dead.

    Value matching (not key names) associates a var with its repo, so the
    check travels to new apps without registering new key names.
    """
    errors: list[str] = []
    for repo in _git_repos(spec):
        for value, label in (
            (repo.url, "url"),
            (repo.sparse_path, "sparse_path"),
        ):
            if not value:
                continue
            keys = [
                key
                for key, var_value in spec.template_vars.items()
                if isinstance(var_value, str) and var_value == value
            ]
            if keys and not any(
                _uses_template_var(dockerfile_text, key) for key in keys
            ):
                errors.append(
                    f"{env_name}: template_vars.{keys[0]} mirrors "
                    f"custom_repos[{_repo_identity(repo)!r}].{label} but "
                    "Dockerfile.j2 does not use it (dead dual-write copy)"
                )
    return errors


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------


def check_env_repo_pins(
    env_name: str,
    spec: EnvironmentSpec,
    dockerfile_text: str,
) -> list[str]:
    """All repo-pin invariants for one environment; empty list means clean."""
    errors: list[str] = []
    errors.extend(
        _check_cp2k_dual_write(
            env_name=env_name,
            spec=spec,
            dockerfile_text=dockerfile_text,
        )
    )
    errors.extend(
        _check_force_avx512_image_path(
            env_name=env_name,
            spec=spec,
            dockerfile_text=dockerfile_text,
        )
    )
    errors.extend(_check_s8ga_repo_commit(env_name=env_name, spec=spec))
    errors.extend(_check_monorepo_pin_unity(env_name=env_name, spec=spec))
    errors.extend(_check_repo_commit_vars(env_name=env_name, spec=spec))
    errors.extend(
        _check_staging_path_contract(
            env_name=env_name,
            spec=spec,
            dockerfile_text=dockerfile_text,
        )
    )
    errors.extend(
        _check_staging_var_wiring(
            env_name=env_name,
            spec=spec,
            dockerfile_text=dockerfile_text,
        )
    )
    return errors


def check_project(project_root: Path) -> list[str]:
    """Run the guard over every environment directory under *project_root*."""
    errors: list[str] = []
    for env_dir in _environment_dirs(project_root):
        spec = load_environment_spec(env_dir)
        dockerfile_text = _dockerfile_text(env_dir)
        errors.extend(
            check_env_repo_pins(
                env_dir.name,
                spec,
                dockerfile_text,
            )
        )
    return errors
