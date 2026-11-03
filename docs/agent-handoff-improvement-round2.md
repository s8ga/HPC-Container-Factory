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

Round 1 的「静默漂移」类风险已闭环;经两轮共 5 个只读探查 agent 交叉深挖,剩余风险/摩擦集中在五类:
(a) **4 个真 P0** — `template_vars` 静默覆盖保留键、`pipeline-cp2k.yml` 前缀清理误删 `-force-avx512` 镜像/Release、`apply-source-override.sh` + `cp2k_upstream_clone.j2` 三重断裂、`vasp_mkl-6.6.0-avx512` 开启 buildcache 但缺 `builder-installed` 阶段;
(b) 校验盲区与并发/原子写缺口(默认 `validate` 不渲染 `Dockerfile.j2`/`*.def.j2`、`--format json` 遇异常直接崩溃、producer 构建未拿 `mirror.lock`、6 处侧车文件非原子写);
(c) 镜像体积与环境模板缺陷(`runtime_cleanup.j2` 在 `COPY --from=builder` 后删文件白占 OCI 层、MKL/ROCm 后台 `libint &` 未 `wait`、ROCm MOTD 无 GPU 时崩溃、9/13 个 `.def.j2` 粘贴错误);
(d) 操作者工作流摩擦(版本升级无原子命令、assets 17-flag 面向内部阶段、无总览 status)与结构分解债;
(e) 新鲜度完全靠人工(builtin pin、base image digest、lock manifest 回填)及文档事实漂移。

---

## 2. 核心发现(五方合并,按严重度)

### P0 — 正确性风险与数据误删(4 条)

**2.1 template_vars 静默覆盖 build_context 保留键**
- `hpc_cf/template.py:456-472`:`**template_vars` 在计算默认值**之后**展开,
  env.yaml 里名为 `use_mirror` / `build_only` / `default_image_name/tag` /
  `spack_version` / `env_dir_name` 的变量会悄悄顶掉 CLI 推导值;仅
  `allow_reconcretize` 与 spack-plan 键在 :477-478/:493/:521-524 事后重设,
  `apt_mirror` 又特例 CLI 优先 — 每个键的优先级规则不一致且无冲突校验
- StrictUndefined 无能为力(键存在、值错)。修法:定义
  `RESERVED_CONTEXT_KEYS` 在 `parse_environment_spec`/config profile 拒绝冲突,
  并文档化统一优先级(CLI > env.yaml > defaults)

**2.1a [新增 P0 数据误删] `pipeline-cp2k.yml` 的 `startswith` 前缀清理会误删 `-force-avx512` 和 `-fork-*` 镜像与 Release**
- 位置:`.github/workflows/pipeline-cp2k.yml:396-402`(清理 mirror GHCR 包版本)、
  `:809-815`(清理 runtime GHCR 包版本)、`:1132-1138`(清理 stable Release 与 git tag)
- 证据:三处均用 `startswith("${ENV_NAME}-")` / `startswith("${ENV_SUFFIX}-")` /
  `startswith("stable-${ENV_NAME}-")`。因 `cp2k_opensource-2025.2` 是
  `cp2k_opensource-2025.2-force-avx512`(以及 `<env>-fork-*`)的前缀(同理 `2025.2` 是
  `2025.2-force-avx512` 的前缀),一旦跑基础环境 `cp2k_opensource-2025.2`,
  会把 `-force-avx512` 的镜像版本和 stable release 一并计入并**删掉**
- 修法:改为精确正则匹配:mirror 用 `test("^" + $env + "-[0-9]{8}$")`,runtime 用
  `test("^" + $sfx + "-(nightly|release)(-[0-9]{8})?$")`,stable release 用
  `test("^stable-" + $env + "-[0-9]{8}$")`

**2.1b [新增 P0 功能损坏] `cp2k_upstream_clone.j2` + `apply-source-override.sh`:Fork 构建工作流三重断裂**
- 位置:`templates/partials/cp2k_upstream_clone.j2:17,28,40`、
  `scripts/apply-source-override.sh:47,69-80`、`hpc_cf/repo_pins.py:504-536`、
  `hpc_cf/template.py:133-135,264-278,443`
- 证据:
  1. `16fb3c4` 抽 `cp2k_upstream_clone.j2` 时丢掉了 `{{ cp2k_source_repo_url | default("https://github.com/cp2k/cp2k.git") }}`,三处分支硬编码 `https://github.com/cp2k/cp2k.git`,fork 独有 commit 在构建阶段 `git fetch` 必败;
  2. `apply-source-override.sh:69-80` 同时写 `template_vars.cp2k_source_repo_url` 与 `custom_repos['cp2k_dev'].url`,而 `cp2k_upstream_clone.j2` 未使用该变量,触发 `repo_pins.py:514-525` 报 `repo_pins.inconsistent`(dead dual-write copy);
  3. `apply-source-override.sh:47` 用 `ln -s "$BASE" "$FORK_DIR"` 建软链后 `sed -i` 直接原地污染原环境受控文件,且 `confine_to_root`(`template.py:264-267`)调 `Path.resolve()` 把 `environment_dir` 还原回 `<base>`,使 `resolve_output_image_tag` 与 `env_dir_name` 静默丢失 `-fork` 后缀
- 修法:① `cp2k_upstream_clone.j2` 恢复 `{{ cp2k_source_repo_url | default('https://github.com/cp2k/cp2k.git') }}`;② `apply-source-override.sh` 用 `cp -a "$BASE" "$FORK_DIR"`(拷贝而非软链,目录内对大文件或不需要改的子项按需处理,或直接复制环境目录)避免原地污染与 `Path.resolve()` 坍缩

**2.1c [新增 P0 配置与模板失配] `vasp_mkl-6.6.0-avx512` 开启 `buildcache.enabled: true` 但 `Dockerfile.j2` 无 `builder-installed` 阶段**
- 位置:`spack-envs/vasp_mkl-6.6.0-avx512/spack-env-file/env.yaml:61-65`、
  `spack-envs/vasp_mkl-6.6.0-avx512/Dockerfile.j2:15-107`
- 证据:`env.yaml` 设 `buildcache: enabled: true, padded_length: 128, policy: auto`,但 `Dockerfile.j2` 仍是旧 3 阶段模板(无 `AS builder-installed`、未 include `spack_buildcache_config.j2`/`spack_buildcache_install_deps.j2`、无 `/opt/spack-buildcache` 挂载)。跑 `buildcache build` 在 Podman 报 `--target builder-installed` 不存在;跑普通 `build` 在无缓存挂载下注入 128 字节 padding
- 修法:将 `vasp_mkl-6.6.0-avx512` 的 `buildcache.enabled` 设为 `false`(与 `vasp_mkl-6.6.0-avx2` 对齐,符合 AGENTS.md MKL 6.6.0 不迁 buildcache 原则),并在 `validation.py` 增加规则:当 `buildcache.enabled: true` 时强制校验 `Dockerfile.j2` 含 `AS builder-installed` 及 buildcache 安装 partial

