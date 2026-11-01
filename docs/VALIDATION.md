# 校验体系（sanity checks）

`hpc_cf validate` 的所有检查住在 `hpc_cf/validation.py`，按 profile 分层执行：

| Profile | 谁在用 | 内容 |
|---|---|---|
| `config` | `validate --profile config`（CI prepare） | 便宜的静态检查：schema、repo-pin 一致性、模板渲染探针 |
| `build-input` | `validate` 默认；build 前置 | config 全部 + assets/lock/manifest/mirror 门禁 |
| `assets` | assets 工作流 | assets 侧输入检查 |

默认即全量：渲染探针（StrictUndefined 渲染 `Dockerfile.j2` 与每个
`*.def.j2`、COPY 源存在性）在 `config` 与 `build-input` 都会跑，日常
`python -m hpc_cf validate --app-version <env>` 不需要额外 flag。

## 如何添加一条 sanity 检查

三步，全部在一个模块里：

1. **写一个 `collect_*` 函数**，返回 `list[ValidationFinding]`
   （ERROR / WARNING / INFO，带 `code`、`path`、`fix_hint`——`fix_hint`
   给出下一条可执行命令，不解释理论）。
2. **在 `_COLLECTORS` 表里加一行**：名字、生效 profile 集合、是否
   spack-only（`method: no_spack` 环境跳过）。`validate_environment`
   只执行这张表，没有别的接线点。
3. **补测试**。`tests/test_validation_registry.py` 会自动失败，如果
   公开的 `collect_*` 函数没有注册进表里——新检查不可能悄悄漏接线，
   也不可能注册成死行。

改动前后跑：

```bash
./venv/bin/ruff check hpc_cf/ tests/ scripts/
./venv/bin/pytest -q
```

## 现有检查一览

注册表即清单（`hpc_cf/validation.py` 的 `_COLLECTORS`）。当前条目：
branch_consistency、repo_pin_consistency、buildcache_stage、
render_sanity、spack_yaml、manual_packages、spack_assets（build-input
与 assets 两行，bootstrap 要求不同）、mirror_content、spack_lock、
lock_input_manifest。

## 优先级与保留键

- `build_context` 的键优先级：**CLI > env.yaml > defaults**；工厂自己
  计算或注入的键（`RESERVED_CONTEXT_KEYS`，见 `hpc_cf/environment.py`）
  不允许出现在 `template_vars`——parse 直接拒绝。
- `apt_mirror` 是唯一文档化的例外：env.yaml 可设，CLI `--apt-mirror`
  仍然优先。
