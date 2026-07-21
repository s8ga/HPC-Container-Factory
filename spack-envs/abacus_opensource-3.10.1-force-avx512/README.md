# ABACUS 3.10.1 Opensource（force-avx512）

本环境使用 Spack 1.2.0 构建 ABACUS 3.10.1-lts，采用 OpenMPI 5.0.10、OpenBLAS 0.3.33 和 `x86_64_v3` target，并通过 force-avx512 / force_all_x86_kernel overrides 启用相关 CPU kernels。

在 ABACUS opensource CPU buildcache 轨中，本环境是 **consumer**：共享依赖与 authority 环境 `abacus_opensource-3.9.0.27-force-avx512` 对齐后，以 `--buildcache auto|only` 消费其已发布二进制；不要对本环境再跑 `buildcache build` producer。

## 配置入口

- `spack-env-file/env.yaml`：镜像、Spack 版本、自定义 repo 和模板变量。
- `spack-env-file/spack.yaml`：ABACUS spec、依赖版本、variants 和 builtin repo pin。
- `spack-env-file/spack.lock`：已 concretize 的依赖图。
- `Dockerfile.j2`：该环境的容器模板。
- [发布说明](../../docs/releases/ABACUS_3.10.1.md)：验证范围、应用测试结果和已知限制。

## 验证与构建

在仓库根目录执行：

```bash
./venv/bin/python -m hpc_cf validate --app-version abacus_opensource-3.10.1-force-avx512 --profile config
./venv/bin/python -m hpc_cf validate --app-version abacus_opensource-3.10.1-force-avx512
./venv/bin/python -m hpc_cf validate --app-version abacus_opensource-3.10.1-force-avx512 --profile assets
./venv/bin/python -m hpc_cf assets --env abacus_opensource-3.10.1-force-avx512 --verify-mirror
./venv/bin/python -m hpc_cf dockerfile --app-version abacus_opensource-3.10.1-force-avx512
./venv/bin/python -m hpc_cf build --app-version abacus_opensource-3.10.1-force-avx512 --buildcache auto
```

默认 build-input 校验要求环境声明的 assets 可用，并只读消费现有非空 `spack.lock`。不要删除 lock 后直接构建；需要重新 concretize 时应通过 assets 工作流显式执行（先移除旧 `spack.lock`，再 `--allow-concretize`）。

## 应用测试入口

本环境 abacus spec 已启用 `+tests`；OCI 含 `share/abacus/tests`（含 padded install 前缀）。`abacus_run_*.sh` 均用短路径 glob + `find …/share/abacus/tests` 兜底。

- **Module**：`abacus_run_module_tests.sh`（`TOTAL==0` 非零退出）。证据已入库（213/221；日志经 harness 截断）。
- **Integration**：`abacus_run_integration_tests.sh` 走扁平 `integrate/Autotest.sh` + `CASES_CPU.txt`（与发布证据一致）；**不是** `01_PW`…`10_others` 分组。全量 Autotest 有已知失败，属发布证据而非 L4 门禁。
- **L4**（opt-in）：consumer build + padded 探测 + Autotest 入口存在性；不跑全量 Autotest。

```bash
# Integration (flat Autotest; long-running; may exit non-zero on known fails)
podman run --rm --network=host \
  -v "$PWD/spack-envs/abacus_opensource-3.10.1-force-avx512/abacus_run_integration_tests.sh:/tmp/run_tests.sh:ro" \
  abacus_opensource:3.10.1-force-avx512 bash /tmp/run_tests.sh

# Module unit tests
podman run --rm --network=host \
  -v "$PWD/spack-envs/abacus_opensource-3.10.1-force-avx512/abacus_run_module_tests.sh:/tmp/run_tests.sh:ro" \
  abacus_opensource:3.10.1-force-avx512 bash /tmp/run_tests.sh
```

通过/失败数字、Autotest 失败清单与 module 失败清单见[发布说明](../../docs/releases/ABACUS_3.10.1.md)。
