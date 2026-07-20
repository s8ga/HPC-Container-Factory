# AGENTS.md — HPC Container Factory

Guide for AI agents (and humans) working on this codebase.

## What This Project Does

Builds HPC container images (CP2K, VASP, etc.) from Spack environments.
Renders Jinja2 Dockerfile templates, builds via podman/docker, and converts
to Apptainer SIF.

## Architecture

```
cli.py          → argparse → request → workflows services
workflows.py    → build/assets/buildcache requests + services
environment.py  → EnvironmentSpec v1 (authoritative env.yaml model)
spack_plan.py   → SpackEnvironmentPlan (assets + Dockerfile contract)
template.py     → Jinja2 Dockerfile rendering (StrictUndefined + partials)
spack_ops.py    → Spack operations: _build_*_script (pure) + exec (container)
execution.py    → RunnerPort, ProjectLayout, mirror/buildcache stores and locks
buildcache.py   → buildcache policy, publisher/checker execution, coverage gates
container.py    → Podman RunnerPort implementation
assets.py       → Asset workflow (no argparse); bootstrap + mirror + verify
env.py          → env.yaml discovery + legacy validate_* wrappers
validation.py   → ValidationFinding/Report + config/build-input/assets profiles
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
- **Spack CLI over config surgery**: whenever Spack provides an equivalent command,
  always use its CLI (`spack config` / `mirror` / `repo` / `external` / `env`) instead
  of directly mutating Spack YAML with `sed`, `yq`, PyYAML, string replacement, or
  hand-written merge logic.
- Runtime/image-specific changes must target an explicit environment and scope
  (for example, `spack -e <env> config add ...` or `--scope env:<env>`). Do not
  rewrite the repository's source `spack.yaml` to implement transient build behavior.
- Direct edits to version-controlled `spack.yaml` are reserved for intentional
  declarative source-of-truth changes such as specs, variants, and reproducibility
  pins—not as a substitute for an available Spack CLI operation.
- **English-only code comments** (no mixed languages)
- **No plan-reference tags** in code (e.g., "plan A1" — use descriptive comments)
- **ruff must pass** (0 errors) before every commit
- **One commit per logical change** (e.g., one refactor item = one commit)
- **shlex.quote** all config-derived paths in generated bash

## Key Design Decisions

- `method: spack|no_spack` in env.yaml — discriminator for build mode (default: spack)
- **SpackEnvironmentPlan**: reliable contract for **assets** (prepare/register/mirror scripts).
  Image-side custom repos: ABACUS opensource Dockerfiles include
  `templates/partials/spack_image_repos.j2` (plan-driven `custom_repos[].image_path`);
  other apps still use **per-env Dockerfile.j2 + `template_vars`** handwritten
  `spack repo add` lines (do not assume plan alone registers image repos for every env).
- `mirror_scope` is intentionally fixed to **site** in the plan (not configurable from
  env.yaml). Custom `repo_scope` must not leak into `spack mirror add --scope`.
- `{{ cp2k_branch }}` parametrized in CP2K Dockerfile.j2 (declared once in env.yaml template_vars); opensource commit-pin clones still run a non-comment `git merge-base --is-ancestor` check against `origin/{{ cp2k_branch }}`
- `{{ cp2k_dev_repo_path }}` parametrized — cp2k's spack repo path (changes between versions: `tools/spack/cp2k_dev_repo` → `tools/spack/spack_repo/cp2k_dev`)
- `spack repo update builtin` is the **assets** default and the common image default via
  `spack.image.update_builtin` — but not universal (e.g. VASP sets `image.update_builtin: false`
  and `repo_scope: site`). Do not document “every pipeline / every env” as identical.
- Custom repos for assets are fetched before environment creation, then registered per
  `spack.assets.repo_scope` (often `env:<name>` so overrides beat `repos.builtin.commit`).
  ABACUS opensource image Dockerfiles wire `spack_image_repos`; other apps still emit
  their own `spack repo add` lines until migrated.
- `repos.builtin.commit: <sha>` in spack.yaml — pins builtin repo for reproducible concretization. Without it, validate warns.
- Two-stage lock: **assets produces** `spack.lock`; **build consumes** it read-only
  (fail-closed without `--allow-reconcretize` / assets `--allow-concretize`).
- Source mirror and buildcache are separate artifact classes:
  `assets/spack-mirror/` contains source archives, while the global
  `assets/spack-buildcache/` is an opaque Spack-owned filesystem cache.
  Factory metadata and its shared/exclusive flock live beside it in
  `assets/spack-buildcache-state/`; never inspect or mutate cache internals.
- Buildcache target state is the full CP2K **opensource CPU** track (not a
  single-env pilot). Enable per env via `spack.buildcache.enabled: true`
  (`policy: auto`, `padded_length: 128`). MKL and ROCm environments remain
  out of this migration and stay as-is. Do **not** invent a second binary
  cache format beside Spack's opaque `assets/spack-buildcache/` + factory
  sidecar. Image-size gates and Wave baselines live under `artifacts/`
  (`cp2k-image-size-baseline.md`, `cp2k-image-size-log.md`). Opensource CP2K Dockerfiles must
  not emit `--use-buildcache never` for libint (see
  `tests/test_cp2k_libint_buildcache_ban.py`). Details: `docs/buildcache.md`.
 Production policy defaults come from each env's `spack.buildcache.policy`;
 CLI `--buildcache` is an override. `auto` mounts buildcache and source mirror read-only and permits
  source fallback; strict `only` mounts buildcache alone and fails closed.
 Consumer `auto`/`only` are admitted when global health is healthy **or** the
 env already has successful lock-SHA coverage (another env's failed publish
 must not block a covered consumer). Without coverage, keep prior fail-closed /
 `auto`→`never` behavior. `only` still runs live verify/provenance as coded.
 Producer installs use `--use-buildcache auto` (with padding) so published
 hashes can be reused; misses fall back to the source mirror. Producer Docker
 builds always pass `--no-cache` (CLI `--build-opt` may append more flags).
 Publication uses run-unique temporary tags, then
 promote only after a successful check under the publisher lock; coverage,
 verify, and `only` use the stable
 `{tag}-buildcache-producer` image. Normal builds keep `{tag}-installed`
 separate. Exact lock SHA + producer image digest are authoritative; available
 lock OS/target/compiler and pinned repo commits are compared, with unavailable
 fields represented as unknown.
Producer installs soft-fail when at least one non-external concrete spec is
on disk so a partial install still yields a tagged image. Publication pushes
**installed** hashes only, then `update-index`, then full-lock `check`.
Incomplete coverage stays unhealthy/`partial_publish` but must not discard
already-pushed binaries. If the Docker stage fails with no usable tag, the
temporary tag is removed best-effort and is not recoverable; if a tag exists
despite a reported build failure, the factory still runs full publish — success
promotes + writes coverage + marks healthy (not left as docker-build-only
partial). After `builder-installed` succeeds, publication failures preserve the
run-unique image. Resume only with `hpc_cf buildcache resume --env <env>`
from the latest unhealthy state; it validates environment, current lock SHA,
Spack version, image existence, and immutable digest under the publisher lock.
Never add an arbitrary image-ref resume bypass. Successful completion removes
the temporary tag only after promotion, digest verification,
coverage/provenance, and healthy state. See `docs/buildcache.md`.
- Maximizing Spack buildcache reuse (opensource CPU track lessons):
  - Share one concrete **libint** DAG hash: `libint@2.13.1-cp2k-lmax-7+fortran`
    from namespace `cp2k_dev` (not divergent builtin `tune=cp2k-lmax-6` recipes).
    Authority baseline: `cp2k_opensource-2026.2-force-avx512` /
    `artifacts/cp2k-image-size-baseline.md`.
  - Pin shared upstream deps that affect that hash and image size:
    `python@3.12` and `py-networkx@:2.7` (networkx 2.8+ pulls pandas/numba → llvm).
    Align other libint-adjacent deps; keep each env’s own `py-torch` pin if needed.
  - Align Spack version (track uses 1.2.0), compiler/target/OS, and
    `repos.builtin.commit` with the authority env before expecting hits.
  - Workflow: **publish then consume** — `hpc_cf buildcache build` (or resume),
    then `build --buildcache auto|only`. Producer installs already use
    `--use-buildcache auto` so published hashes can relocate on later runs.
  - Never emit `--use-buildcache never` for libint (or any install step) in
    opensource Dockerfiles; that forces source rebuilds and defeats sharing.
  - Prefer explicit pins + shared lock/hash alignment over `concretizer:reuse`
    as the primary path to stable DAG hashes. MKL/ROCm stay outside this track.
- **ABACUS opensource force-avx512 track** (separate from CP2K): environments
  `abacus_opensource-3.9.0.27-force-avx512` and
  `abacus_opensource-3.10.1-force-avx512` register s8ga `spack_repo/abacus` +
  `spack_repo/s8_overrides` with the same pinned monorepo `commit` in
  `custom_repos` / `template_vars.s8ga_repo_commit`. Align shared math/MPI/ML
  pins across both envs, then **publish** (`buildcache build`) from the
  authority env and **consume** (`build --buildcache auto|only`) on the other.
  Do not assume ABACUS shares DAG hashes with the CP2K track. The dual-write
  guard (`scripts/check-dual-write.py`) fails when either
  `template_vars.s8ga_repo_commit` or s8ga `custom_repos[].commit` is set and
  the two sides disagree or one side is missing (neither side pinned → skip).
- **Buildcache signing**: producer push is `--unsigned` by design for a
  single-tenant trusted host (local flock-owned cache, same-host read-only
  consumers). Not a multi-tenant/remote trust boundary; GPG signing is deferred.
  Operator package inventory: `spack buildcache list` against the cache URL
  (see `docs/buildcache.md`).
- CLI does **not** expose a custom `ProjectLayout`; services accept layout injection mainly
  for tests. Operators use the default project tree.
- `spack mirror create` has NO `--json` — regex parsing of human-readable output is the only option
- `config: deprecated: true` in spack.yaml — allows deprecated package versions (e.g. py-torch@2.4.1). Spack v1.2.0 enforces the check at concretize time; this setting bypasses it.
- `view: false` in spack.yaml + `spack env view enable /opt/spack-view` in Dockerfile — works around spack v1.2.0 PR #52551 which changed view updates from symlink swap to `os.rename` (fails with EXDEV on Docker overlayfs). The view is created after `spack gc` so build dependencies are removed first.
- `_parse_mirror_stats_from_text` returns `failed=-1` (MIRROR_STATS_UNKNOWN) when output is unparseable — callers raise on `< 0`
- `_build_*_script` methods are pure (return str) — unit-testable without a container
- `Container._run` streams output line-by-line via `Popen` when `capture=False` (real-time `[podman]` logging). `capture=True` uses `subprocess.run` for quick programmatic queries. stderr merged into stdout in streaming mode to avoid pipe deadlock.
- `set -o pipefail` in all generated bash scripts — ensures `spack ... | tee` pipelines correctly propagate non-zero exit codes.

## Positioning vs conda

hpc_cf targets **HPC source builds** with pinned Spack concretization, offline source
mirrors, OCI images, and Apptainer SIF — not a general conda replacement. Prefer this
stack when you need compiler/MPI/GPU variant control and air-gapped mirror installs;
use conda/mamba when a binary env solver is enough.

## Adding a New CP2K Version

1. Copy the closest maintained `spack-envs/cp2k_opensource-*` environment.
2. Rename it using `cp2k_opensource-<version>[-suffix]`.
3. Update `env.yaml`: Spack version, `cp2k_branch`, custom repo branch/path, and
   any release-specific `template_vars`.
4. Update `spack.yaml`: CP2K/dependency versions, variants, and builtin commit.
5. Remove the copied lock, then run
   `python -m hpc_cf assets --env <new-env> --allow-concretize`.
6. Run config/build-input validation and render the Dockerfile before building.

## Test Layering

- **L0 unit/contract**: default `tests/test_*.py`; schema, pure scripts, opaque
  store state, locking, policy, and lock fail-closed behavior. No external deps.
- **L1 CLI/service**: default mocked tests; argument mapping, dispatch, exit
  codes, health/coverage gates, and workflow boundaries. No external deps.
- **L2 inventory/render**: default tests over all discovered environments,
  special templates, validation profiles, and the standalone dual-write guard.
- **L3 real Spack**: opt-in `tests/test_integration_spack.py`; Podman,
  `hpc-mirror-builder`, and versioned assets are required. The pkgconf matrix
  covers Spack 1.1.0/1.1.1/1.2.0 push/index/check, padded relocation, auto
  miss/recoverable damaged-entry fallback, and only fail-closed. Missing
  assets are skipped.
- **L4 real application delivery**: deferred. Full CP2K producer/consumer,
  runtime, and SIF smoke are not current acceptance gates.

Run L0-L2 with `./venv/bin/pytest -q`. Run L3 explicitly with
`./venv/bin/pytest --run-integration -v -s tests/test_integration_spack.py`.
Run the inventory guard independently with
`./venv/bin/python scripts/check-dual-write.py`.

L3 creates a persistent container per Spack version and uses a minimal pkgconf
environment; it does not build CP2K. Missing matrix assets are skipped rather
than counted as passes.
Environment inventory is discovered via `EnvironmentSpec` / `list_available_envs()`
— do not hardcode test counts.

## no_spack Build Mode

For non-Spack containers (simple binary packages), set `method: no_spack` in env.yaml.
Uses shared `templates/Dockerfile.nospack.j2` (multi-stage: builder runs user script, runtime copies artifacts).

## Spack Version Compatibility

Current `DEFAULT_SPACK_VERSION = "1.1.1"`. Spack v1.2.0 is already selected by
the ABACUS and CP2K 2026.2 environments; other environments keep their pinned
1.1.0 or 1.1.1 versions.

**v1.2.0 highlights relevant to hpc_cf**:
- New parallel installer (TUI auto-detects non-TTY → text mode in Docker build)
- Concretization caching enabled by default — speeds up repeated solves
- **SBOM auto-generation** (SPDX 2.3 at `$prefix/.spack/sbom/`) — Phase 3 item 6.3 is now free
- Package API v2.5 — our custom packages use v2.2, fully backward compatible
- `spack isolate --self` — future candidate to simplify `SPACK_USER_CONFIG_PATH` setup

**Verified safe**: `--fail-fast`, `-j`, `spack bootstrap mirror --binary-packages`, `spack repo update builtin`

Do not infer an environment's Spack version from `DEFAULT_SPACK_VERSION`; read
`spack-env-file/env.yaml`.

## Branch Strategy

Active work is on the `v2` branch. Commits are atomic per change item.
