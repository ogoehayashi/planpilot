# Start prompt for the implementing Codex/Kiro task

Copy everything below the divider into a new task opened at the root of the
PlanPilot repository.

---

你现在负责 PlanPilot 黑客松项目的 G4 独立 Part：`production-gates`
（生产入口、备份验证与恢复、发布门禁与部署边界硬化）。这是 Design-First
任务：先建事实，再写代码，全程证据驱动。G2/G3 已终审封口，本 Part 不得
重开 publisher transaction 话题。

## 0. 冷启动协议（在任何代码写入之前）

1. 先读 `.kiro/specs/production-gates/design.md` 的 **§0 fact table**。
   它是本 spec（rev.5）的唯一事实底座，每一行都由评审员在 `855a5c0` 工作树上逐行
   重读，标注了 `[LP]` 的条目附实时探针输出。**如果 §0 与代码有任何冲突，
   以代码为准并立即在 tasks Phase 0 修正设计**——绝不允许照错误事实施工。
2. 逐条完成 tasks.md **Phase 0**：复跑基线（验 `855a5c0` 为 HEAD 祖先，
   非相等）、AST env inventory、重跑 §0 `[LP]` 探针四件套（WAL 条件形态
   ——clean close 只留 `<db>`，开放写事务才出 `-wal/-shm`；ro vs rw 探针
   ——ro 能 BEGIN 不能 INSERT；`run_evals` 真实两行 stdout + exit 1；
   六 kill 点矩阵），日志先落仓外 staging，Phase 6 定稿再拷入
   `tests/evidence/g4-production-gates/`。任何数字与本 spec 所载不一致
   时，以新实测为准并更新文档。
3. Phase 0 关闭前不得写任何生产代码。

## 1. 仓库与权威来源

- 合同是唯一权威：`contract/planpilot_agent_contract_v1.8.json`
  （SHA-256 `b92e53f4…fe639`，冻结；模型 **Claude Sonnet 4.5，JSON
  request/response API**——不是 4.6，不是 Agent Runtime）。
- spec：`.kiro/specs/production-gates/`（本目录，rev.5）。
- 设计权威：`design.md`（§0 事实表 + §2–§6 五个工作面包 + §7 命名卫生 +
  §8 negctl 增量 + §9 证据纪律 + §10 测试登记 + §11 撤回词表）。
- 开发基线：HEAD `855a5c0`；tag `g2-baseline-5bf299a` 永不移动。
- 日志：`docs/devlog/DEVELOPMENT_LOG.md`（append-only，每次改动同 PR 补一条：
  类型/证据/边界/验证/结果）。
- 评审资产（只读）：G2/G3 证据包 `tests/evidence/g2-publisher-transaction/`
  与外部交付 bundle `D:\PlanPilot_backups\planpilot-build_g2-g3_855a5c0_20260923.bundle`
  （SHA `91d90657…29692c`）。不修改。

## 2. 本 Part 的硬约束（违反任意一条 = 整体退回）

1. 不修改 `contract/**` 任何字节；不碰 publisher transaction 核心
   （`src/planpilot/publisher.py`、`authority.py`、审批/幂等业务语义）。
2. 不新增 pip 依赖；不动 schema 既有表列（A2 的 `CREATE TRIGGER` 除外，
   且必须 `IF NOT EXISTS` 幂等）。
3. `tools/run_evals.py` 字节冻结：内容 SHA 进测试锁；30 个 EVAL 全部
   BLOCKED、exit 1 的实测行为**不许改出任何绕过开关**（无 exit 77、无
   local-fake、无 ACK 环境变量）。smoke harness 保持独立脚本、独立
   `--output`，其产物目录永不与 `tests/evidence/runtime-eval` 混用。
4. 正式 EVAL 保持 BLOCKED，直到真实 Lightsail/Bedrock 运行；Docker 不
   可用时门禁如实记 `BLOCKED`（exit 2），绝不把跳过算 PASS。本 Part 任何
   "完成"都不暗示 EVAL PASS。
5. **谱系卫生（rev.2 的直接教训）**：每写一条新事实（列名、表名、函数名、
   响应体、CLI 参数、行号、计数）必须当场打开 `855a5c0` 的源码或跑一次真实
   命令确认。禁止引用 rev.2、禁止引用聊天记录、禁止引用任何其他 PlanPilot
   副本。§11 撤回清单里的词（`host/pid/txn_id/boot_id/version` 列、
   `db.clock`、`clock_history`、`clock_service_session`、`digest_history`、
   `docker-compose.yml`、`results/agent_eval`、`Sonnet 4.6`、exit 2 evals、
   `/api/v1/health`…）在任何新代码、新文档、新测试名中都不许出现。
