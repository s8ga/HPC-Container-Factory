# Spack Buildcache

The buildcache path accelerates repeated Spack installs with relocatable binary
packages. Target state is the full CP2K **opensource CPU** track (authority
baseline: `cp2k_opensource-2026.2-force-avx512`, then aligned
`2025.2-force` / `2026.1-force` / `2025.2` base). MKL and ROCm environments
are out of this migration and remain as-is. Do not invent a parallel homemade
binary cache; deepen this Spack buildcache + factory sidecar only.

## Artifact and ownership boundaries

- `assets/spack-mirror/` is the source mirror. Assets workflows may populate it.
- `assets/spack-buildcache/` is one global filesystem buildcache. Its contents
  are opaque and owned by Spack: the Factory creates only the empty mount root.
  Do not copy, delete, rename, parse, or invent paths below it.
- `assets/spack-buildcache-state/` is Factory-owned sidecar state. It contains
  the flock, run logs, producer provenance, global health, and lock-SHA coverage
  records. It is not a second package index.
- Image-size gates (opensource CPU): runtime soft 6 GB / hard 10 GB;
  `{tag}-installed` soft 25 GB / hard 40 GB. Record measurements in
  `artifacts/cp2k-image-size-log.md`; Wave baselines and libint authority
  hashes live in `artifacts/cp2k-image-size-baseline.md`.
- Opensource CP2K Dockerfiles must **not** emit
  `--use-buildcache never` for libint (or any install step). Prefer
  `auto`/`only` so the shared authority libint hash can relocate from
  `assets/spack-buildcache/`. Enforced by
  `tests/test_cp2k_libint_buildcache_ban.py`.
Build the padded `builder-installed` stage into a run-unique temporary image
and publish it. The producer install uses `--use-buildcache auto` with both
the buildcache and source mirror mounted read-only, so already-published
hashes are extracted and misses fall back to source:

```bash
./venv/bin/python -m hpc_cf buildcache build \
  --env cp2k_opensource-2025.2 --network-host
```

The long source build does not hold the global buildcache lock. Under the
exclusive lock, the completed temporary image is inspected and used for
publication; only after push/index/check succeeds is it promoted to the stable
`{tag}-buildcache-producer` tag, whose digest is checked again. A failed
producer therefore cannot replace the last accepted stable image. Concurrent
producer builds never share a mutable build tag. Temporary tags are removed
only after the stable tag has been promoted, its digest rechecked, coverage
and provenance written, and health marked successful.

### Partial install / partial publish

Full-env install success is **not** a prerequisite for push. Producer Docker
installs soft-fail when at least one non-external concrete spec is already on
disk (`HPC_CF_PARTIAL_INSTALL=1`), so the image stays tagged. The publisher
then:

1. Inventories installed non-external concrete specs in the image.
2. Runs `spack buildcache push --unsigned` for those hashes only.
3. Runs `spack buildcache update-index`.
4. Runs full-lock `buildcache check` (may fail).

If check/coverage is incomplete, sidecar health stays unhealthy with
`partial_publish` markers (`HPC_CF_PUSHED_SPEC_COUNT`,
`HPC_CF_PARTIAL_PUBLISH=1`); binaries that did push must not be discarded.
If the Docker stage fails with no usable tag, the temporary tag is removed
best-effort and is not recoverable. If a tag exists despite a reported build
failure, the factory still runs the full publish sequence
(push → update-index → check → promote → coverage). When that sequence
succeeds end-to-end, the store is marked **healthy** (not left as a
docker-build / partial-publish failure). Only a failed publish step keeps
unhealthy/`partial_publish` state while retaining the run-unique image.
Once `builder-installed` succeeds, any later publication failure preserves
the run-unique image and records its immutable digest and recovery identity
in unhealthy state. A normal `build` continues to write `{tag}-installed`.

Producer Docker builds always pass `--no-cache` so soft-fail install layers
cannot reuse a stale `builder-installed` stage. Additional engine flags may
be appended with CLI `--build-opt`.

The service takes the exclusive host lock, resolves the local producer-image
digest under that lock, and runs this Spack-owned sequence in a dedicated
container:

```bash
# installed_hashes = non-external concrete specs present on disk
spack -e "$env" buildcache push \
  --unsigned --fail-fast /work/assets/spack-buildcache \
  "${installed_hashes[@]}"
spack buildcache update-index file:///work/assets/spack-buildcache
# spec_hashes = all non-external concrete specs from the lock/env
spack -e "$env" buildcache check \
  --mirror-url file:///work/assets/spack-buildcache \
  "${spec_hashes[@]}"
```

Push uses installed hashes only. Check still uses every non-external concrete
spec in the active environment. Spack external specs are deliberately excluded
because they have no binary package to push. The publisher does not use
`--force`; package deduplication and internal layout remain Spack's
responsibility.

### Unsigned trust boundary

Publication uses `spack buildcache push --unsigned`. That is intentional for a
**single-tenant trusted host**: the buildcache root is local filesystem state
written only by this factory under the exclusive publisher flock, and consumers
on the same host mount it read-only. There is no multi-tenant or remote-mirror
integrity model yet; GPG signing remains out of scope (see deferred work
below). Do not treat an unsigned local cache as safe to share across untrusted
machines or users.

To inspect which packages are present in the opaque cache (operator inventory,
not a factory API), use Spack's own listing against the mount, for example:

```bash
spack buildcache list -l file:///path/to/assets/spack-buildcache
```

Any push, index, or check failure marks the global sidecar health unhealthy
(partial publish is unhealthy until full-lock check passes). A successful
check writes coverage for the exact lock SHA and then marks the store healthy. Coverage records OS and target values present in `spack.lock`,
compiler values when the lock format provides them, and pinned repository
commits available in `spack.yaml`. Unavailable values are stored as JSON
`null`, not fabricated placeholders or empty dictionaries. Strict `only`
recomputes and compares this structured provenance in addition to the exact
lock SHA, Spack version, immutable producer image digest, install-tree padding,
signing policy, and explicit non-external check result/count. A `null` field
is explicitly unknown and is not an independent compatibility claim.

### Resume a failed producer publication

Inspect status to find the image retained by the latest unhealthy producer
run, then resume without rebuilding CP2K:

```bash
./venv/bin/python -m hpc_cf buildcache status
./venv/bin/python -m hpc_cf buildcache resume \
  --env cp2k_opensource-2025.2
```

`resume` does not accept an arbitrary producer image. Under the same exclusive
publisher lock used by `build`, it reads only the latest unhealthy state and
requires an exact match for environment, current lock SHA, Spack version,
stable producer reference, retained temporary image existence, and immutable
image digest. Any mismatch fails closed without deleting the image. A valid
resume skips the Docker build and repeats the production
push → update-index → explicit check → promote → coverage/provenance → healthy
sequence. A repeated failure updates unhealthy state and continues to retain
the temporary image; complete success removes only the temporary tag and
keeps the stable producer tag.

Recheck the existing `{tag}-buildcache-producer` image or inspect sidecar
health with:

```bash
./venv/bin/python -m hpc_cf buildcache verify \
  --env cp2k_opensource-2025.2
./venv/bin/python -m hpc_cf buildcache status \
  --format json
```

## Consumer policies

`build` and `dockerfile` accept `--buildcache auto|only|never`. When omitted,
the authoritative default is the environment's
`spack.buildcache.policy`; the CLI option is an explicit override.
The environment must explicitly set `spack.buildcache.enabled: true`; an
`auto` request for any other environment becomes `never`, while `only` is
rejected.

### Policy gate: global health vs lock-SHA coverage

`resolve_consumer_policy` admits `auto`/`only` when **either** global
`health.json` is healthy **or** the consumer's current `spack.lock` already
has a successful non-external coverage record. Another environment's failed
publish may leave the global store unhealthy without blocking a covered
consumer. Without that coverage (and without healthy global state), `auto`
falls back to `never` (source path) and `only` fails closed.

### `auto` — production default

```bash
./venv/bin/python -m hpc_cf build \
  --env cp2k_opensource-2025.2 --buildcache auto
```

When the policy gate admits `auto`, the install RUN gets two independent
read-only mounts:

- `assets/spack-buildcache` → `/opt/spack-buildcache`
- `assets/spack-mirror` → `/opt/spack-mirror`

