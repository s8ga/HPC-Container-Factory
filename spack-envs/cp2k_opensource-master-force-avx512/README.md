# CP2K master（浮动轨道，force-avx512）

本环境跟踪 CP2K 上游 `master` 分支，使用 Spack 1.2.0、OpenMPI 5.0.10、OpenBLAS、
`x86_64_v3` target，并通过 force-avx512 overrides 启用相关 CPU kernels。
与 2026.2 环境的关键区别：**spec 是 `cp2k@master`，`spack.lock` 永不提交**，
由 nightly 通道每次 `assets --allow-concretize` 重新解算。

## Commit 变更机制（spack 1.2.0 实测）

浮动与钉住共用同一套 version 语义，已用最小 git-branch 包在 spack 1.2.0 上验证：

- **浮动（默认，nightly）**：spec 写 `cp2k@master`，无需任何人填 commit。
  每次 re-concretize 时 Spack 解析 `master` 分支头，把解析出的 40 位 commit
  写进 `spack.lock` 的 `parameters.commit`，且 commit 参与 DAG hash——上游前进一步，
  下一次解算即得到新 hash 与新 lock。每次运行的 lock 就是该次构建的钉住记录。
- **钉住（按需，dispatch 输入）**`gh workflow run nightly-cp2k.yml
  -f env_name=cp2k_opensource-master-force-avx512 -f commit=<40位sha>`。
  流水线对**临时 checkout** 的 spack.yaml 做 sed 注入 ` commit=<sha>`
  （仓库源码保持浮动，不产生提交）。实测该形式与"当时 head 恰好在该 commit 上的
  浮动解算"产生**完全相同的 DAG hash**——即钉住重跑可以直接命中浮动轨道已发布
  的 buildcache 条目，且 version 身份仍是 `master`，package.py 中的
  `satisfies("@...")` 版本区间逻辑不受影响。用途：上游弄坏 master 时钉在
  上一个好 commit、或对任意 commit 做 bisect。不填输入即恢复浮动。
- **不要用** `cp2k@master=<sha>`：spack 1.2.0 spec 解析器直接报
  SpecTokenizationError。`cp2k@git.<sha>` 虽可解析，但 version 身份变为
  `git.<sha>`，与浮动轨道 hash 不一致且可能干扰版本区间判断，同样不采用。

## 配置入口

- `spack-env-file/env.yaml`：镜像、Spack 版本、自定义 repo 和模板变量。
  cp2k_dev **recipe repo**（tblite/gauxc/libint/pace 的来源命名空间）是
  **工厂级浮动**：env.yaml 不写 `commit`，assets 每次拉取分支 tip 并把解析出
  的 sha 记录到 `spack-env-file/resolved-repos.yaml`（与 lock 同一"assets
  产生、build 只读消费"契约）；build/render 自动应用该 pin（repo commit +
  `cp2k_dev_repo_commit` 模板变量），镜像侧克隆与解算器看到同一个 sha——
  不会出现同 run 内两侧各自拉 tip 的竞态，记录可重放。没有 sidecar 时回退
  到 env.yaml 的静态模板变量（本地手动渲染兜底）。
- `spack-env-file/spack.yaml`：CP2K spec、依赖版本、variants 和 builtin repo
  pin（master 轨道跟踪 spack-packages 最新 tip）。
- `Dockerfile.j2`：该环境的容器模板。
- `spack-env-file/spack.lock`：不存在是常态（浮动轨道）；只有钉住构建才会临时产生。

## 验证与构建

在仓库根目录执行（无 lock 时 build/validate 按设计 fail-closed，需先跑 assets）：

```bash
./venv/bin/python -m hpc_cf validate --app-version cp2k_opensource-master-force-avx512 --profile config
./venv/bin/python -m hpc_cf dockerfile --app-version cp2k_opensource-master-force-avx512
# 重新解算（浮动轨道专用；会改写本地 spack.lock，但该文件不提交）
./venv/bin/python -m hpc_cf assets --env cp2k_opensource-master-force-avx512 --allow-concretize
```

CI 入口：`gh workflow run nightly-cp2k.yml -f env_name=cp2k_opensource-master-force-avx512`
（nightly 通道自动附带 `--allow-concretize`）。

## MPI/OpenMP 使用建议

```bash
export OMP_NUM_THREADS=2
mpirun --bind-to none -np 2 cp2k.psmp -i input.inp
```

master 轨道不承诺上游 regtest 全绿（上游自身可能在任意推进中破坏构建或测试）；
SGLIB 等对混合 MPI/OpenMP 分解敏感的注意事项与 2026.2 环境相同。
生产计算前应以实际输入、节点拓扑和目标分解做短程验证。

## Skala 模型

GauXC 以 `+skala skala_version=1.1` 构建。模型路径由环境变量提供：

```bash
echo "$GAUXC_SKALA_MODEL"
# /opt/spack-view/share/gauxc/onedft_models/skala-1.1.fun
```

容器模板会在构建期间检查该文件存在。只有 `GAUXC_SKALA_MODEL` 非空时，
交互式 MOTD 才显示 `Skala Model` 行。
