"""Integration matrix skeleton: Spack versions + custom-repo prototype.

Opt-in only (``pytest --run-integration``).  Does **not** build full HPC
images — exercises script contracts against a minimal pkgconf env and a
synthetic local repo.  Missing assets for a matrix cell → skip that cell.
"""
from __future__ import annotations

import os
import subprocess
import time
import uuid
from pathlib import Path

import pytest

from hpc_cf.buildcache_ops import build_publish_script, build_verify_script
from hpc_cf.container import Container
from hpc_cf.execution import ProjectLayout
from hpc_cf.spack_ops import EnvConfig, SpackConfig, SpackOps

# Matrix: extend when assets/spack-vX.Y.Z.tar.gz + bootstrap-X.Y.Z exist.
SPACK_VERSIONS = ("1.1.0", "1.1.1", "1.2.0")
IMAGE = "hpc-mirror-builder"
CONTAINER_NAME = "hpc-itest"
ENV_DIR = "/tmp/itest-env"
MIRROR_DIR = "/tmp/itest-mirror"
BUILDCACHE_DIR = "/tmp/itest-buildcache"
SOURCE_MIRROR_DIR = "/opt/itest-source-mirror"


def _assets_ready(version: str, layout: ProjectLayout | None = None) -> bool:
    assets = (layout or ProjectLayout.default()).assets_dir
    return (assets / f"spack-v{version}.tar.gz").exists() and (
        assets / f"bootstrap-{version}"
    ).is_dir()


@pytest.fixture(scope="class", params=SPACK_VERSIONS)
def spack_ops(request):
    """Create container + SpackOps + minimal env (pkgconf) via spack CLI.

    Parametrized over :data:`SPACK_VERSIONS`; cells without assets are skipped.
    """
    version = request.param
    layout = ProjectLayout.default()
    if not _assets_ready(version, layout):
        pytest.skip(f"assets for spack {version} not found under {layout.assets_dir}")

    env_config = EnvConfig(spack=SpackConfig(version=version, env_name="itest"))
    ctr = Container(
        name=f"{CONTAINER_NAME}-{version.replace('.', '_')}",
        image=IMAGE,
        project_root=layout.project_root,
        extra_opts=[
            f"-v {layout.assets_dir / 'spack-mirror'}:{SOURCE_MIRROR_DIR}:ro"
        ],
    )
    ops = SpackOps(env_config, ctr)

    ctr.create()
    try:
        ops.install_system_pkgs()
        ops.clean_stale_state()
        ops.compiler_find()

        ops.ctr.exec(f"""\
{ops._source_spack()}
rm -rf {ENV_DIR}
spack env create -d {ENV_DIR}
spack -e {ENV_DIR} add pkgconf
spack -e {ENV_DIR} config add concretizer:unify:true
""")
        yield ops
    finally:
        try:
            ctr.destroy()
        except Exception:
            pass


def _run_mirror(ops: SpackOps) -> dict:
    """Helper: clean, run mirror create, return parsed stats."""
    from hpc_cf.spack_ops import MIRROR_CREATE_LOG

    ops.clean_stale_state()
    ops.prepare_environment(ENV_DIR, import_lock=True)
    ops.ctr.exec(ops._build_mirror_create_script(MIRROR_DIR))
    return ops._parse_mirror_stats(MIRROR_CREATE_LOG)


