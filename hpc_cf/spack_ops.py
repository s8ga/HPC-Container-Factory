"""Spack operations — bootstrap, concretize, mirror, verify.

All operations are executed via a :class:`~hpc_cf.execution.RunnerPort`
(typically Podman :class:`~hpc_cf.container.Container`).
Replaces the Spack-specific portions of:
  - ``scripts/spack-common.sh`` (streamline_parse_env, step_*, spack_bootstrap,
    mirror_create, mirror_verify, streamline_dispatch)
  - ``scripts/prepare-bootstrap-cache.sh`` (bootstrap generation logic)
  - ``scripts/build-mirror-in-container.sh`` (cmd_bootstrap, cmd_concretize,
    cmd_mirror, cmd_verify, cmd_all)
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Literal

from hpc_cf.environment import (
    CustomRepo as CustomRepo,
    EnvironmentSpec,
    MirrorBuilderConfig as MirrorBuilderConfig,
    RepoScope,
    SpackConfig as SpackConfig,
    load_environment_spec,
)
from hpc_cf.execution import ProjectLayout, RunnerPort
from hpc_cf.shell_quote import shell_quote
from hpc_cf.spack_plan import (
    SpackEnvironmentPlan,
    build_spack_environment_plan,
)

logger = logging.getLogger(__name__)


# ── Compatibility aliases (prefer EnvironmentSpec / load_environment_spec) ─


# Historical name retained for tests and callers; same object as EnvironmentSpec.
EnvConfig = EnvironmentSpec


def load_env_config(env_dir: Path) -> EnvConfig:
    """Deprecated wrapper around :func:`load_environment_spec`.

    Prefer ``load_environment_spec`` — this alias will be removed in a later
    phase.
    """
    logger.warning(
        "load_env_config is deprecated; use hpc_cf.environment.load_environment_spec"
    )
    return load_environment_spec(env_dir)


def resolve_env_paths(
    env_name: str,
    *,
    layout: ProjectLayout | None = None,
) -> tuple[Path, Path]:
    """Return (host_env_dir, container_env_dir) for the given env name.

    Handles the ``spack-env-file/`` subdirectory layout. Paths are resolved
    under *layout* (or :meth:`ProjectLayout.default` when omitted).
    """
    return (layout or ProjectLayout.default()).resolve_env_paths(env_name)


# ── Mirror stats parsing (pure, unit-testable) ──────────────────────────


# Sentinel: callers treat failed < 0 as "status could not be determined".
# This is the fix for the silent-success bug: previously any parse
# exception returned failed=0, and callers only raised on failed>0, so a
# broken/incomplete mirror was reported as success.
MIRROR_STATS_UNKNOWN = -1

# Per-operation log paths inside the container. Create and verify must NOT
# share a parse of both files — leftover lines from a prior run would win
# "last match" in ``_parse_mirror_stats_from_text``.
MIRROR_CREATE_LOG = "/tmp/mirror-output.log"
MIRROR_VERIFY_LOG = "/tmp/verify-output.log"

# Bootstrap binaries that a complete bootstrap mirror must provide.
# Single source of truth — previously the ("clingo","gnupg","patchelf") tuple
# was duplicated in spack_ops (_bootstrap_metadata_complete, _verify_bootstrap)
# and assets._verify_host_side.
EXPECTED_BOOTSTRAP_BINARIES = ("clingo", "gnupg", "patchelf")


def _parse_mirror_stats_from_text(text: str) -> dict[str, int]:
    """Parse spack mirror-create/verify stdout into {present, added, failed}.

    NOTE: ``spack mirror create`` has NO ``--json`` flag (verified in spack
    1.1.1 — only find/spec/config/diff/blame support JSON). Regex parsing of
    the human-readable "Archive stats" summary is the only option. The format
    is: ``N already present / N added / N failed to fetch.``

    Pure function (no I/O) so it can be unit-tested directly. Returns the
    LAST match of each counter (spack prints progress lines; the final
    summary is what matters). Missing counters become
    ``MIRROR_STATS_UNKNOWN`` (-1) so callers can distinguish "0" from
    "couldn't tell" — never reports 0 on garbage. A partial summary
    (e.g. only ``0 failed``) must be rejected by callers via
    :meth:`SpackOps._require_complete_mirror_stats`.
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
        Object implementing :class:`~hpc_cf.execution.RunnerPort`
        (typically :class:`~hpc_cf.container.Container`).
    """

    def __init__(
        self,
        env: EnvConfig,
        container: RunnerPort,
        *,
        layout: ProjectLayout | None = None,
    ) -> None:
        self.env = env
        self.ctr = container
        # Lazily resolved when None so tests can monkeypatch config.PROJECT_ROOT
        # before calling bootstrap_mirror without reconstructing SpackOps.
        self._layout = layout
        self.spack_ver = env.spack.version
        self.spack_root = f"/opt/spack-{self.spack_ver}"
        self.user_dir = env.spack_user_dir_in_container
        self.user_cache = f"{self.user_dir}/cache"
        self.plan: SpackEnvironmentPlan = build_spack_environment_plan(env)

    @property
    def layout(self) -> ProjectLayout:
        return self._layout or ProjectLayout.default()

    def _assets_repos(self) -> list[CustomRepo]:
        """CustomRepos from env.yaml that apply to the assets workflow."""
        return [
            r for r in self.env.spack.custom_repos if r.phases.applies_to("assets")
        ]

    def _setup_env_vars(self) -> str:
        """Common environment setup for all Spack commands.

        Uses ``set -e`` + ``pipefail`` so mid-script failures and ``cmd | tee``
        pipelines exit non-zero. Deliberately omits ``set -u``: Spack's
        ``setup-env.sh`` may reference unset variables under nounset.
        """
        return f"""
