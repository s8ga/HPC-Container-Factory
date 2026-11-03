#!/usr/bin/env bash
# Prune GHCR package versions by exact tag pattern — shared by the
# pipeline's three prune steps (mirror keep-3, runtime keep-7, fork keep-1).
#
# The inline steps this replaces piped jq's --arg into `gh api`, which has
# no such flag: every run from 2026-09-07 to 2026-11-03 died with
# "unknown flag: --arg" inside a process substitution (invisible to
# set -e), the prune list came back empty, and the steps printed
# "within budget" while nothing was ever deleted.
#
# Usage:
#   ghcr_prune_versions.sh <api_repo_path> <tag_regex> <keep_tag> <keep_n>
#     keep_n > 0 — keep the newest N matching versions (plus <keep_tag>'s)
#     keep_n = 0 — keep only versions carrying <keep_tag> (fork: exactly one)
#   Versions carrying <keep_tag> (this run's tags) are never deleted.
#
# Correctness rules:
#   * ALL pages are fetched before filtering. `gh api --paginate` applies
#     its built-in --jq per page, so a per-page keep-N undercounts once an
#     env's versions span pages; standalone jq sees the whole stream.
#   * A failed query (gh or jq) fails the step instead of masquerading as
#     "within budget". The workflow keeps continue-on-error on the step:
#     the failure is visible in the run, but never kills a green publish.
set -euo pipefail

REPO_PATH="${1:?api repo path, e.g. /user/packages/container/<pkg>/versions}"
PATTERN="${2:?anchored tag regex}"
KEEP_TAG="${3:?tag of the current run; its version is never deleted}"
KEEP_N="${4:?keep count (0 = only the keep-tagged version)}"

TMPD="$(mktemp -d)"
trap 'rm -rf "$TMPD"' EXIT

gh api --paginate "$REPO_PATH" \
  | jq -rs --arg pat "$PATTERN" --arg keep "$KEEP_TAG" --argjson n "$KEEP_N" '
      flatten
      | [ .[] | select(any((.metadata.container.tags // [])[]; test($pat))) ]
      | sort_by(.created_at) | reverse
      | (if $n > 0 then .[$n:] else . end)
      | map(select(((.metadata.container.tags // []) | index($keep)) == null))
      | .[].id
    ' > "$TMPD/prune.txt"

mapfile -t OLD < "$TMPD/prune.txt"
if [ "${#OLD[@]}" -eq 0 ]; then
  echo "ghcr prune: nothing to delete (repo ${REPO_PATH}, keep ${KEEP_N}, pattern ${PATTERN})"
  exit 0
fi
for id in "${OLD[@]}"; do
  gh api -X DELETE "$REPO_PATH/$id" && echo "pruned ${REPO_PATH} version ${id}"
done
