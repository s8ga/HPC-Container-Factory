# 当前已知问题

## CP2K 2026.2：SPGLIB 混合并行敏感性

测试 `QS/regtest-kp-1-spg/c_geo_opt_kpsym_spglib.inp` 对特定 MPI/OpenMP 分解敏感。补充测试中：

- `1x1`、`1x2`、`2x1`、`2x2`、`1x4`、`4x1`、`2x4`、`8x1`、`1x8`、`3x2` 通过。
- `4x2` 连续 5/5 次失败，residual 为 0.282 至 0.489；`2x3` 和 `4x3` 也失败。
- 关闭 keepalive 未解决 `4x2`；提高 cutoffs 至 400/60 后仍在 3 次中失败 1 次。

建议避免未经目标输入验证的混合分解。只有测试名和 residual/abort 特征精确匹配、且 targeted retest 通过时，才可归类为此已知间歇性问题；其他失败仍是 release blocker。

## CP2K 2026.2：TDDFPT tolerance-sensitive 变化

`QS/regtest-tddfpt-4/test09.inp` 的 `TDDFPT_Check_Osc_Strength` 曾出现一次相对差 `4.38e-5`，略高于当前 `4e-5` tolerance。之后 isolated、concurrent 和 keepalive 复测均通过；8 种已测并行组合各自通过 46/46 checks，相对误差为 `5.12e-6` 至 `1.99e-5`。

这不是当前发布中的失败项，但后续结果必须按测试名、matcher 和 tolerance 判断，不能把任意 TDDFPT 偏差归入已知问题。

完整边界、上游引用和原始结果见：

- [CP2K 2026.2 发布说明](releases/CP2K_2026.2.md)
- [CP2K regtest 日志](../spack-envs/cp2k_opensource-2026.2-force-avx512/cp2k-regtest.log)

## ABACUS 3.9.0.27：Module unit tests 部分失败

已入库 [abacus-module-test.log](../spack-envs/abacus_opensource-3.9.0.27-force-avx512/abacus-module-test.log)：227/241 passed，14 failed。主要类别：

- `INPUT` / `KPT` / `STRU`：非 gtest 可执行文件（rc=127），不应当作单元测试二进制。
- `MODULE_HSOLVER_*`（`LCAO`、`cg`、`dav*`）：runner 默认 `timeout 30` 超时（rc=124）。
- `MODULE_HSOLVER_LCAO_PEXSI`：PEXSI 与参考差值超阈值。
- `MODULE_ESOLVER_esolver_dp_test` / `test_deepks`：DeePMD TensorFlow 后端未构建或 DeePKS 相关失败。
- `MODULE_BASE_*`：`clebsch_gordan`、`cubic_spline`（assert abort）、`matrix3` 数值/断言失败。

Integration（`abacus_run_integration_tests.sh`）仍为 10/10。完整清单见 [ABACUS 3.9.0.27 发布说明](releases/ABACUS_3.9.0.27.md)。

## ABACUS 3.10.1：Module unit tests BLOCKED

现有镜像 concrete spec 为 `abacus@3.10.1-lts tests=false`，无 `share/abacus/tests` / `MODULE_*` 二进制。证据日志标记 Status=BLOCKED；需以 `+tests` 重建 OCI 后才能补跑。见 [abacus-module-test.log](../spack-envs/abacus_opensource-3.10.1-force-avx512/abacus-module-test.log) 与 [发布说明](releases/ABACUS_3.10.1.md)。

## ABACUS 3.10.1：Integration Autotest 8 项失败

挂载上游 `tests/integrate/Autotest.sh`（`CASES_CPU.txt`，356 cases）结果为 348/356。已知失败：

- `101_PW_15_paw`：未编译 PAW（`USE_PAW`）。
- `101_PW_upf201_uspp_NaCl`、`102_PW_BPCG`、`102_PW_PINT_UKS`、`107_PW_outWfcR`：属性/数值检查失败。
- `212_NO_wfc_get_wf`、`312_NO_GO_wfc_get_wf`、`312_NO_GO_wfc_get_pchg`：harness 缺少 `sum_ENV_H2_cube`（Fatal Error in catch_properties.sh）。

原始结果见 [abacus-integration-test.log](../spack-envs/abacus_opensource-3.10.1-force-avx512/abacus-integration-test.log) 与 [发布说明](releases/ABACUS_3.10.1.md)。
