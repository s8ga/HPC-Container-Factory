# Handoff:HPC-Container-Factory 改进审计 Round 2(stage 2 修复程序之后)

- 日期:2026-11-02
- 方法:3 个并行只读探查 agent(代码/架构健康、运维工作流与 CLI 体验、交付管线与 CI 质量)
- 状态:**只审计,未改动任何代码**。所有发现附 file:line 证据(基于 `ceae79a` 工作树;
  其后仓内另有 `ac1cccd..aa32f72` 四个 vasp testsuite/SIF/motd 小提交,不触及
  hpc_cf/ 代码与 workflows,已核对不影响本审计的证据行号)
- 给接手 agent 的任务:对下列发现做修复方案设计,重点是评级、拆任务、
  判断哪些是「删代码」哪些是「加校验」哪些是「纯自动化」

---

## 0. 前置上下文(必读)

- **Round 1 审计**:`docs/agent-handoff-fragility-audit.md`(2026-11-01),
  其 §4 修复程序 A→E 已在 `b63c269..ceae79a` **全部落地**(17 个原子 commit):
  守卫并入 validate + CI(ci-tests.yml)、lock-manifest 侧车、`pin set` 原子命令、
  `lock_source_commits` 声明化、plan-driven staging/upstream partial(手写
  `spack repo add` 与 `s8ga_*` 变量镜像全部退役)、docs 再生 + drift-guard。
- 当前基线:ruff 0 错误,645 passed / 42 skipped,守卫 13 env 全过,13 env 渲染全 OK。
- **分支 v2 未 push**;本地留有 `stage2-backup`、`stage2-backup2` 两个历史重写前的
  备份分支(确认后可删)。
- 历史改写说明:`ceae79a` 把 vasp 6.6.1 特性从首个 commit 统一抽出到顶部
  (feat(vasp): 6.6.1 avx2/avx512 + force-OpenMP preset + deps-only buildcache)。
- 验证闸门(每 commit):`./venv/bin/ruff check hpc_cf/ tests/ scripts/` 0 错误 +
  `./venv/bin/pytest -q` 全绿;约定见 AGENTS.md(test-first、Spack CLI over
  config surgery、英文注释、每逻辑变更一 commit)。

## 1. 一句话诊断

Round 1 的「静默漂移」类风险已闭环;剩余风险/摩擦集中在四类:
(a) **一个真 P0** — template_vars 可静默覆盖渲染上下文保留键;
(b) 操作者工作流摩擦(版本升级无原子命令、assets 17-flag 面向内部阶段、
无总览 status);(c) buildcache/环境加载器的结构分解债;
(d) 新鲜度完全靠人工(builtin pin、base image digest、lock manifest 回填)。

---

## 2. 核心发现(三方合并,按严重度)

### P0 — 正确性风险(仅 1 条)

**2.1 template_vars 静默覆盖 build_context 保留键**
- `hpc_cf/template.py:456-472`:`**template_vars` 在计算默认值**之后**展开,
  env.yaml 里名为 `use_mirror` / `build_only` / `default_image_name/tag` /
  `spack_version` / `env_dir_name` 的变量会悄悄顶掉 CLI 推导值;仅
  `allow_reconcretize` 与 spack-plan 键在 :477-478/:493/:521-524 事后重设,
  `apt_mirror` 又特例 CLI 优先 — 每个键的优先级规则不一致且无冲突校验
- StrictUndefined 无能为力(键存在、值错)。修法:定义
  `RESERVED_CONTEXT_KEYS` 在 `parse_environment_spec`/config profile 拒绝冲突,
  并文档化统一优先级(CLI > env.yaml > defaults)

### P1 — 晚爆炸 / 运维摩擦

**2.2 stable-release 无 env 族守卫 → 误路由跑数小时 + 错误文案 release**
- `stable-release.yml:72-75` 只查目录存在;`pipeline-cp2k.yml:59-60` 明确把
  vasp/mkl/rocm 排除在管线外,但 dispatch 仍会跑;release notes 硬编码 CP2K 文案
  (`pipeline-cp2k.yml:1020-1044`)。前科:2026-10-29 abacus 误路由
- 修法:resolve job 加白名单 `case "$ENV_NAME" in cp2k_opensource-*|abacus_opensource-*)`

**2.3 build-lock artifact 丢 lock-manifest;12/12 存量 lock 全是 provenance_unknown**
- `pipeline-cp2k.yml:339-341` 只上传 spack.lock + resolved-repos.yaml,
  **不含 lock-manifest.json** → CI regen 路径把刚生成的 manifest 扔掉