### P1 — 晚爆炸 / 运维摩擦 / 校验与并发漏洞

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

**2.6 工作流权限最小化、`timeout-minutes` 与并发键碰撞(含 ci-tests / pipeline-cp2k / stable-release / nightly-auto-retry)**
- `.github/workflows/ci-tests.yml:25-28`:缺 `permissions: contents: read` 与 `timeout-minutes`
- `.github/workflows/pipeline-cp2k.yml:118-135,836`:`:836` 注释称仅 `sif` 有 `contents: write`,但 `:118` 顶层设了 `contents: write`,导致 `prepare`/`binaries`/`runtime` 全继承写权限;`:128` concurrency group `hpc-cf-pipeline-${{ inputs.fork_branch || inputs.env_name || ... }}` 在传 `fork_url` 不传 `fork_branch` 时撞入上游并发组,不同 `env_name` 同 `fork_branch` 也互相阻塞
- `.github/workflows/stable-release.yml:36-44` 与 `nightly-auto-retry.yml:17-22`:均缺 `timeout-minutes`;`stable-release.yml` 顶层多给了 `packages: write`
- 修法:全工作流补齐 job 级最小 `permissions` 与 `timeout-minutes`;`pipeline-cp2k.yml` concurrency group 改为 `hpc-cf-pipeline-${{ inputs.env_name || 'cp2k_opensource-2026.2-force-avx512' }}-${{ inputs.fork_url || 'upstream' }}-${{ inputs.fork_branch || 'default' }}`

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
  `docs/ADD_NEW_ENV.md:37`),3 个 env 的 debian digest 落后 ~3.5 个月;
  另有 4 个旧 env(`cp2k_mkl-2025.2-experimental`、`cp2k_rocm-2026.1-gfx942`、
  `vasp_mkl-6.6.0-avx2`、`vasp_mkl-6.6.0-avx512`)完全无 `repos.builtin.commit` 与 base image `@sha256` digest
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
  (test_validate/test_preflight/test_env_parsing/test_environment_spec);
  另有零调用死函数:`sif.py:318-329`(`build_apptainer`)、
  `buildcache_ops.py:95-108`(`build_check_script`)、
  `mirror_prune.py:75`(`GitPrunePlan` 别名)、
  `tests/test_integration_abacus_l4.py:136-149`(`_assert_integration_summary_passed`)
- `spack.lock` 位置 4 种拼法:validation 候选列表×2(:529-532,:580-583)、
  buildcache_workflow **反序** fallback(:346-348)、execution 目录下探
  (:194-205)、裸 join(workflows.py:222,:323;assets.py:188)— env.yaml 早有
  `find_env_yaml`,lock 一直没有对应物
- root 推导启发式在两处逐字重复(`validation.py:490-494`、`pin.py:232-236`),
  且对自定义 layout 回退到真实仓库根(潜在部分展开 bug)
- resolved-pins 注入静默覆盖已有键(`environment.py:1088,:1121`)
- sidecar 注入循环引用:`environment.py:1001,1041` → env.find_env_yaml
  (model 模块 lazy import discovery)

**2.12 [新增 P1 并发锁与状态序列化缺口] Producer 构建漏拿 `mirror.lock`、`write_coverage` 漏 `check_kind`、`as_dict()` 漏 `ManualPackage.spec`、6 处侧车非原子写**
- 位置:
  - `hpc_cf/buildcache_workflow.py:534-567`(对比 `hpc_cf/workflows.py:296-302`)
  - `hpc_cf/execution.py:471,576-600` vs `hpc_cf/buildcache.py:510-525`
  - `hpc_cf/environment.py:347,449-456`
  - `hpc_cf/lock_manifest.py:64-68`、`hpc_cf/assets.py:150-156,441-445`、
    `hpc_cf/execution.py:428-429`、`hpc_cf/spack_ops.py:852`、`hpc_cf/sif.py:263-265,541`
- 证据:
  1. `BuildcacheService.run` 的 producer `build` 挂载 `/opt/spack-mirror:ro` 但只拿 `store.consumer_lock()`(`buildcache.lock`),未拿 `SharedMirrorStore.shared_read()`(`mirror.lock`),并发跑 `assets` / `--prune-mirror-apply` 会在构建中删改归档;
  2. `SharedBuildcacheStore.write_coverage` 序列化字典时漏写 `"check_kind": record.check_kind`(与 `publish_success_state` 分叉);
  3. `EnvironmentSpec.as_dict()` 序列化 `manual_packages` 时漏写 `spec` 字段(VASP 6.6.1 在用);
  4. `lock-manifest.json`、`resolved-repos.yaml`、mirror `manifest.json`、`prune-report.json`、`fetch-specs.json`(`spack python > fetch-specs.json` 直接重定向)及 `sif.py` 的 `.tar`/`.tar.id` 均直接原地写入,中断或 stdout 混入警告会留下半截损坏文件
- 修法:① Producer 构建外层套 `SharedMirrorStore(self.layout).shared_read()`;② 统一由 `store.write_coverage`(补齐 `check_kind`)写入覆盖记录;③ `as_dict()` 补 `if mp.spec is not None: entry["spec"] = mp.spec`;④ 抽通用 `atomic_write_text`/`atomic_write_json`(`.tmp` + `os.replace`),容器内 `fetch-specs.json` 先写 `.tmp` 再 `mv -f`

