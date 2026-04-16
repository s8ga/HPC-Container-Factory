# PLAN: streamline.sh 共享逻辑重构

**日期**: 2026-04-16
**目标**: 将两个 env 的 `streamline.sh` 中完全相同的 ~250 行逻辑抽出到 `scripts/spack-common.sh`，使每个 env 的 `streamline.sh` 精简为 ~15 行胶水代码。

## 前置分析

### 两个文件的差异

```bash
diff spack-envs/cp2k-opensource-2025.2/streamline.sh spack-envs/cp2k-rocm-2026.1-gfx942/streamline.sh
```

**结论**: 逻辑 100% 一致，差异全部在 `step_register_repos()` 的 debug logging 级别（opensource 多了几行 `_sc_info` 和验证确认）。

### 影响范围

| 外部调用方 | 调用方式 | 需改动？ |
|-----------|---------|:-------:|
| `scripts/build-mirror-in-container.sh` | `bash /work/spack-envs/<env>/streamline.sh <mode>` + 存在性检查 | ❌ |
| `generate.py` | 通过 `build-mirror-in-container.sh` 间接调用 | ❌ |
| `scripts/prepare-bootstrap-cache.sh` | 仅注释引用 | ❌ |
| `containers/Dockerfile.mirror-builder` | 仅注释引用 | ❌ |

**外部调用方零改动**。每个 env 仍保留 `streamline.sh` 文件（精简版），入口不变。

### 需清理的遗留

`scripts/spack-common.sh` 中有旧版 `install_system_pkgs()` 函数（使用 `PKG_MANAGER`/`INSTALL_ARGS` 旧接口），无任何调用方，需删除。

---

## 执行步骤

### Step 1: 扩充 `scripts/spack-common.sh`

**改动点**:

1. **删除**旧版 `install_system_pkgs()` 函数（~L38-53，使用 `PKG_MANAGER`/`INSTALL_ARGS`）
2. **新增** `streamline_parse_env()` — env.yaml 解析逻辑（从 streamline.sh L17-71 搬入）
3. **新增**以下 step 函数（从 streamline.sh 搬入，使用 opensource 的详细 logging 版本）：
   - `step_install_system_pkgs()` — 系统包安装
   - `step_clean_stale_repos()` — 清理残留 site-level repo 注册
   - `step_register_repos()` — 自定义 repo 注册（git sparse clone + local）
   - `step_find()` — 编译器/外部包发现
   - `step_concretize()` — concretize 并导出 spack.lock
4. **新增** `streamline_dispatch()` — main case/esac 调度逻辑
5. **更新**文件头注释

**函数依赖的变量**（由 `streamline_parse_env()` 设置，bash 延迟绑定，无问题）：

| 变量 | 来源 | 使用者 |
|------|------|--------|
| `SPACK_ENV_NAME` | env.yaml → spack.env_name | `step_concretize` |
| `SYSTEM_PKGS` | env.yaml → mirror_builder.system_pkgs | `step_install_system_pkgs` |
| `PKG_MIRROR_SETUP` | env.yaml → mirror_builder.pkg_mirror_setup | `step_install_system_pkgs` |
| `PKG_INSTALL_CMD` | env.yaml → mirror_builder.pkg_install_cmd | `step_install_system_pkgs` |
| `CUSTOM_REPOS[]` | env.yaml → spack.custom_repos | `step_register_repos` |
| `ENV_NAME` | 调用方设置 | `streamline_dispatch` |
| `ENV_DIR` | 调用方设置 | 多处 |
| `MIRROR_DIR` | 调用方设置 | `streamline_dispatch` |

**验证**: `bash -n scripts/spack-common.sh`

---

### Step 2: 精简 `spack-envs/cp2k-opensource-2025.2/streamline.sh`

**改动点**: 将整个文件替换为 ~15 行：

```bash
#!/bin/bash
# streamline.sh — per-env pipeline entrypoint
# All logic is in scripts/spack-common.sh. This file only sets env-specific paths.
set -euo pipefail

ENV_NAME="${ENV_NAME:?ENV_NAME not set}"
MIRROR_DIR="${MIRROR_DIR:-/work/assets/spack-mirror}"
ENV_DIR="/work/spack-envs/${ENV_NAME}"
MODE="${1:?Usage: streamline.sh <concretize|mirror|all|verify>}"

source /work/scripts/spack-common.sh
streamline_parse_env
streamline_dispatch "${MODE}"
```

**验证**:
1. `bash -n spack-envs/cp2k-opensource-2025.2/streamline.sh`
2. `python generate.py assets --env cp2k-opensource-2025.2` 运行 `concretize` 或 `status` 命令

---

### Step 3: 精简 `spack-envs/cp2k-rocm-2026.1-gfx942/streamline.sh`

**改动点**: 与 Step 2 完全相同的内容（因为逻辑已全部在 spack-common.sh 中）。

**验证**:
1. `bash -n spack-envs/cp2k-rocm-2026.1-gfx942/streamline.sh`

---

### Step 4: 语法验证 + 集成测试

**验证清单**:

```bash
# 1. 语法检查
bash -n scripts/spack-common.sh
bash -n spack-envs/cp2k-opensource-2025.2/streamline.sh
bash -n spack-envs/cp2k-rocm-2026.1-gfx942/streamline.sh

# 2. 集成测试 — opensource concretize
python generate.py assets --env cp2k-opensource-2025.2

# 3. 确认 build-mirror-in-container.sh 仍能正确调用
./scripts/build-mirror-in-container.sh -e cp2k-opensource-2025.2 status
```

**预期结果**: 所有命令正常执行，输出与重构前一致。

---

### Step 5: 更新相关文档

| 文档 | 改动 |
|------|------|
| `docs/ASSETS_GUIDE.md` L101 | 更新函数列表：删除 `install_system_pkgs`，新增 `step_*` 系列 |
| `scripts/spack-common.sh` 头部注释 | 更新 "This script is sourced by per-env streamline.sh scripts" → 补充说明包含所有 pipeline step 函数 |

---

## 不做的事情

- ❌ 不修改 `scripts/build-mirror-in-container.sh`
- ❌ 不修改 `generate.py`
- ❌ 不修改 `scripts/prepare-bootstrap-cache.sh`
- ❌ 不修改 `containers/Dockerfile.mirror-builder`
- ❌ 不修改 `docs/PLAN_per_env_dockerfile.md`（历史文档，保留原样）