def _buildcache_poc_script(ops: SpackOps) -> str:
    """Build the Phase 0 pkgconf producer/consumer compatibility script."""
    producer_root = "/tmp/itest-store-padded"
    strict_root = "/tmp/itest-store-strict"
    miss_root = "/tmp/itest-store-auto-miss"
    corrupt_root = "/tmp/itest-store-auto-corrupt"
    missing_cache = "/tmp/itest-buildcache-missing"
    corrupt_cache = "/tmp/itest-buildcache-corrupt"
    setup_script = (
        f"/opt/spack-{ops.spack_ver}/share/spack/setup-env.sh"
    )
    production_publish = build_publish_script(
        env_name=ENV_DIR,
        store_path=BUILDCACHE_DIR,
        setup_script=setup_script,
    )
    production_verify = build_verify_script(
        env_name=ENV_DIR,
        store_path=BUILDCACHE_DIR,
        setup_script=setup_script,
    )
    return f"""\
{ops._source_spack()}
env_name={ENV_DIR}
producer_root={producer_root}
strict_root={strict_root}
miss_root={miss_root}
corrupt_root={corrupt_root}
buildcache={BUILDCACHE_DIR}
missing_cache={missing_cache}
corrupt_cache={corrupt_cache}

configure_tree() {{
    local root=$1
    local padding=$2
    spack -e "$env_name" config add "config:install_tree:root:$root"
    spack -e "$env_name" config add "config:install_tree:padded_length:$padding"
}}

remove_mirrors() {{
    spack -e "$env_name" mirror remove --scope "env:$env_name" \
        binary-cache 2>/dev/null || true
    spack -e "$env_name" mirror remove --scope "env:$env_name" \
        source-mirror 2>/dev/null || true
}}

rm -rf "$producer_root" "$strict_root" "$miss_root" "$corrupt_root"
rm -rf "$buildcache" "$missing_cache" "$corrupt_cache"
remove_mirrors
spack -e "$env_name" concretize -f
cache_specs=$(spack -e "$env_name" python -c \
    'import spack.environment as ev; print(" ".join("/" + s.dag_hash() for s in ev.active_environment().all_specs() if not s.external))')
test -n "$cache_specs"
spack -e "$env_name" mirror add --scope "env:$env_name" source-mirror \
    file://{SOURCE_MIRROR_DIR}

# Padded producer: force source installation, then let Spack create the cache.
configure_tree "$producer_root" 128
spack -e "$env_name" install \
    --only-concrete --use-buildcache never --fail-fast
producer_location=$(spack -e "$env_name" location -i pkgconf)
test "${{#producer_location}}" -ge 128
case "$producer_location" in
    "$producer_root"/*) ;;
    *) echo "producer was not installed under padded root: $producer_location" >&2; exit 1 ;;
esac
mkdir -p "$buildcache"
{production_publish}
{production_verify}
test -n "$(find "$buildcache" -type f -print -quit)"
mkdir -p "$missing_cache"
spack -e "$env_name" buildcache push \
    --only dependencies --unsigned --fail-fast \
    "$missing_cache"
spack buildcache update-index "file://$missing_cache"
chmod -R a-w "$missing_cache"
echo POC_PUSH_INDEX_CHECK_OK

# Remove the padded producer tree. The strict consumer has no source mirror
# configured and invalid proxies, so success can only come from buildcache.
rm -rf "$producer_root"
remove_mirrors
spack -e "$env_name" mirror add --scope "env:$env_name" --unsigned binary-cache \
    "file://$buildcache"
configure_tree "$strict_root" 0
export http_proxy=http://127.0.0.1:1
export https_proxy=http://127.0.0.1:1
export all_proxy=http://127.0.0.1:1
spack -e "$env_name" install \
    --only-concrete --use-buildcache only --fail-fast
strict_location=$(spack -e "$env_name" location -i pkgconf)
case "$strict_location" in
    "$strict_root"/*) ;;
    *) echo "consumer did not relocate to short root: $strict_location" >&2; exit 1 ;;
esac
"$strict_location/bin/pkgconf" --version
echo POC_PADDED_RELOCATION_OK

# A Spack-created cache missing the root package must fail closed under "only".
unset http_proxy https_proxy all_proxy
remove_mirrors
spack -e "$env_name" mirror add --scope "env:$env_name" --unsigned binary-cache \
    "file://$missing_cache"
configure_tree /tmp/itest-store-only-missing 0
export http_proxy=http://127.0.0.1:1
export https_proxy=http://127.0.0.1:1
export all_proxy=http://127.0.0.1:1
set +e
spack -e "$env_name" install \
    --only-concrete --use-buildcache only --fail-fast \
    >/tmp/itest-only-missing.log 2>&1
only_rc=$?
set -e
test "$only_rc" -ne 0
test -z "$(find /tmp/itest-store-only-missing -type f -name pkgconf -print -quit 2>/dev/null)"
echo POC_ONLY_MISSING_FAILED_CLOSED

# "auto" cache miss falls back to the read-only source mirror.
unset http_proxy https_proxy all_proxy
remove_mirrors
spack -e "$env_name" mirror add --scope "env:$env_name" --unsigned binary-cache \
    "file://$missing_cache"
spack -e "$env_name" mirror add --scope "env:$env_name" source-mirror \
    file://{SOURCE_MIRROR_DIR}
configure_tree "$miss_root" 0
export http_proxy=http://127.0.0.1:1
export https_proxy=http://127.0.0.1:1
export all_proxy=http://127.0.0.1:1
spack -e "$env_name" install \
    --only-concrete --use-buildcache auto --fail-fast
test -x "$(spack -e "$env_name" location -i pkgconf)/bin/pkgconf"
echo POC_AUTO_MISS_FELL_BACK

# Advertise a valid index, remove one referenced blob, and verify that "auto"
# treats the recoverable damaged-entry error as a source-build fallback.
unset http_proxy https_proxy all_proxy
cp -a "$buildcache" "$corrupt_cache"
artifact=$(find "$corrupt_cache" -type f -path '*/blobs/*' -print -quit)
test -n "$artifact"
rm "$artifact"
chmod -R a-w "$corrupt_cache"
remove_mirrors
spack -e "$env_name" mirror add --scope "env:$env_name" --unsigned binary-cache \
    "file://$corrupt_cache"
spack -e "$env_name" mirror add --scope "env:$env_name" source-mirror \
    file://{SOURCE_MIRROR_DIR}
configure_tree "$corrupt_root" 0
export http_proxy=http://127.0.0.1:1
export https_proxy=http://127.0.0.1:1
export all_proxy=http://127.0.0.1:1
spack -e "$env_name" install \
    --only-concrete --use-buildcache auto --fail-fast \
    2>&1 | tee /tmp/itest-auto-corrupt.log
test -x "$(spack -e "$env_name" location -i pkgconf)/bin/pkgconf"
echo POC_AUTO_CORRUPT_FELL_BACK
"""


