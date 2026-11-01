"""Unit tests for manual-package buildcache exclusion and VASP 6.6.1 Dockerfile rendering."""

from __future__ import annotations

from hpc_cf.buildcache import manual_package_exclude_specs
from hpc_cf.buildcache_ops import (
    build_publish_script,
    build_publish_script_oci,
    build_verify_script,
)
from hpc_cf.template import build_context, render_template, resolve_build_input


def test_vasp_excludes_union_of_manual_packages_and_declared_list() -> None:
    for env_name in ("vasp_mkl-6.6.1-avx2", "vasp_mkl-6.6.1-avx512"):
        resolved = resolve_build_input(env_name)
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


def test_vasp_661_dockerfile_separates_producer_deps_from_proprietary_vasp() -> None:
    for env_name in ("vasp_mkl-6.6.1-avx2", "vasp_mkl-6.6.1-avx512"):
        resolved = resolve_build_input(env_name)
        producer_ctx = build_context(
            use_mirror=True,
            build_only=True,
            app_version=env_name,
            template_path=resolved.render_template,
            resolved=resolved,
            buildcache_policy="auto",
            buildcache_producer=True,
        )
        producer_df = render_template(resolved.render_template, producer_ctx)
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
        consumer_df = render_template(resolved.render_template, consumer_ctx)
        assert (
            "--only dependencies \\\n"
            "        --use-buildcache only --fail-fast \\\n"
            '        -j "$(nproc)" -p 20 vasp;'
        ) in consumer_df
        assert "e.explicit_roots()" in consumer_df
        assert "COPY assets/manual-packages/vasp.6.6.1.tgz" in consumer_df
        assert '--use-buildcache never --fail-fast -j "$(nproc)" -p 20 --keep-stage vasp' in consumer_df

        never_ctx = build_context(
            use_mirror=True,
            build_only=False,
            app_version=env_name,
            template_path=resolved.render_template,
            resolved=resolved,
            buildcache_policy="never",
            buildcache_producer=False,
        )
        never_df = render_template(resolved.render_template, never_ctx)
        assert '--only dependencies --fail-fast -j "$(nproc)" -p 20 vasp;' in never_df
        assert "e.explicit_roots()" in never_df
