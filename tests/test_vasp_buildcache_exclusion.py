"""Unit tests for manual-package buildcache exclusion and dependency-only install."""

from __future__ import annotations

import shutil
from pathlib import Path

from hpc_cf.buildcache import manual_package_exclude_specs
from hpc_cf.buildcache_ops import (
    build_publish_script,
    build_publish_script_oci,
    build_verify_script,
)
from hpc_cf.config import TEMPLATES_DIR
from hpc_cf.execution import ProjectLayout
from hpc_cf.template import build_context, render_template, resolve_build_input


def _make_synthetic_vasp_layout(tmp_path: Path) -> tuple[ProjectLayout, str]:
    layout = ProjectLayout(project_root=tmp_path)
    shutil.copytree(TEMPLATES_DIR, tmp_path / "templates")
    env_name = "vasp_mkl-6.6.1-avx512"
    env_dir = tmp_path / "spack-envs" / env_name
    spack_dir = env_dir / "spack-env-file"
    spack_dir.mkdir(parents=True)
    (spack_dir / "env.yaml").write_text(
        """schema_version: 1
spack:
  version: "1.2.0"
  env_name: vasp-env
  buildcache:
    enabled: true
    padded_length: 128
    policy: auto
    coverage: non_external
    exclude: [intel-oneapi-mkl]
manual_packages:
  - spec: vasp
    file: assets/manual-packages/vasp.6.6.1.tgz
    dest: /opt/manual-package/
    sha256: "261f9c1fd730468106191afeb185821d67cb7a58c982f6e063824dd5432106b0"
""",
        encoding="utf-8",
    )
    (spack_dir / "spack.yaml").write_text("spack:\n  specs: []\n", encoding="utf-8")
    (env_dir / "Dockerfile.j2").write_text(
        """{% if spack_buildcache_policy in ['auto', 'only'] %}
{% include 'partials/spack_buildcache_install_deps.j2' %}
{% else %}
{% set _excl_csv = spack_buildcache_exclude_specs | default([]) | join(',') %}
{% set _excl_args = spack_buildcache_exclude_specs | default(['vasp'], true) | join(' ') %}
RUN bash -o pipefail -c 'set -euo pipefail; \
    spack -e {{ spack_env_name }} install --only-concrete --only dependencies --fail-fast -j "$(nproc)" -p 20 {{ _excl_args }}; \
    mapfile -t _hpc_cf_extra_roots < <(spack -e {{ spack_env_name }} python -c \
        "import spack.environment as ev; e=ev.active_environment(); excl={x for x in \\"{{ _excl_csv }}\\".split(\\",\\") if x}; print(chr(10).join(\\"/\\"+x.hash for x in e.explicit_roots() if (not e.specs_by_hash[x.hash].external) and (not e.specs_by_hash[x.hash].installed) and (e.specs_by_hash[x.hash].name not in excl)))"); \
    if [ -n "${_hpc_cf_extra_roots[0]:-}" ]; then \
        spack -e {{ spack_env_name }} install --only-concrete --fail-fast -j "$(nproc)" -p 20 "${_hpc_cf_extra_roots[@]}"; \
    fi'
{% endif %}
{% if not spack_buildcache_producer %}
{% for mp in manual_packages %}
COPY {{ mp.file }} {{ mp.dest }}
{% endfor %}
RUN spack -e {{ spack_env_name }} install --only-concrete --use-buildcache never --fail-fast -j "$(nproc)" -p 20 --keep-stage vasp
{% endif %}
""",
        encoding="utf-8",
    )
    return layout, env_name


def test_vasp_excludes_union_of_manual_packages_and_declared_list(
    tmp_path: Path,
) -> None:
    layout, env_name = _make_synthetic_vasp_layout(tmp_path)
    resolved = resolve_build_input(env_name, layout=layout)
    spec = resolved.environment_spec
    assert spec is not None
    excludes = manual_package_exclude_specs(spec)
    assert excludes == ("vasp", "intel-oneapi-mkl")
    pub = build_publish_script(
        env_name="vasp-env", store_path="/cache", exclude_specs=excludes
    )
    ver = build_verify_script(
        env_name="vasp-env", store_path="/cache", exclude_specs=excludes
    )
    for script in (pub, ver):
        assert '(spec.name not in {"intel-oneapi-mkl", "vasp"})' in script


def test_buildcache_ops_excludes_vasp_when_configured() -> None:
    default_pub = build_publish_script(env_name="cp2k-env", store_path="/cache")
    assert "spec.name not in" not in default_pub

    vasp_pub = build_publish_script(
        env_name="vasp-env",
        store_path="/cache",
        exclude_specs=("vasp",),
    )
    vasp_oci = build_publish_script_oci(
        env_name="vasp-env",
        mirror_url="oci://registry.example/cache",
        exclude_specs=("vasp",),
    )
    vasp_ver = build_verify_script(
        env_name="vasp-env",
        store_path="/cache",
        exclude_specs=("vasp",),
    )
    for script in (vasp_pub, vasp_oci, vasp_ver):
        assert '(spec.name not in {"vasp"})' in script


def test_vasp_dockerfile_separates_producer_deps_from_proprietary_vasp(
    tmp_path: Path,
) -> None:
    layout, env_name = _make_synthetic_vasp_layout(tmp_path)
    resolved = resolve_build_input(env_name, layout=layout)

    producer_ctx = build_context(
        use_mirror=True,
        build_only=True,
        app_version=env_name,
        template_path=resolved.render_template,
        resolved=resolved,
        buildcache_policy="auto",
        buildcache_producer=True,
    )
    producer_df = render_template(
        resolved.render_template, producer_ctx, layout=layout
    )
    assert (
        "--only dependencies \\\n"
        "        --use-buildcache auto --fail-fast \\\n"
        '        -j "$(nproc)" -p 20 vasp;'
    ) in producer_df
    assert "COPY assets/manual-packages/vasp.6.6.1.tgz" not in producer_df
    assert "--keep-stage vasp" not in producer_df

    consumer_ctx = build_context(
        use_mirror=True,
        build_only=False,
        app_version=env_name,
        template_path=resolved.render_template,
        resolved=resolved,
        buildcache_policy="only",
        buildcache_producer=False,
    )
    consumer_df = render_template(
        resolved.render_template, consumer_ctx, layout=layout
    )
    assert (
        "--only dependencies \\\n"
        "        --use-buildcache only --fail-fast \\\n"
        '        -j "$(nproc)" -p 20 vasp;'
    ) in consumer_df
    assert "e.explicit_roots()" in consumer_df
    assert "COPY assets/manual-packages/vasp.6.6.1.tgz" in consumer_df
    assert (
        '--use-buildcache never --fail-fast -j "$(nproc)" -p 20 --keep-stage vasp'
        in consumer_df
    )

    never_ctx = build_context(
        use_mirror=True,
        build_only=False,
        app_version=env_name,
        template_path=resolved.render_template,
        resolved=resolved,
        buildcache_policy="never",
        buildcache_producer=False,
    )
    never_df = render_template(
        resolved.render_template, never_ctx, layout=layout
    )
    assert '--only dependencies --fail-fast -j "$(nproc)" -p 20 vasp;' in never_df
    assert "e.explicit_roots()" in never_df
