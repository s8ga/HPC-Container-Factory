# Handoff：HPC-Container-Factory Fragility 审计（人类可用性专项）

- 日期：2026-11-01
- 方法：1 轮主线人工追踪 + 3 个并行只读探查 agent（配置契约 / 人类工作流 / 模板漂移）
- 状态：**只审计，未改动任何代码**。所有发现附 file:line 证据
- 给接手 agent 的任务：对下列发现做修复方案设计（§4 已给方向骨架），重点是
  评级、拆任务、判断哪些是"删代码"哪些是"加校验"

---

## 0. 一句话诊断

工具的 fail-closed 哲学（schema 校验、StrictUndefined、lock 门禁）方向正确，
但**所有契约只验存在性、不验一致性**；高频变更操作（升 pin / 升版本）无原子
命令，要求人类手改 3-7 处并自觉跑守卫脚本；且关键运维 doctrine 三层割裂——
一部分在人类文档、一部分只在 AGENTS.md（AI 指南）、一部分只在 CLI help。

---

## 1. 背景

- 仓库：`~/HPC-Container-Factory`（hpc_cf/ Python 工具 + spack-envs/ 13 个环境 + templates/partials/ 24 个共享 partial）
- 配套：`~/s8ga-spack-packages`（spack recipe monorepo，被 env.yaml 的
  custom_repos + template_vars 双机制 pin，当前 pin `faf9ef86…`）
- 触发本次审计的实例：升级该 pin 时发现同一 commit 在每个 env.yaml 里手抄
  3 份（custom_repos ×2 + template_vars），引发"双重添加是否 fragile"的追问

---

## 2. 核心发现（三方去重合并，按严重度）

### P0 — 静默错误 / 最晚爆炸

**2.1 commit-pin 三写，守卫脚本存在但不在 CI，入口只写在 AGENTS.md**
- `spack-envs/vasp_mkl-6.6.1-avx512/spack-env-file/env.yaml:78,84,103`（三份
  同值）；abacus 同型（`abacus_opensource-3.10.1-force-avx512/.../env.yaml:79,85,95`）
- 守卫 `scripts/check-dual-write.py:257-301` 存在，但 5 个 GitHub workflows
  均未调用（pipeline-cp2k.yml:224 只跑 `validate --profile config`，不含
  dual-write）；唯一执行入口记载于 `AGENTS.md:280` = "靠 agent 自觉手动跑"
- 失配后果：assets 按新 pin concretize，镜像 clone 旧 pin → recipe 与 lock
  不一致，**多数情况静默**（变体/patch 漂移），坏情况在数小时构建后爆

**2.2 改 spack.yaml 但 lock 是旧的——全链路预检放行**
- `hpc_cf/validation.py:463-511`（`is_nonempty_spack_lock` 只查存在+非空）；
  `workflows.py:229-235`；`templates/partials/spack_env_create.j2:5-16`（镜像内
  无条件 cp 旧 lock）。全仓库无 spack.yaml↔lock 一致性比较
- 后果：`validate`/`dockerfile`/`build` 全绿，镜像内 `spack install
  --only-concrete` **静默构建旧依赖图**——可能永远不报错
- 修法提示：spack.lock 本身存有 concretize 时的环境 hash，可比对当前
  spack.yaml 后给出"请重跑 assets --allow-concretize"

**2.3 floating-repo doctrine 对变量名"撞对"才生效；rocm env 今天就失效**
- 注入逻辑 `hpc_cf/environment.py:1026`（`{namespace}_repo_commit`），但
  `spack-envs/cp2k_rocm-2026.1-gfx942/Dockerfile.j2:93` 裸 `-b {{cp2k_branch}}`
  clone，不消费任何 commit 变量 → resolved tip 永远到不了镜像，静默漂移
- 变量普查：`s8ga_repo_commit`×16、`cp2k_dev_repo_commit`×13，无一使用
  `s8_overrides_repo_commit` 形态

**2.4 cp2k 专用逻辑硬编码进通用 loader**
- `environment.py:1041-1045` 在通用路径解析 lock 找 `"cp2k"`；非 cp2k env
  一旦出现 floating repo（生成 sidecar）即被 "malformed spack.lock" **假错误**
  炸掉（`:1052-1055`）；反向：pinned+floating 混合 env 静默失去锁

### P1 — 晚爆炸 / 契约裸奔

