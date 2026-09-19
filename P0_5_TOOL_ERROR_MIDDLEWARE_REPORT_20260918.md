# P0-5 八工具统一错误中间件整改报告

**整改依据：** `PLANPILOT_CHAMPION_REVIEW_20260916.md` 第五部分，以及
`.kiro/specs/tool-error-middleware/{design,tasks}.md`。  
**合同锚：** `contract/planpilot_agent_contract_v1.8.json`，SHA-256
`b92e53f4ff0541050ec6585f3237f4d764a26e455447a672f3419dfb284fe639`。

## 结论

P0-5 已实现为八个公开工具共用的执行和序列化边界。它从 V1.8 合同读取八个
工具、18 个错误码、逐码 details schema、重试性和重试策略，不维护第二份业务
词表。未知工具或错误码失败关闭。

本部分只交付中间件和合成事务协议。八个业务工具 handler、publisher 事务、
审计哈希链与 decision trace 持久化仍属于后续独立模块，没有混入本次范围。

## 已完成的冠军标准整改

- 完整 `tool_error`：`error_code`、安全 `message`、注册表控制的 `retryable`、
  小写 RFC 4122 UUIDv4 `correlation_id`、逐码校验的 `details`。
- 最终完整错误信封按规范 JSON 序列化后计算 UTF-8 字节，严格不超过 4096；
  超限或不可序列化时整包替换为 schema-valid `INTERNAL_ERROR`，不截断 JSON。
- 可信异常只允许 `FrameworkDomainError`、`SchemaValidationError`、`StoreError`
  和 `ApprovalError`；任意异常即使伪造 `.code/.details` 也只会得到清洗后的
  `INTERNAL_ERROR`。原始异常文本、路径、payload 和 secret 不进入公开 wire。
- 固定执行顺序：已知工具 → correlation → input schema → prepare → deadline →
  output schema → 精确序列化 → commit → 单次 observer。失败路径触发 rollback，
  `KeyboardInterrupt`、`SystemExit` 等进程控制异常继续向上传播。
- deadline 使用注入式单调时钟；生成阶段携带 `profiles_completed`；求解阶梯耗尽
  保持 `SEARCH_ESCALATION_EXHAUSTED`，不会被误写成 deadline。
- middleware 不执行隐藏重试。三种 retryable 错误只暴露合同策略供更高层读取。
- 内部 outcome、事务状态和 observer 字段不会扩张公开 success/failure payload。
- 旧 `engine.framework_error()` 已收口到同一严格构建器，调用方不能控制重试性。

## 验证结果

- 中间件与兼容接口专项：**35 passed**；
- P0-6 完成后的累计单元回归：**604 passed**；
- P0-6 完成后的累计 `tests/` 回归：**610 passed**；
- 一次性副本负向变异：**11 caught / 0 escaped / 0 broken**；
- 审批基础模块回归：**40 passed**；
- 封闭词表与自测：**PASS**；Kiro workspace：**190 ok / 0 fail**；
- Python compile：**PASS**；合同 SHA 未变化。

变异控制实际破坏了输入/输出校验、提交顺序、异常白名单、重试性、字节计数、
超限处理、异常清洗、BaseException 边界、单次调用和 observer 结果稳定性。每次
变异只发生在 pytest 临时目录中的项目副本，结束后逐字节验证源项目未变化。

## 边界与待办

恢复包不含 `.git`，因此无法完成 clean worktree、基线 commit/祖先校验、创建
branch、commit 或 annotated tag；这两项在任务表中保持未勾选。该限制不影响
代码和可重复测试证据，但后续进入正式 Git 仓库时必须补做基线标记。

正式 EVAL-001～030、真实 Bedrock、Lightsail、公网 TLS 和真实人工审批仍未执行，
不得把本报告的 610 项实现测试解释成正式 EVAL 通过。P0-6 已在后续独立报告中
完成安全审计链路；其余七个业务 handler 的统一运行接入和 publisher 仍是后续范围。
