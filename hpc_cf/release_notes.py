"""GitHub Release identity, metadata bundling, notes generation, and asset pruning."""

from __future__ import annotations

import argparse
import json
import re
import sys
import tarfile
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

_ASSET_DATE_RE = re.compile(
    r"(?:"
    r"-(?P<date1>20[0-9]{6})(?:\.sif|-meta\.tar\.gz)$|"
    r"^(?:BUILD_MANIFEST|DependencyGraph)-.*-(?P<date2>20[0-9]{6})\.(?:txt|dot)$"
    r")"
)
_SPEC_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")


@dataclass(frozen=True)
class ReleaseIdentity:
    """Short, deterministic release tags, titles, and asset names for a build."""

    release_tag: str
    release_title: str
    dated_sif: str
    floating_sif: str
    meta_bundle: str
    create_flags: str
    stem: str

    def to_github_env_lines(self) -> list[str]:
        return [
            f"RELEASE_TAG={self.release_tag}",
            f"RELEASE_TITLE={self.release_title}",
            f"SIF_NAME={self.dated_sif}",
            f"FLOATING_SIF_NAME={self.floating_sif}",
            f"META_BUNDLE_NAME={self.meta_bundle}",
            f"RELEASE_STEM={self.stem}",
            f"RELEASE_CREATE_FLAGS={self.create_flags}",
        ]


def _format_iso_date(date_yyyymmdd: str) -> str:
    if len(date_yyyymmdd) == 8 and date_yyyymmdd.isdigit():
        return f"{date_yyyymmdd[:4]}-{date_yyyymmdd[4:6]}-{date_yyyymmdd[6:8]}"
    return date_yyyymmdd


def _derive_short_base(env_name: str) -> tuple[str, str]:
    """Return ``(app, track)`` stripping opensource/force-avx512 boilerplate."""
    app = env_name.split("_", 1)[0].split("-", 1)[0]
    remainder = env_name[len(app) :].lstrip("_-")
    if remainder.startswith("opensource-"):
        remainder = remainder[len("opensource-") :]
    remainder = re.sub(r"-force-avx\d+$", "", remainder)
    track = remainder or "latest"
    return app, track


def resolve_release_identity(
    *,
    env_name: str,
    channel: str,
    date_yyyymmdd: str,
    flagship_env: str,
    ident: str = "",
) -> ReleaseIdentity:
    """Resolve short GitHub Release tags, titles, and asset names."""
    iso_date = _format_iso_date(date_yyyymmdd)
    ident_suffix = f" ({ident})" if ident else ""

    if "-fork-" in env_name:
        base_part, fork_slug = env_name.split("-fork-", 1)
        app, _track = _derive_short_base(base_part)
        stem = f"{app}-fork-{fork_slug}"
        return ReleaseIdentity(
            release_tag=stem,
            release_title=f"{app.upper()} Fork ({fork_slug}) · {iso_date}{ident_suffix}",
            dated_sif=f"{stem}-{date_yyyymmdd}.sif",
            floating_sif=f"{stem}.sif",
            meta_bundle=f"{stem}-{date_yyyymmdd}-meta.tar.gz",
            create_flags="--prerelease",
            stem=stem,
        )

    app, track = _derive_short_base(env_name)
    stem = f"{app}-{track}"
    is_flagship = env_name == flagship_env

    if channel == "stable":
        return ReleaseIdentity(
            release_tag=f"{stem}-{date_yyyymmdd}",
            release_title=f"{app.upper()} {track} · {iso_date}{ident_suffix}",
            dated_sif=f"{stem}-{date_yyyymmdd}.sif",
            floating_sif="",
            meta_bundle=f"{stem}-{date_yyyymmdd}-meta.tar.gz",
            create_flags="--latest" if is_flagship else "--latest=false",
            stem=stem,
        )

    release_tag = f"{app}-nightly" if is_flagship else f"{stem}-nightly"
    return ReleaseIdentity(
        release_tag=release_tag,
        release_title=f"{app.upper()} Nightly · {iso_date}{ident_suffix}",
        dated_sif=f"{stem}-{date_yyyymmdd}.sif",
        floating_sif=f"{stem}.sif",
        meta_bundle=f"{stem}-{date_yyyymmdd}-meta.tar.gz",
        create_flags="--prerelease",
        stem=stem,
    )


