# PlanPilot AI 项目介绍 / Project Overview

## 中文介绍

### 项目名称

**PlanPilot AI：制造业生产计划 Agent**

PlanPilot AI 是一个面向制造业中小企业生产计划员的智能排程协作系统。它帮助计划员协调客户订单、物料库存、机器产能、员工技能、维护窗口、班次和换线要求，并快速生成可比较、可审计、需要人工批准后才能发布的生产计划。

### 要解决的问题

制造业中小企业经常依赖电子表格和个人经验安排生产。当订单数量变化、客户插入紧急订单、物料延迟、机器故障或员工缺勤发生时，人工计划很难快速调整，也容易产生机器冲突、员工重复分配、物料不可用和交期失真的问题。

PlanPilot 将这些隐含在个人经验中的规则显式化为可验证、可追溯的配置和约束，让计划结果可以重现、比较和审计。

### 核心功能

- 基于 CP-SAT 的确定性有限产能排程
- Balanced、Delivery First、Cost First 三种计划方案
- FIFO 批次库存预留和确认到货时间约束
- 固定最大批量拆分和数量校验
- 机器、员工、班次、维护、换线和工序前置约束
- 终检工序和未排工序显式报告
- EVT-001 至 EVT-007 事件驱动重排
- 紧急订单、机器故障、物料延迟和员工缺勤处理
- Prompt Injection 阻断与审计哈希链记录
- overtime、changeover、late order、coverage 和 stability KPI
- 计划版本、digest、乐观并发和幂等发布
- Planner/Manager 角色权限与审批队列
- Excel 和 JSON 工厂数据导入
- SQLite WAL 持久化与在线备份
- `/health`、`/metrics` 和受保护 API
- Docker Compose、定时备份和生产运行手册

### Agent 如何参与

Agent 负责理解和协调，不负责计算关键业务结果。

1. Agent 读取计划员的自然语言请求，识别目标、约束和事件。
2. Agent 选择允许调用的确定性工具。
3. 数据加载、输入验证、排程、KPI 计算和硬约束验证由本地工具完成。
4. Agent 比较多个已验证方案，解释交付率、延迟、换线、物料和 overtime 的影响。
5. Agent 明确提示未排工序、延迟订单、物料短缺、风险和审批要求。
6. Agent 可以发起审批，但不能自行批准 overtime、交期变更、secondary skill 或发布计划。
7. 发布前，系统重新检查计划版本、digest、可行性和完整审批集合。

这种设计让 Agent 提供自然语言理解和决策解释，同时把正确性和安全边界交给可测试、可审计的确定性代码。

### 安全边界

- LLM 不执行算术、排程、约束检查或 KPI 计算。
- 客户备注和事件 payload 被视为不可信数据。
- Prompt Injection 会被阻断，并写入安全审计记录。
- 旧计划的审批不能用于新版本计划。
- 没有完整审批的计划不能发布。
- 机器维护、物料、技能和安全约束不能被 Agent 覆盖。
- 所有计划变更都重新进入 `RECEIVED` 状态并生成新版本。

### 数据集

项目包含一个可重复生成的 V1.8 合规测试数据集：

- [PlanPilot_Mock_Factory_Dataset.xlsx](data/PlanPilot_Mock_Factory_Dataset.xlsx)
- [factory_demo_v18.json](data/factory_demo_v18.json)

数据集包括 17 个工作表、7 个事件、30 个 EVAL 案例、产品 BOM、批次库存、机器和员工日历、员工技能、Changeovers、审批策略和计划输出字段。

生成和校验：

```powershell
.venv-runtime\Scripts\python.exe tools/generate_compliant_dataset.py
.venv-runtime\Scripts\python.exe tools/validate_compliant_dataset.py
```

### 本地运行

```powershell
$env:PYTHONPATH="src"
$env:PLANPILOT_AUTH_SECRET="请设置至少 32 个字符的密钥"
.venv-runtime\Scripts\python.exe tools/api_server.py
```

访问 `http://localhost:8080/` 打开计划页面。

运行测试：

```powershell
.venv-runtime\Scripts\python.exe -m pytest tests/unit -q
.venv-runtime\Scripts\python.exe tools/check_closed_vocabularies.py
.venv-runtime\Scripts\python.exe tools/run_smoke_harness.py
.venv-runtime\Scripts\python.exe tools/run_evals.py
```

当前证据口径：

```text
CLOSED VOCABULARY CHECK | PASS
component smoke: 与正式验收隔离
EVAL-001..030: 0 PASS / 0 FAIL / 30 BLOCKED
```

`run_evals.py` 是 fail-closed 正式验收门：当前会写出 30 个逐案
`BLOCKED` 结果并返回退出码 2。组件 smoke 通过不代表任何 EVAL 通过。
正式验收证据位于 [tests/evidence/runtime-eval/EVIDENCE.json](tests/evidence/runtime-eval/EVIDENCE.json)，
组件证据位于 [tests/evidence/smoke-harness/EVIDENCE.json](tests/evidence/smoke-harness/EVIDENCE.json)。

### 部署和限制

项目提供 AWS Lightsail 的 Docker Compose 部署配置，并预留 AWS Bedrock Claude Sonnet 4.5 适配器。真实 Bedrock 调用、Lightsail 部署、公网 TLS、AWS 费用和真实人工审批仍需要外部环境验证。当前本地测试不会调用 Bedrock，也不会消耗 AWS 配额。

### 项目定位

PlanPilot 不是一个自动替代生产计划员的黑箱模型。它是一个由 Agent 协调、由确定性排程器计算、由验证器把关、由人类审批发布的生产计划协作系统。

