# CP2K 2026.2 Opensource（force-avx512）

本环境使用 Spack 1.2.0 构建 CP2K 2026.2，采用 OpenMPI 5.0.10、OpenBLAS 和 `x86_64_v3` target，并通过 force-avx512 overrides 启用相关 CPU kernels。

## 配置入口

- `spack-env-file/env.yaml`：镜像、Spack 版本、自定义 repo 和模板变量。
- `spack-env-file/spack.yaml`：CP2K spec、依赖版本、variants 和 builtin repo pin。
- `spack-env-file/spack.lock`：已 concretize 的依赖图。
- `Dockerfile.j2`：该环境的容器模板。
- [发布说明](../../docs/releases/CP2K_2026.2.md)：验证范围、regtest 结果和已知限制。

## 验证与构建

在仓库根目录执行：

```bash
./venv/bin/python -m hpc_cf validate --app-version cp2k_opensource-2026.2-force-avx512 --profile config
./venv/bin/python -m hpc_cf validate --app-version cp2k_opensource-2026.2-force-avx512
./venv/bin/python -m hpc_cf validate --app-version cp2k_opensource-2026.2-force-avx512 --profile assets
./venv/bin/python -m hpc_cf assets --env cp2k_opensource-2026.2-force-avx512 --verify-mirror
./venv/bin/python -m hpc_cf dockerfile --app-version cp2k_opensource-2026.2-force-avx512
./venv/bin/python -m hpc_cf build --app-version cp2k_opensource-2026.2-force-avx512
```

默认 build-input 校验要求环境声明的 assets 可用，并只读消费现有非空 `spack.lock`。不要删除 lock 后直接构建；需要重新 concretize 时应通过 assets 工作流显式执行。

## MPI/OpenMP 使用建议

可从以下形式开始测试目标输入：

```bash
export OMP_NUM_THREADS=2
mpirun --bind-to none -np 2 cp2k.psmp -i input.inp
```

已提交的所选 regtest 使用 2 MPI ranks × 2 OpenMP threads，并通过 5713/5713 tests。这个结果不是对所有上游 requirements 或所有并行分解的保证。

SPGLIB 测试已确认对部分混合 MPI/OpenMP 分解敏感，尤其不能把 `2x2` 的结果直接外推到 `4x2`、`2x3` 或 `4x3`。在生产计算前，应以实际输入、节点拓扑和目标分解做短程验证。详细矩阵见[发布说明](../../docs/releases/CP2K_2026.2.md)。

## Skala 模型

GauXC 以 `+skala skala_version=1.1` 构建。模型路径由环境变量提供：

```bash
echo "$GAUXC_SKALA_MODEL"
# /opt/spack-view/share/gauxc/onedft_models/skala-1.1.fun
```

容器模板会在构建期间检查该文件存在。只有 `GAUXC_SKALA_MODEL` 非空时，交互式 MOTD 才显示 `Skala Model` 行；普通非交互命令不会显示 MOTD。
