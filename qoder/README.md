# 研有所依 — ReSearch 科研资料工作台（Qoder × OceanBase SDD）

本模块是 2026 OceanBase 大赛生态题 **Qoder（20 分）** 的交付物：用 Qoder 开发的一个
**科研资料工作台**，资料检索存 **seekdb**（全文索引 + 类型过滤），任务进度存
**PowerContext**（跨会话 / 跨重启复用），Qoder 作为开发与发布平台贯穿全程。
项目网站（评审入口）由 **Qoder Sites** 发布。

```
qoder/
├── run.sh                     # 一键启动（自动编排 PowerContext + 工作台）
├── app/
│   ├── app.py                 # 工作台服务（标准库 http.server，唯一三方依赖 pymysql）
│   ├── static/                # 前端三件套（index.html / style.css / app.js）
│   └── state/                 # 运行后生成：powercontext.db / access.log / 服务日志
├── demo_data/                 # 3 篇中文演示资料（导入后立即可检）
├── scripts/seed.py            # 演示数据导入脚本
├── devlog/qoder_dev_record.md # Qoder 开发记录（需求拆解/编码/调试全过程）
├── README.md / tech_note.md / verification.md
└── site/                      # Qoder Sites 项目源（评审入口网站）
```

## 1 · 技术分工

| 技术 | 承担的作用 | 接入方式 |
| --- | --- | --- |
| **seekdb** | 资料存储（`materials` 表）+ 中文全文检索（IK 分词 FULLTEXT）+ 类型过滤 | MySQL 协议（pymysql），默认 `127.0.0.1:2881` |
| **PowerContext** | 任务状态（待办/笔记）持久化，服务重启后复用 | HTTP API，Source 版本化追加保存（`workbench-state-000N`） |
| **Qoder** | 开发平台：需求拆解、编码、调试、Qoder Sites 发布（评审入口） | 本交付全程由 Qoder agent 完成，过程见 `devlog/` |

## 2 · 环境要求

- Linux 或 macOS；Python 3.11+；[uv](https://docs.astral.sh/uv/)
- 一个运行中的 **seekdb** 服务（MySQL 协议端口 2881，默认 root/空密码）
- PowerContext 由 `run.sh` 自动启动（本地 sqlite，端口 8600），无需预装

```bash
git clone <你的 powercontext fork> && cd powercontext && git checkout 2026-competition-ecosystem
uv sync --extra seekdb                          # 安装依赖（含 dev 组 pytest）
export SEEKDB_HOST=127.0.0.1 SEEKDB_PORT=2881   # seekdb 连接（可选，默认值同左）
export SEEKDB_USER=root SEEKDB_PASSWORD=""      # seekdb 账号（可选）
bash qoder/run.sh                               # 一键启动；Ctrl+C 自动关闭两个服务
```

首次启动会自动：创建 `research_workbench` 库与 `materials` 表、建立全文索引
（优先 `WITH PARSER ik`，退回默认解析器，再退回 LIKE）、创建 PowerContext 作用域。
导入演示数据：

```bash
cd qoder && uv run --with pymysql python scripts/seed.py
```

## 3 · 接口一览（工作台，默认 8080）

| 接口 | 方法 | 说明 |
| --- | --- | --- |
| `/` | GET | 前端页面（三面板：检索 / 导入 / 进度） |
| `/api/health` | GET | 健康检查（含 seekdb / PowerContext 连通状态） |
| `/api/search?q=关键词&kind=note` | GET | 全文检索（MATCH..AGAINST，IK 分词）+ 类型过滤 |
| `/api/materials` | GET | 最近 20 条资料 |
| `/api/ingest` | POST | 导入资料 `{title, kind, content}` → seekdb |
| `/api/state` | GET | 读取 PowerContext 中的任务状态（自动取最新版本） |
| `/api/state` | POST | 保存任务状态 `{todo, notes}`（版本化追加） |

每次 seekdb 查询 / PowerContext 写入都会以 JSON-lines 记录到 `app/state/access.log`，
用于核对两项技术的实际作用。

## 4 · 验证样例

见 [verification.md](verification.md)：启动 → 导入 3 篇中文资料 → 中文全文检索
（关键词 + 类型过滤）→ 保存任务状态 → **重启全部服务** → 状态自动恢复并可继续
追加新版本。核心断言（均已实测通过）：

1. `q=索引` 命中 2 篇、`q=索引&kind=note` 命中 1 篇、`q=混合检索` 命中 3 篇；
2. 保存状态返回 `workbench-state-0001`，再保存返回 `-0002`（Source 不可变，版本化追加）；
3. 重启 PowerContext 与工作台后，GET `/api/state` 仍返回原待办/笔记；
4. 页面徽章显示 `seekdb: 已连接`、`PowerContext: 已连接`。

## 5 · 已知限制

- 工作台用标准库 `http.server`（单进程多线程），适合演示与个人使用，不做横向扩展。
- seekdb 全文检索需实例已启用全文索引能力；无 IK 解析器时自动退回普通解析器 / LIKE。
- PowerContext 采用本地 sqlite 持久化；如需跨机器复用，把 `app/state/powercontext.db`
  与 env 文件一并迁移即可。
