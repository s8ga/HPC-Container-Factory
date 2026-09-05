"""Template discovery, rendering, and build-context assembly."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

try:
    from jinja2 import (
        ChoiceLoader,
        Environment,
        FileSystemLoader,
        StrictUndefined,
        TemplateNotFound,
        TemplateSyntaxError,
    )
    from jinja2.exceptions import UndefinedError
except ImportError as exc:
    raise ImportError(f"Required package not installed: {exc}. Install: pip install jinja2") from exc

from hpc_cf.config import DEFAULT_APT_MIRROR, DEFAULT_SPACK_VERSION
from hpc_cf.env import list_available_envs
from hpc_cf.environment import (
    BuildMethod,
    EnvironmentSpec,
    apply_resolved_repo_pins,
    load_environment_spec,
    load_environment_spec_from_template,
)
from hpc_cf.execution import ProjectLayout
from hpc_cf.shell_quote import confine_to_root, shell_quote
from hpc_cf.spack_plan import build_spack_environment_plan, plan_context

logger = logging.getLogger(__name__)


def _layout(layout: ProjectLayout | None = None) -> ProjectLayout:
    return layout or ProjectLayout.default()


@dataclass(frozen=True)
class ResolvedBuildInput:
    """Unified resolve result: config path vs Jinja render path.

    *environment_dir* is where env.yaml / spack.yaml live.
    *render_template* is the Dockerfile.j2 actually rendered (may be a
    shared template such as ``templates/Dockerfile.nospack.j2``).
    *compatibility_mode* is True when rendering a legacy explicit template
    without an adjacent env.yaml (defaults + warning).
    """

    environment_spec: EnvironmentSpec | None
    environment_dir: Path
    render_template: Path
    compatibility_mode: bool = False


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


def resolve_output_image_tag(
    template_path: Path | None,
    *,
    environment_dir: Path | None = None,
    spec: EnvironmentSpec | None = None,
) -> tuple[str, str]:
    """Resolve (image_name, tag) with env.yaml override taking priority."""
    if spec is None:
        if environment_dir is not None:
            try:
                spec = load_environment_spec(environment_dir)
            except FileNotFoundError:
                spec = None
        else:
            spec = load_environment_spec_from_template(template_path)
    if spec is not None and spec.images.output_name:
        return spec.images.output_name, spec.images.output_tag or "latest"

    naming_anchor = environment_dir or (template_path.parent if template_path else None)
    if naming_anchor is not None:
        return _parse_dir_to_image_tag(naming_anchor.name)
    return infer_image_defaults("", template_path)


# ── Template discovery ──────────────────────────────────────────────────


def _extract_available_versions(
    *,
    layout: ProjectLayout | None = None,
) -> list[str]:
    """List selectable ``--app-version`` / ``--env`` values.

    Uses :func:`list_available_envs` (env.yaml-backed dirs under spack-envs/)
    so CLI listing matches ``assets --env``. Differs from a raw Dockerfile.j2
    scan: includes no_spack envs that lack a per-env Dockerfile.j2, and
    omits dirs that have only a template with no env.yaml.

    Also appends legacy ``templates/Dockerfile-*.j2`` stems not already listed.
    """
    root = _layout(layout)
    versions = list(list_available_envs(layout=root))
    seen = set(versions)

    for f in sorted(root.templates_dir.glob("Dockerfile-*.j2")):
        if f.name == "Dockerfile-base.j2":
            continue
        stem = f.name[len("Dockerfile-"): -len(".j2")]
        if stem not in seen:
            versions.append(stem)
            seen.add(stem)

    return versions


def _shared_method_template(
    spec: EnvironmentSpec | None,
    *,
    layout: ProjectLayout,
) -> Path | None:
    """Return shared templates/<default> when the method declares one."""
    if spec is None:
        return None
    name = spec.method.default_template
    if not name:
        return None
    shared = layout.templates_dir / name
    return shared if shared.is_file() else None


def _try_load_env_spec(env_dir: Path) -> EnvironmentSpec | None:
    try:
        return load_environment_spec(env_dir)
    except FileNotFoundError:
        return None


def _load_spec_applying_repo_pins(env_dir: Path) -> EnvironmentSpec | None:
    """Load the spec, then apply assets-resolved pins for floating repos.

    resolve_build_input is the single entry the dockerfile/build flows
    share, so applying here keeps every consumer (render, image build)
    on the sha recorded in resolved-repos.yaml beside spack.yaml.
    """
    spec = _try_load_env_spec(env_dir)
    if spec is None:
        return None
    return apply_resolved_repo_pins(spec, env_dir)


def resolve_build_input(
    app_version: str | None = None,
    explicit_template: Path | None = None,
    *,
    app: str = "",
    layout: ProjectLayout | None = None,
) -> ResolvedBuildInput:
    """Resolve env config directory and Dockerfile render path separately.

    Config (env.yaml) may live under ``spack-envs/<name>/`` while rendering
    uses a shared template (e.g. no_spack → ``Dockerfile.nospack.j2``).
    Legacy ``templates/Dockerfile-*.j2`` without env.yaml enters compatibility
    mode.

    Path discovery uses *layout* (or :meth:`ProjectLayout.default`).
    Caller-supplied ``--app-version`` / ``--template`` paths are resolved and
    must stay under ``layout.project_root``.
    """
    root = _layout(layout)
    project_root = root.project_root

    if explicit_template is not None:
        template_path = confine_to_root(
            explicit_template,
            root=project_root,
            label="--template",
        )
        if not template_path.exists():
            raise FileNotFoundError(
                f"Specified template not found: {explicit_template}"
            )
        env_dir = template_path.parent
        spec = _load_spec_applying_repo_pins(env_dir)
        if spec is None:
            logger.warning(
                "Template %s has no adjacent env.yaml — "
                "running in compatibility mode with defaults",
                template_path,
            )
            return ResolvedBuildInput(
                environment_spec=None,
                environment_dir=env_dir,
                render_template=template_path,
                compatibility_mode=True,
            )
        return ResolvedBuildInput(
            environment_spec=spec,
            environment_dir=env_dir,
            render_template=template_path,
            compatibility_mode=False,
        )

    if not app_version:
        raise FileNotFoundError(
            "Cannot resolve build input without --app-version or --template"
        )

    # Prefer: spack-envs/<app-version>/Dockerfile.j2 (current layout).
    # Confine under spack-envs/ so ``../artifacts`` cannot select non-env trees.
    env_dir = confine_to_root(
        root.spack_envs_dir / app_version,
        root=root.spack_envs_dir,
        label="--app-version/--env",
    )
    confine_to_root(env_dir, root=project_root, label="--app-version/--env")
    per_env = env_dir / "Dockerfile.j2"
    if per_env.exists():
        spec = _load_spec_applying_repo_pins(env_dir)
        return ResolvedBuildInput(
            environment_spec=spec,
            environment_dir=env_dir,
            render_template=per_env,
            compatibility_mode=False,
        )

    # no_spack (and similar): shared template when env.yaml exists but
    # per-env Dockerfile.j2 does not.
    if env_dir.is_dir():
        spec = _load_spec_applying_repo_pins(env_dir)
        shared = _shared_method_template(spec, layout=root)
        if shared is not None:
            return ResolvedBuildInput(
                environment_spec=spec,
                environment_dir=env_dir,
                render_template=shared,
                compatibility_mode=False,
            )

    # Fallback: spack-envs/<app>_<app-version>/Dockerfile.j2
    if app:
        env_dir = confine_to_root(
            root.spack_envs_dir / f"{app}_{app_version}",
            root=root.spack_envs_dir,
            label="--app-version/--env",
        )
        confine_to_root(env_dir, root=project_root, label="--app-version/--env")
        per_env = env_dir / "Dockerfile.j2"
        if per_env.exists():
            spec = _load_spec_applying_repo_pins(env_dir)
            return ResolvedBuildInput(
                environment_spec=spec,
                environment_dir=env_dir,
                render_template=per_env,
                compatibility_mode=False,
            )
        if env_dir.is_dir():
            spec = _load_spec_applying_repo_pins(env_dir)
            shared = _shared_method_template(spec, layout=root)
            if shared is not None:
                return ResolvedBuildInput(
                    environment_spec=spec,
                    environment_dir=env_dir,
                    render_template=shared,
                    compatibility_mode=False,
                )

    # Support user passing the template filename directly as app-version
    raw = app_version
    if raw.startswith("Dockerfile-"):
        candidate = (
            root.templates_dir / raw
            if raw.endswith(".j2")
            else root.templates_dir / f"{raw}.j2"
        )
        confined = confine_to_root(
            candidate, root=project_root, label="--app-version/--env"
        )
        if confined.exists():
            return resolve_build_input(
                explicit_template=confined, layout=root
            )

    # Fallback: templates/Dockerfile-<app>-<app-version>.j2 (legacy)
    if app:
        template_name = f"Dockerfile-{app}-{app_version}.j2"
        template_path = confine_to_root(
            root.templates_dir / template_name,
            root=project_root,
            label="--app-version/--env",
        )
        if template_path.exists():
            return resolve_build_input(
                explicit_template=template_path, layout=root
            )

    # Legacy stem: templates/Dockerfile-<app_version>.j2 without env.yaml
    legacy = confine_to_root(
        root.templates_dir / f"Dockerfile-{app_version}.j2",
        root=project_root,
        label="--app-version/--env",
    )
    if legacy.exists():
        return resolve_build_input(explicit_template=legacy, layout=root)

    available_versions = _extract_available_versions(layout=root)
    available_list = "\n  ".join(available_versions)
    raise FileNotFoundError(
        f"No template found for --app-version '{app_version}'.\n"
        f"Available versions:\n  {available_list}\n"
        "Usage: python -m hpc_cf build --app-version <version>"
    )


def select_template(
    app_version: str,
    explicit_template: Path | None = None,
    *,
    app: str = "",
    layout: ProjectLayout | None = None,
) -> Path:
    """Locate the Jinja2 Dockerfile template for the given env / version.

    Prefer :func:`resolve_build_input` when both the env directory and the
    render path are needed (they can differ for shared no_spack templates).
    """
    return resolve_build_input(
        app_version, explicit_template, app=app, layout=layout
    ).render_template


# ── Build context & rendering ───────────────────────────────────────────


def build_context(
    *,
    use_mirror: bool,
    build_only: bool,
    app_version: str,
    template_path: Path | None,
    resolved: ResolvedBuildInput | None = None,
    layout: ProjectLayout | None = None,
    allow_reconcretize: bool = False,
    buildcache_policy: str | None = None,
    buildcache_producer: bool = False,
    buildcache_mode: str | None = None,
    buildcache_url: str | None = None,
    buildcache_username_var: str | None = None,
    buildcache_password_var: str | None = None,
) -> dict:
    """Assemble the Jinja2 rendering context from env.yaml and CLI flags.

    Prefer passing *resolved* (:class:`ResolvedBuildInput`) so shared templates
    still load env.yaml from the environment directory.  When only
    *template_path* is given (legacy callers / tests), missing env.yaml enters
    compatibility mode with a warning.

    *layout* is accepted for API consistency with path-discovery helpers; this
    function does not resolve template paths itself.
    """
    if resolved is not None:
        spec = resolved.environment_spec
        env_dir = resolved.environment_dir
        compatibility = resolved.compatibility_mode
        render_path = resolved.render_template
    else:
        spec = load_environment_spec_from_template(template_path)
        env_dir = template_path.parent if template_path else Path(".")
        render_path = template_path
        compatibility = spec is None and template_path is not None
        if compatibility:
            logger.warning(
                "Template %s has no adjacent env.yaml — "
                "running in compatibility mode with defaults",
                template_path,
            )

    method = spec.method if spec is not None else BuildMethod.SPACK
    images = spec.images if spec is not None else None

    builder_base_image = images.builder if images else "debian:trixie"
    runtime_base_image = images.runtime if images else "debian:trixie-slim"
    default_image_name, default_image_tag = resolve_output_image_tag(
        render_path,
        environment_dir=env_dir,
        spec=spec,
    )

    env_dir_name = env_dir.name if env_dir else ""
    spack_version = (
        spec.spack.version if spec is not None else DEFAULT_SPACK_VERSION
    )
    manual_packages = (
        [mp.__dict__ for mp in spec.manual_packages] if spec is not None else []
    )
    # Drop None dest/sha256 keys so templates see the same shape as raw YAML.
    manual_packages = [
        {k: v for k, v in mp.items() if v is not None} for mp in manual_packages
    ]
    template_vars = dict(spec.template_vars) if spec is not None else {}

    context = {
        "timestamp": datetime.now().isoformat(),
        "generated_with": "HPC Dockerfile Generator",
        "method": method.value,
        "builder_base_image": builder_base_image,
        "runtime_base_image": runtime_base_image,
        "use_mirror": use_mirror and method.allows_mirror,
        "build_only": build_only,
        "default_image_name": default_image_name,
        "default_image_tag": default_image_tag,
        "spack_version": spack_version,
        "env_dir_name": env_dir_name,
        "manual_packages": manual_packages,
        "compatibility_mode": compatibility,
        "allow_reconcretize": allow_reconcretize,
        **template_vars,
    }

    # Spack contract from EnvironmentSpec → SpackEnvironmentPlan (image phase).
    # plan_context keys override raw template_vars for reserved names.
    if spec is not None and method is BuildMethod.SPACK:
        context.update(plan_context(build_spack_environment_plan(spec)))
        context["allow_reconcretize"] = allow_reconcretize
    elif method is BuildMethod.SPACK:
        # Legacy templates without env.yaml: do NOT invent a cp2k-env name.
        # Templates that need {{ spack_env_name }} will fail under StrictUndefined.
        if "spack_env_name" not in context:
            logger.warning(
                "Compatibility mode without EnvironmentSpec: spack_env_name "
                "is unset (explicit env.yaml spack.env_name required)"
            )
        context.setdefault("spack_update_builtin", True)
        context.setdefault("spack_repo_scope", "site")
        context.setdefault("spack_repo_scope_kind", "site")
        context.setdefault("spack_mirror_scope", "site")
        context.setdefault("spack_mirror_scope_kind", "site")
        context.setdefault("spack_image_repos", [])
        context["allow_reconcretize"] = allow_reconcretize

    if method is BuildMethod.SPACK:
        if buildcache_policy is not None:
            context["spack_buildcache_policy"] = buildcache_policy
        if buildcache_mode is not None:
            context["spack_buildcache_mode"] = buildcache_mode
        if buildcache_url is not None:
            context["spack_buildcache_url"] = buildcache_url
        if buildcache_username_var is not None:
            context["spack_buildcache_username_var"] = buildcache_username_var
        if buildcache_password_var is not None:
            context["spack_buildcache_password_var"] = buildcache_password_var
        context["spack_buildcache_producer"] = buildcache_producer

    if method is BuildMethod.NO_SPACK:
        context["script"] = spec.script if spec is not None else ""
        context["runtime_copy_dirs"] = (
            list(spec.runtime.copy_dirs) if spec is not None else []
        )
        context["runtime_extra_pkgs"] = (
            list(spec.runtime.extra_pkgs) if spec is not None else []
        )

    # APT mirror for Debian templates; unset → USTC. Empty/"official" skip sed.
    if "apt_mirror" not in context or context["apt_mirror"] is None:
        context["apt_mirror"] = DEFAULT_APT_MIRROR

    logger.debug("Build context keys: %s", list(context.keys()))
    return context


def _jinja_search_paths(
    template_path: Path,
    *,
    layout: ProjectLayout | None = None,
) -> list[str]:
    """Search paths for Dockerfile rendering.

    Per-env templates live under ``spack-envs/<env>/`` but may
    ``{% include 'partials/...' %}`` from the global ``templates/`` tree.
    Order: template directory first (local overrides), then layout.templates_dir.
    """
    templates_dir = _layout(layout).templates_dir
    paths = [
        str(template_path.parent.resolve()),
        str(templates_dir.resolve()),
    ]
    # Preserve order while dropping duplicates (e.g. shared nospack template).
    return list(dict.fromkeys(paths))


def render_template(
    template_path: Path,
    context: dict,
    *,
    layout: ProjectLayout | None = None,
) -> str:
    """Render a Jinja2 Dockerfile template with the given context."""
    logger.info("Rendering template: %s", template_path)

    env = Environment(
        loader=ChoiceLoader(
            [
                FileSystemLoader(p)
                for p in _jinja_search_paths(template_path, layout=layout)
            ]
        ),
        trim_blocks=True,
        lstrip_blocks=True,
        # Included partials often end with a final newline that separates adjacent
        # Dockerfile RUN/COMMENT blocks. Jinja strips it unless this is enabled,
        # which glues ``echo ...mirror`` onto the next ``# comment`` / ``RUN``.
        keep_trailing_newline=True,
        undefined=StrictUndefined,
    )
    env.filters["shell_quote"] = shell_quote

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
    except UndefinedError as exc:
        raise RuntimeError(
            f"Undefined template variable in {template_path}: {exc}"
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
    layout: ProjectLayout | None = None,
    allow_reconcretize: bool = False,
    buildcache_policy: str | None = None,
    buildcache_producer: bool = False,
    buildcache_mode: str | None = None,
    buildcache_url: str | None = None,
    buildcache_username_var: str | None = None,
    buildcache_password_var: str | None = None,
) -> Path:
    root = _layout(layout)
    resolved = resolve_build_input(app_version, template, layout=root)
    context = build_context(
        use_mirror=use_mirror,
        build_only=build_only,
        app_version=app_version,
        template_path=resolved.render_template,
        resolved=resolved,
        layout=root,
        allow_reconcretize=allow_reconcretize,
        buildcache_policy=buildcache_policy,
        buildcache_producer=buildcache_producer,
        buildcache_mode=buildcache_mode,
        buildcache_url=buildcache_url,
        buildcache_username_var=buildcache_username_var,
        buildcache_password_var=buildcache_password_var,
    )
    content = render_template(
        resolved.render_template, context, layout=root
    )
    write_output(content, output)
    return output


def resolve_image_and_tag(
    *,
    app_version: str,
    template: Path | None,
    image_arg: str | None,
    tag_arg: str | None,
    layout: ProjectLayout | None = None,
) -> tuple[str, str]:
    resolved = resolve_build_input(app_version, template, layout=layout)
    default_image, default_tag = resolve_output_image_tag(
        resolved.render_template,
        environment_dir=resolved.environment_dir,
        spec=resolved.environment_spec,
    )
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
