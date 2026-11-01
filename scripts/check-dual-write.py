#!/usr/bin/env python3
"""Fail when dual-written env.yaml fields drift from their authoritative copies.

Thin wrapper over ``hpc_cf.repo_pins`` (the same invariants also run inside
``python -m hpc_cf validate --profile config|build-input``). Checks:

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
- Key-name-agnostic: monorepo pin unity per URL, ``<name>_repo_commit``
  var ↔ repo-commit matching, staging path contract
  (``image_path == <clone-root>/<sparse_path>``), and URL/sparse-valued
  template vars must actually be wired into the Dockerfile.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from hpc_cf.repo_pins import check_project


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