set -e
set -o pipefail
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
# Configure local bootstrap mirror as highest-priority trusted source.
# Idempotent ``bootstrap add`` may fail when already registered — tolerate that.
# ``bootstrap now`` failures must propagate (set -e); never swallow with || true.
if [[ -d "{bootstrap_dir}/metadata/sources" ]]; then
    spack bootstrap add --trust local-sources "{bootstrap_dir}/metadata/sources" 2>/dev/null || true
    spack bootstrap add --trust local-binaries "{bootstrap_dir}/metadata/binaries" 2>/dev/null || true
    spack bootstrap now
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

        Returns the local bootstrap directory path under the injected layout.
        """
        bootstrap_dir_name = self.env.bootstrap_dir_name
        local_dir = self.layout.assets_dir / bootstrap_dir_name
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
        logger.info("Generating bootstrap mirror (binary-only) for Spack %s", self.spack_ver)
        self.ctr.run_ephemeral(
            self._build_bootstrap_mirror_script(container_dir, binary_packages=True)
        )

        self._verify_bootstrap(local_dir)
        return local_dir

    def _build_bootstrap_mirror_script(self, container_dir: str, *, binary_packages: bool) -> str:
        flag = " --binary-packages" if binary_packages else ""
        return f"""{self._source_spack()}
rm -f "${{SPACK_USER_CONFIG_PATH}}/repos.yaml"
rm -rf /tmp/spack-repos /tmp/spack-env-*
mkdir -p "{container_dir}"
spack bootstrap mirror{flag} "{container_dir}"
"""

    def _bootstrap_metadata_complete(self, bootstrap_dir: Path) -> bool:
        """Check whether all expected bootstrap metadata files are present."""
        metadata = bootstrap_dir / "metadata" / "sources" / "metadata.yaml"
        if not metadata.exists() or metadata.stat().st_size == 0:
            return False
        for name in EXPECTED_BOOTSTRAP_BINARIES:
            f = bootstrap_dir / "metadata" / "binaries" / f"{name}.json"
            if not f.exists() or f.stat().st_size == 0:
                return False
        return True

    def _verify_bootstrap(self, bootstrap_dir: Path) -> None:
        """Validate generated bootstrap metadata."""
        metadata = bootstrap_dir / "metadata" / "sources" / "metadata.yaml"
        if not metadata.exists() or metadata.stat().st_size == 0:
            raise RuntimeError(f"Bootstrap metadata missing or empty: {metadata}")

        for name in EXPECTED_BOOTSTRAP_BINARIES:
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
            self.ctr.exec(f"sudo bash -c {shell_quote(mb.pkg_mirror_setup)}")

        if mb.system_pkgs and mb.pkg_install_cmd:
            logger.info("Installing system packages...")
            pkgs = " ".join(shell_quote(p) for p in mb.system_pkgs)
            self.ctr.exec(
                f"sudo bash -c {shell_quote(mb.pkg_install_cmd + ' ' + pkgs)}"
            )
            logger.info("System packages installed")
        else:
            logger.info("No system packages declared — skipping")

    # ── Repo management ───────────────────────────────────────────────────

    def clean_stale_state(self) -> None:
        """Remove stale Spack state from previous runs in persistent container.

        Cleans:
          1. repos.yaml — stale user-scope registrations (custom repos now use env scope)
          2. packages.yaml — external package detections (re-added by compiler_find)
          3. Spack environments under var/spack/environments/ — avoids ``env create`` conflicts
          4. /tmp work dirs from previous runs

        This MUST run before any ``spack env create`` / ``spack repo add`` calls
        to guarantee a clean slate on persistent worker containers.
        """
        self.ctr.exec(self._build_clean_stale_state_script())

    def _build_clean_stale_state_script(self) -> str:
        return f"""{self._source_spack()}
