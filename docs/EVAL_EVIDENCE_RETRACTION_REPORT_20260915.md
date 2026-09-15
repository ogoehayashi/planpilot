# PlanPilot EVAL 证据撤回与分层整改报告

日期：2026-09-15（Asia/Shanghai）  
范围：正式验收证据、组件 smoke、交付校验；不声称完成合同端到端验收。

## 结论

原 `EVAL-001..030: 30 PASS` 声明已撤回。旧 `tools/run_evals.py` 把组件函数、
弱断言、常量表达式和未执行的安全场景映射成正式 PASS，不能证明合同逐案
`scenario` 与 `pass_condition`。其中包括无条件 `check=True` 和
`100 < 300` 一类断言；这类结果不具备验收效力。

整改后的正式状态是：

```text
EVAL-001..030: 0 PASS / 0 FAIL / 30 BLOCKED
runtime_evaluation: PENDING_UNTIL_EVAL_001_TO_030_EXECUTE
acceptance_ready: false
acceptance_claimed: false
```

当前可以继续开发，但不能对外宣称正式 EVAL 已通过或项目 pilot-ready。

## 已落地的分层

### 1. 正式验收门

`tools/run_evals.py` 现在只承担正式验收职责：

- 从合同读取且严格校验 EVAL-001..030 的顺序、唯一性和必需字段；
- 原样保存每一案的 `scenario` 与 `pass_condition`；
- 在没有专用端到端 runner 和可复核 artifacts 时一律 `BLOCKED`；
- 每案保存 `unmet_pass_condition`，`evidence_refs` 保持空数组；
- 原子写入 `tests/evidence/runtime-eval/EVIDENCE.json`；
- 未就绪时返回退出码 2，使 CI 和人工脚本 fail closed。

### 2. 组件 smoke

原本有一定价值的局部检查迁移到 `tools/run_smoke_harness.py`，证据写入
`tests/evidence/smoke-harness/EVIDENCE.json`：

- 使用 `check_id`，不使用正式 `case_id`；
- 使用小写 `pass` / `fail`，避免与合同验收状态混淆；
- `related_contract_cases` 仅作需求追踪，不表示执行了对应 EVAL；
- 固定写入 `acceptance_claimed=false` 与正式 runtime pending 状态；
- 当前 8 个 smoke 覆盖候选确定性、FIFO ready time、注入边界、审批优先级、
  fallback provenance、固定拆 lot、stability utility 和 overtime utility。

### 3. 防回归与交付卫生

- 新增 5 个诚实性回归测试，钉住正式 gate 的 30 个逐案 BLOCKED、原文
  pass condition、非零退出、smoke 隔离和文档禁用旧声明；
- 闭集词汇守卫现在从合同 `release_readiness` 自动读取正式 runtime 状态，
  没有通过字符串拆分绕过守卫；
- `setup_local.ps1` 用 Python `-X utf8` 启动 pip，修复中文 Windows 默认
  编码读取 requirements 注释时的失败；
- 两套 mutation negative control 排除所有 `.venv*`，防止复制 465 MiB
  本地环境进入沙箱；
- 打包器排除旧 `PACKAGE_MANIFEST.json`，支持 `.log` 证据，并新增
  `--refresh-manifest` 以维护解压源码包的完整性清单。

## 本轮实测证据

| 检查 | 实测结果 |
|---|---|
| 新增诚实性回归 | 5 passed |
| 单元测试 | 530 passed in 11.92s |
| 全仓测试（含 mutation control） | 534 passed in 212.38s |
| 正式验收 gate | 0 PASS / 0 FAIL / 30 BLOCKED；退出码 2 |
| 组件 smoke | 8 pass / 0 fail；不声称 acceptance |
| 闭集词汇守卫 | PASS；24 vocabularies / 152 known members |
| 词汇守卫 self-test | PASS |
| 数据集结构 | 17 sheets / 7 events / 30 case rows |
| 网页真实 solver 输出与 DOM smoke | PASS |
| EVAL 撤回只读 fact-check | 21 项 PASS / fails=0 |
| 临时 ZIP 构建与清单唯一性 | PASS；149 files，单一 manifest，逐项完整性校验 |
| 合同 SHA-256 | `b92e53f4ff0541050ec6585f3237f4d764a26e455447a672f3419dfb284fe639` |

## 进入正式 PASS 的硬门槛

任一 EVAL 只能在以下条件全部满足后从 `BLOCKED` 变为 `PASS`：

1. 有该 case 独立、可复跑的端到端 runner，而不是共享默认断言；
2. runner 执行合同中的准确 scenario，并逐项证明准确 pass condition；
3. 输入、输出、日志、耗时、solver build、随机种子和环境信息形成带哈希 artifact；
4. 安全案例包含负控，证明删掉关键防线会使测试失败；
5. validator、审批、发布和 audit 边界使用真实集成路径，不走 demo 旁路；
6. 证据引用存在、可读、哈希一致，且独立 fact-check 为零失败；
7. case 失败记 `FAIL`，缺依赖或缺证据记 `BLOCKED`，禁止用 smoke 代替。

## 尚未满足的阻断项

- 30 个正式 case-specific runner 尚未实现；
- 合同形状的公共 tool/error middleware 尚未完成；
- demo runtime 尚未完全接入已审计的 PlanStore 与 ApprovalService；
- 独立 validator、发布事务与 audit hash chain 尚未形成完整端到端证据链。

因此，本轮成果是“证据口径恢复可信并建立 fail-closed 地基”，不是“30 个
EVAL 已重做完成”。下一阶段应先完成 tool-error-middleware 与真实运行路径集成，
再按风险优先顺序逐案实现 acceptance runners。
