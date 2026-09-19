# PlanPilot 队友使用教程 / Team Quick Start

这是包含源码、示例数据、测试和启动脚本的交付包，不是免安装 EXE。
Windows 10/11 推荐使用下面的 PowerShell 流程。第一次安装需要联网下载
Python 依赖；安装后本地排程无需 AWS。请以本文件为本次交付的入口，旧审计
报告和历史介绍中的功能说明可能不代表当前网页能力。

## 1. 解压和安装

安装 **Python 3.11，64 位版本**：https://www.python.org/downloads/windows/ 。
安装时启用 Python Launcher 或加入 PATH，然后重新打开 PowerShell。

将 ZIP 完整解压到自己选择的位置，不要在压缩包浏览窗口里直接运行脚本。
打开解压后的 `PlanPilot` 文件夹，在资源管理器地址栏输入 `powershell` 并回车。
当前目录应当能看到 `START_HERE.md`、`src` 和 `tools`。

```powershell
powershell -ExecutionPolicy Bypass -File .\tools\setup_local.ps1 -Dev
```

脚本创建 `.venv-runtime`，安装精确版本依赖并实际生成三个示例方案。
`-Dev` 同时安装测试依赖；只运行软件时可以省略。脚本不需要管理员权限，
不会删除已有虚拟环境或数据。安装目录可以包含空格或中文。

如果电脑有多个 Python，可明确指定 Python 3.11 的路径：

```powershell
$pythonPath = Read-Host '输入 Python 3.11 的 python.exe 完整路径'
powershell -ExecutionPolicy Bypass -File .\tools\setup_local.ps1 -Python $pythonPath -Dev
```

## 2. 先测试本地排程

```powershell
powershell -ExecutionPolicy Bypass -File .\tools\start_local.ps1
```

按提示输入**自己设置的至少 32 位数字密钥**，保管好；它用于签发本地网页登录
Token，不是 AWS 密钥。同一服务运行期间，新 Token 必须用同一数字密钥签发。
脚本会将短期网页 Token 复制到剪贴板。不要公开分享该 Token。

打开 http://127.0.0.1:8080/ ，将剪贴板内容粘贴到页面顶部的 Bearer token
输入框，**不要额外加 `Bearer ` 前缀**。默认一小时后过期。

点击“生成三套方案”。默认读取 `data/factory_demo_v18.json`，页面中保留
`factory_demo_v18.json` 即可，不要填写目录前缀。

在“计划与方案”比较 Balanced、Delivery First、Cost First，切换候选方案
查看 KPI 和排程。在“风险中心”查看缺料、待到货、延期、未排工序和约束违规。
也可通过上传框选择 `data/factory_demo_v18.json`。网页上传目前仅支持 JSON；
附带的 17-sheet Excel 已接入后端生产导入器，可通过 API 的
`factory_file` 使用，但不可直接放入这个 JSON 上传框。

关闭服务时回到终端按 Ctrl+C。重启后填写新 Token；多个队友各自在自己电脑
运行即可，不需要共享数据库。默认仅绑定本机，不是公网协作服务。

## 3. 启用真实模型对话（可选）

当前默认并强制绑定 **Bedrock Claude Sonnet 4.5**，全局推理配置 ID
`global.anthropic.claude-sonnet-4-5-20250929-v1:0`，源区域
`ap-southeast-1`（新加坡）。Bedrock Converse 接口只通过后端调用。此组合与
V1.8 比赛合同一致；全局推理配置可从新加坡源区域调用，但账号权限和实际连通性
仍须在部署环境验证。

包内没有 AWS 密钥。每位队友使用有权使用的 Bedrock API Key，保存为外部
TXT 文件，只保留完整单行密钥，不要加变量名、引号或 Bearer 前缀。
IAM Access Key ID/Secret Access Key 不能当作此接口的 Bearer API Key。

先停止本地服务，然后运行：

```powershell
$keyPath = Read-Host '输入 Bedrock API Key TXT 文件的完整路径'
powershell -ExecutionPolicy Bypass -File .\tools\start_local.ps1 -Bedrock -BedrockKeyFile $keyPath -BedrockRegion ap-southeast-1 -BedrockModel global.anthropic.claude-sonnet-4-5-20250929-v1:0
```

也可自行创建 `secrets/bedrock-api-key.txt`；使用 `-Bedrock` 时脚本可自动找到它。
该目录已被 Git、Docker 构建和交付打包排除。若已有
`PLANPILOT_BEDROCK_KEY_FILE` 环境变量，请明确使用 `-BedrockKeyFile` 覆盖。