**2.13 [新增 P1 `validate` 三大盲区] 默认 profile 不验模板渲染、`--format json` 遇异常直接崩溃、`*.def.j2` 从不校验、`collect_render_copy_sources` 解析缺陷**
- 位置:`hpc_cf/cli.py:560,720-793`、`hpc_cf/validation.py:644-677,799-800,831-900`
- 证据:
  1. `cli.py:759` 仅在 `profile is ValidationProfile.CONFIG` 时跑 `render_template` 和 `collect_render_copy_sources`,而默认 CLI profile 是 `BUILD_INPUT`(`:560`),日常 `hpc_cf validate --app-version <env>` 完全跳过 `Dockerfile.j2` 渲染和 `COPY` 源检查;且该检查写在 `cli.py` 而非 `validate_environment()` 内;
  2. `_cmd_validate`(`cli.py:720-732`)在调 `validate_environment` 前先调 `resolve_build_input`,仅 catch `FileNotFoundError` — 任何 `env.yaml`/`resolved-repos.yaml`/`spack.lock`(`lock_source_commits`)触发的 `ValueError` 直接逃逸,导致 `--format json` 不输出 JSON 直接崩溃;
  3. `collect_render_copy_sources`(`validation.py:644-677`)不处理多行 `COPY ... \` 续行、把布尔 flag `COPY --link src dst` 的 `src` 当作参数跳过、且未用 `confine_to_root` 拦截 `COPY ../outside`;
  4. 13 个 `spack-envs/*/*.def.j2` 从不参与 `validate` 渲染校验;`collect_spack_yaml:799` 只查 `len(commit) == 40` 未验 40-hex
- 修法:将 `Dockerfile.j2` 与 `*.def.j2` 的 `StrictUndefined` 渲染探针及加固后的 `collect_render_copy_sources` 沉入 `validation.py` 的 `validate_environment()`(在 `CONFIG` 与 `BUILD_INPUT` 下均生效);`_cmd_validate` 捕获 `ValueError` 转为 structured finding 确保 `--format json` 永不逃逸裸异常

**2.14 [新增 P1 OCI 镜像体积膨胀] 13 个环境的 `runtime_cleanup.j2` 在 `COPY --from=builder` 之后才删文件(仅产生 whiteout)**
- 位置:`templates/partials/runtime_cleanup.j2:16-48`、12 个 Debian `spack-envs/*/Dockerfile.j2` 及 `spack-envs/cp2k_rocm-2026.1-gfx942/Dockerfile.j2:245-329`
- 证据:所有环境先在 `runtime` 阶段 `COPY --from=builder /opt/spack /opt/spack`(把 `.a`、`include/`、`.spack`、Python `test/__pycache__` 全部写死进 `COPY` 层 blob),随后在下一条 `RUN` 里 `find /opt/spack ... -delete`。OCI 多阶段构建中跨层删除只写 overlayfs whiteout,不会缩减 OCI 镜像层字节
- 修法:将 `runtime_cleanup.j2` 中针对 `/opt/spack` 与 `/opt/spack-view` 的清理步骤(`[2/9]..[9/9]`)前移至 `builder` 阶段 `spack_post_install.j2`(`spack_manifest.j2` 之后、`COPY --from=builder` 之前);`runtime` 阶段仅保留 `[1/9]` 系统层清理(或合入 `runtime_apt_debian.j2` 同一 `RUN` 层)

**2.15 [新增 P1 构建竞态 & MOTD 崩溃 & 交付配置错配]**
- 位置:
  - `spack-envs/cp2k_mkl-2025.2-experimental/Dockerfile.j2:86-94`、`spack-envs/cp2k_rocm-2026.1-gfx942/Dockerfile.j2:123-131`
  - `spack-envs/cp2k_rocm-2026.1-gfx942/cp2k-gpu-motd.sh:79-92,141`
  - `.github/workflows/pipeline-cp2k.yml:981-982,1059-1061,1109-1118`
  - 9 个 `spack-envs/*/*.def.j2` 及 `spack-envs/vasp_mkl-6.6.1-{avx2,avx512}/testsuite-file/spack_build_test*.conf:15-20`
- 证据:
  1. MKL/ROCm `Dockerfile.j2` 后台跑 `spack -e ... install -j "$(nproc)" -p 20 libint & sleep 10 && spack -e ... install -j "$(nproc)" -p 20;`,双倍 `-j $(nproc)` 抢锁、绕过 `libint_parallel_cap=12` 且无 `wait`;
  2. `cp2k-gpu-motd.sh:82` 在 `set -euo pipefail` 下跑 `rocm-smi ... | grep -c 'GPU\[' || echo "0"`:无 GPU 时 `grep -c` 打 `0\n` 并 exit 1,触发 `|| echo "0"` 再打 `0\n`,使 `GPU_COUNT=$'0\n0'`,在 `:141` `[[ "$GPU_COUNT" -gt 0 ]]` 报语法错误崩溃(`:86-88` `/dev/kfd` 分支同理打出 `0\n1` 崩溃);
  3. `pipeline-cp2k.yml:981` 提取了 `DependencyGraph-${ENV_NAME}-${NIGHTLY_DATE}.dot`(见 AGENTS.md:209),但 `:1059` 未上传至 Release、`:1109` 未纳入清理;
  4. `abacus.def.j2:63` 帮助文本写 `ABACUS.psmp`(应为 `abacus`),`:2` 写大写 `ABACUS_opensource-...`;4 个 `vasp.def.j2:2` 的 `--app-version` 漏 `-avx2/-avx512`;4 个 `cp2k.def.j2:1-2` 版本号写串;VASP 6.6.1 的 `testsuite-file/spack_build_test*.conf` 未随 Force-OpenMP preset 更新,仍硬编码 `--bind-to core` 与 `OMP_PLACES=cores`
- 修法:① MKL/ROCm 改用 `spack_libint_parallel_jobs.j2` 串行受控安装;② `cp2k-gpu-motd.sh` 改为 `count=$(rocm-smi --showproductname 2>/dev/null | grep -c 'GPU\[' || true); echo "${count:-0}"`,`/dev/kfd` 分支直接 `echo "1"`;③ `pipeline-cp2k.yml` 补上传与清理 `DependencyGraph-*.dot`;④ 修正 9 个 `.def.j2` 与 VASP 6.6.1 `spack_build_test*.conf`

**2.16 [新增 P1 文档事实漂移(按文档操作会触发守卫报错)]**
- 位置:`docs/ADD_NEW_ENV.md:70-76`、`docs/ASSETS_GUIDE.md:14-18`、`docs/TEMPLATE_MATRIX.md:37,42-45`、`README.md:24-26,133-136`、`docs/README.md:90-92`、`AGENTS.md:201-203`、`spack-envs/cp2k_opensource-{2025.2,2025.2-force-avx512,master-force-avx512}/README.md`
- 证据:
  1. `ADD_NEW_ENV.md:70-76` 仍指导新增环境声明已退役的 `force_avx512_repo_path`(照做会直接被 `repo_pins.py` 报错拦截);`ASSETS_GUIDE.md` 与 `TEMPLATE_MATRIX.md` 仍写仅部分环境使用 `spack_image_repos.j2`;
  2. `README.md` 与 `docs/README.md` 环境表仍将 `cp2k_opensource-2025.2*` 标为 Spack `1.1.0`、`2026.1` 标为 `1.1.1`(实际 `env.yaml` 均已升至 `1.2.0`);
  3. 环境级 `README.md` 仍写旧版依赖(`libint 2.9.0 lmax=6`/`openmpi 5.0.8`)与旧 `skala-1.1.fun` 路径

### P2 — 打磨 / 预防

- **无 pyproject.toml**:ruff 只跑默认规则集(E4/E7/E9/F);mypy 缺位但
  **386/386 函数已有返回注解**(近白捡);无打包元数据;
  `tests/test_pin_set.py:251` 有失效 `# noqa: ANN001`
- **新增边界小缺陷**:
  - `mirror_prune.py:209,236-240,396-397`:`plan.delete_blobs` 做了 `doomed.extend(archive_doomed)` 已包含 `archive_delete_blobs`,`apply_git_prune_plan` 又遍历 `(*plan.delete_blobs, *plan.archive_delete_blobs)` 导致 archive blob 二次 unlink 且报告计数混淆;
  - `container.py:94,243,362`:`ps -q --filter name={self.name}` 为子串匹配,同名后缀容器(如 `hpc-mirror-builder-work-test`)运行中会导致已停止的 `hpc-mirror-builder-work` 误判为 running;应改为锚定正则 `f"name=^/?{re.escape(self.name)}$"`;
  - `storage_gc.py:19-30`:`_is_build_argv` 遇到带参全局 flag(如 `podman --root /scratch build`)会把 `/scratch` 当成首个非 `-` token 漏检活跃构建;
  - `environment.py:692-702,825,833` + `spack_ops.py:846`:`spack.env_name`、`spack.version`、`spack.buildcache.url` 在单引号 `bash -c '...'` partial 及 `_FETCH_SPECS_PY.replace("__ENV_NAME__", ...)` 中直接内插,应在 `environment.py` 解析期统一强制安全字符集正则(`_SAFE_ENV_NAME_RE`);
  - `scripts/{cp2k,vasp,vasp-force-openmp}-motd.sh`:在原生 `x86_64_v3` AVX2 镜像(`cp2k_opensource-2025.2`、`vasp_mkl-6.6.{0,1}-avx2`)上检测到 AVX2 仍误报 `WARNING! Only AVX2 detected!`;
  - 重复资产与死脚本:`scripts/apptainer-motd-hook.sh`(0 引用死脚本)、`scripts/{entrypoint,cp2k-entrypoint,vasp-entrypoint}.sh`(3 份逻辑完全一致)、5 份 `spack-envs/*/libfakeintel/` 副本(VASP 下 README 还是 CP2K 文案)、2 份 166 行 `abacus_run_module_tests.sh` 副本、4 个旧 env `spack.yaml` 仍留 `view: default: root: ./view`
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
- dispatch 输入 `fork_url/fork_branch`/`channel`/`cp2k_commit` 原样内插进 `run:` 脚本
  (`pipeline-cp2k.yml:168-176,294,298,322,763,1062`、`stable-release.yml:81,89`)→ `env:` 间接化
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
| **A. 快赢止血**(≈1–1.5 天) | ① `RESERVED_CONTEXT_KEYS` 保留键校验;② `pipeline-cp2k.yml` 三处 `startswith` 清理改精确正则;③ `cp2k_upstream_clone.j2` 恢复 `cp2k_source_repo_url` + `apply-source-override.sh` 改拷贝防污染;④ `vasp_mkl-6.6.0-avx512` 关 `buildcache.enabled` + `validate` 增 `builder-installed` 阶段守卫;⑤ 全工作流 `permissions`/`timeout-minutes`/concurrency group + `stable-release` 白名单 + `build-lock` 补 manifest + `DependencyGraph.dot` 上传;⑥ 失败路径 next-command + 顶层异常漏斗放宽 | 2.1/2.1a/2.1b/2.1c/2.2/2.3(部分)/2.4/2.6/2.15(部分) |
| **A+. 校验、并发与镜像层加固**(≈1 天) | ① Producer 构建加 `SharedMirrorStore.shared_read()` + `write_coverage` 补 `check_kind` + `as_dict()` 补 `spec` + 6 处侧车原子写;② `Dockerfile.j2`/`*.def.j2` 渲染与加固版 `collect_render_copy_sources` 沉入 `validate_environment`(`CONFIG`+`BUILD_INPUT`) + 修复 `validate --format json` 异常逃逸;③ `runtime_cleanup.j2` Spack 清理前移至 `builder` 阶段 `COPY` 之前;④ 修 `cp2k-gpu-motd.sh` 无 GPU 崩溃、MKL/ROCm `libint` 串行化、9 个 `.def.j2` 与 VASP 6.6.1 `testsuite-file` 修正;⑤ 修正 `ADD_NEW_ENV.md`/`README.md` 等漂移文档 | 2.12/2.13/2.14/2.15/2.16 + P2 边界项 |
| **B. manifest 回填 + 升级门禁** | 逐 env regen-lock 轮转(或文档化本地循环)→ 12/12 manifest 齐 → missing-manifest 升 flag-gated ERROR;rocm lock 早于新 pin 也在此轮重建 | 2.3 |
| **C. 工作流完形** | `env set-version`(复用 pin set 机制);`assets run\|status\|prune` 任务化 + prune 独立;`hpc_cf status` 聚合;nightly-failure runbook 文档 | 2.7/2.8/2.9 |
| **D. 结构分解** | buildcache 拆 admission/exec + run() 拆动作 handler;environment 拆 parser/model + sidecar I/O 外移;删 legacy shims 与死函数/死脚本(测试迁移);`find_spack_lock` + `ProjectLayout.from_env_dir`;cli 命令表化 | 2.11 |
| **E. 管线与自动化** | pipeline 前导抽 composite action;stable 本地 tag 修 channel 化;FLAGSHIP_ENV 配置化;月度 builtin-bump PR + digest 漂移 job;dependabot;ci-tests 失败开 issue;evidence gzip + artifacts 清理 doctrine | 2.5/2.10/多项 P2 |
| **F. 工具链打磨** | pyproject(ruff 严格集 + mypy strict-lite);argcomplete;GENERATE_CLI 补全 + flag drift 测试;QUICKSTART pip/WSL 段 | P2 余项 |

建议顺序:A → A+ → B → C → E(管线件)→ D(结构)→ F。
D 与 E 可并行;C 的 `env set-version` 依赖 pin.py 机制已稳定,可最先做。

---

## 3.5 新增发现的验收规则(Acceptance Criteria)

每个新增修复项落地时,必须满足下表对应的**自动化测试 / 命令验收闸门**,且全仓保持 `./venv/bin/ruff check hpc_cf/ tests/ scripts/` 0 错误与 `./venv/bin/pytest -q` 全绿:

| 编号 | 验收规则(可执行的断言与门禁) |
|---|---|
| **2.1 (`RESERVED_CONTEXT_KEYS`)** | 1. 单测:构造在 `template_vars` 中含 `use_mirror`、`build_only`、`spack_version`、`default_image_name`、`default_image_tag`、`env_dir_name`、`allow_reconcretize` 的 `env.yaml`,`parse_environment_spec` 与 `validate --profile config` 均报 `schema.invalid` / `template_vars.reserved_key` ERROR。<br>2. 回归:13 个现有环境 `validate --profile config` 全绿。 |
| **2.1a (GHCR/Release 前缀清理)** | 1. 单测/脚本契约测试:给定 tag 列表 `["cp2k_opensource-2025.2-20261101", "cp2k_opensource-2025.2-force-avx512-20261101", "cp2k_opensource-2025.2-fork-alice-feat-20261101"]`,当 `ENV_NAME="cp2k_opensource-2025.2"` 时,jq 过滤表达式仅命中 `cp2k_opensource-2025.2-20261101`,绝不命中 `-force-avx512` 或 `-fork-*`(对 runtime `ENV_SUFFIX` 与 `stable-${ENV_NAME}-` 同理验证)。 |
| **2.1b (`cp2k_upstream_clone.j2` + `apply-source-override.sh`)** | 1. 单测(`tests/test_apply_source_override.py`):在临时工作树上对 `cp2k_opensource-2026.2-force-avx512` 和 `cp2k_opensource-master-force-avx512` 执行 `scripts/apply-source-override.sh <base> <fork> --url https://github.com/myorg/cp2k.git --branch my-branch` 后:<br>  a) 原目录 `spack-envs/<base>/` 下所有文件 `git diff` 为空(零原地污染);<br>  b) 对 `<fork>` 环境运行 `validate_environment(..., ValidationProfile.CONFIG)` 结果 `report.ok is True`(零 `repo_pins.inconsistent`);<br>  c) 对 `<fork>` 环境运行 `render_template` 产出的 Dockerfile 包含 `https://github.com/myorg/cp2k.git`,且 `default_image_tag` / `HPC_CONTAINER_NAME` 保留 `<fork>` 名称(未被 `Path.resolve()` 还原为 `<base>`)。 |
| **2.1c (`vasp_mkl-6.6.0-avx512` buildcache 契约)** | 1. `spack-envs/vasp_mkl-6.6.0-avx512/spack-env-file/env.yaml` 中 `buildcache.enabled` 为 `false`(或补齐 4 阶段 `builder-installed`)。<br>2. 单测:构造 `buildcache.enabled: true` 但 `Dockerfile.j2` 不含 `AS builder-installed` 的环境,`validate_environment` 报 `buildcache.dockerfile_missing_stage` ERROR;<br>3. 全量 13 个环境跑 `validate` 全过。 |
| **2.6 (工作流权限 / timeout / concurrency)** | 1. 静态契约测试:扫描 `.github/workflows/*.yml` 中所有 job,断言每个非 reusable workflow caller 的 job 均显式声明 `timeout-minutes` 且非写操作 job 的 `permissions.contents` 为 `read`;<br>2. `pipeline-cp2k.yml` 的 `concurrency.group` 同时包含 `inputs.env_name`、`inputs.fork_url`、`inputs.fork_branch` 三个维度。 |
| **2.12 (并发锁 / 状态序列化 / 原子写)** | 1. 单测:mock `SharedMirrorStore.shared_read`,断言 `BuildcacheService.run(action="build")` 在调用 `build_docker_stage` 期间持有 `shared_read()` 锁;<br>2. 单测:`SharedBuildcacheStore.write_coverage` 写入 `check_kind="count"` 后读回仍为 `"count"`;<br>3. 单测:含 `ManualPackage(file=..., dest=..., sha256=..., spec="vasp")` 的 `EnvironmentSpec` 经 `parse_environment_spec(spec.as_dict())` 往返后 ` == spec`;<br>4. 单测:模拟写入中途抛异常,断言 `write_lock_manifest`、`_write_resolved_repo_sidecar`、`SharedMirrorStore.write_manifest`、`run_mirror_prune` 不会覆盖或截断已存在的目标文件,且不残留未清理的损坏目标文件;`_build_write_fetch_specs_script` 生成脚本采用 `.tmp` + `mv -f`。 |
| **2.13 (`validate` 模板渲染与 `--format json` 完备性)** | 1. 单测:在 `Dockerfile.j2` 或 `*.def.j2` 中注入未定义变量 `{{ no_such_var }}`,运行默认 `hpc_cf validate --app-version <env>`(`--profile build-input`)与 `validate_environment(env_dir, ValidationProfile.CONFIG)` 均返回 `report.ok is False` 且含 `template.undefined` finding;<br>2. 单测:测试 `collect_render_copy_sources` 对多行 `COPY a \\\n b /dst`、`COPY --link missing.txt /dst`、`COPY ../outside /dst` 均能准确检出 `render.missing_copy_source` / 路径越界;<br>3. 单测:破坏 `env.yaml`、`resolved-repos.yaml` 或 `spack.lock`(`lock_source_commits`),运行 `hpc_cf validate --app-version <env> --format json`,断言 stdout 始终为合法 `json.loads(...)` 对象且 `ok is False`,进程不抛未捕获 traceback;<br>4. 单测:`spack.yaml` 中 `repos.builtin.commit` 填入 40 位非 hex 字符(`"z" * 40`),`collect_spack_yaml` 报 `spack_yaml.commit_format` ERROR。 |
| **2.14 (`runtime_cleanup.j2` 前移至 `builder`)** | 1. 渲染测试:全量 12 个 Debian Spack 环境渲染后的 Dockerfile 中,`find /opt/spack /opt/spack-view` 静态库/头文件/`.spack` 删除指令出现在 `STAGE 2: BUILDER`(`COPY --from=builder /opt/spack` **之前**),`runtime` 阶段不再对 `/opt/spack` 执行事后跨层 `find ... -delete`;<br>2. 保持 `spack_manifest.j2` 在清理 `.spack` 之前运行(确保 `spack find` 生成 manifest 不受影响)。 |
| **2.15 (MKL/ROCm 构建 / GPU MOTD / `.def.j2` / `DependencyGraph.dot`)** | 1. 渲染测试:`cp2k_mkl-2025.2-experimental` 与 `cp2k_rocm-2026.1-gfx942` 渲染后的 Dockerfile 不含后台 `libint &` 或 `sleep 10`,且 `libint` 安装受 `-j 12` 约束;<br>2. 脚本测试:在 `PATH` 含模拟 `rocm-smi`(输出空、exit 0)且无 `/dev/kfd`、以及有 `/dev/kfd` 两种场景下,用 `bash -euo pipefail spack-envs/cp2k_rocm-2026.1-gfx942/cp2k-gpu-motd.sh` 执行返回码为 `0`(不报 `syntax error in expression`);<br>3. 静态检查:13 个 `*.def.j2` 中的 `--app-version <name>` 均精确匹配其所在目录名,`abacus.def.j2` 不含 `ABACUS.psmp` 或 `ABACUS_opensource`;`vasp_mkl-6.6.1-*/testsuite-file/spack_build_test*.conf` 使用 `--bind-to none` 且不含 `OMP_PLACES=cores`;<br>4. `pipeline-cp2k.yml` 的 `gh release upload` 与清理步骤包含 `DependencyGraph-${ENV_NAME}-${NIGHTLY_DATE}.dot`。 |
| **2.16 & P2 边界项** | 1. 文档守卫:`python scripts/generate_docs.py --check` 通过;`docs/ADD_NEW_ENV.md`、`docs/ASSETS_GUIDE.md`、`docs/TEMPLATE_MATRIX.md` 不再出现已退役的 `force_avx512_repo_path` 指导;`README.md` 与 `docs/README.md` 中 Spack 版本与各环境 `env.yaml` 完全一致;<br>2. `mirror_prune.py` 单测:`plan.delete_blobs` 仅含 git blobs,与 `plan.archive_delete_blobs` 集合无交集(`set(plan.delete_blobs).isdisjoint(plan.archive_delete_blobs)`);<br>3. `container.py` 单测:`is_running`/`status` 使用锚定正则 `^/?<name>$`;<br>4. `storage_gc.py` 单测:`_is_build_argv(["/usr/bin/podman", "--root", "/scratch/podman", "build", "-t", "x", "."]) is True`;<br>5. `environment.py` 单测:`spack.env_name` 或 `spack.version` 含空格/单引号/分号时 `parse_environment_spec` 直接拒绝。 |

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
| `templates/partials/cp2k_upstream_clone.j2` / `scripts/apply-source-override.sh` | **P0 所在**:fork `cp2k_source_repo_url` 丢失(:17,28,40)与软链原地污染(:47) |
| `spack-envs/vasp_mkl-6.6.0-avx512/spack-env-file/env.yaml` | **P0 所在**:`buildcache.enabled: true`(:61-65)与 3 阶段 `Dockerfile.j2` 失配 |
| `hpc_cf/buildcache.py` / `buildcache_workflow.py` / `execution.py` | 756/422 行分解对象;producer 漏 `mirror.lock`(:534-567);`write_coverage` 漏 `check_kind`(:576-600) |
| `hpc_cf/environment.py` | model+parser 混装;resolved-pins I/O(:1018-1128);`as_dict()` 漏 `spec`(:449-456) |
| `hpc_cf/cli.py` / `env.py` / `spack_ops.py` | 分发器内联(:610-874);`validate` 默认 profile 漏渲染(:759);legacy shims(env.py:63-196) |
| `hpc_cf/pin.py` | `env set-version` 的机制模板(外科编辑+守卫+回滚,:172-249) |
| `hpc_cf/validation.py` / `lock_manifest.py` / `repo_pins.py` | 模板/`.def.j2` 渲染下沉落点;`collect_render_copy_sources`(:644-677);manifest 原子写(:64-68) |
| `templates/partials/runtime_cleanup.j2` | Spack 文件删除前移至 `builder` 阶段 `COPY` 之前的落点(:16-48) |
| `.github/workflows/pipeline-cp2k.yml` | **P0 所在**:前缀清理误删(:396/809/1132);4× 前导(:144/433/605/846);artifact 路径(:339-341);`DependencyGraph.dot`(:981/1059) |
| `.github/workflows/stable-release.yml` / `ci-tests.yml` / `nightly-auto-retry.yml` | env 族白名单(:72-75);permissions/timeout 最小化落点 |
| `docs/agent-handoff-fragility-audit.md` | Round 1 审计(全部已修,作对照) |

