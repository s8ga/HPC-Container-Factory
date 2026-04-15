# CP2K InfinityHub 参数引入计划（含 Credit）

更新时间：2026-04-14
状态：Draft（待实施）

## 1. 背景与目标

目标是把 AMD InfinityHub-CI 已验证过的 CP2K Docker + Spack 构建参数直接引入到本仓库的构建系统中，并保证：

- 尽量复用上游已验证参数，减少试错成本。
- 重点吸收 cp2k_environment/repos 与 spack.yaml 的做法。
- 保留清晰、可追溯的来源 credit。
- 保持我们当前策略：ROCm/HIP 组件优先 external discovery，不强制由 Spack 下载构建。

## 2. 上游来源（必须保留 Credit）

上游来源仓库：

- https://github.com/amd/InfinityHub-CI

本次重点参考目录：

- https://github.com/amd/InfinityHub-CI/tree/main/cp2k/docker
- https://github.com/amd/InfinityHub-CI/tree/main/cp2k/docker/cp2k_environment

本仓库对应快照位置：

- docs/cp2k/docker/
- docs/cp2k/docker/cp2k_environment/

## 3. 直接采用的已验证构建参数

计划作为默认参数引入（保持与上游一致）：

- IMAGE=rocm/dev-ubuntu-24.04:7.0-complete
- UCX_BRANCH=v1.19.0
- UCC_BRANCH=v1.5.1
- OMPI_BRANCH=v5.0.8
- AMDGPU_TARGETS=gfx908,gfx90a,gfx942
- CP2K_BRANCH=v2026.1

说明：

- 上述参数先作为兼容基线。
- 如需继续使用本仓库 7.2.1 路线，可在后续通过可选参数覆盖，不影响默认上游对齐。

## 4. 迁移重点（repos 与 spack.yaml）

### 4.1 cp2k_environment/repos 引入策略

计划把以下内容从文档快照转为“可执行输入”而非仅文档：

- docs/cp2k/docker/cp2k_environment/repos/repo.yaml
- docs/cp2k/docker/cp2k_environment/repos/packages/cp2k/package.py
- docs/cp2k/docker/cp2k_environment/repos/packages/dbcsr/package.py
- docs/cp2k/docker/cp2k_environment/repos/packages/libvdwxc/package.py
- 同目录 patch 文件

落地方式（计划）：

- 新建可执行目录：spack-envs/cp2k-2026.1-rocm/upstream-repos/
- 将上游 repos 内容拷贝到该目录。
- 在本仓库 spack 环境中通过 repos 字段显式启用该目录。

### 4.2 spack.yaml 融合策略

以 docs/cp2k/docker/cp2k_environment/spack.yaml 为语义参考，对当前文件进行融合：

- 当前：spack-envs/cp2k-2026.1-rocm/spack.yaml
- 目标：保留我们已验证的 Stage1 约束，同时引入上游 cp2k spec 与 repos 机制。

融合原则：

- 保留我们已验证的约束（例如某些 Fortran 包强制 gcc、ELPA/DLAF 路线决策）。
- 吸收上游 cp2k spec 关键变体组合与包版本意图。
- 引入 include config.yaml + repos 的结构化方式。

## 5. HIP discovery（不强制下载 HIP 包）

落实方式：

- 在 spack.yaml 中，不把 hip/hipblas/hipfft/hipcc 作为必须编译的 specs。
- 通过 packages.externals + buildable: false 指向 /opt/rocm（或镜像内路径）。
- 在执行脚本里统一先跑：spack external find --all --not-buildable

效果：

- 优先使用基础镜像自带 ROCm 组件。
- 避免重复下载/构建 hip 系列包。

## 6. 构建入口改造计划

计划新增或改造以下入口：

- templates/Dockerfile-cp2k-rocm-2026.1-stage1
  - 增加 InfinityHub 同款 build args（IMAGE/UCX/UCC/OMPI/AMDGPU_TARGETS/CP2K_BRANCH）。
  - 将 cp2k_environment/repos 纳入可执行构建上下文。

- 新增脚本（建议）scripts/cp2k/spack.sh
  - 参考上游 spack.sh 执行顺序：env activate -> external find -> concretize -> install。
  - 适配本仓库路径和镜像约束。

- generate.py 与 docs/GENERATE_CLI.md
  - 增加 cp2k rocm 2026.1 模板入口与参数说明。

## 7. Credit 保留实施项

必须做：

- 在新增可执行目录添加 README_SOURCE.md，记录：
  - 来源仓库/目录 URL
  - 引入日期
  - 本地改动列表

- 在相关文件头增加简短来源说明（不删除原许可信息）。

- 新增文档：docs/cp2k/UPSTREAM_CREDITS.md
  - 明确感谢 AMD InfinityHub-CI 团队。
  - 列出对应路径映射与改动范围。

## 8. 实施步骤（按顺序）

1. 建立 upstream-repos 可执行目录并拷贝 docs/cp2k 快照内容。
2. 调整 spack-envs/cp2k-2026.1-rocm/spack.yaml：引入 repos、融合 cp2k spec。
3. 新增/改造 spack 执行脚本，固定 external discovery 流程。
4. 改造 Stage1 模板，接入上游 build args。
5. 跑最小验证（concretize + install + find cp2k）。
6. 补全 credit 文档并在 README 中链接。

## 9. 验证清单

- spack env activate 成功。
- spack external find 可识别 ROCm 组件。
- spack concretize -f 成功。
- spack install 完成关键 specs。
- 可生成或定位 cp2k.psmp（或 Stage1 对应产物）。
- 文档中能追溯 credit 来源。

## 10. 风险与注意事项

- 上游 spack.yaml 默认是 SIRIUS 全功能链路，和我们当前无 SIRIUS 路线存在差异，需要在融合时显式取舍。
- docs/cp2k/docker/README.md 当前包含冲突标记，不能直接作为执行文档发布，需清理后再引用。
- 上游参数以 ROCm 7.0 验证为主，若继续使用 7.2.1 基镜像需做一次兼容回归。
