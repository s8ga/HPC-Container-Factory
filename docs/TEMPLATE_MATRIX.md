# 模板与环境映射矩阵

环境清单由 `spack-envs/*/spack-env-file/env.yaml`（或 legacy `env.yaml`）发现，
并通过 `EnvironmentSpec`（`schema_version: 1`）校验。下表为人工可读摘要；
以仓库内实际目录为准，勿依赖硬编码测试数量。

## 总表

| 环境 (`--app-version`) | method | 模板 | 共享 partials | 状态 |
|---|---|---|---|---|
| `abacus_opensource-3.9.0.27-force-avx512` | spack | per-env `Dockerfile.j2` | ✅ | ✅ |
| `cp2k_opensource-2025.2` | spack | per-env `Dockerfile.j2` | ✅ | ✅ |
| `cp2k_opensource-2025.2-force-avx512` | spack | per-env `Dockerfile.j2` | ✅ | ✅ |
| `cp2k_opensource-2026.1-force-avx512` | spack | per-env `Dockerfile.j2` | ✅ | ✅ |
| `cp2k_opensource-2026.2-force-avx512` | spack | per-env `Dockerfile.j2` | ✅ | ✅ |
| `cp2k_mkl-2025.2-experimental` | spack | per-env `Dockerfile.j2` | ✅ | 🧪 |
| `cp2k_rocm-2026.1-gfx942` | spack | per-env `Dockerfile.j2` | ✅ | ✅ |
| `vasp_mkl-6.6.0-avx2` | spack | per-env `Dockerfile.j2` | ✅ | ✅ |
| `vasp_mkl-6.6.0-avx512` | spack | per-env `Dockerfile.j2` | ✅ | ✅ |

## 共享 Jinja partials

位于 `templates/partials/`，由 per-env `Dockerfile.j2` include：

| Partial | 用途 |
|---|---|
| Spack install / bootstrap / mirror 注册 | 公共安装步骤；mirror 使用 `{{ spack_mirror_scope }}`（默认 site） |
| 环境创建 + lock 导入 | 使用 `{{ spack_env_name }}`（来自 `SpackEnvironmentPlan`） |
| view / cleanup | `spack env view` 与 gc |
| `spack_image_repos.j2`（可选） | 按 `spack_image_repos` 注册 image-phase 自定义 repo；**尚未**接入现有应用模板 |

应用构建、manual source、regtest、ROCm 等仍留在各 per-env 模板。

**契约诚实化**：
- `SpackEnvironmentPlan` 对 **assets** 路径是可靠共享约束；image 侧自定义 repo
  仍以 **per-env Dockerfile.j2 + `template_vars`** 为准（双写时需人工保持同步）
- 渲染结果中的 `spack env create <name>` 必须来自 plan，不得硬编码固定 env 名
- `repo_scope`（自定义 repo）与 `mirror_scope`（mirror 注册）相互独立；
  `mirror_scope` **有意固定 site**，不从 env.yaml 配置
- 并非所有 env 都 `repo_scope: env` / 都在 image 侧 `update_builtin`
  （例如 VASP：`image.update_builtin: false` + `repo_scope: site`）
- Jinja 使用 `StrictUndefined`；`template_vars` 缺项在渲染时失败
- CLI **不**暴露自定义 `ProjectLayout`（注入主要用于测试）

## no_spack

`method: no_spack` 使用共享 `templates/Dockerfile.nospack.j2`（多阶段：builder
跑用户脚本，runtime 拷贝产物）。无需 Spack 资产。

## Legacy 模板（回退）

| 模板 | 状态 |
|---|---|
| `templates/Dockerfile-cp2k_opensource-2025.2.j2` | 回退（`spack-envs/` 优先） |
| `templates/Dockerfile-cp2k_rocm-2026.1-gfx942.j2` | 回退（`spack-envs/` 优先） |
| `templates/Dockerfile.nospack.j2` | no_spack 默认模板 |

## 校验与冒烟

```bash
# 不要求大体积资产（纯 config/template）
python -m hpc_cf validate --app-version <env> --profile config

# 渲染冒烟
python -m hpc_cf dockerfile --app-version <env> --output /tmp/Dockerfile

# 契约测试（默认 pytest 包含）
./venv/bin/pytest tests/test_spack_plan.py tests/test_validation_profiles.py -q
```