## 6. Round 2 确认的强项(接手时勿破坏)

- fail-closed schema(`_reject_unknown_keys` + 严格 schema_version + 类型化枚举)
- ValidationReport/profiles + fix_hint 体系;零裸 except;路径禁闭全覆盖
- 513 测试含失败基线/快照/契约测试;100% 返回注解
- doctrine 写进代码(两阶段 lock、resolved-repos、lock-manifest)
- 管线:per-env 并发族、前缀过滤 prune、ephemeral token、secret mount、
  事故记忆内联在修复行处

---

## 7. 复核结论(2026-11-02,两个并行验证 agent 逐条对照代码)

外部补充部分经逐条验证:**机制与行号基本全部属实,但严重度需校准**。
执行 §3 计划时请以本节为准。

### 7.1 三个「新增 P0」的校准

| 编号 | 验证结论 | 校准后严重度 | 关键事实 |
|---|---|---|---|
| 2.1a 前缀清理误删 | **部分成立** | **P1** | 三处 startswith 过滤与嵌套前缀逐行属实;但触发条件(跑基础 env `cp2k_opensource-2025.2`)从未发生且无调度路径(release/ 分支只有 fa 与 abacus)。**真实在线变体**:fork 镜像 tag(`-fork-*`)以 nightly 前缀开头,被计划任务计入 keep-3/keep-7 预算,**一周后真的会删掉过期 fork 镜像**(其 SIF release 幸存);stable 分支误删需人为误 dispatch(有 2026-10-29 前科)。三步均 continue-on-error、SIF 主交付物不受影响 |
| 2.1b fork 工作流断裂 | **B1/B2 成立;B3 部分成立** | **P1**(B1 是 `16fb3c4` 引入的真回归);B3 → P2 | B1:partial 三分支硬编码上游 URL,fork-only commit 的 `git fetch` 必败(regtest clone 段,非 install 源);B2:`repo_pins.py:528-535` 对未被使用的 `cp2k_source_repo_url` 报 dead dual-write → fork dispatch 在 prepare 的 validate 即死(~1 分钟,快速失败);B3:软链+sed 是**脚本自身文档化的设计**(其测试断言"through the symlink"),CI 内 ephemerally 重建、无回写,resolve() 坍缩只影响本地污染与错误归因。**综合:fork 路径 DOA(广告的功能完全不可用),但无数据丢失、快速响亮失败** |
| 2.1c vasp 6.6.0-avx512 buildcache 失配 | **配置失配属实;行为无害** | **P2** | env.yaml enabled:true + 3 阶段旧模板属实;但 `--buildcache auto` 无 coverage 时降级 NEVER(纯源码构建,padding 注入子断言**被驳回**——`spack_buildcache_config.j2` 不在模板里,`padded_length` 根本不注入);producer 会在 podman 阶段响亮失败但**副作用是把共享 health.json 标 unhealthy**,累及同机无 coverage 的 `only` 消费者。修法照旧:enabled:false + validate 守卫 |

