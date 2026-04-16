# PLAN: ROCm Dockerfile.j2 重构 — 对齐 Opensource 模板

**日期**: 2026-04-16
**目标**: 将 `spack-envs/cp2k-rocm-2026.1-gfx942/Dockerfile.j2` 重构为与 opensource 模板一致的结构，消除旧版 InfinityHub 遗留逻辑。

## 背景

当前 ROCm Dockerfile.j2 沿用了 InfinityHub CI 的构建模式：
- 手动 `rsync --copy-links` 扁平化 spack view
- Build ARG sed 替换版本号
- 全量 git clone cp2k 仓库
- 无 strip / 无 manifest

Opensource 模板已经完成了所有这些修复。

## 前置分析

### ROCm 特有约束（不可删除）

| 约束 | 原因 |
|------|------|
| ROCm 基础镜像 `rocm/dev-ubuntu-24.04:7.2.1-complete` | 提供 ROCm 7.2.1 运行时 |
| Ubuntu APT mirror（不是 Debian） | 基础镜像不同 |
| `spack external find` 发现 ROCm 外部包 | spack.yaml 中声明了 `buildable: false` |
| `ln -sf /opt/rocm /opt/rocm/hip` | BLT FindHIP.cmake 兼容性 |
| Runtime 需额外复制 ROCm 计算库 | spack view 不包含 `buildable: false` 的外部包 |
| `AMDGPU_TARGETS` Build ARG | 控制 GPU 架构目标 |
| `HIP_PLATFORM=amd` / `ROCM_PATH` ENV | ROCm 运行时必要环境变量 |

### 可对齐的部分

| 当前 ROCm 做法 | Opensource 做法（目标） | 说明 |
|---------------|----------------------|------|
| 全量 `git clone --depth 1 --recursive` | sparse checkout | 节省 ~1GB+ |
| Build ARG sed 替换 spack.yaml 版本号 | 无 sed，版本在 spack.yaml 硬编码 | 版本已固定，ARG 多余 |
| `rsync --copy-links` 扁平化 view | `spack env view enable` + Docker COPY | 正确的 symlink farm |
| 无 strip | strip .so + ELF executables | 节省空间 |
| 无 manifest | 生成 BUILD_MANIFEST.txt + DependencyGraph.dot | 调试信息 |
| 无 `spack gc` | install 后 `spack gc -y` | 清理构建缓存 |
| `COPY /opt/runtime-export/*` 旧结构 | `COPY /opt/spack` + `/opt/spack-view` | 统一布局 |

---

## 执行步骤

### Step 1: 重写 Stage 1 (Builder)

**文件**: `spack-envs/cp2k-rocm-2026.1-gfx942/Dockerfile.j2`

**删除的块**:

1. **Build ARG 版本号 sed 替换**（~L143-155）
   ```dockerfile
   # 删除整个 "Map upstream-style build args to concrete Spack versions" RUN 块
   ```
   原因：版本已在 `spack.yaml` 中硬编码，不需要运行时 sed 替换。

2. **手动 `rsync --copy-links` 导出块**（~L193-230）
   ```dockerfile
   # 删除整个 "Export runtime payloads with dereferenced view files" RUN 块
   ```
   原因：改用 `spack env view enable`。

3. **全量 git clone**（~L183）
   ```dockerfile
   # 删除: git clone --depth 1 --recursive -b support/v2026.1 ...
   ```

**新增/修改的块**:

4. **Sparse git clone**（替换全量 clone）
   ```dockerfile
   RUN git clone --filter=blob:none --no-checkout -b v2026.1 \
           https://github.com/cp2k/cp2k.git /opt/cp2k && \
       cd /opt/cp2k && \
       git sparse-checkout init --cone && \
       git sparse-checkout set tests src/grid/sample_tasks tools/regtesting && \
       git checkout && \
       echo "✅ Sparse checkout done: $(du -sh /opt/cp2k | cut -f1)"
   ```

5. **Spack env 路径统一**
   - `COPY spack-envs/cp2k-rocm-2026.1-gfx942 /opt/hpc-env-file` 保持不变（路径是命名约定，非关键）

6. **Spack install 后加 gc**
   ```dockerfile
   spack -e /opt/hpc-env install --fail-fast -j "${JOBS}"; \
   spack -e /opt/hpc-env gc -y
   ```

7. **View enable**（替换 rsync 导出）
   ```dockerfile
   RUN spack -e /opt/hpc-env env view enable /opt/spack-view
   ```

8. **Strip debug symbols**（同 opensource）
   ```dockerfile
   RUN BEFORE=$(du -sm /opt/spack/ | cut -f1) && \
       echo "=== Stripping debug symbols (${BEFORE}MB) ===" && \
       find /opt/spack -type f \( -name '*.so' -o -name '*.so.*' \) \
           -exec strip --strip-unneeded {} \; 2>/dev/null || true && \
       find /opt/spack -path '*/bin/*' -type f -print0 2>/dev/null \
           | xargs -0 -r file 2>/dev/null \
           | grep 'ELF.*executable' | cut -d: -f1 \
           | xargs -r strip --strip-all 2>/dev/null || true && \
       AFTER=$(du -sm /opt/spack/ | cut -f1) && \
       echo "✅ Stripped: ${BEFORE}MB → ${AFTER}MB (saved $((BEFORE - AFTER))MB)"
   ```