def _assert_buildcache_command_contract(script: str) -> None:
    """Pin the compatibility PoC to the reviewed Phase 0 command forms."""
    normalized = " ".join(script.split())
    required = (
        "config:install_tree:padded_length:$padding",
        "--only-concrete --use-buildcache never --fail-fast",
        "buildcache push --unsigned --fail-fast",
        f"spack buildcache update-index file://{BUILDCACHE_DIR}",
        f"buildcache check --mirror-url file://{BUILDCACHE_DIR}",
        "--only-concrete --use-buildcache only --fail-fast",
        "--only-concrete --use-buildcache auto --fail-fast",
    )
    for command in required:
        assert command in normalized


@pytest.mark.integration
class TestSpackScriptsIntegration:
    """Validate _build_*_script methods against real spack inside a container."""

    def test_concretize_creates_lock(self, spack_ops: SpackOps):
        """_build_concretize_script is accepted by spack and produces spack.lock."""
        ops = spack_ops
        ops.clean_stale_state()
        ops.prepare_environment(ENV_DIR, import_lock=False)
        ops.ctr.exec(ops._build_concretize_script(ENV_DIR))
        result = ops.ctr.exec(
            f"test -f {ENV_DIR}/spack.lock && echo LOCK_OK || echo LOCK_MISSING",
            capture=True,
        )
        assert "LOCK_OK" in (result.stdout or "")

    def test_repo_update_builtin(self, spack_ops: SpackOps):
        """spack repo update builtin works (idempotent after concretize)."""
        ops = spack_ops
        result = ops.ctr.exec(
            f"{ops._source_spack()}\nspack -e itest repo update builtin 2>&1",
            capture=True,
        )
        output = result.stdout or ""
        assert "up to date" in output.lower() or "updated" in output.lower(), \
            f"repo update didn't report success: {output[-300:]}"

    def test_register_local_repo(self, spack_ops: SpackOps):
        """Custom-repo prototype: Spack registers a synthetic local repo."""
        ops = spack_ops
        ops.ctr.exec("""
mkdir -p /tmp/itest-local-repo/packages/fakepkg
cat > /tmp/itest-local-repo/repo.yaml << 'EOF'
repo:
   namespace: itest_local
EOF
cat > /tmp/itest-local-repo/packages/fakepkg/package.py << 'EOF'
from spack_repo.builtin.build_systems.generic import Package
class Fakepkg(Package):
    pass
EOF
""")
        result = ops.ctr.exec(
            f"{ops._source_spack()}\nspack repo add /tmp/itest-local-repo 2>&1 && echo REPO_OK || echo REPO_FAIL",
            capture=True,
        )
        assert "REPO_OK" in (result.stdout or ""), \
            f"local repo registration failed: {result.stdout[-300:] if result.stdout else 'no output'}"

    def test_phase2_mirror_fresh_added(self, spack_ops: SpackOps):
        """First mirror create — all packages are 'added', regex matches."""
        ops = spack_ops
        ops.ctr.exec(f"rm -rf {MIRROR_DIR}")
        stats = _run_mirror(ops)
        assert stats["failed"] != -1, f"regex didn't match spack output: {stats}"
        assert stats["failed"] == 0
        assert stats["added"] >= 1

    def test_phase3_mirror_rerun_present(self, spack_ops: SpackOps):
        """Re-run mirror create — all 'already present', regex matches."""
        stats = _run_mirror(spack_ops)
        assert stats["failed"] != -1, f"regex didn't match: {stats}"
        assert stats["failed"] == 0
        assert stats["present"] >= 1

    def test_phase3b_mirror_verify(self, spack_ops: SpackOps):
        """_build_mirror_verify_script works and stats parse correctly."""
        ops = spack_ops
        ops.clean_stale_state()
        ops.prepare_environment(ENV_DIR, import_lock=True)
        from hpc_cf.spack_ops import MIRROR_VERIFY_LOG

        ops.ctr.exec(ops._build_mirror_verify_script(MIRROR_DIR))
        stats = ops._parse_mirror_stats(MIRROR_VERIFY_LOG)
        assert stats["failed"] != -1, f"verify regex didn't match: {stats}"
        assert stats["failed"] == 0
        assert stats["present"] >= 1

    def test_phase4_forced_failure(self, spack_ops: SpackOps):
        """Bad proxy → fetch fails → 'failed' counter >= 1, regex matches."""
        from hpc_cf.spack_ops import MIRROR_CREATE_LOG

        ops = spack_ops
        ops.clean_stale_state()
        ops.prepare_environment(ENV_DIR, import_lock=False)
        ops.ctr.exec(ops._build_concretize_script(ENV_DIR))
        ops.clean_stale_state()
        ops.prepare_environment(ENV_DIR, import_lock=True)
        ops.ctr.exec(f"rm -rf {MIRROR_DIR}")
        script = (
            "export https_proxy=http://127.0.0.1:1 "
            "http_proxy=http://127.0.0.1:1 all_proxy=http://127.0.0.1:1\n"
            f"{ops._build_mirror_create_script(MIRROR_DIR)}"
        )
        result = ops.ctr.exec(script, check=False)
        assert result.returncode != 0
        stats = ops._parse_mirror_stats(MIRROR_CREATE_LOG)
        assert stats["failed"] != -1, f"regex didn't match: {stats}"
        assert stats["failed"] >= 1


