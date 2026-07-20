# ABACUS 3.10.1 发布说明

## 发布范围

- 环境：`abacus_opensource-3.10.1-force-avx512`
- ABACUS：3.10.1-lts
- Spack：1.2.0
- MPI：OpenMPI 5.0.10
- CPU target：`x86_64_v3`
- BLAS/LAPACK：OpenBLAS 0.3.33，OpenMP threads，启用动态分派和 force-avx512
- Buildcache 角色：**consumer**（消费 `abacus_opensource-3.9.0.27-force-avx512` producer）

权威配置与追溯证据：

- [env.yaml](../../spack-envs/abacus_opensource-3.10.1-force-avx512/spack-env-file/env.yaml)
- [spack.yaml](../../spack-envs/abacus_opensource-3.10.1-force-avx512/spack-env-file/spack.yaml)
- [spack.lock](../../spack-envs/abacus_opensource-3.10.1-force-avx512/spack-env-file/spack.lock)
- [Dockerfile.j2](../../spack-envs/abacus_opensource-3.10.1-force-avx512/Dockerfile.j2)
- [Integration 测试日志](../../spack-envs/abacus_opensource-3.10.1-force-avx512/abacus-integration-test.log)
- [Module 测试日志](../../spack-envs/abacus_opensource-3.10.1-force-avx512/abacus-module-test.log)

ABACUS spec 启用了 `deepmd`、`deepks`、`elpa`、`lcao`、`libri`、`libxc`、`mpi`、`openmp`、`pexsi` 等功能；**未启用 `+tests`**（concrete 为 `tests=false`）。完整 spec 和依赖版本以 `spack.yaml` 与 `spack.lock` 为准。自定义 repo 与 3.9 authority 使用同一 s8ga monorepo commit pin（见 `env.yaml` / dual-write 守卫）。

## 与 3.9 producer 的关系

轨设计是 **publish then consume**：

1. Producer：`abacus_opensource-3.9.0.27-force-avx512` 执行 `buildcache build`（见 [ABACUS 3.9.0.27](ABACUS_3.9.0.27.md)）
2. Consumer：本环境执行 `build --buildcache auto|only`

不要对本环境再跑 `buildcache build`。共享数学/MPI/ML 引脚与 builtin commit 已与 authority 对齐；不假设与 CP2K opensource 轨共享 DAG hash。

## 构建与校验命令

```bash
./venv/bin/python -m hpc_cf validate --app-version abacus_opensource-3.10.1-force-avx512 --profile config
./venv/bin/python -m hpc_cf validate --app-version abacus_opensource-3.10.1-force-avx512
./venv/bin/python -m hpc_cf validate --app-version abacus_opensource-3.10.1-force-avx512 --profile assets
./venv/bin/python -m hpc_cf assets --env abacus_opensource-3.10.1-force-avx512 --verify-mirror
./venv/bin/python -m hpc_cf dockerfile --app-version abacus_opensource-3.10.1-force-avx512
./venv/bin/python -m hpc_cf build --app-version abacus_opensource-3.10.1-force-avx512 --buildcache auto
```

## 仓库验证结果

该 repo 的 validation/test 已通过，实际证据范围如下：

- Ruff：`./venv/bin/ruff check hpc_cf/ tests/ scripts/` 通过，0 errors。
- 默认 pytest：369 passed，32 skipped。
- 三种 validate：`config`、默认 `build-input`、`assets` 均通过。
- Mirror 校验：`assets --verify-mirror` 通过（present: 113，added: 0，failed: 0）。
- 模板渲染：该环境的 `dockerfile` 渲染通过。
- Integration：348/356 cases 通过，8 失败（见下）。
- Module unit tests：**BLOCKED**（镜像无 `share/abacus/tests`；见下）。

这些结果只覆盖上述仓库检查和该环境实际选中的应用测试，不代表所有上游 requirements 均已测试。不记录本地 SIF、artifact 或其他构建产物的状态、大小和 SHA256。

## 应用测试证据

### Integration

已提交的 [abacus-integration-test.log](../../spack-envs/abacus_opensource-3.10.1-force-avx512/abacus-integration-test.log)：

- 镜像无内置 `share/abacus/tests`（`tests=false`），故挂载 3.10.1-lts 源码树，入口为上游 `tests/integrate/Autotest.sh` + `CASES_CPU.txt`（356 cases）。
- 并行：4 MPI ranks × 2 OpenMP threads。
- Autotest 报告：5 failed + 3 fatal（相对 1619 property checks）。
- 按唯一 case 目录计：**348/356 passed**，**8 failed**（EXIT=1）。

失败清单：

| Case | 类别 | 说明 |
|------|------|------|
| `101_PW_15_paw` | accuracy/feature | PAW 未编译（日志：`compile with USE_PAW`） |
| `101_PW_upf201_uspp_NaCl` | accuracy/feature | force/stress 等偏差超阈值 |
| `102_PW_BPCG` | accuracy/feature | force/stress 偏差超阈值 |
| `102_PW_PINT_UKS` | accuracy/feature | 属性检查失败 |
| `107_PW_outWfcR` | accuracy/feature | 属性检查失败 |
| `212_NO_wfc_get_wf` | fatal | `catch_properties.sh`：缺少 `sum_ENV_H2_cube` 工具 |
| `312_NO_GO_wfc_get_wf` | fatal | 同上（Fatal Error in catch_properties.sh） |
| `312_NO_GO_wfc_get_pchg` | fatal | 同上 |

未覆盖：`tests/deepks/`（独立 `Autotest1.sh`）；stock `abacus_run_integration_tests.sh` 的 `01_PW`…`10_others` 分组在 3.10.1-lts 源码中不存在。

### Module unit tests — BLOCKED

已提交的 [abacus-module-test.log](../../spack-envs/abacus_opensource-3.10.1-force-avx512/abacus-module-test.log) 记录 blocker：

- Status：**BLOCKED**（Total/Passed/Failed 均为 0，未可执行）
- 原因：concrete spec `abacus@3.10.1-lts tests=false`；`MODULE_*` 二进制仅在 `+tests` 时安装；现有 OCI（含 `-installed` / `-builder`）均无 `share/abacus/tests`
- 解除条件：以 `abacus +tests` 重建 OCI 后重跑 `abacus_run_module_tests.sh`（本次收尾**不重建镜像**）

## 已知限制

- Module 证据在本发布中为 **BLOCKED**，不能外推 gtest 覆盖。
- Integration 8 项失败含明确配置缺口（无 PAW）与 harness 工具缺失（`sum_ENV_H2_cube`）；其余为数值/特征偏差。详见 [已知问题](../KNOWN_ISSUES.md)。
- 本环境是 consumer：依赖 3.9 producer 已发布且 coverage 健康；不要对本环境执行 `buildcache build`。
