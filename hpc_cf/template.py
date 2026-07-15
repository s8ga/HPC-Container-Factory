"""Template discovery, rendering, and build-context assembly."""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path

try:
    from jinja2 import Environment, FileSystemLoader, TemplateNotFound, TemplateSyntaxError
except ImportError as exc:
    raise ImportError(f"Required package not installed: {exc}. Install: pip install jinja2") from exc

from hpc_cf.config import DEFAULT_SPACK_VERSION, TEMPLATES_DIR, SPACK_ENVS_DIR
from hpc_cf.env import list_available_envs, load_env_yaml

logger = logging.getLogger(__name__)


# ── Helpers ──────────────────────────────────────────────────────────────


def _parse_dir_to_image_tag(env_dir_name: str) -> tuple[str, str]:
    """Derive (image_name, tag) from a spack-envs directory name.

    Convention: ``<app>_<variant>-<version>[-<suffix>]`` — the first ``_``
    separates the application from the variant, and the first hyphen-delimited
    segment starting with a digit marks the version boundary.

    The image name preserves the underscore (e.g. ``cp2k_opensource``).

    Examples::

        cp2k_opensource-2025.2                → ("cp2k_opensource", "2025.2")
        cp2k_opensource-2025.2-force-avx512   → ("cp2k_opensource", "2025.2-force-avx512")
        cp2k_mkl-2025.2-experimental          → ("cp2k_mkl", "2025.2-experimental")
        cp2k_rocm-2026.1-gfx942               → ("cp2k_rocm", "2026.1-gfx942")
        abacus_cpu-2025.2                      → ("abacus_cpu", "2025.2")
    """
    name = env_dir_name.lower()

    # Split on first '_' to get app and variant+version
    underscore = name.find("_")
    if underscore == -1:
        # No underscore — treat whole name as app (legacy compat)
        return name, "latest"

    app = name[:underscore]               # e.g. "cp2k"
    remainder = name[underscore + 1:]     # e.g. "opensource-2025.2"

    parts = remainder.split("-")
    boundary = -1
    for i, part in enumerate(parts):
        if part and part[0].isdigit():
            boundary = i
            break

    if boundary < 0:
        # No version found — use remainder as image name
        return f"{app}_{remainder}", "latest"

    variant = "-".join(parts[:boundary])
    tag = "-".join(parts[boundary:])
    return f"{app}_{variant}", tag


def infer_image_defaults(app_version: str, template_path: Path | None) -> tuple[str, str]:
    """Infer default (image_name, tag) from the spack-envs directory name."""
    env_dir_name = template_path.parent.name if template_path else ""
    if not env_dir_name:
        return "hpc-cp2k", "latest"
    return _parse_dir_to_image_tag(env_dir_name)


def resolve_output_image_tag(template_path: Path | None) -> tuple[str, str]:
    """Resolve (image_name, tag) with env.yaml override taking priority."""
    env_config = load_env_yaml(template_path)
    env_images = env_config.get("images", {})

    override_name = env_images.get("output_name")
    override_tag = env_images.get("output_tag")
    if override_name:
        return override_name, override_tag or "latest"

    return infer_image_defaults("", template_path)


# ── Template discovery ──────────────────────────────────────────────────


def _extract_available_versions() -> list[str]:
    """List selectable ``--app-version`` / ``--env`` values.

    Uses :func:`list_available_envs` (env.yaml-backed dirs under spack-envs/)
    so CLI listing matches ``assets --env``. Differs from a raw Dockerfile.j2
    scan: includes no_spack envs that lack a per-env Dockerfile.j2, and
    omits dirs that have only a template with no env.yaml.

    Also appends legacy ``templates/Dockerfile-*.j2`` stems not already listed.
    """
    versions = list(list_available_envs())
    seen = set(versions)

    for f in sorted(TEMPLATES_DIR.glob("Dockerfile-*.j2")):
        if f.name == "Dockerfile-base.j2":
            continue
        stem = f.name[len("Dockerfile-"): -len(".j2")]
        if stem not in seen:
            versions.append(stem)
            seen.add(stem)

    return versions


def select_template(
    app_version: str,
    explicit_template: Path | None = None,
    *,
    app: str = "",
) -> Path:
    """Locate the Jinja2 Dockerfile template for the given env / version.

    *app_version* is normally the full ``spack-envs/<name>/`` directory name
    (e.g. ``cp2k_opensource-2026.1-force-avx512``). The optional *app* prefix
    is only used for legacy ``<app>_<version>`` / ``Dockerfile-<app>-...``
    fallbacks; callers should not hardcode an app name when using full dir
    names.
    """
    if explicit_template:
        if not explicit_template.exists():
            raise FileNotFoundError(f"Specified template not found: {explicit_template}")
        return explicit_template

    # Prefer: spack-envs/<app-version>/Dockerfile.j2 (current layout)
    env_dir = SPACK_ENVS_DIR / app_version
    env_template = env_dir / "Dockerfile.j2"
    if env_template.exists():
        return env_template

    # Fallback: spack-envs/<app>_<app-version>/Dockerfile.j2 (legacy short versions)
    if app:
        env_dir = SPACK_ENVS_DIR / f"{app}_{app_version}"
        env_template = env_dir / "Dockerfile.j2"
        if env_template.exists():
            return env_template

    # Support user passing the template filename directly as app-version
    raw = app_version
    if raw.startswith("Dockerfile-"):
        candidate = TEMPLATES_DIR / raw if raw.endswith(".j2") else TEMPLATES_DIR / f"{raw}.j2"
        if candidate.exists():
            return candidate

    # Fallback: templates/Dockerfile-<app>-<app-version>.j2 (legacy)
    if app:
        template_name = f"Dockerfile-{app}-{app_version}.j2"
        template_path = TEMPLATES_DIR / template_name
        if template_path.exists():
            return template_path

    available_versions = _extract_available_versions()
    available_list = "\n  ".join(available_versions)
    raise FileNotFoundError(
        f"No template found for --app-version '{app_version}'.\n"
        f"Available versions:\n  {available_list}\n"
        "Usage: python -m hpc_cf build --app-version <version>"
    )


