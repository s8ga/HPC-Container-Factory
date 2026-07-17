"""Integration matrix skeleton: Spack versions + custom-repo prototype.

Opt-in only (``pytest --run-integration``).  Does **not** build full HPC
images — exercises script contracts against a minimal pkgconf env and a
synthetic local repo.  Missing assets for a matrix cell → skip that cell.
"""
from __future__ import annotations

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