**真正保持 P0 的只有原 2.1(template_vars 覆盖保留键)。**

### 7.2 P1/P2 新增项的校准(逐条)

**维持 P1**:
- 2.12a producer 构建漏 `mirror.lock` — 属实(对照 `workflows.py:293-302` 双锁;producer 只拿 consumer_lock,并发 prune-apply 可中途删归档)
- 2.13b validate `--format json` 异常逃逸 — 属实(`apply_resolved_repo_pins` 的 ValueError 逃过 cli 的窄 catch,json 无输出)
- 2.15a MKL/ROCm `libint &` 无 wait — 文本逐字属实(双倍 `-j $(nproc)` 竞态)
- 2.16 文档漂移全部属实:ADD_NEW_ENV 指导已退役变量(照做触发守卫报错)、README 表 Spack 版本错 3 行(实际均 1.2.0)、TEMPLATE_MATRIX 的 partials 状态段未更新

**降级 P2**(事实成立、影响有限):
- 2.13a 渲染探针仅 CONFIG(默认 build-input 跳过)— 属实,但 build 路径自身渲染后仍查 COPY,仅 validate 盲区
- 2.14 runtime_cleanup 层序 — 属实(COPY 后删除=whiteout),**但仅 OCI/GHCR 产物膨胀;SIF 经 mksquashfs 压平会回收**
- 2.12d 六处非原子写 — 属实,消费端对截断 JSON 均 fail-closed(`execution.py:602-610` 已有原子写的先例可循)
- 2.15d 中 4 个 vasp 6.6.1 testsuite conf 硬编码 `--bind-to core`/`OMP_PLACES`(与 force-OpenMP preset 矛盾,功能性);storage_gc `_is_build_argv` 全局 flag 误判