def _index_concrete_specs(lock_path: Path | None) -> dict[str, dict[str, Any]]:
    if lock_path is None or not lock_path.is_file():
        return {}
    payload = json.loads(lock_path.read_text(encoding="utf-8"))
    concrete = payload.get("concrete_specs")
    if not isinstance(concrete, dict):
        return {}
    by_name: dict[str, dict[str, Any]] = {}
    for node in concrete.values():
        if isinstance(node, dict) and node.get("name"):
            by_name.setdefault(str(node["name"]), node)
    return by_name


def _format_os_label(platform_os: str) -> str:
    if platform_os.startswith("fedora") and platform_os[6:].isdigit():
        return f"Fedora {platform_os[6:]}"
    return platform_os


def _format_target_label(target: Any) -> str:
    if isinstance(target, dict):
        target = target.get("name", "")
    text = str(target or "").strip()
    if text == "x86_64_v3":
        return "x86-64-v3 (AVX2) + AVX-512 runtime dispatch"
    return text or "x86-64-v3"


def _spec_ver(specs: dict[str, dict[str, Any]], pkg: str, label: str) -> str | None:
    node = specs.get(pkg)
    if not node:
        return None
    version = str(node.get("version") or "").strip()
    return f"{label} {version}".strip() if version else label


def _repo_commit_url(repo_url: str, commit: str) -> str | None:
    if not _SPEC_COMMIT_RE.match(commit):
        return None
    cleaned = repo_url.strip()
    if cleaned.endswith(".git"):
        cleaned = cleaned[:-4]
    if "github.com/" in cleaned:
        slug = cleaned.split("github.com/", 1)[1].strip("/")
        return f"https://github.com/{slug}/commit/{commit}"
    return None


def _human_mib(size_bytes: int | None) -> str:
    if size_bytes is None or size_bytes <= 0:
        return "unknown"
    return f"{round(size_bytes / (1024 * 1024))} MiB"


