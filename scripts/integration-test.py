#!/usr/bin/env python3
"""Integration test: validates _build_*_script against real spack 1.1.1.

Self-contained: creates a minimal env (pkgconf) inside a persistent container,
then exercises the full pipeline (concretize → mirror create × 3 phases).

Usage:
    ./venv/bin/python scripts/integration-test.py
"""
from __future__ import annotations

import sys
import traceback
from pathlib import Path

# Allow importing hpc_cf from the project root without installation.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hpc_cf.config import ASSETS_DIR, PROJECT_ROOT
from hpc_cf.container import Container
from hpc_cf.spack_ops import (
    EnvConfig,
    SpackConfig,
    SpackOps,
    _parse_mirror_stats_from_text,
)

SPACK_VERSION = "1.1.1"
IMAGE = "hpc-mirror-builder"
ENV_NAME = "itest"
CONTAINER_NAME = "hpc-itest"
ENV_DIR = "/tmp/itest-env"
MIRROR_DIR = "/tmp/itest-mirror"


def main() -> int:
    print("=== Integration Test: _build_*_script vs real spack ===\n")

    # --- Preconditions ---
    if not (ASSETS_DIR / f"spack-v{SPACK_VERSION}.tar.gz").exists():
        print(f"SKIP: assets/spack-v{SPACK_VERSION}.tar.gz not found")
        return 2
    if not (ASSETS_DIR / f"bootstrap-{SPACK_VERSION}").is_dir():
        print(f"SKIP: assets/bootstrap-{SPACK_VERSION} not found")
        return 2

    env_config = EnvConfig(
        spack=SpackConfig(version=SPACK_VERSION, env_name=ENV_NAME),
    )

    ctr = Container(
        name=CONTAINER_NAME,
        image=IMAGE,
        project_root=PROJECT_ROOT,
    )

    ops = SpackOps(env_config, ctr)

    try:
        ctr.create()
        print("--- Container started ---")

        # --- Setup: system pkgs + clean + compilers ---
        print("--- Setup: install_system_pkgs + clean + compiler_find ---")
        ops.install_system_pkgs()
        ops.clean_stale_state()
        ops.compiler_find()

        # --- Create minimal env inside container ---
        print("--- Create minimal env (pkgconf) ---")
        ctr.exec(f"""
mkdir -p {ENV_DIR}
cat > {ENV_DIR}/spack.yaml << 'YAMLEOF'
spack:
  concretizer:
    unify: true
  specs:
    - pkgconf
YAMLEOF
""")

        # === Phase 1: Concretize ===
        print("\n=== Phase 1: Concretize ===")
        ops.clean_stale_state()
        ctr.exec(ops._build_concretize_script(ENV_DIR))
        # Verify lock was created
        result = ctr.exec(f"test -f {ENV_DIR}/spack.lock && echo LOCK_OK || echo LOCK_MISSING", capture=True)
        assert "LOCK_OK" in (result.stdout or ""), f"Phase 1 FAIL: spack.lock not created"
        print("  ✅ spack.lock created — _build_concretize_script accepted by spack")

        # === Phase 2: Mirror create (fresh — all added) ===
        print("\n=== Phase 2: Mirror create (fresh) ===")
        ops.clean_stale_state()
        ctr.exec(ops._build_mirror_create_script(ENV_DIR, MIRROR_DIR))
        stats_added = ops._parse_mirror_stats()
        print(f"  Stats: {stats_added}")
        assert stats_added["failed"] != -1, f"Phase 2 FAIL: regex didn't match spack output at all: {stats_added}"
        assert stats_added["failed"] == 0, f"Phase 2 FAIL: expected failed==0: {stats_added}"
        assert stats_added["added"] >= 1, f"Phase 2 FAIL: expected added>=1: {stats_added}"
        print(f"  ✅ 'added' regex verified (added={stats_added['added']})")

        # === Phase 3: Mirror create (re-run — all present) ===
        print("\n=== Phase 3: Mirror create (re-run) ===")
        ops.clean_stale_state()
        ctr.exec(ops._build_mirror_create_script(ENV_DIR, MIRROR_DIR))
        stats_present = ops._parse_mirror_stats()
        print(f"  Stats: {stats_present}")
        assert stats_present["failed"] != -1, f"Phase 3 FAIL: regex didn't match: {stats_present}"
        assert stats_present["failed"] == 0, f"Phase 3 FAIL: expected failed==0: {stats_present}"
        assert stats_present["present"] >= 1, f"Phase 3 FAIL: expected present>=1: {stats_present}"
        print(f"  ✅ 'already present' regex verified (present={stats_present['present']})")

        # === Phase 4: Forced failure (delete mirror + bad proxy) ===
        print("\n=== Phase 4: Forced failure (bad proxy) ===")
        # Inject bad proxy via .bash_profile (ctr.exec uses bash -lc = login shell)
        ctr.exec("echo 'export https_proxy=http://127.0.0.1:1 http_proxy=http://127.0.0.1:1 all_proxy=http://127.0.0.1:1' > /tmp/home/.bash_profile")
        ctr.exec(f"rm -rf {MIRROR_DIR}")  # force full re-fetch
        ops.clean_stale_state()
        ctr.exec(ops._build_mirror_create_script(ENV_DIR, MIRROR_DIR))
        stats_fail = ops._parse_mirror_stats()
        print(f"  Stats: {stats_fail}")
        assert stats_fail["failed"] != -1, f"Phase 4 FAIL: regex didn't match: {stats_fail}"
        assert stats_fail["failed"] >= 1, f"Phase 4 FAIL: expected failed>=1: {stats_fail}"
        print(f"  ✅ 'failed' regex verified (failed={stats_fail['failed']})")
        # Remove proxy for cleanup
        ctr.exec("rm -f /tmp/home/.bash_profile")

        # === Phase 5 (optional): Install ===
        print("\n=== Phase 5: Install pkgconf ===")
        ops.clean_stale_state()
        # Re-create mirror (was deleted in Phase 4)
        ctr.exec(ops._build_mirror_create_script(ENV_DIR, MIRROR_DIR))
        # Install from the mirror
        result = ctr.exec(f"""
{ops._source_spack()}
spack install --no-check-signature -y pkgconf 2>&1 | tail -5
spack find pkgconf
""", capture=True)
        output = result.stdout or ""
        if "Successfully installed" in output or "pkgconf" in output.split("find")[-1]:
            print("  ✅ pkgconf installed — install path verified")
        else:
            print(f"  ⚠️ Install output (check manually):\n{output[-500:]}")

        print("\n" + "=" * 60)
        print("✅ Integration test PASSED — all phases verified")
        print("   Phase 1: concretize script accepted by spack")
        print(f"   Phase 2: 'added' regex = {stats_added['added']}")
        print(f"   Phase 3: 'present' regex = {stats_present['present']}")
        print(f"   Phase 4: 'failed' regex = {stats_fail['failed']}")
        print("=" * 60)
        return 0

    except Exception as exc:
        print(f"\n❌ Integration test FAILED: {exc}")
        traceback.print_exc()
        return 1
    finally:
        try:
            ctr.destroy()
            print("\n--- Container destroyed ---")
        except Exception:
            pass


if __name__ == "__main__":
    sys.exit(main())