@pytest.mark.integration
class TestBuildcacheCompatibilityPoC:
    """Phase 0/L3 pkgconf buildcache behavior across supported Spack versions."""

    @pytest.mark.parametrize("version", SPACK_VERSIONS)
    def test_command_contract(self, version: str):
        """Keep producer and consumer commands aligned with the reviewed plan."""
        env = EnvConfig(spack=SpackConfig(version=version, env_name="itest"))
        ops = SpackOps(env, object())
        _assert_buildcache_command_contract(_buildcache_poc_script(ops))

    def test_pkgconf_buildcache_round_trip(self, spack_ops: SpackOps):
        """Exercise push/index/check, relocation, only, and auto fallbacks."""
        result = spack_ops.ctr.exec(
            _buildcache_poc_script(spack_ops),
            capture=True,
            check=False,
        )
        output = result.stdout or ""
        assert result.returncode == 0, (
            f"Spack {spack_ops.spack_ver} PoC failed:\n"
            f"{output[-10000:]}\n{(result.stderr or '')[-4000:]}"
        )
        expected_markers = (
            "POC_PUSH_INDEX_CHECK_OK",
            "POC_PADDED_RELOCATION_OK",
            "POC_ONLY_MISSING_FAILED_CLOSED",
            "POC_AUTO_MISS_FELL_BACK",
            "POC_AUTO_CORRUPT_FELL_BACK",
        )
        for marker in expected_markers:
            assert marker in output, (
                f"{marker} missing from Spack {spack_ops.spack_ver} output: "
                f"{output[-2000:]}"
            )