def render_release_notes(
    *,
    env_name: str,
    channel: str,
    date_yyyymmdd: str,
    flagship_env: str,
    repository: str,
    lock_path: Path | None = None,
    env_yaml_path: Path | None = None,
    sif_path: Path | None = None,
    sif_size_bytes: int | None = None,
    oci_dated: str = "",
    oci_floating: str = "",
    fork_url: str = "",
    fork_branch: str = "",
    ident: str = "",
) -> str:
    """Render concise, tabular GitHub Release notes from ``spack.lock`` & build inputs."""
    identity = resolve_release_identity(
        env_name=env_name,
        channel=channel,
        date_yyyymmdd=date_yyyymmdd,
        flagship_env=flagship_env,
        ident=ident,
    )
    specs = _index_concrete_specs(lock_path)

    spack_ver = ""
    if env_yaml_path is not None and env_yaml_path.is_file():
        env_doc = yaml.safe_load(env_yaml_path.read_text(encoding="utf-8")) or {}
        spack_ver = str((env_doc.get("spack") or {}).get("version") or "")

    cp2k_node = specs.get("cp2k", {})
    cp2k_commit = str(((cp2k_node.get("parameters") or {}).get("commit")) or "")
    cp2k_arch = cp2k_node.get("arch") if isinstance(cp2k_node.get("arch"), dict) else {}
    os_label = _format_os_label(str(cp2k_arch.get("platform_os") or "fedora45"))
    target_label = _format_target_label(cp2k_arch.get("target"))

    gcc_ver = str((specs.get("gcc") or {}).get("version") or "")
    toolchain_parts = [os_label]
    if gcc_ver:
        toolchain_parts.append(f"GCC {gcc_ver}")
    if spack_ver:
        toolchain_parts.append(f"Spack {spack_ver}")
    base_summary = " · ".join(toolchain_parts)

    mpi_math = [
        item
        for item in (
            _spec_ver(specs, "openmpi", "OpenMPI"),
            _spec_ver(specs, "openblas", "OpenBLAS"),
            _spec_ver(specs, "fftw", "FFTW"),
            _spec_ver(specs, "elpa", "ELPA"),
            _spec_ver(specs, "cosma", "COSMA"),
            _spec_ver(specs, "libint", "Libint"),
            _spec_ver(specs, "libxc", "LibXC"),
        )
        if item
    ]
    plugins = [
        item
        for item in (
            _spec_ver(specs, "sirius", "SIRIUS"),
            _spec_ver(specs, "gauxc", "GauXC (Skala)"),
            _spec_ver(specs, "deepmdkit", "DeepMD-kit"),
            _spec_ver(specs, "py-torch", "PyTorch"),
            _spec_ver(specs, "plumed", "PLUMED"),
            _spec_ver(specs, "dftd4", "DFTD4"),
        )
        if item
    ]

    if sif_size_bytes is None and sif_path is not None and sif_path.is_file():
        sif_size_bytes = sif_path.stat().st_size
    size_label = _human_mib(sif_size_bytes)

    repo_url = fork_url or "https://github.com/cp2k/cp2k"
    commit_url = _repo_commit_url(repo_url, cp2k_commit)
    branch_label = fork_branch or ("master" if "master" in identity.stem else identity.stem)
    if commit_url:
        source_line = f"[`{branch_label}@{cp2k_commit[:12]}`]({commit_url})"
    elif ident:
        source_line = f"`{ident}`"
    else:
        source_line = f"`{env_name}`"

    download_sif = identity.floating_sif or identity.dated_sif
    download_url = (
        f"https://github.com/{repository}/releases/download/"
        f"{identity.release_tag}/{download_sif}"
    )

    lines: list[str] = [
        f"Automated CP2K container build ({source_line} · **{size_label}** `zstd -22` SIF · {target_label}).",
        "",
        "| Component | Version / Pin |",
        "|---|---|",
        f"| **Base & Toolchain** | {base_summary} |",
    ]
    if mpi_math:
        lines.append(f"| **MPI & Math** | {' · '.join(mpi_math)} |")
    if plugins:
        lines.append(f"| **Plugins & ML** | {' · '.join(plugins)} |")
    lines.append(
        "| **Verification** | `cp2k.psmp --version` + 2-rank MPI H2O PBE (`SCF converged`) |"
    )
    if fork_url:
        lines.append(f"| **Fork Source** | `{fork_url}` (`{fork_branch or 'default'}`) |")

    lines.extend(
        [
            "",
            "### Quick Start",
            "```bash",
            f"curl -fLO {download_url}",
            f"apptainer exec {download_sif} cp2k.psmp --version",
            f"apptainer exec {download_sif} mpirun -np 4 cp2k.psmp -i input.inp",
            "```",
        ]
    )

    if oci_dated or oci_floating:
        lines.extend(["", "### OCI Images (GHCR)", "```text"])
        if oci_floating:
            lines.append(oci_floating)
        if oci_dated:
            lines.append(oci_dated)
        lines.append("```")

    return "\n".join(lines) + "\n"