# 1. Nuke stale user-scope repo/external-package registrations
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

    def prepare_repos(self, env_dir_in_container: str) -> None:
        """Fetch git repos and validate local repos before environment creation."""
        repos = self._assets_repos()
        if not repos:
            logger.info("No custom repos configured for assets — skipping")
            return

        self.ctr.exec(self._build_prepare_repos_script(env_dir_in_container))

    def _build_prepare_repos_script(self, env_dir_in_container: str) -> str:
        repos = self._assets_repos()
        parts = [self._source_spack()]
        for repo in repos:
            if repo.type == "git":
                parts.append(self._prepare_git_repo(repo))
            elif repo.type == "local":
                parts.append(self._prepare_local_repo(repo, env_dir_in_container))
        return "\n".join(parts)

    def _custom_repo_path(self, repo: CustomRepo, env_dir: str) -> str:
        if repo.type == "git":
            return f"/tmp/spack-repos/spack_repo/{repo.namespace}"
        if repo.type == "local" and repo.path:
            return f"{env_dir}/{repo.path}"
        raise ValueError(f"custom repo {repo.namespace!r} has no usable path")

    def _prepare_git_repo(self, repo: CustomRepo) -> str:
        if not repo.url:
            raise ValueError("custom_repos git repo missing url")
        url = shell_quote(repo.url)
        branch = shell_quote(repo.branch or "main")
        sparse = shell_quote(repo.sparse_path) if repo.sparse_path else ""
        clone_dir = self._custom_repo_path(repo, "")
        clone_dir_q = shell_quote(clone_dir)
        clone_tmp = f"/tmp/repo-clone-{repo.namespace}"
        clone_tmp_q = shell_quote(clone_tmp)

        if repo.sparse_path:
            materialize = f"""
            git sparse-checkout set {sparse}
            cp -a {sparse}/. {clone_dir_q}/"""
        else:
            materialize = f"cp -a ./. {clone_dir_q}/"

        if repo.commit:
            commit = shell_quote(repo.commit)
            if repo.sparse_path:
                # Sparse cone must be configured before checkout so only the
                # requested tree is materialized at the pinned commit.
                commit_materialize = f"""
    git sparse-checkout set {sparse}
    git checkout {commit}
    mkdir -p {clone_dir_q}
    cp -a {sparse}/. {clone_dir_q}/"""
            else:
                commit_materialize = f"""
    git checkout {commit}
    mkdir -p {clone_dir_q}
    cp -a ./. {clone_dir_q}/"""
            fetch_block = f"""
    git clone --filter=blob:none --sparse --no-checkout \
        {url} {clone_tmp_q}
    cd {clone_tmp_q}
    git fetch --depth 1 origin {commit}
    {commit_materialize}"""
            pin_note = f" @ {repo.commit}"
        else:
            fetch_block = f"""
    git clone --depth 1 --filter=blob:none --sparse \
        -b {branch} {url} {clone_tmp_q}
    cd {clone_tmp_q}
    mkdir -p {clone_dir_q}
    {materialize}"""
            pin_note = ""

        return f"""
# Prepare git repo: {repo.namespace}{pin_note}
if [[ ! -f {clone_dir_q}/repo.yaml ]]; then
    rm -rf {clone_dir_q} {clone_tmp_q}
    mkdir -p /tmp/spack-repos/spack_repo
    {fetch_block}
    cd /tmp
    rm -rf {clone_tmp_q}
fi
if [[ ! -f {clone_dir_q}/repo.yaml ]]; then
    echo "ERROR: prepared repo {repo.namespace} has no repo.yaml" >&2
    exit 1
fi
echo "Prepared {repo.namespace} (git)"
"""

    def _prepare_local_repo(self, repo: CustomRepo, env_dir: str) -> str:
        if not repo.path:
            raise ValueError("custom_repos local repo missing path")
        repo_path = self._custom_repo_path(repo, env_dir)
        repo_path_q = shell_quote(repo_path)
        return f"""
# Validate local repo: {repo.namespace}
if [[ ! -f {repo_path_q}/repo.yaml ]]; then
    echo "ERROR: local repo {repo.namespace} has no repo.yaml: {repo_path_q}" >&2
    exit 1
fi
echo "Prepared {repo.namespace} (local)"
"""

    # ── Environment preparation ───────────────────────────────────────────

    def prepare_environment(
        self,
        env_dir_container: str,
        *,
        import_lock: bool,
    ) -> None:
        """Create the named environment and register its custom repos."""
        self.ctr.exec(
            self._build_prepare_environment_script(
                env_dir_container,
                import_lock=import_lock,
            )
        )
        logger.info("Environment prepared: %s", self.env.spack.env_name)

    def _build_environment_repo_registration(
        self,
        env_dir_container: str,
    ) -> str:
        env_name = self.plan.env_name
        env_q = shell_quote(env_name)
        scope = self.plan.assets.scope_flag()
        scope_q = shell_quote(scope)
        parts: list[str] = []
        for repo in self._assets_repos():
            repo_path_q = shell_quote(
                self._custom_repo_path(repo, env_dir_container)
            )
            # ENV scope registration requires -e; SITE does not.
            if self.plan.assets.repo_scope is RepoScope.ENV:
                add_cmd = (
                    f"spack -e {env_q} repo add --scope {scope_q} {repo_path_q}"
                )
            else:
                add_cmd = f"spack repo add --scope {scope_q} {repo_path_q}"
            parts.append(
                f"# Register {repo.namespace} ({scope})\n"
                f"{add_cmd}\n"
                f'echo "Registered {repo.namespace} in {scope}"'
            )
        return "\n".join(parts)

    def _build_prepare_environment_script(
        self,
        env_dir_container: str,
        *,
        import_lock: bool,
    ) -> str:
        env_name = self.plan.env_name
        env_q = shell_quote(env_name)
        spack_yaml = shell_quote(f"{env_dir_container}/spack.yaml")
        lock_src = shell_quote(f"{env_dir_container}/spack.lock")
        env_root = shell_quote(
            f"{self.spack_root}/var/spack/environments/{env_name}"
        )

        lock_block = ""
        if import_lock:
            lock_block = f"""
if [[ ! -f {lock_src} ]]; then
    echo "ERROR: spack.lock not found — run concretize first" >&2
    exit 1
fi
cp {lock_src} {env_root}/spack.lock
"""

        register_block = self._build_environment_repo_registration(
            env_dir_container
        )
        update_block = ""
        if self.plan.assets.update_builtin:
            update_block = (
                "# Update the pinned builtin before adding higher-priority "
                "custom repos.\n"
                f"spack -e {env_q} repo update builtin\n"
            )
        return f"""{self._source_spack()}
# Create one named environment shared by concretize, mirror, and verify.
work_env="/tmp/spack-env-$(date +%s)"
mkdir -p "${{work_env}}"
cp {spack_yaml} "${{work_env}}/spack.yaml"
spack env create {env_q} "${{work_env}}/spack.yaml"
{lock_block}
{update_block}{register_block}
spack -e {env_q} repo list
"""

    # ── Compiler find ─────────────────────────────────────────────────────

    def compiler_find(self) -> None:
        """Find system compilers (no external find)."""
        self.ctr.exec(self._build_compiler_find_script())
        logger.info("Compilers registered")

    def _build_compiler_find_script(self) -> str:
        return f"""{self._source_spack()}
spack compiler find
"""

    # ── Concretize ────────────────────────────────────────────────────────

    def concretize(self, env_dir_host: Path, env_dir_container: str) -> None:
        """Run spack concretize and write spack.lock back to host.

        Replaces ``step_concretize`` in spack-common.sh.

        After the container script finishes, verifies that *env_dir_host*
        actually contains a non-empty ``spack.lock`` (bind-mount write-back
        must not be assumed solely from the container path).
        """
        self.ctr.exec(self._build_concretize_script(env_dir_container))
        lock_host = env_dir_host / "spack.lock"
        if not lock_host.is_file() or lock_host.stat().st_size == 0:
            raise RuntimeError(
                f"Concretize did not write a non-empty spack.lock to host: "
                f"{lock_host}"
            )
        logger.info("Concretize complete — host lock: %s", lock_host)

    def _build_concretize_script(self, env_dir_container: str) -> str:
        spack_env_name = self.env.spack.env_name
        lock_dst = shell_quote(f"{env_dir_container}/spack.lock")
        lock_src = shell_quote(
            f"{self.spack_root}/var/spack/environments/{spack_env_name}/spack.lock"
        )
        env_q = shell_quote(spack_env_name)
        return f"""{self._source_spack()}
echo "Concretizing (spack -e {spack_env_name} concretize -f)..."
spack -e {env_q} concretize -f

lock_src={lock_src}
lock_dst={lock_dst}

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

    # ── Mirror create ─────────────────────────────────────────────────────

    def mirror_create(
        self,
        mirror_dir_container: str,
        *,
        create_log: str | None = None,
    ) -> dict[str, int]:
        """Create Spack mirror. Returns stats dict with present/added/failed counts."""
        log_path = create_log or MIRROR_CREATE_LOG
        self.ctr.exec(
            self._build_mirror_create_script(
                mirror_dir_container, create_log=log_path,
            )
        )

        # Parse stats from this run's log only (never concatenate verify log).
        stats = self._parse_mirror_stats(log_path)
        logger.info(
            "Mirror creation complete — present: %d, added: %d, failed: %d",
            stats["present"], stats["added"], stats["failed"],
        )
        self._require_complete_mirror_stats(stats, action="fetch")
        return stats

    def _build_mirror_create_script(
        self,
        mirror_dir_container: str,
        *,
        create_log: str | None = None,
    ) -> str:
        env_q = shell_quote(self.env.spack.env_name)
        mirror_dir = shell_quote(mirror_dir_container)
        log_q = shell_quote(create_log or MIRROR_CREATE_LOG)
        return f"""{self._source_spack()}
