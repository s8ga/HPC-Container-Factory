# ABACUS 3.9.0.27 Opensource（force-avx512）

本环境使用 Spack 1.2.0 构建 ABACUS 3.9.0.27，采用 OpenMPI 5.0.10、OpenBLAS 0.3.33 和 `x86_64_v3` target，并通过 force-avx512 / force_all_x86_kernel overrides 启用相关 CPU kernels。

在 ABACUS opensource CPU buildcache 轨中，本环境是 **authority / producer**：先 `buildcache build` 发布，再由 `abacus_opensource-3.10.1-force-avx512` 以 `--buildcache auto|only` 消费。

## 配置入口

- `spack-env-file/env.yaml`：镜像、Spack 版本、自定义 repo 和模板变量。
- `spack-env-file/spack.yaml`：ABACUS spec、依赖版本、variants 和 builtin repo pin。
- `spack-env-file/spack.lock`：已 concretize 的依赖图。
- `Dockerfile.j2`：该环境的容器模板。
- [发布说明](../../docs/releases/ABACUS_3.9.0.27.md)：验证范围、应用测试结果和已知限制。

## 验证与构建

在仓库根目录执行：

```bash
./venv/bin/python -m hpc_cf validate --app-version abacus_opensource-3.9.0.27-force-avx512 --profile config
./venv/bin/python -m hpc_cf validate --app-version abacus_opensource-3.9.0.27-force-avx512
./venv/bin/python -m hpc_cf validate --app-version abacus_opensource-3.9.0.27-force-avx512 --profile assets
./venv/bin/python -m hpc_cf assets --env abacus_opensource-3.9.0.27-force-avx512 --verify-mirror
./venv/bin/python -m hpc_cf dockerfile --app-version abacus_opensource-3.9.0.27-force-avx512
./venv/bin/python -m hpc_cf build --app-version abacus_opensource-3.9.0.27-force-avx512 --buildcache auto
```

发布 buildcache（producer）时使用：

```bash
./venv/bin/python -m hpc_cf buildcache build --env abacus_opensource-3.9.0.27-force-avx512
```

默认 build-input 校验要求环境声明的 assets 可用，并只读消费现有非空 `spack.lock`。不要删除 lock 后直接构建；需要重新 concretize 时应通过 assets 工作流显式执行。

## 应用测试入口

容器内可用脚本（挂载到镜像中运行）。脚本支持短路径 glob + padded `find …/share/abacus/tests` 兜底；module runner 在 `TOTAL==0` 时非零退出。

```bash
# Integration（01_PW … 10_others 分组；3.9 布局）
podman run --rm --network=host \
  -v "$PWD/spack-envs/abacus_opensource-3.9.0.27-force-avx512/abacus_run_integration_tests.sh:/tmp/run_tests.sh:ro" \
  abacus_opensource:3.9.0.27-force-avx512 bash /tmp/run_tests.sh

# Module unit tests
podman run --rm --network=host \
  -v "$PWD/spack-envs/abacus_opensource-3.9.0.27-force-avx512/abacus_run_module_tests.sh:/tmp/run_tests.sh:ro" \
  abacus_opensource:3.9.0.27-force-avx512 bash /tmp/run_tests.sh
```

已提交日志：integration 10/10；module 232/238（6 fail，上游/依赖限制）。Harness 已跳过假二进制并分级超时/截断日志。详细结果与失败清单见[发布说明](../../docs/releases/ABACUS_3.9.0.27.md)。
