# P0-2 Runtime Authority 整改报告

日期：2026-09-17  
范围：`PLANPILOT_CHAMPION_REVIEW_20260916.md` 的 P0-2

## 结论

P0-2 已完成。HTTP 与 Agent 运行路径不再把 `persistence.Database` 当作计划、审批或发布的业务权威。新增的 `RuntimeAuthority` 是一层薄事务桥：业务规则继续由既有、已审计的 `PlanStore` 与 `ApprovalService` 执行，SQLite 只原子持久化两者的规范状态和审计链。

本整改没有把 compact solver 输出伪装成 V1.8 计划。当前 `/schedule` 与 Agent 生成路径会 fail closed：返回验证失败且不写入权威状态，等待 P0-3 完成合法的 V1.8 适配器与独立验证器。

## 修改结果

1. 新增 `src/planpilot/authority.py`：
   - 原子加载、克隆、提交 `PlanStore` 与 `ApprovalService` 状态；
   - 使用 SQLite revision 做乐观并发控制，拒绝旧快照覆盖新状态；
   - 只接受与计划 id/version/digest 绑定、digest 已独立复算、可行且无硬约束违规的验证结果；
   - 新版本提交时使旧审批集合失效；
   - 发布时重新加载不可变内容、复算 digest、检查 active version、审批集合完整性及发布角色；
   - 同一已发布绑定的重试保持幂等，不产生新的 authority revision。
2. 收缩 `src/planpilot/persistence.py`：
   - 删除平行的计划、版本、审批及发布表和业务方法；
   - 仅保留 `authority_state`、SQLite 事务、审计哈希链与基础设施能力。
3. `tools/api_server.py` 与 `src/planpilot/agent/chat.py` 改用 `RuntimeAuthority`：
   - `/plans`、`/approval/request`、`/approval/decide`、`/publish` 均走统一权威路径；
   - 审批与发布 HTTP 输入在写入前按 V1.8 `request_approval.input_schema` 和 `publish_plan.input_schema` 校验；
   - `/schedule` 对 schema-invalid compact 输出返回 HTTP 422，且 `authoritative_plan_saved=false`。
4. `ApprovalService` 增加只读的请求要求查询，用服务端拥有的 action 决定授权，不信任客户端自报审批类型。
5. 网页和运行文档改用正式字段：`plan_version`、`expected_plan_version`、`plan_digest`、`approval_set_id`、`idempotency_key`。

## 防复发验证

新增 `tests/unit/test_runtime_authority.py`，覆盖：

- Database 不再暴露平行业务 API，也不再创建旧权威表；
- 合法计划安装、审批、发布、重启恢复的完整闭环；
- 发布重试幂等；
- schema-invalid 或未独立验证的计划零写入；
- 重新生成后旧审批失效；
- SQLite 备份恢复；
- 旧桥实例不能覆盖新 revision；
- 篡改持久化快照后重启拒绝加载；
- HTTP 输入 schema 先于权威写入执行，无效附加字段返回 400 且 revision 不变。

最终验证结果：

- 全量测试：`544 passed`，2 个来自 protobuf/Python 3.14 兼容预告的既有 warning；
- RuntimeAuthority 定向测试：`8 passed`；
- 封闭词表：`PASS`，23 sets / 149 members；
- 网页 DOM/真实 solver 候选渲染：2 组 `PASS`；
- Python 编译检查：`PASS`；
- 源码扫描：未发现 `Database.save_plan/request_approval/publish_plan` 调用、旧权威表建表语句或 `candidate_id` 绑定残留。

本轮此前还复核了既有 4 项负控、6 项 compact smoke，并确认正式 EVAL 仍为 `0 PASS / 0 FAIL / 30 BLOCKED`。这些结果保持各自证据边界，不用于宣称完整 V1.8 或比赛就绪。

## 当前边界

P0-2 解决的是权威路径统一与事务持久化。P0-3 的 V1.8 输出适配器和真正独立的语义验证器仍未完成，因此当前 compact 生成结果不能进入审批或发布；这是刻意的 fail-closed 行为。

关键文件 SHA-256：

| 文件 | SHA-256 |
|---|---|
| `src/planpilot/authority.py` | `50c799cc39a803dba1c6aa8933407c61b960ec22825a1cdf75d466197512e224` |
| `src/planpilot/persistence.py` | `f947490c87d68bcc990023d00b26ff8b1ebe380fe3ca1aff1ff26054e19987e6` |
| `tools/api_server.py` | `ee6d2d50d5239a88346a95d6e5fbd06940e70fa023d3c337df1ebe0c6859cecd` |
| `tests/unit/test_runtime_authority.py` | `50d99972ff3c899565cc5e7fa902e8d7fe25469c58d3467851c54edd5ea19416` |

