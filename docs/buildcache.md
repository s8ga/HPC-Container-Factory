# Spack Buildcache

The buildcache path accelerates repeated Spack installs with relocatable binary
packages. It is an MVP pilot for `cp2k_opensource-2025.2`; it has not been
enabled for other environments and does not yet constitute a completed CP2K
binary delivery.

## Artifact and ownership boundaries

- `assets/spack-mirror/` is the source mirror. Assets workflows may populate it.
- `assets/spack-buildcache/` is one global filesystem buildcache. Its contents
  are opaque and owned by Spack: the Factory creates only the empty mount root.
  Do not copy, delete, rename, parse, or invent paths below it.
- `assets/spack-buildcache-state/` is Factory-owned sidecar state. It contains
  the flock, run logs, producer provenance, global health, and lock-SHA coverage
  records. It is not a second package index.

The source mirror and buildcache are independent artifact classes. Assets
commands never write the buildcache, and the dedicated buildcache publisher
never writes the source mirror.

## Prepare and publish

The environment must have a non-empty `spack.lock`, version-matched Spack and
bootstrap assets, and a non-empty source mirror with a successful
`assets --verify-mirror` manifest for the exact environment, Spack version,
and lock SHA. The latest matching Factory assets run must be that successful
verification; a later failed verification or mirror update invalidates the
older success. This is an executable provenance gate, not a claim that static
checks prove every source blob complete. Validate and verify first:

```bash
./venv/bin/python -m hpc_cf validate \
  --env cp2k_opensource-2025.2 --profile build-input
./venv/bin/python -m hpc_cf assets \
  --env cp2k_opensource-2025.2 --verify-mirror
```

Build the padded `builder-installed` stage into a run-unique temporary image
and publish it:

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
and provenance written, and health marked successful. If the Docker stage
itself fails, its incomplete temporary tag is removed best-effort and is not
marked recoverable. Once `builder-installed` succeeds, any later failure
preserves the run-unique image and records its immutable digest and recovery
identity in unhealthy state. A normal `build` continues to write
`{tag}-installed`.

The service takes the exclusive host lock, resolves the local producer-image
digest under that lock, and runs this Spack-owned sequence in a dedicated
container:

```bash
spack -e "$env" buildcache push \
  --unsigned --fail-fast /work/assets/spack-buildcache
spack buildcache update-index file:///work/assets/spack-buildcache
spack -e "$env" buildcache check \
  --mirror-url file:///work/assets/spack-buildcache \
  "${non_external_spec_hashes[@]}"
```

The explicit hashes contain every non-external concrete spec in the active
environment. Spack external specs are deliberately excluded because they have
no binary package to push. The publisher does not use `--force`; package
deduplication and internal layout remain Spack's responsibility.

Any push, index, or check failure marks the global sidecar health unhealthy.
A successful check writes coverage for the exact lock SHA and then marks the
store healthy. Coverage records OS and target values present in `spack.lock`,
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

### `auto` — production default

```bash
./venv/bin/python -m hpc_cf build \
  --env cp2k_opensource-2025.2 --buildcache auto
```

When the global store is healthy, the install RUN gets two independent
read-only mounts:

- `assets/spack-buildcache` → `/opt/spack-buildcache`
- `assets/spack-mirror` → `/opt/spack-mirror`

Spack installs with
`--only-concrete --use-buildcache auto --fail-fast`. Binary hits are used;
cache misses and Spack-recoverable binary errors may fall back to the source
mirror. If the store is absent or unhealthy, the Factory does not register it
and performs the normal source path.

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
mirror or render the source-install branch. Before building, it requires:

- healthy global state;
- a coverage record for the exact lock SHA;
- matching Spack version, dedicated producer-image digest, padding, and
  available lock/environment provenance;
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

L4 is deferred: no real CP2K producer/consumer compile, runtime smoke, or SIF
smoke is claimed. Also outside the MVP are rollout to other environments,
OCI registries, GPG signing, Spack 1.2 index views, VASP/ROCm integration,
cache garbage collection or quotas, autopush, cross-distribution relocation,
and complete OS/base-image air-gap support.
