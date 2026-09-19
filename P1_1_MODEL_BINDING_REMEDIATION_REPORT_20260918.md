# 第七部分（P1-1）模型绑定整改报告

**整改依据：** `PLANPILOT_CHAMPION_REVIEW_20260916.md` 的 P1-1，及
`.kiro/specs/model-binding/{design,tasks}.md`。  
**合同锚：** `contract/planpilot_agent_contract_v1.8.json`，SHA-256
`b92e53f4ff0541050ec6585f3237f4d764a26e455447a672f3419dfb284fe639`。  
**官方能力依据：** AWS Bedrock Claude Sonnet 4.5 模型卡和 Global
cross-Region inference 文档；二者列出
`global.anthropic.claude-sonnet-4-5-20250929-v1:0`，并把
`ap-southeast-1` 列为支持的源区域。

## 章节口径

冠军审查报告共有 **10 个同级核心修改项**：P0-1～P0-6 六项，以及
P1-1～P1-4 四项。此前完成的第一至第六部分对应 P0-1～P0-6；本次第七部分
对应 P1-1。P2 工程卫生、推荐 Gate、禁止捷径和 Champion-ready 门槛是横向清单，
不另行伪装成同级“第十一项”。

## 结论

第七部分已完成离线实现整改。运行时、启动脚本、环境样例、Docker/Compose、
交付 manifest 和运维文档现在只有一个默认模型真相：Bedrock Claude Sonnet 4.5
全局推理配置，源区域为新加坡。非合同模型覆盖会在客户端构造阶段失败，发生在
任何网络、凭据读取或用量预留之前。

本轮没有调用 AWS Bedrock，也没有消耗比赛额度。团队 API Key 权限、真实模型
响应、时延和费用仍需在获授权的部署环境验证，因此不能声称真实 Bedrock 已通过。

## 已完成的冠军标准整改

- `planpilot.inference.bedrock_client` 定义唯一的 region/profile 常量，并拒绝
  `amazon.nova-pro-v1:0` 或任意其他运行时模型覆盖。
- 实际 Converse URL 使用 URL-encoded Claude Sonnet 4.5 全局推理配置；离线测试
  通过注入 transport 捕获请求，不访问网络。
- `tools/start_local.ps1` 只接受合同固定的模型值；`.env.example` 修复了原有损坏的
  `PLANPILO…ODE` 键并写入明确 region/model。
- Docker image、Compose service 和 `build_team_package.py` 的 manifest 默认值
  与 Python 运行时一致；网络调用仍默认关闭。
- `src/planpilot/agent/bedrock.py` 不再创建第二套 boto3 provider client，只保留
  兼容用的意图输出校验；供应商 I/O 只存在于 `inference/bedrock_client.py`。
- `START_HERE.md`、`PRODUCT_RUNBOOK.md`、`docs/bedrock-web-guide.md` 和
  Bedrock Design-First spec 已移除 Nova 默认与“等待主办方批准偏离”的双重口径。
- 新增 no-Nova、精确 profile、错误覆盖失败关闭、单 provider 模块和交付表面一致性
  回归；一次性副本变异会实际破坏六项绑定并确认全部被测试捕获。

## 验证结果

- Bedrock/交付专项：**47 passed**；
- 全量单元测试：**609 passed**；
- 全量 `tests/`：**616 passed**；
- 模型绑定负向变异：**6 caught / 0 escaped / 0 broken**；
- 封闭词表及 self-test：**PASS**；Kiro workspace：**190 ok / 0 fail**；
- Python compile 与交付包 manifest 构建检查：**PASS**；交付 ZIP 只含一份新生成
  manifest，旧 manifest 不再重复收入；合同 SHA 未变化。

## 仍需部署验证

- 使用团队正式 Bedrock Key 验证该 inference profile 的账号权限和首次使用要求；
- 在 Lightsail 上测量两次模型调用的时延、输入/输出 token 和实际费用；
- 演示前按当前 Bedrock 定价复核 USD 100 额度并调整每日上限；
- 保存不含凭据的请求 ID、usage、decision trace 和 audit chain 作为部署证据。

这些条目在 `.kiro/specs/model-binding/tasks.md` 保持未勾选。正式 EVAL-001～030
仍为 **0 PASS / 0 FAIL / 30 BLOCKED**。
