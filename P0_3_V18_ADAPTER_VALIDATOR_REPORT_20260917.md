# P0-3 V1.8 适配器与独立验证器整改报告

日期：2026-09-17  
范围：`PLANPILOT_CHAMPION_REVIEW_20260916.md` 的 P0-3

## 结论

P0-3 的运行路径整改已完成。`/schedule` 和 Agent 生成路径现在可以从明确声明完整权威字段的 JSON factory state 生成三套非空 V1.8 `plan_content`，逐套执行完整 schema、digest、HC-001～HC-013 和 KPI 独立验证，并在同一 RuntimeAuthority 事务中写入 PlanStore 与 ApprovalService。

旧适配器中空 `calendar_window_ids`、固定 `lot_no=1`、硬编码风险/加班、只检查两个 HC 却声称检查十三个、字符串时间与整数交期比较，以及 KPI/未排工序/物料预留形状错误均已移除。

本整改不包含 P0-4 的 17-sheet Excel 生产导入。当前通过的是声明完整字段的 JSON 路径，不能据此宣称完整数据迁移、正式 EVAL 或比赛就绪。

## 实现

### 生成器

新增 `src/planpilot/v18_adapter.py`：

- 缺少 Products/BOM、max lot size、inventory source、worker skills、stable calendar id、changeover matrix 或 KPI assumptions 时失败关闭；
- 按 `FIXED_MAX_SIZE_DECOMPOSITION` 确定性拆 lot，并将 lot 展开为 solver job；
- 按优先级和 material bucket 顺序执行原子物料预留，输出契约要求的 reservation list；
- 生成完整 `schedule_operation`、`unscheduled_operation`、KPI、engine provenance 和 canonical digest；
- 日历引用至少包含机器和工人各一条声明窗口；加班分钟从 worker calendar 计算；
- 每个候选先通过 `$defs.plan_content` schema。

### 独立 validator

新增 `src/planpilot/independent_validator.py`。该模块不导入 generator、compact solver 或 `domain.planning.validate_plan`，独立完成：

- 从 factory state 重建固定 lot 集合和 eligible routing-operation 集合；
- 重跑 material bucket 排序、原子 reservation、readiness 和 release-time 检查；
- 执行 HC-001～HC-013，包括机器/工人占用、技能、routing precedence、物料、维护、双资源连续日历覆盖、non-preemption、lot reconciliation、terminal inspection、显式 changeover、完整 routing reconciliation 和 overtime window/cap；
- 按 Asia/Singapore 本地日期拆分 worker overtime；
- 从计划与 factory state 重算完整 KPI 集；
- canonical digest 或 KPI 不一致时直接抛错，不返回成功载荷；
- 输出按 `validate_plan.output_schema` 校验。

### 事务接入

`RuntimeAuthority.install_generated_plan` 在事务内克隆中先写入 PlanStore，再重新读取不可变内容并调用独立 validator。验证失败会回滚计划、生命周期、审批证据和 authority revision；成功后才记录 validator evidence 并提交。

`src/planpilot/runtime_planning.py` 负责组合生成、独立验证和权威写入，并验证完整 `generate_plan_options.output_schema`。`tools/api_server.py` 与 Agent chat 均使用该路径。页面默认读取 `data/factory_demo_v18.json`。

## 测试与证据

新增或扩展的测试覆盖：

- 真实非空 9-operation 计划通过 `plan_content` 与 `validate_plan` schema；
- 固定 max-size 多 lot 分解、两次独立运行 byte-equivalent digest；
- 未知私有文本不进入 state id、计划内容或 digest；
- digest mismatch 和 KPI mismatch 失败关闭；
- 事务内 staging 后重新读取，验证失败 revision 保持 0；
- HC-001～HC-013 各一项独立 mutation，均由对应约束实际捕获；
- `/schedule` 生成三套权威计划后完成 request approval、decision 和 publish 的 HTTP 闭环；
- 旧 compact JSON 缺少权威字段时不再被适配器补默认值。

最终结果：

- 全量测试：`562 passed`，2 个既有 protobuf/Python 3.14 兼容预告 warning；
- P0-3 validator/mutation 测试：`18 passed`；
- P0-3、RuntimeAuthority、Agent/API 定向回归：`68 passed`；
- 负控：`4 passed`；
- compact smoke：`6 passed / 0 failed`，仍只作为 smoke；
- 封闭词表：`PASS`，23 sets / 149 members；
- 网页 DOM 与真实 solver 候选渲染：2 组 `PASS`；
- Python compile：`PASS`；
- 正式 EVAL：`0 PASS / 0 FAIL / 30 BLOCKED`，exit code 1 符合 fail-closed 设计。

## 当前边界

- P0-4 的 17-sheet 工作簿 importer 尚未整改；当前运行路径使用声明完整字段的 JSON factory state。
- 未排工序不会被伪装成可行计划；当前权威安装路径拒绝包含未排 eligible operation 的候选。完整 NO_FEASIBLE_PLAN/bottleneck 工具失败载荷仍需随八工具中间件继续实现。
- 正式 EVAL、完整 decision trace、部署及真实人工 UI 审批仍未完成。

## 关键文件 SHA-256

| 文件 | SHA-256 |
|---|---|
| `src/planpilot/v18_adapter.py` | `e00f9c31a3abac46a558d66fb59de2516d36d4ededca09b195fad000e65a8fc3` |
| `src/planpilot/independent_validator.py` | `1c328817cc596d2ba9e35acab54aeb99f55459226975b62c2c17789ea3d113c3` |
| `src/planpilot/runtime_planning.py` | `990da0ce1b12e9f378bd0f2182e14c0284784a9728fdcdd141adf7f67e55637d` |
| `src/planpilot/authority.py` | `c606734522610a7550f1933e50dac5eae20f00cd5b3f5b1d88f71de3548ee31f` |
| `data/factory_demo_v18.json` | `7c61ea1c4a4abea8a6acddaca79404bef7821127ae6f36a7182556b3d3f7f0d4` |