---

## English Introduction

### Project Name

**PlanPilot AI: An Agentic Production Planning Copilot**

PlanPilot AI is an intelligent production-planning copilot for manufacturing SMEs. It helps production planners coordinate customer orders, material inventory, machine capacity, worker skills, maintenance windows, shifts, and sequence-dependent changeovers. It produces deterministic, comparable, auditable schedules that require human approval before publication.

### Problem

Manufacturing SMEs often rely on spreadsheets and individual experience. When demand changes, urgent orders arrive, materials are delayed, machines break down, or workers become unavailable, manual plans are difficult to adjust quickly. Manual planning can also hide machine overlaps, worker conflicts, unavailable materials, and unrealistic due dates.

PlanPilot makes this tacit experience explicit as versioned, testable, and auditable configuration and constraints.

### Core Capabilities

- Deterministic finite-capacity scheduling with CP-SAT
- Balanced, Delivery First, and Cost First plan profiles
- FIFO batch reservation with confirmed inbound availability
- Fixed maximum-size lot decomposition and reconciliation
- Machine, worker, shift, maintenance, changeover, and precedence constraints
- Explicit final inspection and unscheduled-operation reporting
- Event-driven replanning for EVT-001 through EVT-007
- Urgent-order, breakdown, material-delay, and worker-absence handling
- Prompt-injection blocking with hash-chained security audit records
- Overtime, changeover, lateness, coverage, and stability KPIs
- Versioned plans, digests, optimistic concurrency, and idempotent publication
- Planner/Manager permissions and approval queue
- Excel and JSON factory-data import
- SQLite WAL persistence and online backups
- `/health`, `/metrics`, and protected APIs
- Docker Compose, scheduled backups, and an operations runbook

### How the Agent Participates

The Agent coordinates and explains; it does not compute correctness-critical results.

1. It interprets the planner's natural-language request, constraints, and event trigger.
2. It selects allowlisted deterministic tools.
3. Loading, validation, scheduling, KPI calculation, and hard-constraint checking are performed by local tools.
4. It compares validated alternatives and explains delivery, lateness, changeover, material, and overtime trade-offs.
5. It surfaces unscheduled operations, late orders, shortages, risks, and approval requirements.
6. It may request approval, but it cannot approve overtime, due-date changes, secondary-skill assignments, or publication.
7. Before publication, the server rechecks plan version, digest, feasibility, and the complete approval set.

This boundary gives the Agent a useful natural-language interface while keeping correctness and safety in deterministic, auditable code.

### Safety Boundary

- The LLM does not perform arithmetic, scheduling, constraint enforcement, or KPI computation.
- Customer notes and event payloads are untrusted data.
- Prompt injection is blocked and recorded in the security audit trail.
- Approvals for an old plan version cannot be reused.
- A plan cannot be published without the complete required approval set.
- Maintenance, material, skill, and safety constraints cannot be overridden by the Agent.
- Every accepted plan change re-enters `RECEIVED` and produces a new plan version.

### Dataset

The repository includes a reproducibly generated V1.8-compliant test dataset:

- [PlanPilot_Mock_Factory_Dataset.xlsx](data/PlanPilot_Mock_Factory_Dataset.xlsx)
- [factory_demo_v18.json](data/factory_demo_v18.json)

It contains 17 sheets, 7 events, 30 EVAL cases, product BOMs, inventory batches, machine and worker calendars, worker skills, changeovers, approval rules, and plan-output fields.

Generate and validate it with:

```powershell
.venv-runtime\Scripts\python.exe tools/generate_compliant_dataset.py
.venv-runtime\Scripts\python.exe tools/validate_compliant_dataset.py
```

### Run Locally

```powershell
$env:PYTHONPATH="src"
$env:PLANPILOT_AUTH_SECRET="set a secret with at least 32 characters"
.venv-runtime\Scripts\python.exe tools/api_server.py
```

Open `http://localhost:8080/` to use the planning interface.

Run verification:

```powershell
.venv-runtime\Scripts\python.exe -m pytest tests/unit -q
.venv-runtime\Scripts\python.exe tools/check_closed_vocabularies.py
.venv-runtime\Scripts\python.exe tools/run_smoke_harness.py
.venv-runtime\Scripts\python.exe tools/run_evals.py
```

Current evidence scope:

```text
CLOSED VOCABULARY CHECK | PASS
component smoke: isolated from formal acceptance
EVAL-001..030: 0 PASS / 0 FAIL / 30 BLOCKED
```

`run_evals.py` is the fail-closed formal acceptance gate. It currently writes
30 case-level `BLOCKED` results and exits with code 2. Passing component smoke
checks does not mean that any EVAL case passed. Formal evidence is stored in
[tests/evidence/runtime-eval/EVIDENCE.json](tests/evidence/runtime-eval/EVIDENCE.json);
component evidence is stored in [tests/evidence/smoke-harness/EVIDENCE.json](tests/evidence/smoke-harness/EVIDENCE.json).

### Deployment and Limitations

The project includes Docker Compose configuration for AWS Lightsail deployment and an adapter for AWS Bedrock Claude Sonnet 4.5. Real Bedrock calls, Lightsail deployment, public TLS, AWS usage and cost, and human approval usability still require external validation. Local tests never call Bedrock and do not consume AWS quota.

### Positioning

PlanPilot is not a black-box model intended to replace the production planner. It is a production-planning collaboration system coordinated by an Agent, computed by a deterministic scheduler, checked by independent validators, and published only through human approval.