6. round-1 评审钉死的五条语义红线（design §1–§5 已展开）：
   - **验证者不得修复被验证物**：`verify_audit_connection` 与
     `tools/verify_backup.py` 全程只读（`mode=ro&immutable=1` +
     `PRAGMA query_only=ON`），不建表、不补 head——缺 head 是 verdict，
     不是 repair；每次验证前后文件 SHA-256 逐字节相等有专项测试。
   - **`/health` 不偷偷改语义**：现有消费者（`test_requirement_delivery`
     的 `call("/health")[0]==200`、旧 Dockerfile HEALTHCHECK）看到的
     body 与今天逐字段一致；新语义只进新路径 `/health/live|ready|deep`。
     `/health/ready` 用独立 **rw** 连接 + 短 busy_timeout 的
     BEGIN IMMEDIATE→ROLLBACK，写锁/只读目录/ro-open 下真 503（真实第二
     连接测，不 mock；探针绝不 call clock.now()——那是写路径，checked_at
     已撤销）；
     `/health/deep` 永远不进任何自动化门禁（grep 锁死）。
   - **restore 是状态机不是拷贝**：跨进程 OS lock（pidfile 只是提示），且启动顺序钉死
     读 env→纯解析/non-DB preflight→**拿 OS lock**→marker/receipt/ACK 检查→构造 Clock→
     构造 Database→bind；关序 停 HTTP→关 Database→最后释放 lock（round-4 P0-2：今天的 main() 是 Database@:409 先于 Server@:414，锁进 Server 即失效）；
     marker 是 intent ledger（PREPARED→QUARANTINED→REPLACED→RECEIPTED，
     每笔文件操作带 SHA 记 pending/done，phase 仅在全部 op done 时推进；
     重跑先按文件系统+SHA 对账再决策，不信任标签；每个 kill 点二次
     restore 收敛）；quarantine 目录保留 main/WAL/SHM 原名；
     receipt 原子写并绑定 backup SHA + audit head + quarantine generation；ACK 是按次恢复的 sidecar（--ack 原子生成，绑定 receipt 自身哈希+generation+
     backup SHA+target_db；operator/timestamp 仅溯源字段，
     门禁=格式非法或四个绑定字段不匹配，不承诺任意字节篡改检测；PLANPILOT_RECOVERY_ACK 环境变量已撤销；任何新 restore 开始前
     先失效旧 ACK——同一备份恢复第二次也必须重新确认；restored_db_sha256_at_ack
     只是确认时证据，不是每次启动的恒等比较）；
     **未 ack 时所有环境（含 dev）拒绝启动 HTTP 服务**（socket 根本不 bind；diagnostics-only server 模式已在 rev.4 删除——构造 Database/ScenarioClock 会污染未确认恢复库），
     诊断一律走离线只读 CLI（不 import Server/Database 类）；hard-kill 测试必须包含"重跑 restore 收敛"。
   - **startup 一次性解析**：`parse_startup_env` 纯（不触文件系统）与
     `preflight` I/O 两层分离；`PLANPILOT_ENV` 封闭 `development|production`
     未知即拒，production 另拒 loopback 绑定——结构性判定（ipaddress
     is_loopback 含 IPv4-mapped + localhost 大小写/尾点归一，review-3 #2；
     Batch-A P1-2；Phase 5 compose 显式 0.0.0.0）；secret 全值入 config
     对象但 `repr=False`，summary 输出非敏感配置与路径、secret 仅
     present/length（Batch-A P2 措辞修正）；启动配置字段只在进程启动时
     快照一次；例外按 Batch-A 评审收紧：Bedrock **凭据与网络开关**
     per-call 重读以支持轮换，region/model/daily limit 在
     BedrockClient 构造时读取（design §3 F2，非 StartupConfig 字段）。
   - **不发明合同状状态码**：`RECOVERY_PENDING`/`CONFIG_REFUSING_START`
     禁用；内部类型 `RecoveryGateState.PENDING` / `StartupConfigError`，
     报告值小写 `recovery_pending`；并加测试锁死合同 error_code 枚举与
     plan lifecycle 枚举不含这些新词。
7. 第一刀代码限定 tasks Phase 0（事实核验）与 Phase 1（clock_session
   防御纵深，真实 4 列 schema：A1 必改 `clock.py` 的 `_persist`，A2 三条
   trigger + 启动时 trigger 形状摘要守卫，A3 真库 raw-SQL 篡改矩阵）。
   "删 trigger 后测试必须红"的哨兵只放 negctl 沙箱子进程，主套件永不
   故意红。Phase 1 关闭前不得写 config/health/restore/gate 新路径。
8. wheelhouse 纪律：仓内只提交 manifest（文件名+SHA256）与生成脚本；
   二进制 `.whl` 永不进 Git（`git ls-files wheelhouse/` 必须为空有测试）；
   构建时 `git ls-files deploy/wheelhouse/MANIFEST.json` 与实际 wheel 目录双核对（文件名+SHA256），不符 BLOCKED（round-4 P1-5：唯一命名布局）。

## 3. 工作流

- 严格按 tasks.md Phase 0→6 顺序；勾账纪律与 G2/G3 相同：没有新鲜真实
  证据不勾，closeout 勾账单独提交并点名被验证 SHA。
- 每个 Phase 的测试必须先在**实现前**存在并红，实现后绿（红→绿记录进
  devlog）。negctl 增量（design §8 五条 must-fail + hold）在 Phase 6 锁
  精确计数，之前不许报任何数字。
- 涉及 Docker 的任务：先探 `docker version` 可用性并记录；不可用则该任务
  标 BLOCKED 并写明，不当作完成。
- 每个 Phase 关闭做一次全量回归（targeted→unit→full→negctl→vocabulary），
  数字记 devlog，不抄历史。

## 4. 语言与产物

- 说明性文字（devlog、closeout、README diff）用中文；代码、测试名、
  契约字段保持英文。
- 完成时产出 `g4-closeout`：逐 Phase 表（任务/证据命令/输出摘要/状态
  PASS·BLOCKED），合同 SHA 复核，G3 bundle 复核（SHA 不变），交付新 bundle
  存 `D:\PlanPilot_backups\` 带 `#` 注释 sidecar。

开始执行 Phase 0。