# ── OCI buildcache PoC (local registry) ─────────────────────────────────
#
# Lab evidence and root-cause analysis: artifacts/oci-registry-lab/notes.md.
# Behavior pinned here (spack 1.x against oci:// mirrors): push works and
# tags are name-version-daghash.spack; update-index runs (uploads an index
# tag); ``buildcache check`` is known-broken (always rc=1, empty output —
# needs_rebuild() has no oci dispatch); strict-only install pulls and
# relocates from padded paths; auto treats an oci miss as recoverable and
# falls back to the source mirror.

OCI_REGISTRY_PORT = 5000
OCI_REGISTRY_CONTAINER = "hpc-itest-registry"
OCI_REGISTRY_IMAGE_CANDIDATES = ("localhost/registry:latest", "docker.io/library/registry:latest")


def _oci_registry_reachable() -> bool:
    import urllib.request

    try:
        with urllib.request.urlopen(
            f"http://localhost:{OCI_REGISTRY_PORT}/v2/", timeout=5
        ) as resp:
            return resp.status == 200
    except OSError:
        return False


@pytest.fixture(scope="session")
def oci_registry():
    """Ensure a local OCI registry is reachable on OCI_REGISTRY_PORT.

    Reuses any registry already bound to the port (e.g. the operator's
    ``hpc-cf-registry`` lab container). Otherwise starts one from a locally
    available registry image; skips when neither exists, mirroring this
    module's "missing matrix assets skip" contract.
    """
    if _oci_registry_reachable():
        yield f"oci+http://localhost:{OCI_REGISTRY_PORT}"
        return

    image = next(
        (
            candidate
            for candidate in OCI_REGISTRY_IMAGE_CANDIDATES
            if subprocess.run(
                ["podman", "image", "exists", candidate], capture_output=True
            ).returncode
            == 0
        ),
        None,
    )
    if image is None:
        pytest.skip(
            f"no OCI registry reachable on :{OCI_REGISTRY_PORT} "
            "and no local registry image to start one"
        )

    subprocess.run(
        ["podman", "rm", "-f", OCI_REGISTRY_CONTAINER], capture_output=True
    )
    started = subprocess.run(
        [
            "podman",
            "run",
            "-d",
            "--name",
            OCI_REGISTRY_CONTAINER,
            "-p",
            f"{OCI_REGISTRY_PORT}:5000",
            image,
        ],
        capture_output=True,
        text=True,
    )
    if started.returncode != 0:
        pytest.skip(f"could not start registry container: {started.stderr.strip()}")
    for _ in range(15):
        if _oci_registry_reachable():
            yield f"oci+http://localhost:{OCI_REGISTRY_PORT}"
            subprocess.run(
                ["podman", "rm", "-f", OCI_REGISTRY_CONTAINER], capture_output=True
            )
            return
        time.sleep(1)
    subprocess.run(
        ["podman", "rm", "-f", OCI_REGISTRY_CONTAINER], capture_output=True
    )
    pytest.skip("registry container did not become ready within 15s")


