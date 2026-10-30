# 一页技术说明：MiniMax × seekdb × PowerContext SDD 上下文模块

## 模块结构

```
输入: 开发任务 (内置样例: list_books 关键词过滤)
  │
  ├─ 1. 资料摄取 (ingest) ─────────────────────────────────────────┐
  │   规格 specs/*.md、代码 booklib/*.py、测试 tests/*.py            │
  │   → seekdb embedded (pylibseekdb + SQLAlchemy/aiomysql)         │
  │   表 materials(kind, path, content)，FULLTEXT 索引               │
  │   规格同时采集为 PowerContext Source（保留版本）                  │
  │                                                                 │
  ├─ 2. 上下文构建 (context) ───────────────────────────────────────┤
  │   任务关键词 → SQL 检索 (MATCH..AGAINST，降级 LIKE)               │
  │   输出: 适用规格条目 / 代码文件与符号 / 验证测试（附真实来源）      │
  │   + PowerContext 中的已完成事项 / 待办事项 / 验证结果              │
  │                                                                 │
  ├─ 3. 代码修改 (fix) ─────────────────────────────────────────────┤
  │   上下文 + 任务提示词 → MiniMax (Anthropic 兼容 /v1/messages)     │
  │   返回完整新文件 → ast 校验(语法/符号/关键词) → 应用 → 快照与 diff │
  │   → 写入开发记录 devlog/minimax_dev_record.md                   │
  │                                                                 │
  ├─ 4. 验证 (verify before/after) ────────────────────────────────┤
  │   pytest: 修改前目标测试失败(TypeError, 功能未实现)+回归通过      │
  │   修改后全部通过 → 结果写入 PowerContext 状态                    │
  │                                                                 │
  └─ 5. 状态保存与复用 (save/resume) ───────────────────────────────┤
     PowerContext Scope 内 Source "sdd-task-state-000N" 追加式保存    │
     JSON 状态（Source 不可变，每次保存新版本）                       │
     模块重启后 resume 读取最新版本，注入后续 context 构建            │
```

## 上下文选择方式

以任务为查询意图、以资料类型为维度（spec/code/test）分别检索 seekdb：
全文匹配 `MATCH(content) AGAINST(关键词)`（数据库内完成，非应用层拼接），
检索不到时降级为 `LIKE` 多关键词 OR 查询。每条输出都携带 `path` 来源，
评审可核对文件真实存在。

## 各项技术分工

| 技术 | 分工 |
| --- | --- |
| **seekdb** | 工程资料的唯一存储与检索：规格、代码、测试入库后按任务查询，查询结果直接构成上下文 |
| **MiniMax** | 唯一代码生成方：接收上下文与提示词，输出修改后的 `catalog.py`，模块负责校验与应用 |
| **PowerContext** | 任务状态的唯一持久化：进度（已完成/待办）、验证结果以 Source 形式保存，重启后读取复用 |