Spack installs with
`--only-concrete --use-buildcache auto --fail-fast`. Binary hits are used;
cache misses and Spack-recoverable binary errors may fall back to the source
mirror. If the store is absent, or global health is unhealthy **and** this
lock has no successful coverage, the Factory does not register the cache and
performs the normal source path.

Fallback is not guaranteed for every malformed binary. In particular, the
tested Spack 1.2.0 client treats an archive SHA256 mismatch as fatal under
`--fail-fast`; it does not silently rebuild that package from source. A cache
with integrity failures must be marked unhealthy and repaired or republished.

### `only` — strict acceptance gate

```bash
./venv/bin/python -m hpc_cf build \
  --env cp2k_opensource-2025.2 --buildcache only
```

`only` mounts only the buildcache read-only; it does not mount the source
mirror or render the source-install branch. The policy gate above must admit
`only` (healthy global **or** successful lock-SHA coverage). Before building,
it still requires (live, as coded):

- a coverage record for the exact lock SHA (with matching Spack version,
  dedicated producer-image digest, padding, and available lock/environment
  provenance);
- successful live `buildcache check` of explicit non-external hashes.

Any mismatch fails closed. `--buildcache only` cannot be combined with
`--allow-reconcretize`.

### `never` — source behavior

```bash
./venv/bin/python -m hpc_cf build \
  --env cp2k_opensource-2025.2 --buildcache never
```

No binary cache is registered. The existing source-mirror install path is
used.

## Permissions and locking

- Publisher container: buildcache mounted read-write at
  `/work/assets/spack-buildcache`; source mirror is not writable.
- Consumer build: buildcache is read-only at `/opt/spack-buildcache`.
  `auto` also mounts the source mirror read-only; `only` does not.
- Factory sidecar state is never mounted into consumers.
- Push → update-index → check holds one exclusive host flock.
- Publisher/checker containers have a configurable
  `--operation-timeout-seconds` (default 24 hours, suitable for long HPC
  pushes). Timeout output is retained, health is marked unhealthy, and the
  exclusive lock is released.
- The complete multi-stage consumer OCI build holds a shared host flock.
  Cache health is rechecked after that lock is acquired.
  Multiple consumers may run together; publication waits for all consumers.

Use a local filesystem with reliable `flock` semantics. Permissions must let
the invoking user create the two top-level directories and sidecar files, and
the publisher write the buildcache root. Podman publishers use
`--userns=keep-id`; Docker publishers use Docker's normal user-namespace mode.

## Air-gap boundary

The MVP's offline claim is deliberately narrow: after OS dependencies, base
images, Spack, environment files, lock, and custom repositories are already
available, package materialization can succeed from the read-only buildcache
under strict `only`.

It does not provide a complete disconnected build for APT repositories, base
image pulls, Spack tarballs, bootstrap preparation, CP2K/custom-repository
clones, or other pre-install network steps. `auto` is not an air-gap guarantee
because cache misses intentionally use the source mirror.

## Verification levels and deferred work

Default L0-L2 tests cover schema, policy, locking, sidecars, CLI/services,
template mounts, lock gates, all-environment inventory, and render contracts.
Opt-in L3 uses pkgconf with real Spack 1.1.0, 1.1.1, and 1.2.0 to exercise
push/index/check, padded relocation, auto miss/recoverable damaged-entry
fallback, and strict
only failure.

L4 application delivery is partially available as an **opt-in ABACUS
lightweight smoke** (`tests/test_integration_abacus_l4.py`, gated by
`--run-integration`): consumer build of
`abacus_opensource-3.10.1-force-avx512` with `--buildcache auto`, then stock
`abacus_run_integration_tests.sh` when the image ships `share/abacus/tests`.
Missing healthy buildcache admission or build assets skips (not a false-green
pass). Full CP2K producer/consumer compile, CP2K runtime/regtest, and SIF
smoke remain deferred and are not default gates. Outside current scope:
MKL/ROCm CP2K migration, OCI registries, GPG signing, Spack 1.2 index views,
VASP integration, cache garbage collection or quotas, autopush,
cross-distribution relocation, and complete OS/base-image air-gap support.
