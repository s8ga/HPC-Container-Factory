#!/usr/bin/env bash
# Materialize a `<base>-fork[-<label>]` environment as a symlink to <base>
# and optionally point its cp2k source at a public fork.
#
# Usage:
#   apply-source-override.sh <env_name> [fork_url] [fork_branch]
#
# Contract:
# - env names not containing "-fork" are a no-op (the upstream nightly).
# - "<base>-fork" / "<base>-fork-<label>" are materialized as a symlink to
#   <base> (single source of truth: recipes, pins, templates stay in the
#   base env; only the run's identity — release, tags, package versions —
#   is fork-specific, derived from the env name).
# - fork_url patches the env-local cp2k recipe's git attribute (Spack then
#   fetches the source from the fork) and env.yaml's
#   template_vars.cp2k_source_repo_url (the Dockerfile tests clone).
# - fork_branch patches version("master", branch=...) and
#   template_vars.cp2k_branch (the ancestor check compares against the
#   fork branch, not upstream master).
# - Exact-commit pinning is the caller's job via the cp2k_commit dispatch
#   input (spec-level commit=, valid for any fetch URL).
#
# Idempotent: safe to re-run in every job on a fresh checkout.
set -euo pipefail

ENV_NAME="${1:?env name required}"
FORK_URL="${2:-}"
FORK_BRANCH="${3:-}"

case "$ENV_NAME" in
*-fork | *-fork-*) : ;;
*)
    echo "not a fork env ('$ENV_NAME'); nothing to do"
    exit 0
    ;;
esac

BASE="${ENV_NAME%%-fork*}"
BASE_DIR="spack-envs/${BASE}"
FORK_DIR="spack-envs/${ENV_NAME}"

if [[ ! -d "$BASE_DIR" ]]; then
    echo "::error::fork base env missing: $BASE_DIR" >&2
    exit 1
fi
if [[ ! -e "$FORK_DIR" ]]; then
    ln -s "$BASE" "$FORK_DIR"
    echo "materialized $FORK_DIR -> $BASE"
fi

if [[ -z "$FORK_URL" && -z "$FORK_BRANCH" ]]; then
    echo "fork env materialized; no source override requested"
    exit 0
fi

RECIPE="$FORK_DIR/spack-env-file/repos/packages/cp2k/package.py"
ENV_YAML="$FORK_DIR/spack-env-file/env.yaml"
UPSTREAM="https://github.com/cp2k/cp2k.git"

if [[ -n "$FORK_URL" ]]; then
    if ! grep -q "git = \"${FORK_URL}\"" "$RECIPE"; then
        grep -q "git = \"${UPSTREAM}\"" "$RECIPE" || {
            echo "::error::cp2k recipe git line not found (upstream recipe format drifted?)" >&2
            exit 1
        }
        sed -i "s|git = \"${UPSTREAM}\"|git = \"${FORK_URL}\"|" "$RECIPE"
    fi
    grep -q "git = \"${FORK_URL}\"" "$RECIPE"
    if grep -q "cp2k_source_repo_url:" "$ENV_YAML"; then
        sed -i "s|cp2k_source_repo_url: .*|cp2k_source_repo_url: \"${FORK_URL}\"|" "$ENV_YAML"
    else
        sed -i "/^template_vars:/a\\
  cp2k_source_repo_url: \"${FORK_URL}\"" "$ENV_YAML"
    fi
    grep -q "cp2k_source_repo_url: \"${FORK_URL}\"" "$ENV_YAML"
    # The cp2k_dev recipe subtree lives inside the cp2k monorepo: point the
    # custom_repos float at the fork too, so recipes, the resolved-repos
    # sidecar, and the image's test clone all follow the fork branch.
    BR_FOR_REPOS="${FORK_BRANCH:-master}"
    sed -i "/url: https:\/\/github.com\/cp2k\/cp2k.git/,/namespace: cp2k_dev/ { s|url: .*|url: ${FORK_URL}|; s|branch: .*|branch: ${BR_FOR_REPOS}| }" "$ENV_YAML"
    grep -q "url: ${FORK_URL}" "$ENV_YAML"
    echo "cp2k_dev recipe repo -> ${FORK_URL} (${BR_FOR_REPOS})"
    echo "cp2k source URL -> ${FORK_URL}"
fi

if [[ -n "$FORK_BRANCH" ]]; then
    if ! grep -q "version(\"master\", branch=\"${FORK_BRANCH}\"" "$RECIPE"; then
        grep -q 'version("master", branch="master"' "$RECIPE" || {
            echo "::error::cp2k recipe master-branch version line not found" >&2
            exit 1
        }
        sed -i "s|version(\"master\", branch=\"master\"|version(\"master\", branch=\"${FORK_BRANCH}\"|" "$RECIPE"
    fi
    grep -q "version(\"master\", branch=\"${FORK_BRANCH}\"" "$RECIPE"
    if grep -q "cp2k_branch:" "$ENV_YAML"; then
        sed -i "s|cp2k_branch: .*|cp2k_branch: \"${FORK_BRANCH}\"|" "$ENV_YAML"
    else
        sed -i "/^template_vars:/a\\
  cp2k_branch: \"${FORK_BRANCH}\"" "$ENV_YAML"
    fi
    grep -q "cp2k_branch: \"${FORK_BRANCH}\"" "$ENV_YAML"
    if [[ -z "$FORK_URL" ]]; then
      sed -i "/url: https:\/\/github.com\/cp2k\/cp2k.git/,/namespace: cp2k_dev/ { s|branch: .*|branch: ${FORK_BRANCH}| }" "$ENV_YAML"
    fi
    echo "cp2k source branch -> ${FORK_BRANCH}"
fi