- 本地现状:12 个 env 有 lock、0 个有 manifest(master 无 lock 属设计);
  `validation.py:604-620` 每次都 WARNING → 警告疲劳风险
- 修法:artifact 路径加一行;逐 env 走 regen-lock 回填;之后把
  missing-manifest 从 WARNING 升为 flag-gated ERROR

**2.4 失败路径不报下一步命令**
- `hpc_cf/buildcache.py:262`(fallback 警告)与 :264-271(3 个 only-fail-closed
  RuntimeError)都不提 `buildcache status` / `resume`(字段已在 health 里,
  `buildcache_workflow.py:293-313`)
- `hpc_cf/validation.py:504-508` 与 `hpc_cf/pin.py:184-196`:守卫 fix_hint 只指向
  诊断脚本,不提 `pin set`
- 修法:纯字符串追加 next-command

**2.5 pipeline-cp2k.yml 4× 前导复制(~350/1139 行)— 全仓最高易碎文件**
- 逐字相同四段::144-185 / :433-474 / :605-646 / :846-887(apt sed、
  28 行 fork-identity 推导、venv、变量解析);外加 3× tarball 下载、3× bootstrap cache。
  两个月 31 次改动、4 次同日修复
- 修法:抽 `./.github/actions/env-preamble/` composite action 或
  `scripts/ci_preamble.sh`(仓库已有 apply-source-override.sh 先例)

**2.6 ci-tests.yml 缺 `permissions: contents: read` 与 `timeout-minutes`**
- `.github/workflows/ci-tests.yml:25-28`:公开门面继承仓库默认 token 权限;
  挂死的 pytest 烧默认 360 分钟。两行修复

**2.7 版本升级无原子命令(Round 1 2.11 余项)**
- cp2k 2026.3 ≈ 13 处手改:env.yaml×4(`.../2026.2fa/env.yaml:79,80,106,108`)+
  spack.yaml spec(:236)+ Dockerfile 8 处硬编码 "2026.2"(标题/LABEL/路径)
- vasp 6.6.2 ≈ 16 处 + 手动下载专有 tarball + 手算 sha256(env.yaml:95-97)+
  Dockerfile 10 处硬编码 "6.6.1"
- `pin set` 只覆盖 commit 双写;修法:`env set-version` 复用 `pin.py:172-249`
  的外科编辑+重解析+守卫+回滚机制,连带 branch/spec/LABEL/tarball/sha256,
  删 lock 后打印 regen-lock dispatch 指引

**2.8 assets 17-flag 面向内部阶段 + prune 陷阱(Round 1 2.16 余项)**
- `hpc_cf/cli.py:152-250` 17 个 flag;`--prune-mirror` 不在 action_flags
  (`hpc_cf/assets.py:546-554`)→ **单独传它触发完整数小时管线**(意图只是看
  dry-run 报告);`--skip-*` 在显式模式静默无效(:570,:591);`--status`+阶段
  flag 组合被静默丢弃(:556-561);合法矩阵藏在 `workflows.py:135-163`
- 修法:任务化子命令 `assets run|status|prune|bootstrap`(prune 独立跑,
  身份输入 `.hpc_cf/fetch-specs.json` 已持久化);非法组合转 `parser.error`;
  旧布尔保留一版作别名

**2.9 无 `hpc_cf status` 总览;状态散落 4+ 处**
- buildcache:`assets/spack-buildcache-state/{health.json, coverage/*.json(8),
  runs/(51), buildcache.lock}`;mirror:`assets/spack-mirror/.hpc_cf/runs/*`;
  per-env:lock/lock-manifest/resolved-repos。`assets --status` 只看 mirror,
  `buildcache status` 只看缓存
- 修法:纯只读聚合命令,复用 `read_lock_manifest`/health/`SharedMirrorStore` API

**2.10 新鲜度完全靠人工**
- builtin pin `fc7222b`(2026-07-09)被 7 个 env 共享,~3.7 个月未动;
  regen-lock 仅 dispatch;无 renovate/dependabot(`.github/` 只有 workflows/)
- base image digest 刷新全靠 `Refresh:` 注释(`10 个 env.yaml` +
  `docs/ADD_NEW_ENV.md:37`),3 个 env 的 debian digest 落后 ~3.5 个月
- 修法:月度定时 builtin-bump PR(`pin set` 机制已就绪)+ digest 漂移检查 job;
  `.github/dependabot.yml`(github-actions 生态)

