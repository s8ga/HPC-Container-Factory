# 模板与环境映射矩阵

## 总表

| 环境 (`--app-version`) | 目录 | 模板位置 | 自动镜像名 | 状态 |
|---|---|---|---|---|
| `cp2k-opensource-2025.2` | `spack-envs/cp2k-opensource-2025.2/` | `spack-envs/cp2k-opensource-2025.2/Dockerfile.j2` | `cp2k-opensource:2025.2` | ✅ 可用 |
| `cp2k-opensource-2025.2-force-avx512` | `spack-envs/cp2k-opensource-2025.2-force-avx512/` | `spack-envs/cp2k-opensource-2025.2-force-avx512/Dockerfile.j2` | `cp2k-opensource:2025.2-force-avx512` | ✅ 可用 |
| `cp2k-rocm-2026.1-gfx942` | `spack-envs/cp2k-rocm-2026.1-gfx942/` | `spack-envs/cp2k-rocm-2026.1-gfx942/Dockerfile.j2` | `cp2k-rocm:2026.1-gfx942` | ✅ 可用 |

## Legacy 模板（仍可用于回退）

| 模板 | 状态 |
|---|---|
| `templates/Dockerfile-cp2k-opensource-2025.2.j2` | 回退（`spack-envs/` 优先） |
| `templates/Dockerfile-cp2k-rocm-2026.1-gfx942.j2` | 回退（`spack-envs/` 优先） |
| `templates/Dockerfile-base.j2` | 基础片段 |

## 归档

VASP 与 CP2K MKL 路线已迁移到 `legacy/` 目录。