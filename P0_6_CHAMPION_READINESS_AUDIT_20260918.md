# PlanPilot 冠军标准复核与第六部分整改建议

**复核日期：** 2026-09-18  
**复核基线：** P0-1～P0-5 当前恢复项目；V1.8 合同 SHA-256
`b92e53f4ff0541050ec6585f3237f4d764a26e455447a672f3419dfb284fe639`。  
**方法：** 合同对照、运行路径检索、针对性执行、现有证据复查。没有把附件中的
历史结论当成当前代码事实。

## 一、当前完成度

| 冠军审查项 | 当前判断 | 证据 |
|---|---|---|
| P0-1 EVAL 诚实性 | 已整改 | `run_evals.py` 实测 0 PASS / 0 FAIL / 30 BLOCKED，退出码 1 |
| P0-2 权威存储与审批 | 已整改 | API 使用 `RuntimeAuthority`；既有 P0-2 报告与回归测试 |
| P0-3 V1.8 adapter/validator | 已整改到当前声明边界 | 非空计划、独立重算与 HC mutation 已有证据；正式 EVAL 仍未执行 |
| P0-4 17-sheet 导入与隔离 | 已整改 | 工作簿生产往返；`EVT-005` 被记录为 quarantine |
| P0-5 tool-error middleware | 模块实现完成，运行接入未完成 | 35 专项、595 unit、600 full、11/11 mutation；运行代码没有实例化中间件 |
| P0-6 注入、安全事件、trace/audit | **未整改** | 当前 EVT-005 仍使整个 workflow BLOCKED；无合同 trace writer、安全工具或持久化安全事件 |

因此，不能把项目描述为冠军就绪。下一项应当是 P0-6，但需要先按设计边界拆成
`audit-hash-chain`、`decision-traces` 和 `log_security_event` 三个可审查组件，再接入
注入路径；不能继续把临时内存 dict 称为 trace 或 audit。

## 二、发现（按严重度排序）

### C1 — 注入记录阻断整个工厂计划，直接违反顶层安全语义

`src/planpilot/workflow/events.py:95-104` 在发现 EVT-005 后把状态改成 `BLOCKED`，
立即返回空 `candidates`。`tools/api_server.py:186-191` 随即返回 HTTP 422，合法订单
完全不再排程。针对性实测结果为：

```text
state=BLOCKED
candidate_count=0
```

现有测试 `tests/unit/test_event_workflow.py:31-36` 反而把这个错误行为固定成预期。

**修改建议：** 注入行只进入 record-level quarantine；原始文本永远不进入排程、
模型提示或计划字段。安全事件持久化成功后，从 `RECEIVED` 重新加载不含该记录的
同一 factory state，对其余合法订单继续 `generate_plan_options` 和 `validate_plan`。
返回结果必须同时包含非空合法候选、quarantine impact 与不透明
`security_event_ref`。

### C2 — “安全事件”没有调用合同中的 `log_security_event`

`SecurityEvent` 只有 `event_id/action/context/excerpt/event_hash`，不满足合同工具的
输入或输出 schema；其 `event_hash` 只是单条对象哈希，不是链式哈希。API 把该 dict
直接返回客户端，没有数据库写入。源码中也不存在 `security_event_id`、
`previous_event_hash` 或真实 `log_security_event` handler。

更严重的是，`events.py:47` 只压平空白后截取文本，没有 credential/PII redaction，
攻击文本中的 token、密钥或个人信息可能被回显给客户端。

**修改建议：** 实现唯一的 server-side `log_security_event` handler：

1. 输入和输出分别通过合同工具 schema；
2. 先做 secret/PII redaction，再做 500 字符边界；
3. 写入 append-only SHA-256 chain，并返回 security/audit ID、时间、前序哈希和事件哈希；
4. 使用 P0-5 middleware 执行，失败时不生成伪造成功引用；
5. 公开结果只传不透明引用，不回传原始攻击内容。

### C3 — decision trace 与 audit hash chain 尚未实现合同语义

`src/planpilot/persistence.py:49-54` 只有通用 `audit_chain` 表；`_audit()` 记录任意
`plan_id/actor/event/payload`。`src/planpilot/agent/chat.py:79-91` 写入的是临时
`agent_step`，缺少 `$defs.decision_trace_record` 要求的 trace ID、correlation、
工作流前后状态、受控摘要、计划引用、错误/重试性、安全事件引用和前序哈希。

数据库没有阻止 `UPDATE`/`DELETE` 的 append-only 约束，也没有一条 trace-per-tool
的唯一性约束。现有 `verify_audit()` 只能从当前第一行向后重算；没有持久化链头或
外部锚时，尾部删除无法被识别。

**修改建议：**

- 建立 canonical event serializer、genesis 规则和单事务 append API；
- 数据库触发器拒绝 audit/trace 的 UPDATE 与 DELETE；
- `trace_id = correlation_id:sequence_no`，建立唯一约束；
- P0-5 observer 只提交有界事实，由 server-side trace writer 组装并验证完整
  `decision_trace_record`；成功、合同错误和未知错误都恰好写一条；
- trace 与安全事件进入同一链；提供 tamper、reorder、middle-delete、duplicate、
  crash-before/after-commit 的 disposable-copy mutation control；
- 对尾部删除增加可信链头/checkpoint；否则报告必须明确“只能检测链内篡改”。

### C4 — P0-5 中间件仍是孤立组件，公开运行路径没有使用

源码检索显示 `ToolErrorMiddleware` 只出现在自身模块和测试；API、Agent 和
RuntimeAuthority 没有实例化它。`tools/api_server.py:89-105,110-127,199-209` 仍返回
自由形态 `{"error": str(exc)}`，并可能把路径或内部 ValueError 文本暴露给客户端。
这表示 P0-5 的模块验收成立，但“八工具成功/失败载荷全部过合同 schema”的冠军
门槛尚未成立。

