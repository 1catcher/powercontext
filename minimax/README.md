# 规有所依 — MiniMax × OceanBase SDD 上下文模块

为 AI 开发助手提供任务所需的规格、代码和测试：工程资料存 **seekdb**，
任务状态存 **PowerContext**，代码修改由 **MiniMax** 生成。本模块是一个可运行的
CLI（`sdd_context`），配套一个带测试的演示业务工程 `demo_project/booklib`。

```
minimax/
├── run.sh                     # 一键端到端验证样例（推荐入口）
├── .env.example               # 环境变量示例（占位值，不提交真实凭据）
├── demo_project/              # 业务工程（可运行、带 pytest 测试）
│   ├── booklib/catalog.py     # 被修改的代码（修改后状态）
│   ├── specs/                 # 规格 v1/v2（验证样例输入）
│   ├── tests/test_catalog.py  # 目标功能测试 + 回归测试
│   └── snapshots/             # 修改前/后快照与 diff（验证样例产物）
├── sdd_context/               # 上下文模块源码（调用入口: cli.py）
├── devlog/                    # MiniMax 开发记录、测试日志（运行后生成）
└── README.md / tech_note.md   # 运行说明 / 一页技术说明
```

## 1 · 技术分工

| 技术 | 承担的作用 | 接入方式 |
| --- | --- | --- |
| **seekdb**（embedded, pylibseekdb 1.x） | 存储规格/代码/测试资料，SQL 检索结果构造任务上下文 | pylibseekdb 嵌入式运行时 + SQLAlchemy/aiomysql（复用 powercontext 自带的 seekdb 驱动，版本见 `uv.lock`） |
| **MiniMax**（MiniMax-M2.7） | 根据上下文生成 `catalog.py` 的代码修改 | Anthropic 兼容 HTTP API：`POST {MINIMAX_BASE_URL}/v1/messages`，`x-api-key` 认证 |
| **PowerContext**（本仓库） | 保存任务进展与验证结果，重启后读取复用 | PowerContextClient HTTP API（Source 持久化） |

## 2 · 环境要求

- Linux 或 macOS（pylibseekdb 为 Linux/macOS 提供二进制 wheel）、Python 3.11+
- [uv](https://docs.astral.sh/uv/)；依赖由本仓库 `pyproject.toml` 声明（`uv sync` 安装；seekdb 相关依赖通过 `--extra seekdb` 随用随装，也可 `uv sync --extra seekdb` 提前安装）
- MiniMax API Key（仅在线生成修改时需要；从 `minimax/.env` 或环境变量 `MINIMAX_API_KEY` 读取）

## 3 · 配置与数据初始化

```bash
git clone <你的 powercontext fork> && cd powercontext && git checkout 2026-competition-ecosystem
uv sync                                    # 安装全部依赖（含 dev 组 pytest）
cp minimax/.env.example minimax/.env       # 填入 MINIMAX_API_KEY（或 export MINIMAX_API_KEY=...）
```

`bash minimax/run.sh` 会自动完成余下初始化：生成 PowerContext 服务配置、创建
seekdb 资料库与 `materials` 表（含全文索引）、创建 PowerContext 作用域。

## 4 · 调用入口（输入 / 输出）

| 命令 | 输入 | 输出 |
| --- | --- | --- |
| `uv run python -m sdd_context.cli init` | 无 | seekdb 资料库建表；PowerContext 作用域与初始任务状态 |
| `uv run python -m sdd_context.cli ingest` | `demo_project/specs/catalog-spec.md`、`booklib/catalog.py`、`tests/test_catalog.py` | 三份资料写入 seekdb；规格采集为 PowerContext Source |
| `uv run python -m sdd_context.cli context` | 任务关键词（内置：list_books/keyword/…） | 适用规格条目、需修改代码文件与符号、验证测试、已完成/待办/验证结果（均附真实来源） |
| `uv run python -m sdd_context.cli verify --phase before\|after` | pytest 测试集 | 测试运行结果，写入 PowerContext 状态与 devlog |
| `uv run python -m sdd_context.cli fix [--offline]` | 任务上下文 | MiniMax 生成的新 `catalog.py`（应用并校验），快照与 diff，开发记录 |
| `uv run python -m sdd_context.cli save` | 当前进度 | 最终任务状态写入 PowerContext |
| `uv run python -m sdd_context.cli resume` | 无（模块重启后） | 从 PowerContext 读取的任务状态，供后续上下文使用 |

命令在 `minimax/` 目录下执行（PowerContext 服务需已启动，`run.sh` 已处理）。

## 5 · 验证样例

**任务**：为 `booklib.Catalog.list_books` 增加关键词过滤（SPEC-2）。

**要改什么**：`list_books` 增加 `keyword` 参数；书名或作者包含关键词（不区分大小写）
的图书才保留；与 `min_pages` 同时生效。

**必须保持的原有行为**（SPEC-1）：返回顺序与插入顺序一致；`min_pages` 过滤语义不变
（缺省 0 返回全部、负数视为 0）；空目录返回空列表。

**初始数据**：`demo_project/` 全部文件；`tests/test_catalog.py` 含 1 个目标测试
`test_filter_by_keyword`（修改前必然失败：`list_books() got an unexpected keyword
argument 'keyword'`）与 3 个回归测试（修改前后都必须通过）。

**操作步骤**：`bash minimax/run.sh`，流程为
初始化 → 导入 v1 规格并输出上下文 → 规格更新为 v2 并重新导入（输出随之更新）→
修改前测试（目标失败、回归通过）→ MiniMax 生成修改 → 修改后测试（全部通过）→
保存状态 → **重启模块** → 读取并复用状态。

**预期结果**：见 `run.sh` 各步骤日志；关键断言：

1. 首次 `context` 只含 SPEC-1；规格更新并 `ingest` 后，`context` 含 SPEC-2；
2. `verify --phase before`：`test_filter_by_keyword` 因 TypeError 失败，其余 3 个通过；
3. `fix` 之后 `catalog.py` 含 `keyword` 参数，`snapshots/catalog.diff` 为真实差异；
4. `verify --phase after`：4 个测试全部通过；
5. 服务重启后 `resume` 输出包含已完成事项、待办事项与验证结果，且被后续 `context` 使用。

**重置方法**：重新执行 `bash minimax/run.sh`（脚本会把规格重置为 v1、代码重置为
修改前快照后重新走完整流程）。仅重置代码/规格而不重建状态时，可执行
`cp demo_project/snapshots/catalog.before.py demo_project/booklib/catalog.py`
与 `cp demo_project/specs/catalog-spec.v1.md demo_project/specs/catalog-spec.md`。

## 6 · 测试命令

```bash
cd minimax/demo_project && uv run pytest -q tests     # 4 个测试（修改前 3 过 1 失败）
bash minimax/run.sh                                   # 端到端（含服务编排）
```

## 7 · 已知限制

- pylibseekdb 仅支持 Linux/macOS；Windows 请使用 WSL2。
- 在线 `fix` 依赖 MiniMax API 可用性；离线模式（`--offline` 或无 API Key）重放
  已记录修改，开发记录仍体现 MiniMax 参与。
- seekdb 资料库为嵌入式单机实例（文件位于 `minimax/state/materials/`）。
