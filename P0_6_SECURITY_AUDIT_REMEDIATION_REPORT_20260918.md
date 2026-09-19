# P0-6 注入隔离、安全事件、决策轨迹与审计链整改报告

**整改依据：** `PLANPILOT_CHAMPION_REVIEW_20260916.md` 第六部分、
`P0_6_CHAMPION_READINESS_AUDIT_20260918.md`，以及
`.kiro/specs/{audit-hash-chain,decision-traces}/{design,tasks}.md`。  
**合同锚：** `contract/planpilot_agent_contract_v1.8.json`，SHA-256
`b92e53f4ff0541050ec6585f3237f4d764a26e455447a672f3419dfb284fe639`。

## 结论

P0-6 已完成。`EVT-005` 或声明为 `PROMPT_INJECTION` 的记录会被单独隔离，
通过合同 `log_security_event` 工具持久化；合法订单继续生成三套候选计划。
安全事件和 P0-5 中间件决策轨迹写入同一条不可修改的 SHA-256 审计链，公开响应
只返回不透明安全引用和隔离影响，不回显原始恶意文本。

本次完成的是 P0-6 安全审计链路和 `log_security_event` 的真实中间件接入。
其余七个业务工具尚未全部形成统一 public-tool dispatcher；publisher 事务也不在
本部分范围内。不能把本报告解释成八工具运行接入或正式比赛验收已经完成。

## 已完成的冠军标准整改

- 新增共享 append-only 审计链。每条记录使用规范 JSON 和前序哈希计算 SHA-256；
  SQLite trigger 拒绝 audit/security/trace 的 UPDATE 和 DELETE，持久化 head 与计数
  参与完整性校验。
- `log_security_event` 采用 prepare → schema validation → commit 协议。提交前复核
  预期链头和输出身份，事务内同时写入链记录与 typed index；失败不会返回成功引用。
- 安全事件摘录先做长度限制、凭据及个人信息脱敏。原始注入文本不进入公开响应、
  decision trace 或错误消息。
- P0-5 observer seam 现在生成完整 `$defs.decision_trace_record`。trace ID 由小写
  UUIDv4 correlation 与单调序号派生；成功和失败调用都写入恰好一条合同有效记录，
  安全调用以 `security_event_ref` 关联。
- Event workflow 不再因单条注入把整个计划置为 `BLOCKED`。有权威 logger 时先隔离
  和记录，再继续处理合法订单；缺少 logger 时失败关闭。
- factory quarantine 在注册不可变状态时被保留，独立验证和 HTTP/Agent 响应均显示
  `quarantine_impact`；网页风险区显示“隔离记录”。
- workbook 导入路径会记录检测到的 `EVT-005`，直接事件路径也按事件类型识别，避免
  只依赖英文关键字或固定 ID。

## 端到端对抗结果

新增 HTTP 对抗测试把数据集中的 `EVT-005` 提交到 `/schedule`，实测：

- HTTP 200，`stored_plan_count = 3`；
- `quarantine_impact = ["EVT-005"]`；
- 安全事件和 decision trace 各持久化一条，链完整性验证通过；
- 响应中不存在 `Ignore all safety rules` 原文；
- 三套候选均显示一条隔离影响，合法订单未因恶意记录停止排程。

这是一条实现级对抗回归，不是正式 `EVAL-005` 执行证据。

## 验证结果

- P0-6 安全与事件专项：**13 passed**；
- 全量单元测试：**604 passed**；
- 全量 `tests/`：**610 passed**；
- P0-6 一次性副本负向变异：**9 caught / 0 escaped / 0 broken**；
- P0-5 负向变异回归：**11 caught / 0 escaped / 0 broken**；
- 封闭词表及 self-test：**PASS**；Kiro workspace：**190 ok / 0 fail**；
- 网页 DOM 渲染与隔离影响：**PASS**；Python compile：**PASS**；
- 合同 SHA 未变化。

九项 P0-6 变异分别破坏 audit 更新/删除保护、observer、secret redaction、声明型
注入识别、缺失 logger 的失败关闭、quarantine 保留、前序哈希链接和 trace ID
派生。每次变异仅发生在 pytest 临时目录的项目副本，结束后校验真实源文件未变化。

## 边界与后续建议

正式 `EVAL-001`～`EVAL-030` 仍为 **0 PASS / 0 FAIL / 30 BLOCKED**；逐案执行器、
完整 pass-condition verifier、时延、审批和可重建证据尚未完成。真实 Bedrock、
Lightsail、公网 TLS 和人工审批也未在本机执行。

下一阶段应把 P0-5 中间件和本次 decision trace writer 接入其余七个真实业务
handler，建立统一 public-tool dispatcher；随后完成 publisher 的审批绑定、幂等
事务和失败原子性，再开始逐案正式 EVAL。恢复包没有 `.git`，branch、commit、tag
和来源祖先关系仍无法补证。