**降级 P3**(代码事实、当前无触发或纯观感):2.12b(write_coverage 漏 check_kind — **零生产调用者**,真写在 publish_success_state)、2.12c(as_dict 漏 spec — 仅 deprecated 路径,推断兜底)、2.13c COPY 解析三缺陷(现有模板无触发形态)、2.15b GPU MOTD 崩溃(登录横幅,非应用)、.dot 提取不上传、def.j2 注释错贴、mirror_prune 二次 unlink(missing_ok 幂等)、ps 子串匹配、AVX2 误报

**事实纠错两处**:①「3 份 entrypoint 完全一致」→ 实为仅注释差异且**全部在用**,不可删;②「普通 build 注入 128 padding」→ 驳回(见 7.1)。

### 7.3 对 §3/§3.5 执行顺序的修订建议

1. **A 档顺序内提级**:2.1b-B1(cp2k_upstream_clone.j2 恢复 `cp2k_source_repo_url`)是本仓自己引入的回归,修复=一行 ×3 分支,应排 A 档第一;2.1a 的正则化修法照旧但按 P1 排期
2. 2.1c 按修法照做(enabled:false + 守卫),无需紧急
3. §3.5 验收表可沿用,但 2.1c 验收第 1 条的"或补齐 builder-installed"分支不建议(6.6.0 明确不迁 buildcache)