**2.11 结构分解债(代码侧)**
- `buildcache_workflow.py:248` `BuildcacheService.run` **422 行**内联
  4 动作×2 后端;`_complete_producer` 172 行 11 参(:75)
- `buildcache.py` 756 行混 4 关注点:准入策略(:108-278,:356-488)/镜像生命周期
  (:60-106)/裸 subprocess(:336-356,:542-620)/发布+状态序列化(:489-541,:641-756)
- `environment.py` 1142 行混 model+parser+workflow I/O;
  `apply_resolved_repo_pins`(:1018-1128)在 schema 模块里读 lock/sidecar;
  `as_dict`(:417-504)手写 87 行
- `cli.py` 874 行:`run_new_cli` 265 行顺序敏感 if/elif + `"__LIST__"` 哨兵协议
  (:610,:826-837);build-sif/validate 命令体内联在分发器里
- legacy 层零生产调用者:`env.py:63-196` 的 `load_env_yaml` + 4 个 `validate_*`
  wrapper、`spack_ops.py:46-56` 的 `load_env_config` — 只有 tests 在用
  (test_validate/test_preflight/test_env_parsing/test_environment_spec)
- `spack.lock` 位置 4 种拼法:validation 候选列表×2(:529-532,:580-583)、
  buildcache_workflow **反序** fallback(:346-348)、execution 目录下探
  (:194-205)、裸 join(workflows.py:222,:323;assets.py:188)— env.yaml 早有
  `find_env_yaml`,lock 一直没有对应物
- root 推导启发式在两处逐字重复(`validation.py:490-494`、`pin.py:232-236`),
  且对自定义 layout 回退到真实仓库根(潜在部分展开 bug)
- resolved-pins 注入静默覆盖已有键(`environment.py:1088,:1121`)
- sidecar 注入循环引用:`environment.py:1001,1041` → env.find_env_yaml
  (model 模块 lazy import discovery)

### P2 — 打磨 / 预防

- **无 pyproject.toml**:ruff 只跑默认规则集(E4/E7/E9/F);mypy 缺位但
  **386/386 函数已有返回注解**(近白捡);无打包元数据;
  `tests/test_pin_set.py:251` 有失效 `# noqa: ANN001`
- nightly-failure 决策树只活在 YAML 注释里(`regen-lock.yml:1-16`、
  `nightly-auto-retry.yml:1-5`),docs 零引用 → 缺 "When nightly fails" runbook
- 无 shell 补全(argcomplete);`--app-version` 补全可复用 `_extract_available_versions()`
- GENERATE_CLI `build` 段缺 8 个真实 flag(--buildcache* ×5、--apt-mirror、
  --build-secret);pin/gc 无章节;drift 测试只覆盖环境表不覆盖 flag
- QUICKSTART 只写 uv(pip 替代埋在 README:61-67);WSL/podman 零说明
  (开发机即 WSL2);requirements-dev 安装只在 AGENTS.md 提及
- stable 渠道 runtime 本地 tag 仍硬编码 `nightly-${NIGHTLY_DATE}`
  (`pipeline-cp2k.yml:735` vs :757,:763-770)— 2026-10-28/29 "image not known" 根因
- `FLAGSHIP_ENV` 硬编码于 workflow env(`pipeline-cp2k.yml:109-113`)
- 入库 evidence logs ~4MB(9 个 `cp2k-regtest.log`,最大 812KB)→ gzip;
  本地 `artifacts/` 25.9GB 无清理 doctrine
- dispatch 输入 `fork_url/fork_branch` 原样内插进 run: 脚本
  (`pipeline-cp2k.yml:168-176`;cp2k_commit 有 regex 校验,fork_url 无)→ env: 间接化
- actions 版本散乱(checkout v7/v4、cache v6、upload v7、download v8)
- `template.py:111` 残留 `"hpc-cp2k","latest"` 回退(cli 已响亮报错,现仅是陷阱)
- `detect_non_host_network` 住在 template.py 却被 assets/sif 用(:678-707)
- `__main__.py:29` 顶层异常漏斗太窄(PermissionError/KeyError → 裸 traceback)
- schema v1 松散点:template_vars 值类型不校验、`manual_packages.sha256`
  不验 64-hex、images 引用无格式校验、无 v2 迁移钩子
- 风险最高弱测试路径:`sif.pack_apptainer`(92 行,交互+网络,零直接测试)、
  `assets.run_assets` 默认编排(155 行)、cli 分发哨兵路径

---

## 3. 修复方向骨架(供细化)