def create_meta_bundle(
    output_path: Path,
    items: Sequence[tuple[str, Path]],
) -> Path:
    """Create a compressed ``-meta.tar.gz`` containing existing build metadata files."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(output_path, "w:gz") as tf:
        for arcname, src in items:
            if src.is_file():
                tf.add(src, arcname=arcname)
    return output_path


def _extract_asset_date(name: str) -> str | None:
    match = _ASSET_DATE_RE.search(name)
    if match is None:
        return None
    return match.group("date1") or match.group("date2")


def plan_dated_asset_deletions(
    assets: Iterable[dict[str, Any]],
    *,
    keep_dates: int,
) -> list[int]:
    """Return asset IDs to delete while keeping floating assets and *keep_dates* newest dates."""
    asset_list = list(assets)
    dates = sorted(
        {
            d
            for item in asset_list
            if (d := _extract_asset_date(str(item.get("name") or ""))) is not None
        },
        reverse=True,
    )
    kept_dates = set(dates[: max(0, keep_dates)])
    to_delete: list[int] = []
    for item in asset_list:
        name = str(item.get("name") or "")
        date_token = _extract_asset_date(name)
        if date_token is None:
            continue
        if date_token not in kept_dates:
            to_delete.append(int(item["id"]))
    return to_delete


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Release helpers for HPC Container Factory")
    sub = parser.add_subparsers(dest="cmd", required=True)

    ident_p = sub.add_parser("identity")
    ident_p.add_argument("--env-name", required=True)
    ident_p.add_argument("--channel", required=True, choices=["nightly", "stable"])
    ident_p.add_argument("--date", required=True)
    ident_p.add_argument("--flagship-env", required=True)
    ident_p.add_argument("--ident", default="")

    bundle_p = sub.add_parser("bundle")
    bundle_p.add_argument("--output", type=Path, required=True)
    bundle_p.add_argument("--item", action="append", default=[], help="arcname=path")

    notes_p = sub.add_parser("notes")
    notes_p.add_argument("--env-name", required=True)
    notes_p.add_argument("--channel", required=True, choices=["nightly", "stable"])
    notes_p.add_argument("--date", required=True)
    notes_p.add_argument("--flagship-env", required=True)
    notes_p.add_argument("--repository", required=True)
    notes_p.add_argument("--lock", type=Path, default=None)
    notes_p.add_argument("--env-yaml", type=Path, default=None)
    notes_p.add_argument("--sif", type=Path, default=None)
    notes_p.add_argument("--oci-dated", default="")
    notes_p.add_argument("--oci-floating", default="")
    notes_p.add_argument("--fork-url", default="")
    notes_p.add_argument("--fork-branch", default="")
    notes_p.add_argument("--ident", default="")
    notes_p.add_argument("--output", type=Path, default=None)

    prune_p = sub.add_parser("prune-plan")
    prune_p.add_argument("--keep-dates", type=int, required=True)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.cmd == "identity":
        identity = resolve_release_identity(
            env_name=args.env_name,
            channel=args.channel,
            date_yyyymmdd=args.date,
            flagship_env=args.flagship_env,
            ident=args.ident,
        )
        print("\n".join(identity.to_github_env_lines()))
        return 0

    if args.cmd == "bundle":
        pairs: list[tuple[str, Path]] = []
        for raw in args.item:
            if "=" not in raw:
                raise SystemExit(f"Expected arcname=path, got {raw!r}")
            arcname, src = raw.split("=", 1)
            pairs.append((arcname, Path(src)))
        create_meta_bundle(args.output, pairs)
        return 0

    if args.cmd == "notes":
        rendered = render_release_notes(
            env_name=args.env_name,
            channel=args.channel,
            date_yyyymmdd=args.date,
            flagship_env=args.flagship_env,
            repository=args.repository,
            lock_path=args.lock,
            env_yaml_path=args.env_yaml,
            sif_path=args.sif,
            oci_dated=args.oci_dated,
            oci_floating=args.oci_floating,
            fork_url=args.fork_url,
            fork_branch=args.fork_branch,
            ident=args.ident,
        )
        if args.output is not None:
            args.output.write_text(rendered, encoding="utf-8")
        else:
            sys.stdout.write(rendered)
        return 0

    if args.cmd == "prune-plan":
        payload = json.load(sys.stdin)
        assets = payload.get("assets", []) if isinstance(payload, dict) else payload
        for asset_id in plan_dated_asset_deletions(assets, keep_dates=args.keep_dates):
            print(asset_id)
        return 0

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
