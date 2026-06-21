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
- `spack mirror create` has NO `--json` — regex parsing of human-readable output is the only option
- `_parse_mirror_stats_from_text` returns `failed=-1` (MIRROR_STATS_UNKNOWN) when output is unparseable — callers raise on `< 0`
- `_build_*_script` methods are pure (return str) — unit-testable without a container

## Adding a New CP2K Version

1. Copy env dir: `spack-envs/cp2k_opensource-2026.1-*` → `2026.2`
2. Update `env.yaml`: `cp2k_branch` (template_vars), `custom_repos.branch`, `sparse_path`
3. Update `spack.yaml`: dependency versions, `cp2k@<version>` spec
4. `python -m hpc_cf validate --app-version <new env>` — must pass
5. `python -m hpc_cf dockerfile --app-version <new env>` — must render cleanly

## Test Layering

| Layer | Location | Default? | External deps | Count |
|---|---|---|---|---|
| **Unit** | `tests/test_*.py` (excl. integration) | ✅ runs always | None | 35 |
| **Integration** | `tests/test_integration_spack.py` | ❌ `--run-integration` | podman + image + assets | 5 |

Integration tests create a persistent container, set up a minimal spack env via CLI, and exercise `_build_*_script` methods against real spack 1.1.1.

## no_spack Build Mode

For non-Spack containers (simple binary packages), set `method: no_spack` in env.yaml.
Uses shared `templates/Dockerfile.nospack.j2` (multi-stage: builder runs user script, runtime copies artifacts).

## Branch Strategy

Active work on `refactor-plan` branch. Commits are atomic per change item.
