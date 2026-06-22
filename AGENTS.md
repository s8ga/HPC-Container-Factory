# AGENTS.md — HPC Container Factory

Guide for AI agents (and humans) working on this codebase.

## What This Project Does

Builds HPC container images (CP2K, VASP, etc.) from Spack environments.
Renders Jinja2 Dockerfile templates, builds via podman/docker, and converts
to Apptainer SIF.

## Architecture

```
cli.py          → argparse dispatch (dockerfile/build/assets/build-sif)
template.py     → Jinja2 Dockerfile rendering + env.yaml → context
spack_ops.py    → Spack operations: _build_*_script (pure) + exec (container)
container.py    → Podman container lifecycle (create/exec/destroy)
assets.py       → Asset workflow: bootstrap + mirror + verify orchestration
env.py          → env.yaml parsing + validators (find_env_yaml, validate_*)
sif.py          → SIF/apptainer building + packing
config.py       → Path constants + DEFAULT_SPACK_VERSION
```

Data flow: `env.yaml` → `build_context()` → Jinja2 `Dockerfile.j2` → Dockerfile → podman build → OCI image → SIF.

## Development Commands

```bash
# Lint (must be 0 errors)
./venv/bin/ruff check hpc_cf/ tests/ scripts/

# Unit tests (default — fast, no external deps)
./venv/bin/pytest -q

# Integration tests (needs podman + assets — opt-in)
./venv/bin/pytest --run-integration -v -s
# or: ./venv/bin/python scripts/integration-test.py

# Render a Dockerfile without building
./venv/bin/python -m hpc_cf dockerfile --app-version cp2k_opensource-2026.1-force-avx512

# Validate an env (static pre-build checks)
./venv/bin/python -m hpc_cf validate --app-version cp2k_opensource-2026.1-force-avx512
```

## Conventions

- **Test-first**: write test → implement → verify all green before commit
- **CLI-over-patching**: use spack CLI (`config`/`mirror`/`repo`), not sed/YAML surgery
- **English-only code comments** (no mixed languages)
- **No plan-reference tags** in code (e.g., "plan A1" — use descriptive comments)
- **ruff must pass** (0 errors) before every commit
- **One commit per logical change** (e.g., one refactor item = one commit)
- **shlex.quote** all config-derived paths in generated bash

## Key Design Decisions

- `method: spack|no_spack` in env.yaml — discriminator for build mode (default: spack)
- `{{ cp2k_branch }}` parametrized in all Dockerfile.j2 (declared once in env.yaml template_vars)
- `{{ cp2k_dev_repo_path }}` parametrized — cp2k's spack repo path (changes between versions: `tools/spack/cp2k_dev_repo` → `tools/spack/spack_repo/cp2k_dev`)
- `spack repo update builtin` runs in every pipeline — ensures builtin repo clone matches env config (`repos.builtin.commit` or default branch). Required because `RemoteRepoDescriptor.initialize()` reuses existing clones without checking commit match.
- `repos.builtin.commit: <sha>` in spack.yaml — pins builtin repo for reproducible concretization. Without it, validate warns.
- `spack mirror create` has NO `--json` — regex parsing of human-readable output is the only option
- `_parse_mirror_stats_from_text` returns `failed=-1` (MIRROR_STATS_UNKNOWN) when output is unparseable — callers raise on `< 0`
- `_build_*_script` methods are pure (return str) — unit-testable without a container
- `Container._run` streams output line-by-line via `Popen` when `capture=False` (real-time `[podman]` logging). `capture=True` uses `subprocess.run` for quick programmatic queries. stderr merged into stdout in streaming mode to avoid pipe deadlock.
- `set -o pipefail` in all generated bash scripts — ensures `spack ... | tee` pipelines correctly propagate non-zero exit codes.

## Adding a New CP2K Version

1. Copy env dir: `spack-envs/cp2k_opensource-2026.1-*` → `2026.2`
2. Update `env.yaml`: `cp2k_branch` (template_vars), `custom_repos.branch`, `sparse_path`
3. Update `spack.yaml`: dependency versions, `cp2k@<version>` spec
4. `python -m hpc_cf validate --app-version <new env>` — must pass
5. `python -m hpc_cf dockerfile --app-version <new env>` — must render cleanly

## Test Layering

| Layer | Location | Default? | External deps | Count |
|---|---|---|---|---|
| **Unit** | `tests/test_*.py` (excl. integration) | ✅ runs always | None | 53 |
| **Integration** | `tests/test_integration_spack.py` | ❌ `--run-integration` | podman + image + assets | 7 |

Integration tests create a persistent container, set up a minimal spack env via CLI, and exercise `_build_*_script` methods against real spack 1.1.1.

## no_spack Build Mode

For non-Spack containers (simple binary packages), set `method: no_spack` in env.yaml.
Uses shared `templates/Dockerfile.nospack.j2` (multi-stage: builder runs user script, runtime copies artifacts).

## Spack Version Compatibility

Current `DEFAULT_SPACK_VERSION = "1.1.1"`. Spack v1.2.0 (2026-06-21) audited — no blocking breaking changes.

**v1.2.0 highlights relevant to hpc_cf**:
- New parallel installer (TUI auto-detects non-TTY → text mode in Docker build)
- Concretization caching enabled by default — speeds up repeated solves
- **SBOM auto-generation** (SPDX 2.3 at `$prefix/.spack/sbom/`) — Phase 3 item 6.3 is now free
- Package API v2.5 — our custom packages use v2.2, fully backward compatible
- `spack isolate --self` — future candidate to simplify `SPACK_USER_CONFIG_PATH` setup

**Verified safe**: `--fail-fast`, `-j`, `spack bootstrap mirror --binary-packages`, `spack repo update builtin`

**Upgrade path** (when ready): `assets/spack-v1.2.0.tar.gz` + `assets/bootstrap-1.2.0` → update `DEFAULT_SPACK_VERSION` → verify `-p 20` flag still exists during first real build.

## Branch Strategy

Active work on `refactor-plan` branch. Commits are atomic per change item.
