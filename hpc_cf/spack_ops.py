"""Spack operations — bootstrap, concretize, mirror, verify.

All operations are executed inside Podman containers via :class:`Container`.
Replaces the Spack-specific portions of:
  - ``scripts/spack-common.sh`` (streamline_parse_env, step_*, spack_bootstrap,
    mirror_create, mirror_verify, streamline_dispatch)
  - ``scripts/prepare-bootstrap-cache.sh`` (bootstrap generation logic)
  - ``scripts/build-mirror-in-container.sh`` (cmd_bootstrap, cmd_concretize,
    cmd_mirror, cmd_verify, cmd_all)
"""

from __future__ import annotations

import logging
import os
import re
import shlex
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from hpc_cf.config import DEFAULT_SPACK_VERSION, PROJECT_ROOT, SPACK_ENVS_DIR
from hpc_cf.container import Container

try:
    import yaml
except ImportError as exc:  # pragma: no cover - exercised when pyyaml missing
    raise ImportError(f"Required package not installed: {exc}. Install: pip install pyyaml") from exc

logger = logging.getLogger(__name__)


# ── Data classes ──────────────────────────────────────────────────────────


@dataclass
class CustomRepo:
    type: Literal["git", "local"]
    namespace: str
    # git only
    url: str | None = None
    branch: str | None = None
    sparse_path: str | None = None
    # local only
    path: str | None = None


@dataclass
class SpackConfig:
    version: str
    env_name: str
    custom_repos: list[CustomRepo] = field(default_factory=list)


@dataclass
class MirrorBuilderConfig:
    system_pkgs: list[str] = field(default_factory=list)
    pkg_mirror_setup: str = ""
    pkg_install_cmd: str = ""


@dataclass
class EnvConfig:
    """Complete env.yaml representation."""
    spack: SpackConfig
    mirror_builder: MirrorBuilderConfig = field(default_factory=MirrorBuilderConfig)
    template_vars: dict = field(default_factory=dict)

    @property
    def spack_user_dir_name(self) -> str:
        return f".spack-v{self.spack.version}"

    @property
    def spack_user_dir_in_container(self) -> str:
        return f"/work/assets/{self.spack_user_dir_name}"

    @property
    def bootstrap_dir_name(self) -> str:
        return f"bootstrap-{self.spack.version}"

    @property
    def bootstrap_dir_in_container(self) -> str:
        return f"/work/assets/{self.bootstrap_dir_name}"


# ── env.yaml loader ──────────────────────────────────────────────────────


def load_env_config(env_dir: Path) -> EnvConfig:
    """Parse env.yaml from an environment directory into an EnvConfig.

    Uses the shared :func:`hpc_cf.env.find_env_yaml` resolver so that this
    loader and ``load_env_yaml`` agree on which file is read (previously these
    two had REVERSED lookup orders; see plan A2).
    """
    from hpc_cf.env import find_env_yaml

    env_yaml = find_env_yaml(env_dir)
    with open(env_yaml) as f:
        raw = yaml.safe_load(f)

    spack_raw = raw.get("spack", {})
    mb_raw = raw.get("mirror_builder", {})

    repos: list[CustomRepo] = []
    for r in spack_raw.get("custom_repos", []):
        if "url" in r:
            repos.append(CustomRepo(
                type="git",
                namespace=r["namespace"],
                url=r["url"],
                branch=r.get("branch", "main"),
                sparse_path=r.get("sparse_path"),
            ))
        elif "path" in r:
            repos.append(CustomRepo(
                type="local",
                namespace=r["namespace"],
                path=r["path"],
            ))

    return EnvConfig(
        spack=SpackConfig(
            version=spack_raw.get("version", DEFAULT_SPACK_VERSION),
            env_name=spack_raw.get("env_name", "cp2k-env"),
            custom_repos=repos,
        ),
        mirror_builder=MirrorBuilderConfig(
            system_pkgs=mb_raw.get("system_pkgs", []),
            pkg_mirror_setup=mb_raw.get("pkg_mirror_setup", ""),
            pkg_install_cmd=mb_raw.get("pkg_install_cmd", ""),
        ),
        template_vars=raw.get("template_vars", {}),
    )