**修改建议：** 在 P0-6 的 trace writer 完成后，建立唯一 public-tool dispatcher，
注册且只注册合同八工具。所有 API/Agent 业务入口调用 dispatcher，并直接发送
middleware 的 `wire_bytes`。HTTP 自身的 404/415 可以保留 transport error；一旦
进入工具语义，就只能返回对应成功 schema 或 `tool_error`。

### H1 — 注入检测可绕过，且存在两条不一致的隔离路径

17-sheet importer 按 `event_type=PROMPT_INJECTION` 隔离整条记录，这是较可靠的路径；
旧 `EventWorkflow` 只检查 EVT-005 或五个英文 substring。大小写以外的变形、Unicode
同形字符、中文指令、分隔符拆词和不在短词表中的策略绕过不会被识别。变量 `kind`
被计算后没有参与安全判断。

**修改建议：** 以已验证的 record type 和来源信任级别为主，不以关键词词表作为
唯一安全边界。所有 `Payload_JSON` 都按 quoted untrusted data 处理；只有经过闭合
schema 的结构化字段可影响确定性工具。关键词检测只作为补充告警，并增加 Unicode
规范化、嵌套 JSON、编码文本和多语言对抗样本。

### H2 — 重新注册 normalized state 会把 quarantine 状态改写成 VALID

`FactoryStateRegistry.register_normalized()` 在
`src/planpilot/factory_state.py:429-450` 无条件写入 `status=VALID` 和空 quarantine。
事件路径在 `tools/api_server.py:192-195` 对变化后的 raw state 使用该方法，因此未来
即使先隔离注入记录，也会丢失 quarantine 证据。

**修改建议：** 禁止该方法接收来源不明的 raw dict，或要求显式传入已验证、不可变的
validation snapshot。派生 state 必须保留 parent_state_id、应用事件、隔离集合和
内容 digest；不能用空 validation 覆盖已有 `VALID_WITH_QUARANTINE`。

### H3 — 已知的封闭词表守卫旁路仍被测试固定为“继续开放”

`tests/unit/test_guard_bypass_regressions.py:177-216` 明确证明非 Python 文件、单词型
枚举、字符串拼接/f-string、注释和 bytes literal 可以绕过检查。这不会直接造成
运行时注入，但会削弱“没有发明新闭合词表成员”的构建证据。

**修改建议：** 扩大扫描范围到配置、模板、脚本与前端；对 Python AST 做安全的常量
折叠；扫描 bytes 和注释中的合同式 token。修复时同步删除“旁路仍开放”的反向测试，
改为要求每个攻击样本被抓住。

## 三、仍未修改的 P1/P2 项

1. **模型绑定：** `src/planpilot/inference/bedrock_client.py:71` 与
   `tools/start_local.ps1:9` 默认仍是 Nova Pro，合同要求 Claude Sonnet 4.5。
2. **引擎完整度：** 已有 CP-SAT，但 `split_lots()` 没有接入主求解路径；合同级共享
   budget fallback、完整 overtime/cap 与最终词典序目标仍需单独审计，不能因出现
   `cp_model` 就判定完整。
3. **UI 闭环：** 页面有“读取计划”按钮但没有事件处理器；没有 approve/reject 控件，
   也没有调用 `/approval/decide`，无法在同一 UI 完成角色审批与 stale rejection。
4. **Git provenance：** 恢复包仍无 `.git`、commit/tag 或 bundle。
5. **Windows 安装：** `setup_local.ps1` 调用 pip 前仍未强制 UTF-8。
6. **负控复制：** PlanStore/ApprovalService 仍只排除 `.venv-review`，没有统一排除
   `.venv*`，安装 `.venv-runtime` 后会放大临时副本。
7. **正式验收：** EVAL-001～030 当前全部 BLOCKED；真实 Bedrock、Lightsail、TLS、
   备份恢复、人工审批和 demo rehearsal 均无运行证据。

## 四、建议修改顺序

1. `audit-hash-chain`：先把 canonical append、不可改写约束和验证器做成独立模块。
2. `decision-traces`：接 P0-5 observer，严格验证 `$defs.decision_trace_record`。
3. `log_security_event`：实现合同 handler、redaction 和安全引用。
4. 修正 EVT-005：隔离恶意行、持久化安全事件、合法订单继续排程。
5. 接入唯一八工具 dispatcher，清除业务路径中的自由形态错误和临时 trace。
6. 增加 P0-6 专项与 mutation control；将 EVAL-005 保持 BLOCKED，直到完整逐案执行器
   能从持久化 trace/audit 重建输入、输出、digest、时间与安全事件。
7. 完成 UI、模型绑定、Git 来源和部署证据后，才开始正式 EVAL-001～030。

## 五、下一轮验收门槛

- 注入测试返回至少一个合法候选，注入记录不会进入 plan 或 LLM prompt；
- `log_security_event` 输入/输出均过合同 schema，敏感片段已脱敏；
- 每个 middleware 调用恰好一个合法 decision trace，失败调用也有记录；
- trace/security event 共用可验证链，数据库拒绝 update/delete；
- observer 或 audit commit 失败时业务状态与公开结果保持原子一致；
- API 业务错误全部是合同 `tool_error`，不存在 `str(exc)` 泄漏；
- tamper/reorder/delete/spoof/redaction/observer-rewrite 等变异全部被测试抓获；
- 全量测试、封闭词表、自测、workspace 与 fact-check 全绿；正式 EVAL 继续按事实报告。

