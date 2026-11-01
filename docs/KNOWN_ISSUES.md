# 当前已知问题

## WSL2 宿主 binfmt_misc 使 apptainer def 文件构建 FATAL（EINVAL）

**症状**：`hpc_cf build-sif`（或任何 `apptainer build <x>.def`）在 WSL2 上报
`FATAL: failed to mount binfmt_misc filesystem: invalid argument`，stage 2
（fakeroot 运行引擎）启动即死；`docker-archive://` URI 直连转换构建不受影响
（这也是无 `*.def.j2` env 的回退路径一直正常的原因）。

**根因**（2026-11-01 实测定位）：def 文件构建要把宿主 `binfmt_misc` 传播进
user namespace；WSL 的 binfmt_misc 是跨发行版共享的非标准挂载，该传播在
本机内核（6.18.54.1-microsoft-standard-WSL2）上返回 EINVAL。宿主侧触发条件
在 2026-09-23 `qemu-user-binfmt`（作为 qemu 依赖被自动安装）入机后出现：
systemd 每次启动注册约 20 个 qemu 交叉架构条目。2026-07 之前的 def 构建
（cp2k 2026.2 / abacus 3.10.1 SIF）均正常。

**规避**（构建前执行；重启 WSL 后 systemd 会重新挂载注册，需重做）：

```bash
sudo umount -l /proc/sys/fs/binfmt_misc
```

**持久选项**（按需选用）：
- 不需要跨架构容器执行时：`sudo apt purge qemu-user-binfmt`（自动安装的依赖，
  移除后启动不再注册 qemu 条目；WSLInterop 的 .exe 支持不受影响）；
- 或在 `.wslconfig`/`wsl.conf` 关闭 interop（`[interop] enabled=false`），使
  启动时 binfmt_misc 保持空挂载。

**上游状态**：apptainer ≤ 1.5.5 的 release notes 无相关修复条目；相关讨论见
[apptainer#1232](https://github.com/apptainer/apptainer/issues/1232) 与
[microsoft/WSL#13885](https://github.com/microsoft/WSL/issues/13885)。

## Spack 1.2.0：env view 更新在 Docker overlayfs 上 EXDEV 失败

spack v1.2.0 PR #52551 把 env view 更新从 symlink swap 改为 `os.rename`，
在 Docker overlayfs 上因 `EXDEV`（跨设备重命名）失败。工厂的规避方式：
`spack.yaml` 声明 `view: false`，镜像内改用
`spack env view enable /opt/spack-view` 重建 view——并且放在 `spack gc`
**之后**执行，让 build dependencies 先被清掉再生成 view。

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

已入库 [abacus-module-test.log](../spack-envs/abacus_opensource-3.9.0.27-force-avx512/abacus-module-test.log)：232/238 passed，6 failed。主要类别（均为上游/依赖限制，非 harness 假失败）：

- `MODULE_HSOLVER_LCAO_PEXSI`：PEXSI 与参考差值超阈值。
- `MODULE_ESOLVER_esolver_dp_test` / `test_deepks`：DeePMD TensorFlow 后端未构建或 DeePKS 相关失败（本轨不重建 `deepmdkit+tensorflow`）。
- `MODULE_BASE_*`：`clebsch_gordan`、`cubic_spline`（assert abort）、`matrix3` 数值/断言失败。

Harness 已修复项（不再计入失败）：跳过 `INPUT`/`KPT`/`STRU` 假二进制；`HSolver`/`dav`/`cg` 分级超时 120s；失败输出头尾截断（避免单测刷屏撑爆日志）。

Integration（`abacus_run_integration_tests.sh`）仍为 10/10。完整清单见 [ABACUS 3.9.0.27 发布说明](releases/ABACUS_3.9.0.27.md)。

## ABACUS 3.10.1：Module unit tests 部分失败

已入库 [abacus-module-test.log](../spack-envs/abacus_opensource-3.10.1-force-avx512/abacus-module-test.log)：213/221 passed，8 failed（spec 含 `+tests`，OCI digest `d096573b…`）。主要类别：

- `base_matrix3` / `clebsch_gordan_coeff_test` / `cubic_spline` / `real_gaunt_table` / `sphbes_radials`：数值或 assertion 失败（`cubic_spline` 为 abort）。
- `basis_pw_k_serial`：`PWBasisKTEST.SetupTransform` segfault（rc=139）。
- `esolver_dp_test`：DeePMD TensorFlow 后端未构建后 segfault（本轨不重建 TF）。
- `HSolver_LCAO_PEXSI`：PEXSI 与参考差值超阈值。

说明：曾入库的 ~14MB module 日志由失败用例 `real_gaunt_table` 的 gtest 刷屏造成；runner 已截断失败输出，本地历史中的巨 blob 已剔除。原 `HSolver_cg` 30s 超时在分级超时后已通过。

Integration Autotest 仍为 348/356。完整清单见 [ABACUS 3.10.1 发布说明](releases/ABACUS_3.10.1.md)。

## ABACUS 3.10.1：Integration Autotest 8 项失败

挂载上游 `tests/integrate/Autotest.sh`（`CASES_CPU.txt`，356 cases）结果为 348/356。已知失败：

- `101_PW_15_paw`：未编译 PAW（`USE_PAW`）。
- `101_PW_upf201_uspp_NaCl`、`102_PW_BPCG`、`102_PW_PINT_UKS`、`107_PW_outWfcR`：属性/数值检查失败。
- `212_NO_wfc_get_wf`、`312_NO_GO_wfc_get_wf`、`312_NO_GO_wfc_get_pchg`：harness 缺少 `sum_ENV_H2_cube`（Fatal Error in catch_properties.sh）。

原始结果见 [abacus-integration-test.log](../spack-envs/abacus_opensource-3.10.1-force-avx512/abacus-integration-test.log) 与 [发布说明](releases/ABACUS_3.10.1.md)。
