# 第八部分（P1-2）排程引擎整改报告

**依据：** 恢复包 `Review/PLANPILOT_CHAMPION_REVIEW_20260916.md` 的 P1-2 和 V1.8 合同。
合同 SHA-256：`b92e53f4ff0541050ec6585f3237f4d764a26e455447a672f3419dfb284fe639`。

## 本轮结果

核心离线引擎已从“所有订单固定 lot_no=1、目标产品最大换型时间”改为固定 lot
展开、lot 级原子预留、精确前后产品成对换型和日历/加班约束。CP-SAT 按合同
Tier 0 → Tier 1（延期订单数、总延期分钟）→ Tier 2 → Tier 3 依次求解；
固定随机种子 42、单 worker。每阶段无已证明最优解时采用确定性整单优先派工，
输出 `HEURISTIC_FALLBACK` / `PRIORITY_DISPATCH_FALLBACK`，不伪称最优。

新增显式基线的稳定性目标：仅当 operation key 与产品、lot 数量、工序类型、
时长、机器和工人均一致时，允许在 `stability_drift_min` 内获得稳定性收益。
生成内容使用并记录参考计划 ID；独立校验器根据参考内容重新计算 union 分母
和 KPI。无参考计划时稳定性为 1.0、参考 ID 为 null。

内部模块边界见 `.kiro/specs/p1-2-scheduling-engine/design.md`。原有
`v18_adapter` 负责 V1.8 内容转换，`independent_validator` 独立复算硬约束与 KPI；
新增 `domain.calendar` 和 `domain.objectives`，没有让生成器的判断代替独立校验。

## 验证

- P1-2 专项：**9 passed**，包括历史换型上限误判回归、零预算 fallback、
  加班硬上限、显式基线、重新签名后篡改换型分钟仍被 HC-011 拦截。
- 全量单元测试：**618 passed**；全量 `tests/`：**625 passed**。
- Python compile、封闭词表、Kiro workspace：**PASS**。
- 原始命令、退出码、stdout 日志 SHA 和被测源码 SHA 见
  `tests/evidence/p1-2-scheduling-engine/EVIDENCE.json`。
- 正式 EVAL-001～030：**0 PASS / 0 FAIL / 30 BLOCKED**；本轮未执行比赛环境验收。

## 尚未达到的冠军门槛

1. 自动稳定性参考只认同一状态 ID 下已发布的当前计划。事件或需求变化会产生
   新状态 ID，尚不能自动查找同一 horizon 最近的已发布计划；调用者目前可显式传
   `baseline_plan_id`，但聊天生成入口尚未暴露这个参数。
2. 目前每个 profile 使用 0.2 deterministic-unit 的求解预算，尚未实现合同中
   三方案共享 40 秒与升级梯度，也未做大数据集的 SLA 实测。`engine` 记录的
   40 秒是合同上限，不能解释为已使用 40 秒或已验证 40 秒性能。
3. 独立校验会报告未排工序，但当前 authority 只存储可行计划；含 shortage 的
   候选不能作为已验证方案入库。需要独立的不可行诊断交付路径，且不得把它标记
   为 `generator_feasible=true`。
4. 恢复包仍无 `.git` 来源，不能凭本次测试证明源代码提交 provenance。此前
   P0/P1 历史报告中的测试计数是当时快照，不应作为本次版本的当前计数。

以上未完成项在 `.kiro/specs/p1-2-scheduling-engine/tasks.md` 保持未勾选。
本次是 P1-2 的可审查离线改进，不宣称项目已达到完整冠军提交标准。
