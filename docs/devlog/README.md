# PlanPilot 开发日志(docs/devlog/)

**本文件夹是项目过程记录的权威位置。** 评委不会替我们翻散落在仓库根目录的
八份报告来拼时间线——所有"哪天做了什么、为什么、怎么验证的"以这里为准。

## 同步规则(硬约定)

任何满足下列一条的改动,必须在 `DEVELOPMENT_LOG.md` 末尾追加一条记录:

1. 修改 `src/`、`tests/`、`contract/`、`tools/` 下任何文件;
2. 创建或移动任何 git commit / branch / tag;
3. 做出一个会影响交付口径的**决策**(哪怕决定"不做");
4. 收到外部/对抗审查的 finding(无论接受还是驳回);
5. 重新生成任何证据包(EVIDENCE.json、factcheck 输出)。

条目格式(照抄即可):

```markdown
## YYYY-MM-DD HH:MM — 一句话标题

- 类型:实现 / 修复 / 审查 / 决策 / 证据
- 证据:commit `xxxxxxx`、tag、文件路径、测试数字(必须是自己跑出来的)
- 内容:做了什么、为什么、如何验证
```

诚实约束与合同一致:**没有时间戳运行证据的事不写。** 数字必须是当轮实测,
不得从旧报告复制粘贴。

## 文件

- `DEVELOPMENT_LOG.md` — 唯一主日志,按时间正序追加,永不改写历史条目
  (写错了就加更正条目,不改旧条目,与 audit trail 同一逻辑)。
- 历史报告(`IMPLEMENTATION_NOTES.md`、`*_AUDIT_REMEDIATION_REPORT.md` 等)
  保留原位不动;本日志是它们的**索引 + 补集**,不是替代品。
