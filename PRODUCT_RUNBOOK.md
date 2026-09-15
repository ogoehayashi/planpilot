# PlanPilot production runbook

## Local run

最简单的本地启动方式是运行：

```powershell
.\tools\start_local.ps1
```

脚本会提示输入数字密钥，自动设置环境变量、签发 planner Token、复制
Token 到剪贴板并启动 API。打开网页后按 `Ctrl+V` 填入“访问令牌”字段即可。
Manager Token 使用：

```powershell
.\tools\start_local.ps1 -Role manager -User manager-demo
```

The one-command local launcher prompts for a numeric secret, issues a signed
planner token, copies it to the clipboard, and starts the API. The secret must
contain at least 32 digits. Use `-Role manager` when testing manager approvals.

```powershell
$env:PYTHONPATH="src"
python tools/api_server.py
```

Open `http://localhost:8080/`. To plan against a factory file, post JSON with
`request` and `factory_file` fields. The file must contain `orders`, inventory,
maintenance and worker records as shown in `examples/factory_demo.json`.

Excel workbooks require `pip install -r requirements-import.txt` and must have
`Orders`, `Operations` and `Inventory` sheets. Inventory may be represented as
FIFO batches with `batch_id`, `quantity` and `available_at`.

Set `PLANPILOT_AUTH_SECRET` before starting the API. Approval and publish calls
must send `Authorization: Bearer <token>`; tokens are signed with the same
secret and carry planner or manager permissions.

## Bedrock configuration

Set `AWS_REGION` and `BEDROCK_MODEL_ID`, configure the AWS credential provider
chain with `aws configure` or an IAM role, and instantiate
`BedrockIntentClient`. The client uses `temperature=0`; tool execution remains
allowlisted and deterministic.

## Persistence and operations

`planpilot.persistence.Database` uses SQLite WAL mode, transactions and an
optimistic version check. Store the database on durable block storage and back
up the file while the service is stopped or after a SQLite online backup.
Expose it behind TLS and an authenticated reverse proxy before any network use.

Run deterministic component checks with `python tools/run_smoke_harness.py`.
Their evidence is labelled `component_smoke`; related EVAL IDs are traceability
hints only and never acceptance results.

Run the formal gate with `python tools/run_evals.py`. It currently records all
30 contract cases as `BLOCKED`, writes the exact unmet pass condition for each,
and exits with code 2. Installing the 17-sheet workbook alone is not sufficient:
each case still needs a dedicated end-to-end runner and reviewable artifacts.
Do not convert smoke results, unit tests, constants or partial adapters into
formal EVAL PASS claims.

Create a consistent SQLite backup with:

```powershell
python tools/backup_database.py planpilot.db backups/planpilot.db
```

`GET /health` is a liveness check and `GET /metrics` returns in-process request
counts and accumulated schedule latency. Export these values to the deployment
monitoring system rather than relying on process memory for long-term metrics.
