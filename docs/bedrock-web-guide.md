# Bedrock 网页接入与验证

## 已配置的默认值

- 区域：`ap-southeast-1`（新加坡）。
- 模型：Amazon Nova Pro，`amazon.nova-pro-v1:0`。
- 密钥文件：用 `-BedrockKeyFile` 指定，只包含一行 Bedrock API Key。
  原开发机可指定 `D:\hackrathon\claudeapi.txt`；队友应使用自己的文件路径。
- 推理接口：Bedrock Runtime Converse，后端通过 HTTPS Bearer 认证调用。

用户在 Claude 的 API 和控制台试验场均遇到 Anthropic 服务地区限制后，
明确要求更换模型，当前默认采用 Amazon Nova Pro。原始比赛合同仍指定
Claude Sonnet 4.5；此运行配置偏离已获用户授权，但比赛提交前须向主办方
确认是否允许。Nova 的权限及区域可用性尚未实测；若 AWS 要求推理配置
文件，使用控制台给出的实际 ID，不自动猜测前缀或切换区域。

没有将密钥复制进代码、HTML、日志或配置示例。网页访问 Token 与 AWS
API Key 是两种凭证，不要把 AWS Key 粘贴进网页 Token 输入框。

显式配置 `PLANPILOT_BEDROCK_KEY_FILE` 时，文件优先于
`PLANPILOT_BEDROCK_API_KEY` 环境变量，每次调用重新读取。更新文件即可轮换
密钥；指定文件不可读取时直接报错，不回退到可能已过期的环境变量密钥。

## 启动

停止旧服务（在其终端按 Ctrl+C），然后运行：

```powershell
Set-Location D:\hackrathon\PlanPilot-Hackathon\planpilot-build
powershell -ExecutionPolicy Bypass -File .\tools\start_local.ps1 -Bedrock
```

按照提示输入至少 32 位的数字密钥。这是本地 API 登录签名密钥，不是
AWS Key。脚本生成的网页 Token 会复制到剪贴板。

打开 `http://127.0.0.1:8080/`，按 Ctrl+F5 刷新，粘贴网页 Token，并点击输入框
之外的位置。Agent 区将显示“BEDROCK 已配置”。此检查不调用 AWS，不证明
密钥权限、模型可用性或实际网络连通性。

只有点击“发送给 Agent”才会调用模型并可能产生 AWS 费用。单独点击
“生成三套方案”仍是本地确定性计算，不调用模型。

## 使用顺序

1. 使用默认 `factory_demo.json` 或上传工厂 JSON。
2. 在 Agent 区输入“请为当前数据生成三个方案并解释交付与换线的取舍”。
3. 等待返回，检查方案区出现三个真实候选方案。
4. 查看执行记录中的 model_intent、read_factory、validate_input、
   generate_candidates、validate_candidates、compare_candidates、save_plan、
   model_explanation。它们是实际紧凑适配器步骤，不是 V1.8 工具调用协议标识。
5. 输入“解释当前计划的缺料、延期和未排工序”。这次只解释现有计划，不重复生成。
6. 审批和发布仍须通过对应的人工作业接口。聊天不能代替批准，也不自动发布。

当前消息请求不携带历史对话，使用的是当前计划版本。修改库存、班次、交期、
自然语言应用事件或调整目标权重还不支持；应先修改源数据再导入生成。
这是自建意图驱动 Agent 的紧凑适配器接入，不是 AWS 托管 Bedrock Agents，
也不是完整的八工具 V1.8 协议实现。模型选择生成、解释或回复，具体计算
顺序由后端固定；没有对模型开放任意工具执行权限。

## 错误处理

| 情况 | 处理方法 |
|---|---|
| 未配置或密钥文件无法读取 | 检查路径，使用 `-Bedrock` 启动 |
| 网页 HTTP 403 | 检查网页 Token 是否与本次启动的本地数字密钥匹配 |
| Bedrock 401/403 | 检查 AWS Key 有效期、模型权限及首次使用要求 |
| Bedrock 400/404 | 确认模型在所选区域的调用方式；如果需要推理配置文件，填写实际 ID |
| Bedrock 429 | 等待后手动重试；程序没有自动重试 |
| 网络超时 | 检查后端到区域 Bedrock Runtime 的 HTTPS 连通性 |
| 已生成计划但解释失败 | 计划已保存并返回网页，直接查看确定性方案数据或重新请求解释 |

模型或推理配置文件可通过启动参数明确覆盖，不自动猜测或切换区域：

```powershell
Get-Help .\tools\start_local.ps1 -Full
```

参数包括 `-BedrockRegion`、`-BedrockModel`、`-BedrockKeyFile`。

## 调用与数据限制

- 每个请求最多两次模型调用，不自动重试或跟随重定向。
- 默认禁止模型网络调用；`-Bedrock` 为部署/演示设置
  `PLANPILOT_FORBID_LLM_NETWORK=0`，未使用此参数时脚本会设置为 `1`。
- 每次输入按 UTF-8 序列化字节数保守限制为 24,000。
- 意图输出最多 512 tokens；解释最多 2,048 tokens。
- 每次模型请求网络超时为 20 秒；这不是完整工作流 SLA 保证。
- 默认每日预算为 100,000 tokens，按 +08:00 日期计数。可通过环境变量
  `PLANPILOT_BEDROCK_DAILY_TOKENS` 调整，不能将令牌数直接当作美元金额。
- 请求前在 SQLite 预留用量，成功后记录实际用量；网络结果未知时保留
  保守预留值。记录和审计均存储在当前数据库中，重启不会清零。
- 不向模型发送原始工厂文件、已排工序数组或批次分配明细；发送用户问题、
  KPI、风险与计划引用。超大摘要会明确拒绝，不截断后假称完整。

## 验证边界

本地测试使用模拟 Bedrock 响应、真实求解器和本地 HTTP 服务，不消耗 AWS
额度。AWS Key 权限、新加坡模型可用性和真实回复质量，需要运行上面的部署/
演示流程验证。接口已接好不等于真实 AWS 验证通过。