| 档 | 内容 | 消除的发现 |
|---|---|---|
| **A. 快赢止血**(≈1 天) | ① RESERVED_CONTEXT_KEYS 保留键校验;② ci-tests permissions+timeout;③ stable-release env 族白名单;④ build-lock artifact 补 manifest 路径;⑤ 失败路径 next-command 扫一遍;⑥ 顶层异常漏斗放宽 | 2.1/2.6/2.2/2.3(部分)/2.4 |
| **B. manifest 回填 + 升级门禁** | 逐 env regen-lock 轮转(或文档化本地循环)→ 12/12 manifest 齐 → missing-manifest 升 flag-gated ERROR;rocm lock 早于新 pin 也在此轮重建 | 2.3 |
| **C. 工作流完形** | `env set-version`(复用 pin set 机制);`assets run\|status\|prune` 任务化 + prune 独立;`hpc_cf status` 聚合;nightly-failure runbook 文档 | 2.7/2.8/2.9 |
| **D. 结构分解** | buildcache 拆 admission/exec + run() 拆动作 handler;environment 拆 parser/model + sidecar I/O 外移;删 legacy shims(测试迁移);`find_spack_lock` + `ProjectLayout.from_env_dir`;cli 命令表化 | 2.11 |
| **E. 管线与自动化** | pipeline 前导抽 composite action;stable 本地 tag 修 channel 化;FLAGSHIP_ENV 配置化;月度 builtin-bump PR + digest 漂移 job;dependabot;ci-tests 失败开 issue;evidence gzip + artifacts 清理 doctrine | 2.5/2.10/多项 P2 |
| **F. 工具链打磨** | pyproject(ruff 严格集 + mypy strict-lite);argcomplete;GENERATE_CLI 补全 + flag drift 测试;QUICKSTART pip/WSL 段 | P2 余项 |

建议顺序:A → B → C → E(管线件)→ D(结构)→ F。
D 与 E 可并行;C 的 `env set-version` 依赖 pin.py 机制已稳定,可最先做。

---

## 4. 操作侧待办(非代码,需要人跑)

1. **12 个 env 的 lock manifest 回填**:对每个 env → 建分支 → `git rm spack.lock`
   → dispatch `regen-lock`(env_name)→ 下载 artifact(lock+manifest)→ 提交
2. **rocm 重建 lock**:其 lock 早于 `757bb76a` pin(commit 9fd7ad3 改 pin 后未重建)
3. 确认历史后删备份分支:`git branch -D stage2-backup stage2-backup2`
4. `legacy/`(167MB,已 ignore 未跟踪)人工清理
5. v2 push 后观察 ci-tests.yml 首轮(推送需用户授权)

## 5. 关键文件索引

| 文件 | 角色 |
|---|---|
| `hpc_cf/template.py` | **P0 所在**:build_context 键优先级(:456-524);`"hpc-cp2k"` 残留(:111) |
| `hpc_cf/buildcache.py` / `buildcache_workflow.py` | 756/422 行分解对象;错误消息 next-command 缺口 |
| `hpc_cf/environment.py` | model+parser 混装;resolved-pins I/O(:1018-1128);schema v1 松散点 |
| `hpc_cf/cli.py` / `env.py` / `spack_ops.py` | 分发器内联(:610-874);legacy shims(env.py:63-196) |
| `hpc_cf/pin.py` | `env set-version` 的机制模板(外科编辑+守卫+回滚,:172-249) |
| `hpc_cf/validation.py` / `lock_manifest.py` / `repo_pins.py` | Round 2 已建机制;manifest WARNING 升 ERROR 的落点(:604-620) |
| `.github/workflows/pipeline-cp2k.yml` | 4× 前导(:144/433/605/846);artifact 路径(:339-341);stable tag(:735) |
| `.github/workflows/stable-release.yml` | env 族白名单落点(:72-75) |
| `.github/workflows/ci-tests.yml` | permissions/timeout 落点(:25-28) |
| `docs/agent-handoff-fragility-audit.md` | Round 1 审计(全部已修,作对照) |

## 6. Round 2 确认的强项(接手时勿破坏)

- fail-closed schema(`_reject_unknown_keys` + 严格 schema_version + 类型化枚举)
- ValidationReport/profiles + fix_hint 体系;零裸 except;路径禁闭全覆盖
- 513 测试含失败基线/快照/契约测试;100% 返回注解
- doctrine 写进代码(两阶段 lock、resolved-repos、lock-manifest)
- 管线:per-env 并发族、前缀过滤 prune、ephemeral token、secret mount、
  事故记忆内联在修复行处