@pytest.fixture(scope="class")
def spack_ops_oci(spack_ops: SpackOps):
    """spack_ops plus an optional offline builtin-repository seed.

    Spack 1.2.0 clones ``spack/spack-packages`` from GitHub on first use.
    On GitHub-hosted runners that clone is direct and fast, so no seed is
    needed. On constrained local networks, point HPC_CF_ITEST_BUILTIN_SEED
    at a local spack-packages checkout: it is copied into the container and
    registered as the builtin ``destination`` (RemoteRepoDescriptor skips
    all network when the destination already has .git).
    """
    seed = os.environ.get("HPC_CF_ITEST_BUILTIN_SEED")
    if seed:
        seed_path = Path(seed).expanduser().resolve()
        if not seed_path.is_dir():
            pytest.skip(f"HPC_CF_ITEST_BUILTIN_SEED={seed} is not a directory")
        copied = subprocess.run(
            [
                "podman",
                "cp",
                str(seed_path),
                f"{spack_ops.ctr.name}:/tmp/spack-packages-seed",
            ],
            capture_output=True,
            text=True,
        )
        assert copied.returncode == 0, copied.stderr
        spack_ops.ctr.exec(
            f"mkdir -p {spack_ops.user_dir} && "
            "printf 'repos:\\n  builtin:\\n"
            "    git: https://github.com/spack/spack-packages\\n"
            "    destination: /tmp/spack-packages-seed\\n' "
            f"> {spack_ops.user_dir}/repos.yaml"
        )
    yield spack_ops