输入本地数字密钥，回到网页按 Ctrl+F5，粘贴新网页 Token，发送：

> 请为当前数据生成三个生产计划方案，并解释交付和换线成本的取舍。

模型识别需求，后端执行确定性排程与校验，模型再解释结果。继续发送：

> 解释当前计划的缺料、延期和未排工序。

第二次只解释当前计划。每条消息绑定当前计划上下文，不包含历史对话。
只有“发送给 Agent”会请求模型；“生成三套方案”仍是纯本地计算。
模型请求可能计费，默认每日令牌额度为 100,000，单次消息最多调用模型两次。
模型是否可用由账号权限、区域与调用方式决定，配置成功不代表实际连通成功。

## 4. 当前功能边界

| 功能 | 状态 |
|---|---|
| 本地生成三方案、切换、Gantt、风险查看 | 已实现 |
| Bedrock 意图识别、固定工具流程、结果解释 | 接口已实现；需各自验证 AWS 访问 |
| 前端工具执行记录 | 显示实际生成、独立验证和权威写入步骤；完整 V1.8 决策轨迹协议仍待后续实现 |
| 聊天直接修改库存、班次、交期或应用事件 | 未开放；修改源数据后重新导入 |
| 网页“读取计划”按钮 | 已接通：输入计划 ID 和可选版本，读取服务端权威内容与生命周期 |
| 网页发起审批和发布 | 已接通；选中方案的版本和摘要绑定服务端审批集合 |
| 网页批准/拒绝审批按钮 | 已接通；逐项显示角色、影响和到期时间，拒绝需选合同原因 |
| 网页审计 | 显示计划摘要、真实 Agent 步骤及服务端审计链验证状态 |
| MES/ERP 下发生产任务 | 未实现 |

## 5. 网页审批与后端核查

在方案比较中选择要发布的候选，点击“发起审批”。审批队列会从服务端读取
**全部**所需动作。Production Planner 使用页面顶部的 Planner Token；若出现
`add_overtime` 或 `change_promised_due_date`，请由有权限的经理签发 Manager
Token，填入审批队列的“经理 Token”。各角色分别点击对应审批项的“批准”或
“拒绝”；拒绝时需选择合同原因。所有必需项均批准后，Planner 点击“发布选中
方案”。审批状态、版本、plan_digest 和审计链可在同一网页查看。

用“读取计划”可检查先前的版本。重新生成后，旧版本的审批集合会失效，网页
刷新显示 `INVALIDATED`；旧版本不能继续发布。网页发布只写本项目数据库，不会
向 MES/ERP 下发任务。Planner 和 Manager Token 必须由同一服务密钥签发；
不要把 AWS API Key 填入这两个输入框。

以下命令保留为后端核查方式：

P0-3 整改后，`/schedule` 会把明确声明完整权威字段的 JSON 状态转换为 V1.8
`plan_content`，并由分离实现的 validator 重算 digest、物料预留、HC-001～013
和 KPI。只有验证通过的候选会经 `RuntimeAuthority.install_validated_plan`
写入。以下命令可用于刚生成的权威计划；旧 compact JSON 因缺少权威字段会
失败关闭。

```powershell
$env:PYTHONPATH = Join-Path $PWD 'src'
$env:PLANPILOT_AUTH_SECRET = Read-Host '输入正在运行的服务所使用的同一数字密钥'
$role = Read-Host '角色：planner 或 manager'
$userName = Read-Host '输入你的用户标识'
$token = & .\.venv-runtime\Scripts\python.exe tools\issue_local_token.py --user $userName --role $role
if ($LASTEXITCODE -ne 0) { throw 'Token 签发失败' }
$headers = @{ Authorization = 'Bearer ' + $token.Trim() }
$planId = Read-Host '输入网页上的计划 ID'
$version = [int](Read-Host '输入计划版本')
$digest = Read-Host '输入权威计划的 plan_digest'
$body = @{plan_id=$planId; plan_version=$version; plan_digest=$digest; action='publish_plan'} | ConvertTo-Json
$queue = Invoke-RestMethod http://127.0.0.1:8080/approval/request -Method Post -Headers $headers -ContentType application/json -Body $body
$queue.approvals | Format-Table approval_request_id,action,approver_role,status
```

根据返回的 `role` 确认当前 Token 有权操作。planner 负责发布确认和次级技能
确认；manager 负责加班及承诺交期变更。切换角色需重新签发相应 Token。

