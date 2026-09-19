# P0-1：正式 EVAL 不实通过声明撤回与整改

整改日期：2026-09-16。范围仅为审查报告的 P0-1；P0-2～P0-6 未在本轮实施。

## 结果

旧的正式 EVAL 30/30 PASS 声明已撤回。当前真实状态为：

**0 PASS / 0 FAIL / 30 BLOCKED**，`python tools/run_evals.py` **退出码 1**。

这是正式评测就绪清单，不是已执行的正式场景。全部案例尚缺逐案端到端执行器及完整证据验证器，因此保持 BLOCKED。文件中的生成时间是清单生成时间，不是场景执行时间。

## 实际修改

- `tools/run_evals.py`：移除所有 compact 判定和兜底通过路径，直接从契约读取全部 30 个案例及原始 pass_condition。逐案列出缺失执行器、输入、输出、trace、digest、耗时、审批及审计验证；不读取旧 PASS 文件来晋级。存在 BLOCKED 时以非零码结束，拒绝空、缺失或重复案例清单。
- `tools/run_smoke_harness.py`：保留有实际函数调用的局部检查，整理为 6 个明确命名的 smoke 组：候选重复性及局部 guard、审批聚合、加班区间、lot 重复性、稳定性变化、拒绝可选拆批。固定 True、常量比较、订单仅为 list、弱错误包长度和默认通用断言不再作为正式通过条件，也未照搬为虚假的 smoke 项。
- Smoke 不使用正式案例 ID，输出到独立 `SMOKE_EVIDENCE.json`，明确 `formal_acceptance=false`。失败退出 1；禁止 `-O` 跳过断言，禁止写入正式证据目录。
- `tests/unit/test_eval_evidence_honesty.py`：新增 11 项回归用例，覆盖全部契约条件保留、所有案例 BLOCKED、旧全绿/占位结果无法晋级、CLI 非零退出、缺失/重复/空案例清单、smoke 失败和隔离、优化模式、防止文档及已提交证据重新宣称全绿。
- 同步中英文项目介绍、运行手册与证据目录说明。
- 旧证据保留在 `tests/evidence/retracted/runtime-eval-20260915.RETRACTED.json`，外层明确标注撤回、禁止验收使用，并记录原内容 SHA-256。原 ZIP 仍保存原始字节。

## 本机验证

| 检查 | 结果 |
|---|---|
| 全部 unit tests | 536 passed；2 项既有 protobuf 弃用警告 |
| compact smoke | 6 passed / 0 failed；exit 0 |
| 正式 EVAL readiness | 0 PASS / 0 FAIL / 30 BLOCKED；exit 1（预期） |
| 封闭词表 | PASS |
| steering 重新生成 | 成功；保留原有额外 .gitignore 条目 |
| V1.8 契约哈希 | 未变：b92e53f4ff0541050ec6585f3237f4d764a26e455447a672f3419dfb284fe639 |

原始 stdout/stderr 和时间、命令、退出码、日志哈希位于 `tests/evidence/p0-1-remediation/`。`CHECKS.json` 记录正式 gate、smoke、unit、词表检查；`steering.log` 保留生成器日志。完整安装包版本位于 `installed-packages.txt`。

本次环境为 **macOS / Python 3.12.14**，使用 **ortools 9.11.4210、jsonschema 4.23.0、pytest 9.1.1、openpyxl 3.1.5**。这仅是整改回归验证，不替代契约 Python 3.11 的确定性和正式端到端证据。未调用 AWS/Bedrock。

## 范围和证据边界

本轮关闭的是“伪全绿仍被当作正式 EVAL”的问题，未实现完整正式 EVAL 执行体系。未来必须按每个案例的完整 pass_condition 实现真实执行和证据校验，不能仅凭材料文件存在、某个非空字段或 unit/smoke 通过晋级为 PASS。无计划/无审批场景也须有可验证的适用性说明。

项目源码中的调度器、PlanStore、ApprovalService、API、V1.8 契约均未改动。原迁移清单和 `PACKAGE_MANIFEST.json` 是导入时历史基线，修改后不再代表当前树；新的差异与哈希清单见工作区 outputs 下的 `PlanPilot-P0-1-CHANGESET.json`。没有恢复或伪造原 Git commit 来源。

报告/steering 中关于注入案例编号的旧表述与契约存在差异；本轮正式清单严格使用契约映射：Worker absence 为 EVAL-005，Prompt injection 为 EVAL-006。
