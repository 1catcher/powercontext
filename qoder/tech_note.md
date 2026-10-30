# 一页技术说明 — ReSearch 科研资料工作台

## 架构

```
浏览器 (index.html / app.js)
        │  fetch 同源
        ▼
ReSearch 工作台 (app.py, 标准库 http.server, :8080)
   ├── 资料路径 ──► seekdb (MySQL 协议, :2881)
   │                 research_workbench.materials(id, kind, title, content, created_at)
   │                 FULLTEXT INDEX ft_materials_content WITH PARSER ik
   └── 状态路径 ──► PowerContext (:8600, sqlite)
                     scope「ReSearch 科研资料工作台」
                     source workbench-state-000N（版本化追加，读最新）
        │
        └── 审计: app/state/access.log（JSON-lines：每次 seekdb 查询 / PC 写入）
```

## 关键决策

1. **零框架**：后端只用标准库 `http.server`，唯一三方依赖 `pymysql`（seekdb 走
   MySQL 协议），保证评审环境可一键复现。
2. **中文检索**：seekdb 全文索引建 `WITH PARSER ik`（默认 space 解析器不切中文词）。
   启动时按 `ik → 默认解析器 → LIKE` 三级降级，任何环境都能搜。
3. **状态版本化**：PowerContext Source 不可变（同 source_id 不同内容返回 409）。
   保存用 `workbench-state-0001/0002/…` 追加，读取时取最大序号，天然获得修改历史。
4. **幂等初始化**：启动即检查建库/建表/建索引/建作用域（作用域按 title 查重），
   重复启动不会产生脏数据。
5. **可审计**：所有外部技术动作写 access.log，验证样例据此核对
   seekdb / PowerContext 的实际参与。

## 数据流

- **导入**：POST /api/ingest → `INSERT INTO materials`（seekdb）→ 立即可检。
- **检索**：GET /api/search → `MATCH(content) AGAINST(%s)`（IK 分词）+ `kind` 过滤；
  无全文索引时退回 `LIKE '%kw%'`。返回命中条目 + 命中片段（关键词高亮在浏览器侧做）。
- **状态**：POST /api/state → 先列 scope 内 sources 找最大序号 → 以 `000N+1` 调
  `POST /v1/sources/content`（capture_content_source）；GET /api/state → 取最大序号
  source 的 content 反序列化。服务重启后同一 sqlite 文件 + 同一 scope 自动恢复。

## 依赖清单

| 组件 | 版本 | 用途 |
| --- | --- | --- |
| seekdb | 本仓库编译产物（MySQL 协议 2881） | 资料存储 + 全文检索 |
| PowerContext | 本仓库（`uv sync --extra seekdb` 安装） | 任务状态持久化 |
| pymysql | 最新（`uv run --with pymysql` 自动解析） | seekdb 客户端 |
| 标准库 http.server / json / urllib | 内置 | 工作台服务与 PowerContext 调用 |
