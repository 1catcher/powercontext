# 验证样例 — ReSearch 科研资料工作台

任务：一个可运行的科研资料工作台——中文资料写入 seekdb 并可全文检索（含类型
过滤），任务进度写入 PowerContext 且**服务重启后复用**。

## 初始数据

`demo_data/` 三篇中文资料，`scripts/seed.py` 一键导入：

| 标题 | 类型(kind) | 字符数 |
| --- | --- | --- |
| 数据库系统复习笔记-索引 | note | 412 |
| 论文笔记-RAG混合检索 | paper | 420 |
| seekdb部署与全文检索速查 | doc | 575 |

## 操作步骤

1. `bash qoder/run.sh`（自动启动 PowerContext 与工作台）；
2. `cd qoder && uv run --with pymysql python scripts/seed.py` 导入演示数据；
3. 浏览器打开 `http://127.0.0.1:8080`：检索关键词、查看资料库、填写待办/笔记并保存；
4. Ctrl+C 停止（或直接 kill 两个进程）→ 重新 `bash qoder/run.sh` → 刷新页面，
   待办/笔记应原样恢复；
5. 再次修改并保存 → 状态追加为新版本，不覆盖旧版本。

## 实测证据（2026-10-31，WSL Ubuntu-22.04）

### 1. 中文全文检索 + 类型过滤

```json
GET /api/search?q=索引          → {"mode":"MATCH..AGAINST","count":2, ...}
GET /api/search?q=索引&kind=note → count=1，命中 id=2（数据库系统复习笔记-索引）
GET /api/search?q=混合检索       → count=3（三篇均含相关词）
```

### 2. 状态保存为版本化 Source

```json
POST /api/state {"todo":["复习 HNSW 构图","重读 RAG 论文 3.1 节"],"notes":"对 posting list 合并还不熟"}
→ {"ok": true, "source_id": "workbench-state-0001"}
POST /api/state {...新增"写周报"...}  → {"ok": true, "source_id": "workbench-state-0002"}
```

### 3. 重启后状态复用（核心断言）

完整重启（kill PowerContext + 工作台 → run.sh 重新拉起）后：

```json
GET /api/state → {"state":{"todo":["复习 HNSW 构图","重读 RAG 论文 3.1 节"],
                  "notes":"对 posting list 合并还不熟","updated_at":"2026-10-30T16:58:32+00:00"},"ok":true}
```

页面快照同样确认：待办文本框自动恢复 3 条待办（含重启后新增的「写周报」），
「上次保存」显示 `2026-10-30T17:01:52+00:00`；顶部徽章 `seekdb: 已连接`、
`PowerContext: 已连接`。

### 4. 审计日志佐证技术实际参与

`app/state/access.log`（节选）：

```
{"op":"powercontext.write","source_id":"workbench-state-0002","scope":"scp_61mep6sq8j6rv9fzddyfxmzx8r","at":"2026-10-30T17:01:52+00:00"}
{"op":"seekdb.select","sql":"SELECT ... WHERE MATCH(content) AGAINST(%s) AND kind = %s ...","params":["索引","note",20],"rows":1,...}
{"op":"seekdb.select","sql":"SELECT ... WHERE MATCH(content) AGAINST(%s) ...","params":["混合检索",null,20],"rows":3,...}
```

### 5. 重置方法

- 重置资料库：`DROP DATABASE research_workbench;` 后重启工作台（自动重建）。
- 重置任务状态：删除 `app/state/powercontext.db` 后重启（作用域按 title 复用或重建）。
