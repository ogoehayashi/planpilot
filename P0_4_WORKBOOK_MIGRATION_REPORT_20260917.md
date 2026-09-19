# P0-4 生产工作簿迁移与 Factory State 整改报告

日期：2026-09-17  
范围：`PLANPILOT_CHAMPION_REVIEW_20260916.md` 的 P0-4

## 结论

P0-4 已完成。运行时不再把 V1.8 工作簿交给旧的三表 compact Excel
adapter。新的生产导入器读取合同要求的全部 17 张表，执行结构、记录、引用、
单位、时区、BOM、技能、日历、路线和换线校验，再把可计划的规范状态以不可变
`state_id` 存入 SQLite。生成工具只通过该引用取回状态。

标准工作簿的生产导入结果为 `VALID_WITH_QUARANTINE`。唯一隔离记录是
`Events/EVT-005/Payload_JSON`，代码为 `PROMPT_INJECTION`；其他 3 个订单保留为
eligible orders 并能生成三套通过独立验证的计划。当前规范状态引用为：

```text
c3916c045df7f2679be04b908022c871db1c205894b4cde1f96e2f60aea7befb
```

## 实现结果

### 17-sheet 生产迁移

新增 `src/planpilot/factory_state.py`：

- 强制检查 17 张必需表及每张表的必需、唯一表头；结构破坏返回 `INVALID`；
- 解析 README 和 Assumptions，不以隐藏默认值补齐缺失的排程参数；
- 校验 Machines、Workers、Products、Routing、Inventory、Orders、Events、
  Objectives、Approval Policy、Evaluation Cases、Plan Output Schema、Shift
  Calendar、Worker Skills、Changeovers 和空 Baseline 的记录形状；
- 所有时间要求带 UTC+08:00，并从 `as_of_time` 与连续五个工作日推导 7200 分钟
  horizon；日期时间统一换算为相对整数分钟；
- 物料数量、BOM 单耗、订单量、工时、换线时间、熟练度和能力均使用有界整数；
- 机器组日历展开到已声明机器，工人组日历只绑定已声明工人；
- Worker Skills 按 primary、熟练度和 worker id 确定性选择合格工人；不创建
  工作簿外资源；
- Changeovers 从机器组完整矩阵展开到机器，并检查每组、每产品对的完整性；
- `Payload_JSON` 不进入计划状态，注入型事件按记录隔离。

### Record-level 与传递性 quarantine

记录错误使用合同的 `validation_issue` 六字段结构和 36 项封闭代码。坏路线先以
`ROUTING_INVALID` 隔离；依赖该路线的产品和订单通过
`QUARANTINE_DEPENDENCY` 传播隔离。独立测试证明 BRACKET-A 路线损坏时，
`ORD-1001` 和 `ORD-URGENT` 被隔离，而干净的 `ORD-1002` 仍进入计划。

单个坏库存 bucket 不会误删同物料的合法 sibling bucket。只有产品失去合法 BOM
依赖后，隔离才继续传播到产品和订单。隔离记录不会静默删除，生成摘要会报告
`quarantined_entity_count`。

### 不可变 Factory State

`FactoryStateRegistry` 新增 SQLite `factory_states` 表。`state_id` 是
dataset version、规范状态和验证结果的内容哈希，因此 Excel 格式变化不会改变
业务状态引用；相同输入幂等返回同一引用，不同业务内容生成新引用，已有行不会被
覆盖。原始 17 表快照、source SHA-256、entity counts 和验证结果一起保存用于审计。

生产入口新增 `generate_authoritative_plans_from_state`。`/schedule` 和 Agent chat
加载工作簿或注册规范 JSON 后，均通过 `state_id` 重新读取状态，再进入 P0-3 的
generator、独立 validator 和 RuntimeAuthority。旧三表 Excel adapter 只为既有
兼容测试保留，不承担 V1.8 迁移。

### 标准工作簿修复

原工作簿的 `Routing` 缺少排程必需工时，并按订单重复 BRACKET-A 路线。现已：

- 增加显式 `duration_min`；
- 将 Routing 改为 2 个产品、每个 3 道工序的 6 条唯一产品级路线；
- 保留每条路线末端 `INSPECTION`；
- 在 Assumptions 增加 `max_overtime_min_per_worker_per_day=240`；
- 由生产迁移结果刷新 `data/factory_demo_v18.json`，使 JSON 与工作簿使用同一规范
  factory state。

`tools/validate_compliant_dataset.py` 不再只做静态表格抽查。它现在实际创建
SQLite registry、调用生产导入器两次、验证稳定引用、重新读取规范状态并比较
canonical round trip。

## 验证结果

- 全量单元测试：`564 passed`；
- P0-4 专项测试：`6 passed`，覆盖 17 表、合同工具输出 schema、路线传递隔离、
  sibling 库存保留、结构性 `INVALID`、state immutability 和引用式排程；
- 标准工作簿生产往返：17 sheets、7 events、30 evaluation cases、6 routing rows、
  3 inventory buckets，`VALID_WITH_QUARANTINE`，1 个隔离实体；
- compact smoke：`6 passed / 0 failed`，仍只作为局部 smoke；
- 封闭词表：`PASS`，26 sets / 160 members；
- 网页 DOM 与真实 solver 候选渲染：2 组 `PASS`；
- Python compile：`PASS`；
- 工作簿公式错误扫描：0 项；Routing 修改区已渲染复核；
- 正式 EVAL：仍为 `0 PASS / 0 FAIL / 30 BLOCKED`，本整改不把单元测试或工作簿
  往返冒充正式验收证据。

全量测试的两个 warning 来自 protobuf 对 Python 3.14 的弃用预告，不是本次导入
路径失败。

## 当前边界

- 网页文件上传控件仍只接受 JSON；17-sheet Excel 可通过 API `factory_file` 和
  Agent 的后端文件选择路径导入。
- P0-5 八工具中间件、统一 tool-error envelope，以及 P0-6 的完整安全审计语义仍需
  后续整改；当前 `EVT-005` 已在导入边界隔离，但不能据此声称 P0-6 完成。
- 正式 EVAL、真实 Bedrock、Lightsail、公网 TLS 和真实人工审批尚未执行。

## 关键文件 SHA-256

| 文件 | SHA-256 |
|---|---|
| `src/planpilot/factory_state.py` | `e950391eb1e4c62beb810f8a1d1fda2db8cb23c5aec2efbf4556ef73568cfd75` |
| `src/planpilot/runtime_planning.py` | `03cdc8fd0679229a86f85861589f8a661f1bcf2a699610863f46f93bdcef0497` |
| `src/planpilot/domain/importer.py` | `3683297e27535766b54c20b695d8304236f86da544cd9f83cb3fa919b0f84a18` |
| `tools/validate_compliant_dataset.py` | `6bab65fab9007be98eaaeda5204c2c62c4929db4ab7a5504897fb46107e2ed02` |
| `data/PlanPilot_Mock_Factory_Dataset.xlsx` | `05d28505af1ace9c5c626e76e2ae74291ddbb6932503a119bbb92f0bba135411` |
| `data/factory_demo_v18.json` | `5331297ff00699b979ce48b091c992e01db2a0cf5bc9a7d06f224671085812f4` |
| `tests/unit/test_factory_state_migration.py` | `dad95d4717b3fe9cd5ea0090fef63462e51e88b5e35e13666be48e20c6cadc17` |