def _buildcache_oci_poc_script(
    ops: SpackOps, registry_base: str, repo_suffix: str
) -> str:
    """OCI-mirror counterpart of _buildcache_poc_script.

    Exit codes are captured without pipelines (``out=$(cmd 2>&1) || rc=$?``):
    piping spack into ``tail`` would report tail's status, not spack's.
    """
    return f"""\
{ops._source_spack()}
env_name={ENV_DIR}
oci_repo="hpccf-itest-oci-{repo_suffix}"
oci_mirror="{registry_base}/$oci_repo"
miss_mirror="{registry_base}/$oci_repo-miss"
producer_root=/tmp/itest-oci-store-padded
strict_root=/tmp/itest-oci-store-strict
miss_root=/tmp/itest-oci-store-auto-miss

configure_tree() {{
    local root=$1
    local padding=$2
    spack -e "$env_name" config add "config:install_tree:root:$root"
    spack -e "$env_name" config add "config:install_tree:padded_length:$padding"
}}

remove_mirrors() {{
    spack -e "$env_name" mirror remove --scope "env:$env_name" \\
        binary-cache 2>/dev/null || true
    spack -e "$env_name" mirror remove --scope "env:$env_name" \\
        source-mirror 2>/dev/null || true
}}

rm -rf "$producer_root" "$strict_root" "$miss_root"
remove_mirrors
spack -e "$env_name" concretize -f
cache_specs=$(spack -e "$env_name" python -c \\
    'import spack.environment as ev; print(" ".join("/" + s.dag_hash() for s in ev.active_environment().all_specs() if not s.external))')
test -n "$cache_specs"
spack -e "$env_name" mirror add --scope "env:$env_name" source-mirror \\
    file://{SOURCE_MIRROR_DIR}

# Padded producer install from source (mirrors the file:// PoC producer).
configure_tree "$producer_root" 128
spack -e "$env_name" install \\
    --only-concrete --use-buildcache never --fail-fast
producer_location=$(spack -e "$env_name" location -i pkgconf)
test "${{#producer_location}}" -ge 128
case "$producer_location" in
    "$producer_root"/*) ;;
    *) echo "producer was not installed under padded root: $producer_location" >&2; exit 1 ;;
esac

# Push to the OCI mirror.
push_rc=0
push_out=$(spack -e "$env_name" buildcache push --unsigned --fail-fast \\
    "$oci_mirror" $cache_specs 2>&1) || push_rc=$?
echo "$push_out" | tail -3
test "$push_rc" -eq 0
echo OCI_PUSH_OK

# Tags must be name-version-daghash.spack and contain a pushed dag hash.
first_hash=$(printf '%s\\n' $cache_specs | head -1 | tr -d '/')
OCI_TAGS_URL="http://localhost:{OCI_REGISTRY_PORT}/v2/$oci_repo/tags/list" \\
OCI_HASH_1="$first_hash" python3 - <<'PYEOF'
import json, os, re, urllib.request
data = json.load(urllib.request.urlopen(os.environ["OCI_TAGS_URL"], timeout=15))
tags = data.get("tags") or []
pkg_tags = [t for t in tags if re.match(r"^pkgconf-[^-]+-[a-z0-9]{{32}}\.spack$", t)]
assert pkg_tags, "no pkgconf tag in registry: " + repr(tags)
assert any(os.environ["OCI_HASH_1"] in t for t in tags), "pushed dag hash missing: " + repr(tags)
print("TAGS_OK count=" + str(len(tags)))
PYEOF
echo OCI_TAG_SHAPE_OK

# update-index runs against oci mirrors (uploads an index tag).
idx_rc=0
idx_out=$(spack buildcache update-index "$oci_mirror" 2>&1) || idx_rc=$?
echo "$idx_out" | tail -2
test "$idx_rc" -eq 0
echo OCI_INDEX_NO_CRASH

# Known-broken contract: check cannot see oci mirrors and reports rc=1
# regardless of content. If this ever turns rc=0, the oci admission
# design (coverage-based) must be revisited.
chk_rc=0
chk_out=$(spack -e "$env_name" buildcache check --mirror-url "$oci_mirror" \\
    $cache_specs 2>&1) || chk_rc=$?
echo "oci check rc=$chk_rc (known-broken contract: 1)"
test "$chk_rc" -eq 1
echo OCI_CHECK_KNOWN_BROKEN

# Strict consumer: only-mode pull + padded relocation, network isolated.
# no_proxy keeps the local registry reachable through the invalid proxies.
rm -rf "$producer_root"
remove_mirrors
spack -e "$env_name" mirror add --scope "env:$env_name" --unsigned binary-cache \\
    "$oci_mirror"
configure_tree "$strict_root" 0
export http_proxy=http://127.0.0.1:1
export https_proxy=http://127.0.0.1:1
export all_proxy=http://127.0.0.1:1
export no_proxy=localhost,127.0.0.1
export NO_PROXY=localhost,127.0.0.1
spack -e "$env_name" install \\
    --only-concrete --use-buildcache only --fail-fast
strict_location=$(spack -e "$env_name" location -i pkgconf)
case "$strict_location" in
    "$strict_root"/*) ;;
    *) echo "consumer did not relocate to short root: $strict_location" >&2; exit 1 ;;
esac
"$strict_location/bin/pkgconf" --version
echo OCI_RELOCATION_OK

# Auto policy on an empty oci repo: recoverable miss -> source fallback.
unset http_proxy https_proxy all_proxy no_proxy NO_PROXY
remove_mirrors
spack -e "$env_name" mirror add --scope "env:$env_name" --unsigned binary-cache \\
    "$miss_mirror"
spack -e "$env_name" mirror add --scope "env:$env_name" source-mirror \\
    file://{SOURCE_MIRROR_DIR}
configure_tree "$miss_root" 0
export http_proxy=http://127.0.0.1:1
export https_proxy=http://127.0.0.1:1
export all_proxy=http://127.0.0.1:1
export no_proxy=localhost,127.0.0.1
export NO_PROXY=localhost,127.0.0.1
spack -e "$env_name" install \\
    --only-concrete --use-buildcache auto --fail-fast
miss_location=$(spack -e "$env_name" location -i pkgconf)
test -x "$miss_location/bin/pkgconf"
echo OCI_AUTO_MISS_FELL_BACK
"""


def _assert_oci_poc_contract(script: str) -> None:
    """Pin the OCI PoC to the lab-verified command forms."""
    normalized = " ".join(script.split())
    required = (
        "buildcache push --unsigned --fail-fast",
        "buildcache update-index",
        "buildcache check --mirror-url",
        "--only-concrete --use-buildcache never --fail-fast",
        "--only-concrete --use-buildcache only --fail-fast",
        "--only-concrete --use-buildcache auto --fail-fast",
        "no_proxy=localhost,127.0.0.1",
        "|| push_rc=$?",
        "|| idx_rc=$?",
        "|| chk_rc=$?",
    )
    for command in required:
        assert command in normalized, f"missing command form: {command}"
    for marker in (
        "OCI_PUSH_OK",
        "OCI_TAG_SHAPE_OK",
        "OCI_INDEX_NO_CRASH",
        "OCI_CHECK_KNOWN_BROKEN",
        "OCI_RELOCATION_OK",
        "OCI_AUTO_MISS_FELL_BACK",
    ):
        assert marker in script, f"missing marker: {marker}"


