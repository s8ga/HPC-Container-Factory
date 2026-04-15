# 模板与环境映射矩阵

本页用于明确：每个模板依赖哪个环境目录、当前是否匹配、现在能否直接使用。

## 总表

| 模板 | 模板内期望环境目录 | 仓库实际目录 | 当前状态 |
|---|---|---|---|
| templates/Dockerfile-cp2k-opensource-2025.2.j2 | spack-envs/cp2k-opensource-2025.2 | spack-envs/cp2k-opensource-2025.2 | 可用 |
| templates/Dockerfile-cp2k-rocm-2026.1-gfx942.j2 | spack-envs/cp2k-rocm-2026.1-gfx942 | spack-envs/cp2k-rocm-2026.1-gfx942 | 可用 |
| templates/Dockerfile-base.j2 | N/A（基础片段） | templates/Dockerfile-base.j2 | 可用 |

## 结论

当前最稳路径：
- 使用 CP2K opensource 模板
- 使用 CP2K ROCm InfinityHub 模板（SIRIUS + DLAF + Spack 通信栈）

已归档路径：
- VASP 与 CP2K MKL 路线已迁移到 legacy 目录，不再属于活跃模板集合。