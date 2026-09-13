# PlanPilot `plan-store-and-digest` 第六轮审查整改报告

**日期：** 2026-09-13  
**整改起点：** Git `fc908d7` 及其后的未提交第五轮工作树  
**契约：** `contract/planpilot_agent_contract_v1.8.json`  
**契约 SHA-256：** `b92e53f4ff0541050ec6585f3237f4d764a26e455447a672f3419dfb284fe639`

## 结论

审查提出的三项收口工作均已完成：negative control 不再修改真实工作树；
孤立 UTF-16 surrogate 现在产生已注册、契约形状正确的错误；`-0.0` 与
`0.0` 归一为同一 canonical JSON 和 digest。证据包已重新生成，完整
handoff fact-check 为 `fails=0`。

本报告随同一个干净 Git commit 交付；接收方应以交付时给出的 commit SHA
为下一 part 的唯一基线，而不是以 `fc908d7` 或任意未提交工作树为基线。

## 1. P1：mutation 中断污染真实源码

### 已证实风险

旧 runner 把 mutation 写进仓库中的真实文件，再依赖 `finally` 恢复。
`finally` 只能覆盖 Python 控制流中的异常，无法覆盖 SIGKILL、解释器崩溃、
重启或断电。审查者已经在一次交付树中发现两处残留 mutation；其中
defence-in-depth mutation 甚至可能让普通测试继续全绿。

### 修复

- 每个 negative-control module 创建一个 pytest 临时仓库副本；
- 除 `.git`、缓存和 `.venv-review` 外，复制所有项目输入，保留路径敏感测试；
- 58 个 mutation 和所有 pytest 子进程只在临时副本中运行；
- 每轮仍在 `finally` 恢复副本，确保顺序执行相互独立；
- 额外只读快照真实五个 target，并断言其字节完全未变；
- 保留 `PYTHONDONTWRITEBYTECODE=1` 和 cache 清理，继续防止 D20 的陈旧
  `.pyc` 误判。

没有采用“发现 `# MUTATION:` 就让 collection 失败”作为主方案，因为大多数
mutation 本来就带该标记；这种方案会让所有变体因同一个表面原因失败，无法
证明各自命名的行为防线能捕获缺陷。

实测输出：

```text
NEGATIVE CONTROL (store) | caught=56 escaped=0 broken_fixtures=0 of 58
sources restored to pre-test bytes: True
working repository unchanged by mutations: True
```

## 2. P2：孤立 surrogate 泄漏裸异常

旧实现使用 `ensure_ascii=False`，孤立 surrogate 能通过 `json.dumps`，直到
`.encode("utf-8")` 才抛出未注册的 `UnicodeEncodeError`。这与工具错误必须
映射为契约注册错误的约定冲突。

现在 canonicalization 在编码前递归检查：

- 字符串值中的 U+D800–U+DFFF；
- JSON object key 中的 U+D800–U+DFFF；
- 嵌套 dict、list、tuple 中的上述值。

命中时抛出 `CanonicalizationError(code="INVALID_INPUT")`，携带 `json_path`
和可通过 `$defs.error_details_invalid_input` 的 `details`。新增参数化回归测试
覆盖 value 与 key；negative control 可删除该 guard 并确认对应测试失败。

## 3. P3：IEEE-754 signed zero

旧实现分别输出 `-0.0` 与 `0.0`，导致数值相等、规划语义相同的 KPI 产生不同
digest。现在 canonicalization 在一个递归复制出的树上把所有浮点零转换为
`0.0`：

- `canonical_json({"x": -0.0}) == canonical_json({"x": 0.0})`；
- 两者 `canonical_plan_digest` 相同；
- 原输入中的 sign bit 保持为负，证明没有修改调用方对象。

该规则已写入 design、task、handoff 和 evidence reference；negative control
可删除 normalization 并确认回归测试失败。

## 4. 验证结果

| 门槛 | 结果 |
|---|---|
| unit suite | **418 passed** |
| full suite | **421 passed** |
| store negative control | **56 caught / 0 escaped / 0 broken fixtures of 58**；另有 2 个 defence-in-depth |
| mutation crash containment | **working repository unchanged: true** |
| closed-vocabulary guard | **PASS**，21 vocabularies / 135 members / 32 modules |
| guard self-test | **PASS**，17 cases |
| Kiro workspace validation | **ok=190 fail=0** |
| workspace negative control | **caught=14 escaped=0 of 14** |
| handoff fact-check | **fails=0** |
| contract hash | **未改变** |

证据目录：`tests/evidence/plan-store-and-digest/`。其中 JSON 新增
`signed_zero`、`isolated_surrogate` reference，并把
`working_repository_unchanged` 纳入 `all_green` 判定。

## 5. 推进判断

`plan-store-and-digest` 已具备进入下一 part 外部审查的条件。建议外部审查者
先从交付 commit 运行 `REVIEW_PROMPT.md` 中的四个基线命令；全部通过后推进
`approval-service`。

该判断仍只覆盖 store/digest 模块。scheduler、approval service、tool layer、
audit chain、真实数据迁移和 EVAL-001..030 尚未完成，因此不得据此声称整个
PlanPilot 已 pilot-ready。