---

## 8. A 档执行记录(2026-11-02,§7.3 修订版顺序,11 commit 全绿)

按 §7.3 修订顺序完成 A 档全部快赢项(每 commit ruff 0 错误 + pytest 全绿;
基线 645 → 718 passed)。**§3.A 与 §7.2 维持 P1 的全部项已收口**;A+ 档剩余
项见下表「未做」列。

| 项 | commit | 内容 |
|---|---|---|
| 2.1b-B1+B2(§7.3 提级第一) | `296b39f` | `cp2k_upstream_clone.j2` 三变体恢复 `{{ cp2k_source_repo_url \| default(upstream) }}`;fork 形状过守卫(渲染逐字节不变已验证) |
| 2.1(真 P0) | `cc65e41` | `RESERVED_CONTEXT_KEYS`(build_context 计算键 + plan/buildcache/no_spack 注入键)在 parse 拒绝;`lock_source_commits` 目标同受约束;契约测试钉住集合 ↔ build_context/plan_context 实际键集;`apt_mirror` 仍为文档化例外 |
| 2.1a(+第四处) | `03fcc1b` | 四处清理(mirror/runtime/**fork 单版本强制**/stable release)全部 startswith → 锚定 `test()` 正则;契约测试按 tag 列表钉住匹配集 |
| 2.1c | `6d76b15` | vasp 6.6.0-avx512 `buildcache.enabled: false`;validate config/build-input 新增 `buildcache.dockerfile_missing_stage` ERROR |
| 2.6 + 2.2 | `d68746b` | prepare/binaries/runtime job 级降权(contents:read+packages:write,仅 sif 留 contents:write);concurrency 组三维化(env_name × fork_url × fork_branch),**regen-lock 组同步为 upstream-default 变体保持互斥**;ci-tests contents:read+timeout;stable-release packages:write 移到调用点 + resolve 5min timeout + env 族白名单 |
| 2.3(artifact 部分) + 2.15c(.dot) | `f8485c9` | build-lock artifact 补 `lock-manifest.json`(两处消费点同步 cp);release 上传/清理 `DependencyGraph-*.dot`(与 BUILD_MANIFEST 共享保留预算) |
| 2.4 | `74812af` | buildcache only-fail-closed/auto-fallback 消息带 status/resume/build 下一命令;repo-pin fix_hint 提 `pin set`;`__main__` 漏斗放宽(OSError/KeyError,KeyError 具名) |
| 2.13b | `b58f572` | `_cmd_validate` 捕 ValueError → `build_input.invalid` finding;渲染探针补捕 jinja2 `TemplateError`(StrictUndefined 抛 UndefinedError 非 RuntimeError) |
| 2.12a | `3ba3127` | producer 构建外层套 `SharedMirrorStore.shared_read()`(与正常 build 双锁 doctrine 对齐);测试断言 build_docker_stage 期间持锁 |
| 2.15a | `2566a2b` | MKL/ROCm `libint &`+sleep 后台竞态 → 前台 `-j 12` 先装 libint 再全量;渲染+源测试钉住 |
| 2.16 | `5159684` | ADD_NEW_ENV/ASSETS_GUIDE/TEMPLATE_MATRIX 改为 plan-driven 现实;README 两表 3 行 Spack 版本纠错;2025.2 README libint 版本、master-fa skala 路径纠错;新增两个 drift 守卫(版本列 == env.yaml;退役变量零出现) |

**A 档未做(属 A+/B/C 档,见 §3)**:2.14 runtime_cleanup 前移(P2,仅 OCI 膨胀);
2.12b/c/d(write_coverage check_kind 死代码、as_dict 漏 spec、六处原子写);
2.15b GPU MOTD(P3);2.15d 其余 .def.j2/testsuite-file 修正;2.12 manifest
回填(操作侧,§4);C 档 `env set-version`/`assets run|status|prune`/`status`。

### §8.1 A+ 追加(2026-11-02,同日):渲染 sanity 默认化 + 收集器注册表

用户目标「sanity 的默认 + 人类也可以轻松添加」落地为三个 commit:

| commit | 内容 |
|---|---|
| `d6b522b` | `collect_render_sanity` 沉入 validate_environment(config+build-input):Dockerfile.j2 以完整真实形态(use_mirror=True 含 runtime 阶段)StrictUndefined 渲染 + COPY 源扫描;**全部 13 个 `*.def.j2`** 以 sif 契约上下文(docker_tar_filename 桩值)首次纳入渲染校验;cli 内联探针删除。COPY 扫描加固:续行合并、`--link` 无值 flag、根外路径 `copy.source_outside_root` ERROR;`repos.builtin.commit` 40-hex(原 40 任意字符 WARNING → ERROR)。2.13a/2.13c(部分)/2.13d 关闭 |
| `7769f7f` | `_COLLECTORS` 声明式注册表:检查 = 一个 `collect_*` 函数 + 一行注册(profiles + spack_only);`test_validation_registry.py` 元测试保证公开 collect_* 不注册即失败。无行为变更 |
| (本 commit) | `docs/VALIDATION.md` 配方 + AGENTS.md 一行指针 |