def resolve_env_paths(env_name: str) -> tuple[Path, Path]:
    """Return (host_env_dir, container_env_dir) for the given env name.

    Handles the ``spack-env-file/`` subdirectory layout.
    """
    host_dir = SPACK_ENVS_DIR / env_name
    container_dir = Path(f"/work/spack-envs/{env_name}")

    if (host_dir / "spack-env-file" / "env.yaml").exists() or (host_dir / "spack-env-file" / "spack.yaml").exists():
        host_dir = host_dir / "spack-env-file"
        container_dir = container_dir / "spack-env-file"

    return host_dir, container_dir


# ── Mirror stats parsing (pure, unit-testable) ──────────────────────────


# Sentinel: callers treat failed < 0 as "status could not be determined".
# This is the fix for the silent-success bug (plan A3): previously any parse
# exception returned failed=0, and callers only raised on failed>0, so a
# broken/incomplete mirror was reported as success.
MIRROR_STATS_UNKNOWN = -1


def _parse_mirror_stats_from_text(text: str) -> dict[str, int]:
    """Parse spack mirror-create/verify stdout into {present, added, failed}.

    Pure function (no I/O) so it can be unit-tested directly. Returns the
    LAST match of each counter (spack prints progress lines; the final
    summary is what matters). If the text has no recognizable failed-count
    line, returns ``failed=MIRROR_STATS_UNKNOWN`` (-1) so callers can
    distinguish "0 failures" from "couldn't tell" — never reports 0 on
    garbage.
    """
    present: int | None = None
    added: int | None = None
    failed: int | None = None
    if text:
        for m in re.finditer(r"(\d+)\s+already present", text):
            present = int(m.group(1))
        for m in re.finditer(r"(\d+)\s+added", text):
            added = int(m.group(1))
        for m in re.finditer(r"(\d+)\s+failed", text):
            failed = int(m.group(1))
    return {
        "present": present if present is not None else MIRROR_STATS_UNKNOWN,
        "added": added if added is not None else MIRROR_STATS_UNKNOWN,
        "failed": failed if failed is not None else MIRROR_STATS_UNKNOWN,
    }


# ── SpackOps ─────────────────────────────────────────────────────────────