```powershell
$requestId = Read-Host '输入你要决定的 request_id'
$decision = Read-Host '输入 APPROVED 或 REJECTED'
$decisionBody = @{request_id=$requestId; decision=$decision} | ConvertTo-Json
Invoke-RestMethod http://127.0.0.1:8080/approval/decide -Method Post -Headers $headers -ContentType application/json -Body $decisionBody
```

每项所需审批均完成后，使用 planner Token 在网页发布。也可在保留上述
`$body` 及 planner `$headers` 的终端执行：

```powershell
$idempotencyKey = 'planpilot-publish-' + [guid]::NewGuid().ToString()
$publishBody = @{plan_id=$planId; expected_plan_version=$version; plan_digest=$digest; approval_set_id=$queue.approval_set_id; idempotency_key=$idempotencyKey} | ConvertTo-Json
Invoke-RestMethod http://127.0.0.1:8080/publish -Method Post -Headers $headers -ContentType application/json -Body $publishBody
```

发布是本项目内的数据库记录，不是向工厂设备下发任务。不要用重新生成方案
后的新版本去复用旧审批。

## 6. 测试与常见故障

```powershell
$env:PYTHONPATH = Join-Path $PWD 'src'
$env:PLANPILOT_FORBID_LLM_NETWORK = '1'
.\.venv-runtime\Scripts\python.exe -m pytest tests/unit -q
.\.venv-runtime\Scripts\python.exe tools/check_closed_vocabularies.py
Invoke-RestMethod http://127.0.0.1:8080/health
```

健康检查要求服务正在运行。单元测试和词汇检查不需要服务或 AWS。
Node.js 为可选开发工具，仅前端 DOM 模拟回归脚本使用，网页运行不需要 Node。

| 问题 | 处理 |
|---|---|
| 找不到 .venv-runtime 或 planpilot 模块 | 先安装，再在项目根目录运行提供的启动脚本 |
| Bearer token required | 网页粘贴本地 Token，不能填 AWS Key |
| Token 过期或验签失败 | 用同一本地数字密钥签发新 Token，或重新启动并粘贴新 Token |
| 8080 端口占用 | 停止旧服务；不要多开两个启动终端 |
| Bedrock 密钥格式错误 | 复制完整 Bedrock API Key，不用 IAM 密钥 |
| Bedrock 403 | 检查页面 AWS 原因、密钥有效期、模型权限，必要时联系 AWS Support |
| Claude 403 | 检查团队 API Key、Claude 权限及提供商首次使用要求 |
| inference profile 不可用 | 核对源区域为 `ap-southeast-1` 且账号允许合同固定的全局推理配置 |
| 已生成但模型解释失败 | 计划已保存，查看确定性结果或另发解释请求，不必重复生成 |

## 7. 包内容与交付验证

`src/` 是后端，`tools/web/index.html` 是网页，`tools/` 是启动和验证工具，
`examples/` 和 `data/` 是本地合成工厂数据，`tests/` 为测试，`contract/` 保存
原始合同，`docs/` 为补充说明。未包含 Git 历史、个人 AWS 密钥、网页登录
Token、运行数据库、虚拟环境或缓存。每位队友安装依赖并创建自己的数据库。

ZIP 内有 `PACKAGE_MANIFEST.json`，列出每个文件的 SHA-256。ZIP 旁还有
`.sha256` 校验文件。Windows 可用 `Get-FileHash -Algorithm SHA256` 核对 ZIP。
打包不会自动发送给任何人；将 ZIP 和校验文件自行分享给队友即可。

## English quick start

Install Python 3.11 x64, extract the entire archive, open PowerShell in the
`PlanPilot` folder and run `tools/setup_local.ps1 -Dev` with execution-policy bypass.
Run `tools/start_local.ps1`, enter your own local signing secret (32+ digits), then
paste the generated clipboard token into http://127.0.0.1:8080/ . Click the generate
button for offline scheduling. AWS credentials are not included.

For model chat, stop the server and restart with `-Bedrock -BedrockKeyFile` pointing
to your own one-line Bedrock API key file. Defaults are Claude Sonnet 4.5 via its
global inference profile, with Singapore as the source region.
The web token and AWS key are different credentials. Model chat can incur charges.
AWS access is not guaranteed by offline tests. The runtime, startup script and
package manifest all enforce the contract-pinned Claude Sonnet 4.5 profile.