mkdir -p {mirror_dir}
mkdir -p "$(dirname {log_q})"
: > {log_q}
echo "Running: spack mirror create -d {mirror_dir_container} --all -D --private"
spack -e {env_q} mirror create -d {mirror_dir} --all -D --private 2>&1 | tee {log_q}
"""

    def _build_mirror_verify_script(
        self,
        mirror_dir_container: str,
        *,
        verify_log: str | None = None,
    ) -> str:
        env_q = shell_quote(self.env.spack.env_name)
        mirror_dir = shell_quote(mirror_dir_container)
        log_q = shell_quote(verify_log or MIRROR_VERIFY_LOG)
        return f"""{self._source_spack()}
mkdir -p "$(dirname {log_q})"
: > {log_q}
echo "Re-running: spack mirror create -d {mirror_dir_container} --all -D --private"
spack -e {env_q} mirror create -d {mirror_dir} --all -D --private 2>&1 | tee {log_q}
"""

    # ── Mirror verify ─────────────────────────────────────────────────────

    def mirror_verify(
        self,
        mirror_dir_container: str,
        *,
        verify_log: str | None = None,
    ) -> dict[str, int]:
        """Verify mirror completeness by re-running mirror create."""
        log_path = verify_log or MIRROR_VERIFY_LOG
        self.ctr.exec(
            self._build_mirror_verify_script(
                mirror_dir_container, verify_log=log_path,
            )
        )

        stats = self._parse_mirror_stats(log_path)
        logger.info(
            "Verification — present: %d, added: %d, failed: %d",
            stats["present"], stats["added"], stats["failed"],
        )
        self._require_complete_mirror_stats(stats, action="verify")
        return stats

    # ── Pipeline dispatch ─────────────────────────────────────────────────

    def run_concretize_pipeline(self, env_dir_host: Path, env_dir_container: str) -> None:
        """Prepare a named environment, then concretize it."""
        logger.info("MODE: concretize — Env: %s", self.env.spack.env_name)

        self.install_system_pkgs()
        self.clean_stale_state()
        self.prepare_repos(env_dir_container)
        self.compiler_find()
        self.prepare_environment(env_dir_container, import_lock=False)
        self.concretize(env_dir_host, env_dir_container)

    def run_mirror_pipeline(
        self,
        env_dir_container: str,
        mirror_dir_container: str,
        *,
        create_log: str | None = None,
    ) -> dict[str, int]:
        """Prepare a named environment from an existing lock, then mirror it."""
        logger.info("MODE: mirror — Env: %s", self.env.spack.env_name)

        self.clean_stale_state()
        self.prepare_repos(env_dir_container)
        self.prepare_environment(env_dir_container, import_lock=True)
        return self.mirror_create(mirror_dir_container, create_log=create_log)

    def run_all_pipeline(
        self,
        env_dir_host: Path,
        env_dir_container: str,
        mirror_dir_container: str,
        *,
        create_log: str | None = None,
    ) -> dict[str, int]:
        """Prepare one named environment, concretize it, then create its mirror."""
        logger.info("MODE: all (concretize + mirror) — Env: %s", self.env.spack.env_name)

        self.install_system_pkgs()
        self.clean_stale_state()
        self.prepare_repos(env_dir_container)
        self.compiler_find()
        self.prepare_environment(env_dir_container, import_lock=False)
        self.concretize(env_dir_host, env_dir_container)
        return self.mirror_create(mirror_dir_container, create_log=create_log)

    def run_verify_pipeline(
        self,
        env_dir_container: str,
        mirror_dir_container: str,
        *,
        verify_log: str | None = None,
    ) -> dict[str, int]:
        """Prepare a named environment from an existing lock, then verify its mirror."""
        logger.info("MODE: verify — Env: %s", self.env.spack.env_name)

        self.clean_stale_state()
        self.prepare_repos(env_dir_container)
        self.prepare_environment(env_dir_container, import_lock=True)
        return self.mirror_verify(mirror_dir_container, verify_log=verify_log)

    # ── Internal helpers ──────────────────────────────────────────────────

    @staticmethod
    def _require_complete_mirror_stats(
        stats: dict[str, int],
        *,
        action: Literal["fetch", "verify"],
    ) -> None:
        """Raise unless present/added/failed are all known and failed == 0.

        A partial summary (e.g. only ``0 failed``) must not be treated as
        success — previously only ``failed`` was gated.
        """
        if any(stats[k] < 0 for k in ("present", "added", "failed")):
            raise RuntimeError(
                "Could not determine mirror status — stats log unreadable or "
                "unparseable. Treat the mirror as untrusted."
            )
        if stats["failed"] > 0:
            if action == "fetch":
                raise RuntimeError(
                    f"{stats['failed']} package(s) failed to fetch!"
                )
            raise RuntimeError(
                f"{stats['failed']} package(s) still missing!"
            )

    def _parse_mirror_stats(self, log_path: str) -> dict[str, int]:
        """Read one mirror ops log from the container and parse stats.

        *log_path* must be the log for this operation only
        (:data:`MIRROR_CREATE_LOG` or :data:`MIRROR_VERIFY_LOG`). Concatenating
        both files lets a previous run's "last match" pollute the result.

        Delegates parsing to the pure :func:`_parse_mirror_stats_from_text`.
        If the container read itself fails, returns ``failed=MIRROR_STATS_UNKNOWN``
        rather than masking the error as ``failed=0``.
        """
        log_q = shell_quote(log_path)
        try:
            result = self.ctr.exec(
                f"cat {log_q} 2>/dev/null || true",
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