**2.5 image_path 四方路径契约，守卫只认 CP2K 键名**
- `image_path` 必须等于"手写 clone 根路径 + 手写 sparse 变量 + sparse_path"
  拼出的串（vasp 例：env.yaml:79-87 ↔ Dockerfile.j2:32,35 ↔ env.yaml:104-105）
- `check-dual-write.py:25-32` 的键名全是 CP2K/s8ga 历史值
  （`force_avx512_repo_path`、`S8GA_IMAGE_ROOT`）；vasp/abacus 的
  `s8ga_vasp_repo_path`/`s8ga_abacus_repo_path` 三角耦合**零覆盖**——
  守卫形态是"按历史事故逐 app 打补丁"
- 同一 `default_image_path` 规则存在 3 份实现：plan（`spack_plan.py:108-116`）、
  Dockerfile 内联 Jinja（`cp2k_opensource-2025.2/Dockerfile.j2:54` 等）、
  assets（`spack_ops.py:514-516`），namespace==sparse 末段纯靠约定

**2.6 StrictUndefined 隐式必需变量 + `default()` 静默吞错**
- 每个 app 的必需 template_vars 无清单（schema 是开放 mapping，
  `environment.py:927-928`）；validation 只认 `cp2k_branch` 一个
  （`validation.py:400-460`）
- 带默认值的更阴险：`cp2k_opensource-master.../Dockerfile.j2:36-42` 的
  `cp2k_source_commit | default(cp2k_dev_repo_commit)` 恰好掩盖 2.4 的锁丢失

**2.7 `build-sif --app-version <拼错>` 不报错，静默回退 `hpc-cp2k:latest`**
- `cli.py:635-640` 吞掉 FileNotFoundError → `template.py:107-113` 落到
  `("hpc-cp2k","latest")`。对照组：`dockerfile` 子命令会列全环境后报错
  （`template.py:359-365`）——同一错误两种行为

**2.8 空 mirror 不拦截，`build` 还会自动 mkdir**
- `validation.py:345-397` 不查 mirror 内容；`execution.py:282-284` 自动创建；
  有网时静默回退上游直连（离线承诺无声失效），无网时编译中段才爆

**2.9 COPY 暗依赖（libfakeintel/、testsuite-file/）不在任何校验里**
- `vasp_mkl-6.6.1-avx2/Dockerfile.j2:52,162-163`；对照组 manual_packages
  有校验（`validation.py:272-342`）——工具会做但没做全。修法提示：渲染后
  Dockerfile 就在手边，扫描 COPY 源逐一验证

**2.10 manual_packages 三方文件名契约 + sha256 双记**
- vasp tarball 文件名硬编码 `Dockerfile.j2:84` + env.yaml:94-97 + spack.yaml
  spec + recipe version()，升版本 ≥4 处；sha256 两处各验各的，drift 静默

### P2 — 人类体验 / 文档 / 漂移

**2.11 高频操作无原子命令**：升 pin / 升版本 = 手改 3-7 处 + 手算 sha256 +
  手跑守卫。理想形态：`hpc_cf pin set <repo> <sha>`、`env set-app-version`，
  并把 check-dual-write 并入 `validate --profile config`
**2.12 知识只活在 AGENTS.md**：view EXDEV 规避（AGENTS.md:228-229）、
  mirror git 缓存 4GiB/pin 与 prune 流程（AGENTS.md:109-129）——docs 零命中，
  标题却是 "Guide for AI agents"
**2.13 文档三处环境表全过时**：CLI 实际 13 个 env，README:19-30 /
  docs/README.md:85-96 / TEMPLATE_MATRIX.md:9-20 均只列 10；QUICKSTART:89
  写死 9；`buildcache`/`gc` 子命令不在 CLI 文档
**2.14 fix_hint 循环引用**：tarball 缺失提示 "run assets bootstrap"，而
  bootstrap 的输入恰是该 tarball（`spack_ops.py:347`）——唯一真实路径是
  QUICKSTART.md:17-29 的手工 curl
**2.15 首跑阻断**：vasp_mkl-6.6.1 两 env 出厂无 spack.lock 且不在文档表——
  新用户必卡"两阶段 lock 契约"，需自行领悟 `--allow-concretize`