@pytest.mark.integration
class TestOciBuildcachePoC:
    """OCI-mirror buildcache behavior across supported Spack versions."""

    @pytest.mark.parametrize("version", SPACK_VERSIONS)
    def test_oci_command_contract(self, version: str):
        """Script builder emits the reviewed, lab-verified command forms."""
        env = EnvConfig(spack=SpackConfig(version=version, env_name="itest"))
        ops = SpackOps(env, object())
        script = _buildcache_oci_poc_script(
            ops, "oci+http://localhost:5000", "contract0"
        )
        _assert_oci_poc_contract(script)

    def test_oci_registry_round_trip(self, spack_ops_oci, oci_registry):
        """push/tags/index/check/relocation/auto-miss against a real registry."""
        ops = spack_ops_oci
        suffix = f"{ops.spack_ver.replace('.', '_')}-{uuid.uuid4().hex[:8]}"
        script = _buildcache_oci_poc_script(ops, oci_registry, suffix)
        result = ops.ctr.exec(script, capture=True, check=False)
        output = result.stdout or ""
        assert result.returncode == 0, (
            f"Spack {ops.spack_ver} OCI PoC failed:\n"
            f"{output[-10000:]}\n{(result.stderr or '')[-4000:]}"
        )
        expected_markers = (
            "OCI_PUSH_OK",
            "OCI_TAG_SHAPE_OK",
            "OCI_INDEX_NO_CRASH",
            "OCI_CHECK_KNOWN_BROKEN",
            "OCI_RELOCATION_OK",
            "OCI_AUTO_MISS_FELL_BACK",
        )
        for marker in expected_markers:
            assert marker in output, (
                f"{marker} missing from Spack {ops.spack_ver} output: "
                f"{output[-2000:]}"
            )


@pytest.mark.integration
@pytest.mark.e2e
def test_e2e_boundary_skeleton_render_smoke(tmp_path):
    """Opt-in E2E skeleton: validation → render → Dockerfile parser smoke.

    Covers ``use_mirror=True`` and the current ``generate_dockerfile`` signature
    without building a full image. Uses the newest ``cp2k_opensource-2026*``
    env when present; skips otherwise.
    """
    from hpc_cf.env import list_available_envs
    from hpc_cf.environment import load_environment_spec
    from hpc_cf.execution import ProjectLayout
    from hpc_cf.spack_plan import build_spack_environment_plan
    from hpc_cf.template import generate_dockerfile
    from hpc_cf.validation import ValidationProfile, validate_environment

    layout = ProjectLayout.default()
    candidates = [
        e for e in list_available_envs(layout=layout) if e.startswith("cp2k_opensource-2026")
    ]
    if not candidates:
        pytest.skip("no cp2k_opensource-2026* env present")
    env_name = sorted(candidates)[-1]
    env_dir = layout.spack_envs_dir / env_name

    report = validate_environment(env_dir, ValidationProfile.CONFIG, layout=layout)
    assert report.ok, report.format_text()

    out = tmp_path / f".e2e-{env_name}.Dockerfile"
    path = generate_dockerfile(
        template=None,
        app_version=env_name,
        output=out,
        use_mirror=True,
        build_only=False,
    )
    rendered = path.read_text(encoding="utf-8")
    plan = build_spack_environment_plan(load_environment_spec(env_dir))

    # Render smoke: plan env name + mirror registration under independent site scope.
    assert f"spack env create {plan.env_name}" in rendered
    assert "local-mirror file:///opt/spack-mirror" in rendered
    assert f"spack mirror add --scope {plan.mirror_scope_flag()} " in rendered

    # Lightweight Dockerfile parser smoke (multi-stage / FROM lines).
    from_lines = [ln for ln in rendered.splitlines() if ln.startswith("FROM ")]
    assert from_lines, "rendered Dockerfile must contain FROM instructions"
    assert "AS builder" in rendered or len(from_lines) >= 1
    assert "{{" not in rendered and "{%" not in rendered
