# Qoder 开发记录

- 时间: 2026-10-30 ~ 2026-10-31
- 任务: 用 Qoder 开发「ReSearch 科研资料工作台」（PowerContext 存任务状态、seekdb 存检索索引），并通过 Qoder Sites 发布项目网站
- 开发方式: Qoder Desktop agent（本项目全部开发、调试、验证与发布由 Qoder agent 完成）
- Qoder 账号 UID: {{QODER_ACCOUNT_UID}}（提交时替换为实际账号 UID）

## 1 · 需求拆解（Qoder 做了什么）

接手任务「用 Qoder 做 PowerContext / seekdb 开发者工作台，网站必须 Qoder Sites 发布」后，Qoder 拆出如下子任务：

1. **勘察两项技术**：读 powercontext 仓库源码（`_generated/operations.py`）摸清
   HTTP 路由与 Source 语义；实测 seekdb（2881 端口）全文索引的解析器行为。
2. **PowerContext 记忆模块**：scope 管理 + 任务状态读写，解决 Source 不可变
   带来的版本化问题。
3. **seekdb 资料模块**：建库建表、中文全文索引、关键词 + 类型联合检索。
4. **端到端应用**：零框架工作台（标准库 http.server + pymysql）+ 前端三面板 + 审计日志。
5. **验证与交付**：重启复用验证、运行说明、验证样例、Qoder Sites 建站发布、提交仓库。

## 2 · 编码（关键实现决策）

- 工作台仅依赖标准库 + pymysql：`app/app.py` 单文件约 500 行，含 seekdb 初始化
  （幂等建库/建表/建索引）、检索（MATCH..AGAINST → LIKE 降级）、PowerContext
  客户端（urllib 标准库直连 HTTP API）、JSON-lines 审计。
- 状态保存采用**版本化 source ID**（`workbench-state-0001/0002/…`）：PowerContext
  Source 追加不可变，同 ID 不同内容会 409，Qoder 用「列现有 sources 取最大序号 + 1」
  实现追加保存；读取时取最大序号，重启后自动恢复最新状态。
- 全文索引优先 `WITH PARSER ik`（中文分词），失败退回默认解析器，再退回 LIKE，
  保证任意 seekdb 环境可跑。
- 前端三面板（资料检索 / 导入资料 / 学习进度），双连通徽章，保存按钮即写
  PowerContext。

## 3 · 调试（真实踩坑与修复）

| # | 现象 | 原因 | 修复 |
| --- | --- | --- | --- |
| 1 | 第二次保存状态报 HTTP 409 source_conflict | PowerContext Source 不可变 | 版本化 source ID（见上） |
| 2 | 中文检索始终 0 命中 | seekdb 默认全文解析器为 space（不切中文词） | `WITH PARSER ik`（并确认 ik 不支持 PARSER_PROPERTIES） |
| 3 | Windows 侧 curl 发中文 POST 挂起、服务端 UnicodeDecodeError | Git Bash 按 GBK 编码请求体，非 UTF-8 | app 对非法 UTF-8 返回 400（服务不受影响）；验证改用 WSL 内 UTF-8 curl |
| 4 | WSL 内 `pkill -f "app.py"` 误杀外层 shell（exit 15） | pkill 正则匹配到 shell 自身命令行 | 改为「先杀后起分离执行」，启动脚本化 |
| 5 | 重启后 MATCH..AGAINST 无命中（虚惊） | 验证脚本按 `total/materials` 解析，实际字段是 `count/results` | 修正解析，检索一直正常 |

## 4 · 验证（全部实测）

- 中文检索：`索引`→2、`索引+kind=note`→1、`混合检索`→3（MATCH..AGAINST，IK 分词）。
- 状态：保存 `workbench-state-0001` → 完整重启 PowerContext + 工作台 → GET 返回原
  待办/笔记 → 再保存得 `workbench-state-0002`（不覆盖历史）。
- 页面：双徽章「已连接」、资料列表 3 条、待办自动恢复、检索/导入/保存全部可交互。
- 审计：access.log 记录每次 seekdb 查询与 PowerContext 写入（见 verification.md）。
- 细节见 `verification.md`。

## 5 · 交付

- 网站: https://research-workbench-0roz63rjfdi.qoder.zone （Qoder Sites 发布，公开访问，评审入口）
- 仓库: powercontext 分支 `2026-competition-ecosystem`，`qoder/` 目录
- 运行说明: `qoder/README.md`；技术说明: `qoder/tech_note.md`；验证样例: `qoder/verification.md`
- 提交信息: `Co-Authored-By: Qoder <noreply@qoder.com>`