**2.16 flag 面向内部阶段而非用户意图**：assets 的 4 个阶段布尔 + 逃生口；
  无总控命令；`--allow-concretize` 合法组合取决于 `workflows.py:144-163`
  内部规则

---

## 3. 模板重复全景（量化）

| 重复块 | 份数 | partial 化 |
|---|---|---|
| s8ga monorepo sparse clone 块 | 8 份逐字拷贝 | 否 |
| cp2k 上游 clone（pinned 变体） | 5 份 | 否 |
| cp2k 上游 clone（floating 变体） | 2 份 | 否 |
| 手写 `spack repo add` | 6 env（vs 7 env 已用 partial） | 54% |
| `RUN spack repo ls` 验证行 | 13 份 | 否 |
| MKL purge / fakeintel 块 | 各 5 份 | 否 |
| VASP testsuite 抽取 | 4 份（6.6.0 与 6.6.1 内部已漂移） | 否 |
| Spack 生命周期块 | 12/13 | **是** |

- 迁移完成度估计 **55-60%**；STATUS 注释已落后现实
  （`spack_image_repos.j2:5-7`、`spack_plan.py:82-88` 漏计 vasp6.6.1 与
  cp2k master 的接入）
- 同一逻辑三种 template_vars 命名（`s8ga_abacus_repo_path` /
  `s8ga_vasp_repo_path` / `force_avx512_repo_path`）；cp2k 2026.1 的
  `force_avx512_repo_url/branch` 自认 "Unused by image clone"（死配置）
- 死模板 1 个：`spack_buildcache_register.j2`（仅测试引用）
- `legacy/` 目录（167MB，含 2026-04-12 快照与 VASP 源码 tarball）：**已复核
  确认未被 git 跟踪、从未进历史/远端**（`.gitignore:1` 的 `legacy/**` 挡住；
  `git ls-files legacy/` = 0）。无代码引用（CLI 只扫 `spack-envs/`）。
  属仓库卫生问题（167MB 死重量 + `git add -f` 手滑面），**非合规事故**。
  初版审计曾误报为"留在 git 里"，已更正

---

## 4. 修复方向骨架（供接手 agent 细化）

| 档 | 内容 | 消除的发现 |
|---|---|---|
| **A. 校验收口**（最小） | ① check-dual-write 泛化为键名无关的一致性检查并入 `validate --profile config` + CI；② spack.yaml↔lock hash 比对；③ 渲染后扫描 COPY 源存在性；④ mirror 非空检查或显式警告 | 2.1/2.2/2.9/2.10/2.8 |
| **B. 原子命令** | `hpc_cf pin set`（一处改、多处写、自动跑守卫）、`env set-app-version`（连带 tarball 名/sha256/LABEL） | 2.1/2.10/2.11 |
| **C. 去 app 特化** | 从 environment.py 抽走 cp2k 硬编码（2.4）；floating repo 注入改为显式声明而非变量名约定（2.3） | 2.3/2.4 |
| **D. plan 化收官** | staging clone partial（读 custom_repos url/commit/sparse_path，处理 monorepo 去重）→ template_vars 的 s8ga_* 退役 | §3 全部 + 2.5/2.6 |
| **E. 文档再生** | 环境表由 CLI 生成（--format json 已支持）；AGENTS.md 独有知识迁入 docs；build-sif 对齐 dockerfile 的 fail 行为 | 2.7/2.12/2.13/2.14 |

建议顺序：A（纯加法、立刻止血）→ B（高频操作）→ C/D（结构收敛）→ E。

---

## 5. 关键文件索引

| 文件 | 角色 |
|---|---|
| `hpc_cf/environment.py` | env.yaml schema/加载；floating pin 注入（:1026）；cp2k 硬编码（:1041-1055） |
| `hpc_cf/validation.py` | 校验体系（Finding+fix_hint）；lock 检查缺口（:463-511） |
| `hpc_cf/spack_plan.py` | plan 化核心；`default_image_path`（:108-116）；迁移 docstring（:82-88） |
| `hpc_cf/spack_ops.py` | assets 侧 repo clone（:514-542）；bootstrap 输入（:347） |
| `scripts/check-dual-write.py` | 现存守卫（CP2K 键名硬编码 :25-32） |
| `templates/partials/spack_image_repos.j2` | plan 驱动 repo add（不 clone） |
| `AGENTS.md` | 守卫入口（:280）、prune/view-EXDEV doctrine（:109-129, :228） |
