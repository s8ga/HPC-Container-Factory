# ABACUS 3.9.0.27 发布说明

## 发布范围

- 环境：`abacus_opensource-3.9.0.27-force-avx512`
- ABACUS：3.9.0.27
- Spack：1.2.0
- MPI：OpenMPI 5.0.10
- CPU target：`x86_64_v3`
- BLAS/LAPACK：OpenBLAS 0.3.33，OpenMP threads，启用动态分派和 force-avx512
- Buildcache 角色：**authority / producer**（ABACUS opensource CPU 轨）

权威配置与追溯证据：

- [env.yaml](../../spack-envs/abacus_opensource-3.9.0.27-force-avx512/spack-env-file/env.yaml)
- [spack.yaml](../../spack-envs/abacus_opensource-3.9.0.27-force-avx512/spack-env-file/spack.yaml)
- [spack.lock](../../spack-envs/abacus_opensource-3.9.0.27-force-avx512/spack-env-file/spack.lock)
- [Dockerfile.j2](../../spack-envs/abacus_opensource-3.9.0.27-force-avx512/Dockerfile.j2)
- [Integration 测试日志](../../spack-envs/abacus_opensource-3.9.0.27-force-avx512/abacus-integration-test.log)
- [Module 测试日志](../../spack-envs/abacus_opensource-3.9.0.27-force-avx512/abacus-module-test.log)

ABACUS spec 启用了 `deepmd`、`mlalgo`、`elpa`、`lcao`、`libri`、`libxc`、`mpi`、`openmp`、`pexsi`、`nep`、`tests`、`rapidjson` 等功能；完整 spec 和依赖版本以 `spack.yaml` 与 `spack.lock` 为准。自定义 repo 来自 s8ga monorepo（`spack_repo/abacus` + `spack_repo/s8_overrides`），commit pin 见 `env.yaml`。

## 构建与校验命令

```bash
./venv/bin/python -m hpc_cf validate --app-version abacus_opensource-3.9.0.27-force-avx512 --profile config
./venv/bin/python -m hpc_cf validate --app-version abacus_opensource-3.9.0.27-force-avx512
./venv/bin/python -m hpc_cf validate --app-version abacus_opensource-3.9.0.27-force-avx512 --profile assets
./venv/bin/python -m hpc_cf assets --env abacus_opensource-3.9.0.27-force-avx512 --verify-mirror
./venv/bin/python -m hpc_cf dockerfile --app-version abacus_opensource-3.9.0.27-force-avx512
./venv/bin/python -m hpc_cf build --app-version abacus_opensource-3.9.0.27-force-avx512 --buildcache auto
./venv/bin/python -m hpc_cf buildcache build --env abacus_opensource-3.9.0.27-force-avx512
```

## 仓库验证结果

该 repo 的 validation/test 已通过，实际证据范围如下：

- Ruff：`./venv/bin/ruff check hpc_cf/ tests/ scripts/` 通过，0 errors。
- 默认 pytest：369 passed，32 skipped。
- 三种 validate：`config`、默认 `build-input`、`assets` 均通过。
- Mirror 校验：`assets --verify-mirror` 通过（present: 115，added: 0，failed: 0）。
- 模板渲染：该环境的 `dockerfile` 渲染通过。
- Integration：10/10 组通过（见下）。
- Module unit tests：227/241 通过，14 失败（见下）。

这些结果只覆盖上述仓库检查和该环境实际选中的应用测试，不代表所有上游 requirements 均已测试。不记录本地 SIF、artifact 或其他构建产物的状态、大小和 SHA256。

## 应用测试证据

### Integration

已提交的 [abacus-integration-test.log](../../spack-envs/abacus_opensource-3.9.0.27-force-avx512/abacus-integration-test.log)：

- 入口：`abacus_run_integration_tests.sh`（目录 `01_PW` … `10_others`）
- 并行：4 MPI × 若干 OpenMP（日志记录各组运行）
- **10/10 passed**，0 failed，0 skipped（总耗时约 1243s）

### Module unit tests

已提交的 [abacus-module-test.log](../../spack-envs/abacus_opensource-3.9.0.27-force-avx512/abacus-module-test.log)：

- 入口：`abacus_run_module_tests.sh`
- 镜像内路径：`share/abacus/tests`（spec 含 `+tests`）
- **227/241 passed**，**14 failed**（总耗时约 339s）

失败清单（按日志 Summary）：

| 测试名 | 退出码 / 特征 | 说明 |
|--------|---------------|------|
| `INPUT` / `KPT` / `STRU` | rc=127 | 非 gtest 可执行文件；shell 当二进制跑导致 “not found” |
| `MODULE_BASE_clebsch_gordan_coeff_test` | rc=1 | `ClebschGordanTest.ClebschGordan` 失败 |
| `MODULE_BASE_cubic_spline` | rc=134 | assertion abort（插值点越界） |
| `MODULE_BASE_matrix3` | rc=1 | `Matrix3Test.Inverse` 失败 |
| `MODULE_ESOLVER_esolver_dp_test` | rc=139 | DeePMD TensorFlow backend 未构建 → 异常后 segfault |
| `MODULE_HSOLVER_LCAO` | rc=124 | 30s timeout（`timeout 30`） |
| `MODULE_HSOLVER_LCAO_PEXSI` | rc=1 | PEXSI vs ref 差值超阈值（约 0.003 vs 0.0005） |
| `MODULE_HSOLVER_cg` / `dav` / `dav_float` / `dav_real` | rc=124 | 30s timeout |
| `test_deepks` | rc=1 | DeePKS 相关失败 |

完整边界与建议见 [已知问题](../KNOWN_ISSUES.md)。

## Buildcache 轨说明

本环境为 **authority / producer**。发布流程：

1. 本环境执行 `buildcache build`（或 `buildcache resume`）发布二进制到全局 `assets/spack-buildcache/`。
2. Consumer `abacus_opensource-3.10.1-force-avx512` 以 `build --buildcache auto|only` 消费（见 [ABACUS 3.10.1](ABACUS_3.10.1.md)）。

不要假设与 CP2K opensource 轨共享 DAG hash。共享数学/MPI/ML 引脚与 builtin commit 已与 consumer 对齐。

## 已知限制

- Module 套件中有 14 项失败：部分为非可执行脚本被误跑（`INPUT`/`KPT`/`STRU`）、部分为 30s 超时、部分为 DeePMD/DeePKS/PEXSI 数值或后端限制；**不阻塞** integration 10/10 与 buildcache producer 角色。
- Integration 覆盖的是 `abacus_run_integration_tests.sh` 所选 10 组目录，不是完整上游 Autotest 全集。
- 生产运行应先用目标输入验证计划采用的 MPI/OpenMP 分解与功能路径（尤其 DeePMD / PEXSI）。

详见 [已知问题](../KNOWN_ISSUES.md)。