class SpackOps:
    """All Spack operations, executed inside containers.

    Parameters
    ----------
    env:
        Parsed environment configuration.
    container:
        Container to execute commands in.
    """

    def __init__(self, env: EnvConfig, container: Container) -> None:
        self.env = env
        self.ctr = container
        self.spack_ver = env.spack.version
        self.spack_root = f"/opt/spack-{self.spack_ver}"
        self.user_dir = env.spack_user_dir_in_container
        self.user_cache = f"{self.user_dir}/cache"

    def _setup_env_vars(self) -> str:
        """Common environment setup for all Spack commands."""
        return f"""
mkdir -p {self.user_dir} {self.user_cache}
export SPACK_USER_CONFIG_PATH="{self.user_dir}"
export SPACK_USER_CACHE_PATH="{self.user_cache}"
export HOME=/tmp/home && mkdir -p "$HOME"
"""

    def _source_spack(self, *, extract: bool = True) -> str:
        """Return a script fragment that extracts + sources Spack.

        Uses ``sudo`` for extraction because the persistent worker container
        runs as a non-root user and ``/opt`` may not be world-writable
        (the ``chmod a+w`` from the Dockerfile can be lost after overlay
        layer mutations).  The mirror-builder image grants NOPASSWD sudo
        to all users.
        """
        tarball = f"/work/assets/spack-v{self.spack_ver}.tar.gz"
        extract_block = ""
        if extract:
            extract_block = f"""
if [[ ! -f "{self.spack_root}/share/spack/setup-env.sh" ]]; then
    sudo mkdir -p "{self.spack_root}"
    sudo tar -axf "{tarball}" --strip-components=1 -C "{self.spack_root}"
    sudo chown -R "$(id -u):$(id -g)" "{self.spack_root}"
fi
"""
        bootstrap_dir = self.env.bootstrap_dir_in_container
        bootstrap_block = f"""
# Configure local bootstrap mirror as highest-priority trusted source
if [[ -d "{bootstrap_dir}/metadata/sources" ]]; then
    spack bootstrap add --trust local-sources "{bootstrap_dir}/metadata/sources" 2>/dev/null || true
    spack bootstrap add --trust local-binaries "{bootstrap_dir}/metadata/binaries" 2>/dev/null || true
    spack bootstrap now 2>/dev/null || true
else
    echo "No local bootstrap mirror found — falling back to Spack defaults (github-actions, spack-install)"
fi
"""
        return f"""{self._setup_env_vars()}
{extract_block}
. "{self.spack_root}/share/spack/setup-env.sh"
{bootstrap_block}
"""

    # ── Bootstrap mirror ──────────────────────────────────────────────────

    def bootstrap_mirror(self, *, force: bool = False) -> Path:
        """Generate Spack bootstrap mirror.

        Replaces ``prepare-bootstrap-cache.sh`` (~318 lines).

        Returns the local bootstrap directory path.
        """
        from hpc_cf.config import PROJECT_ROOT as root

        bootstrap_dir_name = self.env.bootstrap_dir_name
        local_dir = root / "assets" / bootstrap_dir_name
        container_dir = self.env.bootstrap_dir_in_container

        # Check if already complete
        metadata = local_dir / "metadata" / "sources" / "metadata.yaml"
        if not force and metadata.exists() and metadata.stat().st_size > 0:
            if self._bootstrap_metadata_complete(local_dir):
                logger.info("Bootstrap cache already complete — skipping (use --force to regenerate)")
                return local_dir

        if force and local_dir.exists():
            logger.info("Removing existing bootstrap directory: %s", local_dir)
            import shutil
            shutil.rmtree(local_dir)

        local_dir.mkdir(parents=True, exist_ok=True)

        # Run in ephemeral container to avoid stale repo pollution
        source = self._source_spack()
        script = f"""{source}
rm -f "${{SPACK_USER_CONFIG_PATH}}/repos.yaml"
rm -rf /tmp/spack-repos /tmp/spack-env-*
mkdir -p "{container_dir}"
spack bootstrap mirror --binary-packages "{container_dir}"
"""
        logger.info("Generating bootstrap mirror for Spack %s", self.spack_ver)
        try:
            self.ctr.run_ephemeral(script)
        except subprocess.CalledProcessError:
            logger.warning("Bootstrap with --binary-packages failed, retrying sources only...")
            script_fallback = f"""{source}
rm -f "${{SPACK_USER_CONFIG_PATH}}/repos.yaml"
rm -rf /tmp/spack-repos /tmp/spack-env-*
mkdir -p "{container_dir}"
spack bootstrap mirror "{container_dir}"
"""
            self.ctr.run_ephemeral(script_fallback)

        self._verify_bootstrap(local_dir)
        return local_dir

    def _bootstrap_metadata_complete(self, bootstrap_dir: Path) -> bool:
        """Check whether all expected bootstrap metadata files are present."""
        metadata = bootstrap_dir / "metadata" / "sources" / "metadata.yaml"
        if not metadata.exists() or metadata.stat().st_size == 0:
            return False
        for name in ("clingo", "gnupg", "patchelf"):
            f = bootstrap_dir / "metadata" / "binaries" / f"{name}.json"
            if not f.exists() or f.stat().st_size == 0:
                return False
        return True

    def _verify_bootstrap(self, bootstrap_dir: Path) -> None:
        """Validate generated bootstrap metadata."""
        metadata = bootstrap_dir / "metadata" / "sources" / "metadata.yaml"
        if not metadata.exists() or metadata.stat().st_size == 0:
            raise RuntimeError(f"Bootstrap metadata missing or empty: {metadata}")

        for name in ("clingo", "gnupg", "patchelf"):
            f = bootstrap_dir / "metadata" / "binaries" / f"{name}.json"
            if not f.exists() or f.stat().st_size == 0:
                logger.warning("Missing optional binary metadata: %s", f)

        file_count = sum(1 for _ in bootstrap_dir.rglob("*") if _.is_file())
        logger.info("Bootstrap cache prepared — files: %d", file_count)

    # ── System packages ───────────────────────────────────────────────────

    def install_system_pkgs(self) -> None:
        """Configure mirrors and install system packages declared in env.yaml."""
        mb = self.env.mirror_builder
        if mb.pkg_mirror_setup:
            logger.info("Configuring package mirrors...")
            self.ctr.exec(f"sudo bash -c {shlex.quote(mb.pkg_mirror_setup)}")

        if mb.system_pkgs and mb.pkg_install_cmd:
            logger.info("Installing system packages...")
            pkgs = " ".join(mb.system_pkgs)
            self.ctr.exec(f"sudo bash -c {shlex.quote(mb.pkg_install_cmd + ' ' + pkgs)}")
            logger.info("System packages installed")
        else:
            logger.info("No system packages declared — skipping")

    # ── Repo management ───────────────────────────────────────────────────

    def clean_stale_state(self) -> None:
        """Remove stale Spack state from previous runs in persistent container.

        Cleans:
          1. repos.yaml — non-builtin repo registrations (re-added by register_repos)
          2. packages.yaml — external package detections (re-added by compiler_find)
          3. Spack environments under var/spack/environments/ — avoids ``env create`` conflicts
          4. /tmp work dirs from previous runs

        This MUST run before any ``spack env create`` / ``spack repo add`` calls
        to guarantee a clean slate on persistent worker containers.
        """
        source = self._source_spack()
        script = f"""{source}
# 1. Nuke repo/external-package registrations — will be re-added from scratch
rm -f "${{SPACK_USER_CONFIG_PATH}}/repos.yaml"
rm -f "${{SPACK_USER_CONFIG_PATH}}/packages.yaml"

# 2. Remove ALL Spack environments to avoid ``env create`` name collisions.
#    Each pipeline recreates its env from spack.yaml anyway.
env_dir="{self.spack_root}/var/spack/environments"
if [[ -d "${{env_dir}}" ]]; then
    rm -rf "${{env_dir}}"/*
fi

# 3. Clean /tmp work dirs from previous runs
rm -rf /tmp/spack-repos /tmp/spack-env-* /tmp/spack-mirror-* /tmp/spack-verify-*
"""
        self.ctr.exec(script)

    def register_repos(self, env_dir_in_container: str) -> None:
        """Register custom repos from env.yaml."""
        repos = self.env.spack.custom_repos
        if not repos:
            logger.info("No custom repos configured — skipping")
            return

        parts = [self._source_spack()]

        for repo in repos:
            if repo.type == "git":
                parts.append(self._register_git_repo(repo))
            elif repo.type == "local":
                parts.append(self._register_local_repo(repo, env_dir_in_container))

        self.ctr.exec("\n".join(parts))

    def _register_git_repo(self, repo: CustomRepo) -> str:
        if not repo.url:
            raise ValueError("custom_repos git repo missing url")
        ns = shlex.quote(repo.namespace)
        url = shlex.quote(repo.url)
        branch = shlex.quote(repo.branch or "main")
        sparse = shlex.quote(repo.sparse_path) if repo.sparse_path else ""
        clone_dir = f"/tmp/spack-repos/spack_repo/{repo.namespace}"
        clone_dir_q = shlex.quote(clone_dir)
        clone_tmp = f"/tmp/repo-clone-{repo.namespace}"
        clone_tmp_q = shlex.quote(clone_tmp)

        sparse_block = ""
        if repo.sparse_path:
            sparse_block = f"""
            git sparse-checkout set {sparse}
            cp -a {sparse}/. {clone_dir_q}/"""
        else:
            sparse_block = f"cp -a . {clone_dir_q}/"

        return f"""
# Register git repo: {repo.namespace}
registered_dir=$(spack repo list 2>/dev/null | awk -v ns={ns} '$1 == ns {{print $2}}')
if [[ -n "${{registered_dir}}" ]]; then
    if [[ -d "${{registered_dir}}" && -f "${{registered_dir}}/repo.yaml" ]]; then
        echo "Repo {repo.namespace} already registered at ${{registered_dir}} — skipping"
    else
        echo "Stale registration for {repo.namespace} — removing"
        spack repo remove {ns} 2>/dev/null || true
    fi
fi

if ! spack repo list 2>/dev/null | awk '{{print $1}}' | grep -Fxq {ns}; then
    if [[ ! -d {clone_dir_q}/repo.yaml ]]; then
        rm -rf {clone_dir_q} {clone_tmp_q}
        mkdir -p /tmp/spack-repos/spack_repo
        git clone --depth 1 --filter=blob:none --sparse \
            -b {branch} {url} {clone_tmp_q}
        cd {clone_tmp_q}
        mkdir -p {clone_dir_q}
        {sparse_block}
        cd /tmp
        rm -rf {clone_tmp_q}
    fi
    spack repo add {clone_dir_q}
    echo "Registered {repo.namespace} (git)"
fi
"""

    def _register_local_repo(self, repo: CustomRepo, env_dir: str) -> str:
        if not repo.path:
            raise ValueError("custom_repos local repo missing path")
        ns = shlex.quote(repo.namespace)
        repo_path = f"{env_dir}/{repo.path}"
        repo_path_q = shlex.quote(repo_path)
        return f"""
# Register local repo: {repo.namespace}
if spack repo list 2>/dev/null | awk '{{print $1}}' | grep -Fxq {ns}; then
    echo "Repo {repo.namespace} already registered — skipping"
else
    spack repo add {repo_path_q}
    echo "Registered {repo.namespace} (local)"
fi
"""

    # ── Compiler find ─────────────────────────────────────────────────────

    def compiler_find(self) -> None:
        """Find system compilers (no external find)."""
        source = self._source_spack()
        script = f"""{source}
spack compiler find
"""
        self.ctr.exec(script)
        logger.info("Compilers registered")

    # ── Concretize ────────────────────────────────────────────────────────

    def concretize(self, env_dir_host: Path, env_dir_container: str) -> None:
        """Run spack concretize and write spack.lock back to host.

        Replaces ``step_concretize`` in spack-common.sh.
        """
        source = self._source_spack()
        spack_env_name = self.env.spack.env_name
        script = f"""{source}
# Create environment from spack.yaml
work_env="/tmp/spack-env-$(date +%s)"
mkdir -p "${{work_env}}"
cp "{env_dir_container}/spack.yaml" "${{work_env}}/spack.yaml"


spack env create "{spack_env_name}" "${{work_env}}/spack.yaml"

echo "Concretizing (spack -e {spack_env_name} concretize -f)..."
spack -e "{spack_env_name}" concretize -f

lock_src="{self.spack_root}/var/spack/environments/{spack_env_name}/spack.lock"
lock_dst="{env_dir_container}/spack.lock"

if [[ -f "${{lock_src}}" ]]; then
    if [[ -f "${{lock_dst}}" ]]; then
        if diff -q "${{lock_dst}}" "${{lock_src}}" >/dev/null 2>&1; then
            echo "spack.lock unchanged"
        else
            echo "spack.lock has changed — updating"
            cp -f "${{lock_src}}" "${{lock_dst}}"
        fi
    else
        cp -f "${{lock_src}}" "${{lock_dst}}"
    fi
    echo "spack.lock written to ${{lock_dst}}"
else
    echo "ERROR: Concretize did not produce spack.lock" >&2
    exit 1
fi
"""
        self.ctr.exec(script)
        logger.info("Concretize complete")

    # ── Mirror create ─────────────────────────────────────────────────────

    def mirror_create(
        self,
        env_dir_container: str,
        mirror_dir_container: str,
    ) -> dict[str, int]:
        """Create Spack mirror. Returns stats dict with present/added/failed counts."""
        source = self._source_spack()
        script = f"""{source}
work_env="/tmp/spack-mirror-$(date +%s)"
mkdir -p "${{work_env}}"
cp "{env_dir_container}/spack.yaml" "${{work_env}}/spack.yaml"

if [[ -f "{env_dir_container}/spack.lock" ]]; then
    cp "{env_dir_container}/spack.lock" "${{work_env}}/spack.lock"
else
    echo "ERROR: spack.lock not found — run concretize first" >&2
    exit 1
fi

cd "${{work_env}}"
spack env activate . 2>/dev/null || true

mkdir -p "{mirror_dir_container}"
echo "Running: spack mirror create -d {mirror_dir_container} --all -D --private"
spack -e . mirror create -d "{mirror_dir_container}" --all -D --private 2>&1 | tee /tmp/mirror-output.log
"""
        self.ctr.exec(script)

        # Parse stats from the output log
        stats = self._parse_mirror_stats()
        logger.info(
            "Mirror creation complete — present: %d, added: %d, failed: %d",
            stats["present"], stats["added"], stats["failed"],
        )

        if stats["failed"] < 0:
            raise RuntimeError(
                "Could not determine mirror status — stats log unreadable or "
                "unparseable. Treat the mirror as untrusted (plan A3)."
            )
        if stats["failed"] > 0:
            raise RuntimeError(f"{stats['failed']} package(s) failed to fetch!")

        return stats

    # ── Mirror verify ─────────────────────────────────────────────────────

    def mirror_verify(
        self,
        env_dir_container: str,
        mirror_dir_container: str,
    ) -> dict[str, int]:
        """Verify mirror completeness by re-running mirror create."""
        source = self._source_spack()
        script = f"""{source}
work_env="/tmp/spack-verify-$(date +%s)"
mkdir -p "${{work_env}}"
cp "{env_dir_container}/spack.yaml" "${{work_env}}/spack.yaml"

if [[ -f "{env_dir_container}/spack.lock" ]]; then
    cp "{env_dir_container}/spack.lock" "${{work_env}}/spack.lock"
else
    echo "ERROR: spack.lock not found" >&2
    exit 1
fi

cd "${{work_env}}"
spack env activate . 2>/dev/null || true

echo "Re-running: spack mirror create -d {mirror_dir_container} --all -D --private"
spack -e . mirror create -d "{mirror_dir_container}" --all -D --private 2>&1 | tee /tmp/verify-output.log
"""
        self.ctr.exec(script)

        stats = self._parse_mirror_stats()
        logger.info(
            "Verification — present: %d, added: %d, failed: %d",
            stats["present"], stats["added"], stats["failed"],
        )

        if stats["failed"] < 0:
            raise RuntimeError(
                "Could not determine mirror status — stats log unreadable or "
                "unparseable. Treat the mirror as untrusted (plan A3)."
            )
        if stats["failed"] > 0:
            raise RuntimeError(f"{stats['failed']} package(s) still missing!")

        return stats

    # ── Pipeline dispatch ─────────────────────────────────────────────────

    def run_concretize_pipeline(self, env_dir_host: Path, env_dir_container: str) -> None:
        """Full concretize pipeline: system pkgs → bootstrap → repos → find → concretize."""
        logger.info("MODE: concretize — Env: %s", self.env.spack.env_name)

        self.install_system_pkgs()
        self.clean_stale_state()
        self.register_repos(env_dir_container)
        self.compiler_find()
        self.concretize(env_dir_host, env_dir_container)

    def run_mirror_pipeline(self, env_dir_container: str, mirror_dir_container: str) -> None:
        """Full mirror pipeline: bootstrap → repos → mirror_create."""
        logger.info("MODE: mirror — Env: %s", self.env.spack.env_name)

        self.clean_stale_state()
        self.register_repos(env_dir_container)
        self.mirror_create(env_dir_container, mirror_dir_container)

    def run_all_pipeline(
        self,
        env_dir_host: Path,
        env_dir_container: str,
        mirror_dir_container: str,
    ) -> None:
        """Full pipeline: system pkgs → bootstrap → repos → find → concretize → mirror."""
        logger.info("MODE: all (concretize + mirror) — Env: %s", self.env.spack.env_name)

        self.install_system_pkgs()
        self.clean_stale_state()
        self.register_repos(env_dir_container)
        self.compiler_find()
        self.concretize(env_dir_host, env_dir_container)
        self.mirror_create(env_dir_container, mirror_dir_container)

    def run_verify_pipeline(self, env_dir_container: str, mirror_dir_container: str) -> None:
        """Verify pipeline: bootstrap → repos → mirror_verify."""
        logger.info("MODE: verify — Env: %s", self.env.spack.env_name)

        self.clean_stale_state()
        self.register_repos(env_dir_container)
        self.mirror_verify(env_dir_container, mirror_dir_container)

    # ── Internal helpers ──────────────────────────────────────────────────

    def _parse_mirror_stats(self) -> dict[str, int]:
        """Read the mirror/verify log from the container and parse stats.

        Delegates parsing to the pure :func:`_parse_mirror_stats_from_text`.
        If the container read itself fails, returns ``failed=MIRROR_STATS_UNKNOWN``
        rather than masking the error as ``failed=0`` (plan A3).
        """
        try:
            result = self.ctr.exec(
                "cat /tmp/mirror-output.log /tmp/verify-output.log 2>/dev/null || true",
                capture=True,
            )
            text = result.stdout or ""
        except Exception as exc:
            logger.error("Failed to read mirror stats log from container: %s", exc)
            return {
                "present": MIRROR_STATS_UNKNOWN,
                "added": MIRROR_STATS_UNKNOWN,
                "failed": MIRROR_STATS_UNKNOWN,
            }
        return _parse_mirror_stats_from_text(text)