# ── Build context & rendering ───────────────────────────────────────────


def build_context(
    *,
    use_mirror: bool,
    build_only: bool,
    app_version: str,
    template_path: Path | None,
) -> dict:
    """Assemble the Jinja2 rendering context from env.yaml and CLI flags."""
    env_config = load_env_yaml(template_path)

    method = env_config.get("method", "spack")

    env_images = env_config.get("images", {})
    builder_base_image = env_images.get("builder", "debian:trixie")
    runtime_base_image = env_images.get("runtime", "debian:trixie-slim")
    default_image_name, default_image_tag = resolve_output_image_tag(template_path)

    env_dir_name = template_path.parent.name if template_path else ""

    context = {
        "timestamp": datetime.now().isoformat(),
        "generated_with": "HPC Dockerfile Generator",
        "method": method,
        "builder_base_image": builder_base_image,
        "runtime_base_image": runtime_base_image,
        "use_mirror": use_mirror and (method == "spack"),
        "build_only": build_only,
        "default_image_name": default_image_name,
        "default_image_tag": default_image_tag,
        "spack_version": env_config.get("spack", {}).get("version", DEFAULT_SPACK_VERSION),
        "env_dir_name": env_dir_name,
        "manual_packages": env_config.get("manual_packages", []),
        **env_config.get("template_vars", {}),
    }

    # Pass through no_spack-specific keys when applicable.
    if method == "no_spack":
        context["script"] = env_config.get("script", "")
        runtime_cfg = env_config.get("runtime", {}) or {}
        context["runtime_copy_dirs"] = runtime_cfg.get("copy_dirs", [])
        context["runtime_extra_pkgs"] = runtime_cfg.get("extra_pkgs", [])

    logger.debug("Build context keys: %s", list(context.keys()))
    return context


def render_template(template_path: Path, context: dict) -> str:
    """Render a Jinja2 Dockerfile template with the given context."""
    logger.info("Rendering template: %s", template_path)

    env = Environment(
        loader=FileSystemLoader(str(template_path.parent)),
        trim_blocks=True,
        lstrip_blocks=True,
    )

    try:
        template = env.get_template(template_path.name)
    except TemplateNotFound as exc:
        raise FileNotFoundError(f"Template not found: {exc}") from exc

    try:
        return template.render(context)
    except TemplateSyntaxError as exc:
        raise RuntimeError(
            f"Jinja2 syntax error in {template_path}:{exc.lineno}: {exc.message}"
        ) from exc


def write_output(content: str, output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(content, encoding="utf-8")
    logger.info("Dockerfile written: %s", output_path)


def generate_dockerfile(
    *,
    template: Path | None,
    app_version: str,
    output: Path,
    use_mirror: bool,
    build_only: bool,
) -> Path:
    # Prefer select_template (per-env Dockerfile.j2 or explicit --template).
    # no_spack envs may omit Dockerfile.j2 and use templates/Dockerfile.nospack.j2;
    # build_context still needs template_path.parent for env.yaml, so we keep a
    # synthetic per-env path when falling through to the shared nospack template.
    nospack_tpl = TEMPLATES_DIR / "Dockerfile.nospack.j2"
    per_env_tpl = SPACK_ENVS_DIR / app_version / "Dockerfile.j2"

    try:
        template_path = select_template(app_version, template)
        render_path = template_path
    except FileNotFoundError:
        if not nospack_tpl.exists():
            raise
        template_path = per_env_tpl  # .parent resolves env.yaml
        render_path = nospack_tpl

    context = build_context(
        use_mirror=use_mirror,
        build_only=build_only,
        app_version=app_version,
        template_path=template_path,
    )

    # Shared nospack template when the env has no per-env Dockerfile.j2.
    if context.get("method") == "no_spack" and not per_env_tpl.exists() and nospack_tpl.exists():
        render_path = nospack_tpl

    content = render_template(render_path, context)
    write_output(content, output)
    return output


def resolve_image_and_tag(
    *,
    app_version: str,
    template: Path | None,
    image_arg: str | None,
    tag_arg: str | None,
) -> tuple[str, str]:
    resolved_template = select_template(app_version, template)
    default_image, default_tag = resolve_output_image_tag(resolved_template)
    image = image_arg if image_arg else default_image
    tag = tag_arg if tag_arg else default_tag
    return image, tag


def detect_non_host_network(opts: list[str] | None) -> str | None:
    """Scan a list of options for a non-host --network/--net mode."""
    import shlex as _shlex

    if not opts:
        return None

    tokens: list[str] = []
    for item in opts:
        tokens.extend(_shlex.split(item))

    i = 0
    while i < len(tokens):
        token = tokens[i]
        mode: str | None = None

        if token.startswith("--network="):
            mode = token.split("=", 1)[1]
        elif token.startswith("--net="):
            mode = token.split("=", 1)[1]
        elif token in {"--network", "--net"}:
            if i + 1 < len(tokens):
                mode = tokens[i + 1]
                i += 1

        if mode and mode != "host":
            return mode
        i += 1

    return None