9. **Build manifest**（同 opensource）

**保留不变的块**:
- ROCm base image FROM
- Ubuntu APT mirror 配置
- 系统包安装列表
- Spack 安装 + bootstrap 配置
- `spack compiler find` + `spack external find`
- ROCm symlink hack (`ln -sf /opt/rocm /opt/rocm/hip`)
- `ln -sf /usr/lib/x86_64-linux-gnu/libz.so /usr/lib/libz.so`
- `AMDGPU_TARGETS` / `SPACK_MAKE_JOBS` Build ARG（保留用于灵活性）
- Custom repo 注册

**验证**: 语法检查 + 视觉审阅 builder stage

---

### Step 2: 重写 Stage 2 (Runtime)

**删除的块**:

1. **Build ARGs 重复声明**（runtime stage 不需要 `UCX_BRANCH` 等）
2. **旧路径 COPY**（`/opt/runtime-export/*`）
3. **`OPAL_PREFIX` / `OMPI_PRTERUN` hack**（改用 view 后不再需要）

**新增的块**:

4. **ENV 对齐 opensource 结构**
   ```dockerfile
   ENV DEBIAN_FRONTEND=noninteractive \
       PATH="/opt/spack-view/bin:/opt/cp2k-runtime/bin:/opt/bin:$PATH" \
       LD_LIBRARY_PATH="/opt/spack-view/lib:/opt/rocm/lib:$LD_LIBRARY_PATH" \
       CP2K_DATA_DIR="/opt/spack-view/share/cp2k/data" \
       ROCM_PATH="/opt/rocm" \
       HIP_PLATFORM="amd"
   ```

5. **COPY 块重写**
   ```dockerfile
   # 1. Spack install tree
   COPY --from=builder --chown=hpc:hpc /opt/spack /opt/spack
   # 2. Spack view (symlinks)
   COPY --from=builder --chown=hpc:hpc /opt/spack-view /opt/spack-view
   # 3. CP2K test files (sparse checkout)
   COPY --from=builder --chown=hpc:hpc /opt/cp2k/tests /opt/cp2k/tests
   COPY --from=builder --chown=hpc:hpc /opt/cp2k/src/grid/sample_tasks /opt/cp2k/src/grid/sample_tasks
   COPY --from=builder --chown=hpc:hpc /opt/cp2k/tools/regtesting /opt/cp2k/tools/regtesting
   # 4. ROCm 计算库（不在 spack install tree 中，需单独复制）
   COPY --from=builder /opt/rocm/lib /opt/rocm/lib
   ```

6. **ldconfig 简化**
   ```dockerfile
   RUN { echo "/opt/spack-view/lib"; echo "/opt/rocm/lib"; } > /etc/ld.so.conf.d/cp2k.conf && \
       ldconfig
   ```

7. **APT 包精简**（同 opensource，移除编译器，保留运行时库 + ROCm 必要包）

**保留不变的块**:
- `FROM {{ runtime_base_image }} AS runtime`
- Ubuntu APT mirror 配置
- `useradd -m -s /bin/bash hpc`
- Static library cleanup
- Workspace directory

**验证**: 语法检查 + 对比 opensource runtime stage 确认结构一致

---

### Step 3: 生成 + 语法验证

```bash
# 1. 生成 Dockerfile
python generate.py dockerfile --app-version rocm-2026.1-gfx942 --output /tmp/Dockerfile.rocm

# 2. 语法检查
dockerfile-lint /tmp/Dockerfile.rocm || true

# 3. 对比两个模板的结构一致性
diff <(grep -E '^(FROM|COPY|RUN|ENV|ARG|WORKDIR|USER|LABEL)' /tmp/Dockerfile.opensource | sed 's/cp2k-opensource/cp2k-rocm/g') \
     <(grep -E '^(FROM|COPY|RUN|ENV|ARG|WORKDIR|USER|LABEL)' /tmp/Dockerfile.rocm | sed 's/cp2k-rocm/cp2k-opensource/g')
```

---

### Step 4: 更新文档

| 文档 | 改动 |
|------|------|
| `docs/TEMPLATE_MATRIX.md` | 更新 ROCm 模板说明：移除 "rsync copy-links" 描述 |

---

## 不做的事情

- ❌ 不修改 `env.yaml`（基础镜像配置不变）
- ❌ 不修改 `spack.yaml`（依赖声明不变）
- ❌ 不修改 `streamline.sh`（那是另一个重构计划）
- ❌ 不修改 `generate.py`
- ❌ 不删除 `AMDGPU_TARGETS` Build ARG（保留灵活性）
- ❌ 不删除 ROCm symlink hack（运行时需要）

## 风险点

| 风险 | 缓解 |
|------|------|
| spack-view 中 ROCm 外部库的 symlink 指向 `/opt/rocm` | runtime stage COPY `/opt/rocm/lib` 确保目标存在 |
| OpenMPI OPAL_PREFIX 在 view 模式下可能不正确 | spack view 自动设置正确前缀；如有问题可恢复 `OPAL_PREFIX` |
| ROCm kernel data dirs（rocblas/library, hipblaslt/library）需要复制 | 在 COPY `/opt/rocm/lib` 中已包含子目录 |