注意:探针发现两个接线陷阱并已处理 —— hpc_cf 的 `render_template` 把
UndefinedError 包成 **RuntimeError**(只捕 TemplateError 接不住);
渲染不带 layout 才能回落默认仓库 templates 树(tmp 布局的合成环境需要)。

### §8.2 A+/C/E 档执行记录(2026-11-02 续,11 commit,基线 718 → 760 passed)

| commit | 档 | 内容 |
|---|---|---|
| `93f5c34` | A+ | `hpc_cf.atomic_io`(唯一 tmp + os.replace)迁移六处侧车/状态写;容器侧 fetch-specs 改 `.tmp`+`mv -f`(2.12d 关闭) |
| `f444c2f` | A+ | GPU MOTD 无 GPU 崩溃修复(`grep -c`双输出 / kfd 分支)(2.15b) |
| `ae7c13b` | A+ | 9 个 .def.j2 错贴修正 + vasp 6.6.1 testsuite conf 对齐 force-OpenMP + 静态守卫(2.15d) |
| `e39e584` | A+ | write_coverage 补 check_kind;as_dict 补 spec 并改为条件省略 oci-only 键(往返测试顺手抓出 null-key 破坏往返的同类 bug)(2.12b/c 关闭) |
| `cedd590` | C | `env set-version`:token 边界替换 + 重解析 + 守卫 + 整体回滚 + `--delete-lock` 门(2.7) |
| `ad04e62` | C | `assets run\|status\|prune` 任务化;prune 独立(复用持久化 sidecar,单幸存者规则),stage flag 组合响亮拒绝(2.8) |
| `95dd732` | C | `hpc_cf status` 只读聚合(buildcache health/coverage/runs + mirror 最新 run + per-env 锁状态)(2.9) |
| `7c47718` | E | 四段近逐字 job 前导抽 `scripts/ci_job_preamble.sh`(workflow 净 -248 行);dispatch 输入 env: 间接化;契约测试防回归(2.5) |
| `9f43baa` | D-lite | 三个零调用死符号删除(`build_apptainer`/`build_check_script`/`GitPrunePlan`;audit 的第四候选实为在用,保留) |
| `e219c52` | F | pyproject.toml:ruff 钉 E,F,W,I,B,UP(E501 有意关,理由写在文件里);25 自动修 + 1 手修(B904) |

**剩余(有意未做)**:D 档大分解(buildcache_workflow.run 422 行拆分、
buildcache.py/environment.py/cli.py 分层、legacy shims 迁移、
`find_spack_lock` 统一四处 lock 路径拼法);F 档 mypy(建议 D 之后);
runtime_cleanup 前移(用户决定先不做);manifest 回填 + rocm 重建(用户置后);
操作侧(push 需授权、备份分支、legacy/)。

### §8.3 推送后 nightly 回归修复(2026-11-03)

nightly-cp2k 2026-11-02(run 41542691099,自动重试的 attempt 2 同样失败)
挂在 binaries job 的预检:`lock.stale_input: custom_repos['cp2k_dev'] pin
changed since concretization (recorded None, current f8a708ff…)`。

| commit | 内容 |
|---|---|
| `6543aa5` | 根因:assets 用原始 env.yaml 写 lock-manifest(浮动 repo 没有 commit,所以不记录),而所有构建入口用 resolve_build_input 视图比较(先套 resolved-repos.yaml,浮动 repo 带 resolved tip)。写方 `0b995bb`(11-01)与把 manifest 送进消费 job 的 `f8485c9`(11-02)同一次推送上线,当晚首个 nightly 即失败("潜伏"只指本地提交顺序,线上从未有过带 bug 而不出错的阶段)。修法:`environment.load_resolved_environment_spec` 是写方与所有读方(template、status)共用的唯一加载器;assets 先写 sidecar 再写 manifest。用失败 run 的真实 build-lock artifact 在干净 worktree 验证:旧代码逐字复现报错,新代码通过且 status 为 fresh |
| `8880500` | 审查 runtime/sif job(新代码上从未真实运行)时发现两处 round-2 回归:① `03fcc1b` 的 runtime 清理正则漏了 `-stable-<date>`,stable 镜像不再被清理;② `7c47718` 把 `! grep -q ustc` 挪进脚本中段,`set -e` 不拦取反命令,守卫失效 |
| `8333ed1` | stable 首次真实运行暴露的既有 bug(源自 10-29 `d51b540`,非 round 1/2 引入):"Latest 只给 FLAGSHIP_ENV"的实现是非旗舰按 API 默认值创建(即成为 Latest)、上传后再 PATCH `make_latest` 为 false。PATCH 返回 200,但 GitHub 不会把 Latest 还给别的 release,标记留在新 release 上(run 41567667246 后 Latest 落在 2025.2)。修法:非旗舰创建时即带 `--latest=false`,删除无效的事后 PATCH;测试直接执行 sif job 里真实的 "Resolve release identity" 脚本,钉住三种创建参数。已存在的 release 不受代码影响,现状由一次性 PATCH(旗舰 release 设 `make_latest=true`)修复 |
| `190e470` | 三个 GHCR 清理步骤从引入起就没删过任何东西:`gh api` 没有 `--arg` 参数(jq 独立程序的参数),进程替换内的失败 `set -e` 看不见,空列表被当成 "within budget"。fork 步骤坏自 09-07 `3d21102`,mirror/runtime 步骤坏自 10-29 `9d0de29`(照抄了 fork 写法,jq 只在本地 jq 上验证过)。历次 run 日志确认 0 次删除。修法:共享 `scripts/ghcr_prune_versions.sh` — `--paginate` 全页取回 + 独立 `jq -rs` 一次算 keep-N(gh 内置 jq 按页执行会漏算)+ 查询失败要真失败(步骤仍 continue-on-error:可见但不杀发布)。测试用假 gh 真执行脚本:跨页 keep-N、保留保护、兄弟环境免疫、失败响。首次生效只会删 master mirror 的 3 个旧版本(~20.7 GB),其余环境均低于上限;无 tag 版本(同日重推/自动重试产生)对任何按 tag 匹配的清理不可见,为已知缺口 |

**真实运行验证**(推送后):手动 nightly(run 41561284448,master)与 stable
2025.2-force-avx512(run 41567667246)均第一次就全绿,preamble、lock 门、
DependencyGraph 上传、`-release` 浮动 tag 都已真实执行。仍未真实运行:fork 构建
路径;stable 清理新正则已执行但因版本数未超上限(镜像 7、release 5)没有实际删除。

**教训**:round 2 的 CI 改动只经过单元测试和 YAML 解析验证;推送后真实流水线
只跑到 binaries 第 10 步。b63c269 之后 50 个 commit 里,runtime/sif job 的改动
(preamble、清理正则、DependencyGraph 上传)还没在真实 run 中执行过,下一次
nightly(或手动 dispatch)才是第一次。§2 里 2.1a 修法给的正则(`nightly|release`)
同样漏了 stable,以本节为准。
