"""SDD context module CLI — the module's single call entry.

Usage (from the repository root, PowerContext server already running):
    uv run python -m sdd_context.cli init
    uv run python -m sdd_context.cli ingest
    uv run python -m sdd_context.cli context
    uv run python -m sdd_context.cli verify --phase before|after
    uv run python -m sdd_context.cli fix [--offline]
    uv run python -m sdd_context.cli save
    uv run python -m sdd_context.cli resume
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from . import workflow


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="sdd-context",
        description="SDD 上下文模块：seekdb 存资料，MiniMax 改代码，PowerContext 存状态",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init", help="初始化 seekdb 资料库与 PowerContext 作用域")
    sub.add_parser("ingest", help="把规格/代码/测试导入 seekdb 并采集规格到 PowerContext")
    sub.add_parser("context", help="构建并输出任务上下文（含任务状态）")

    verify = sub.add_parser("verify", help="运行 pytest 并记录结果")
    verify.add_argument("--phase", choices=("before", "after"), required=True)

    fix = sub.add_parser("fix", help="用 MiniMax 生成代码修改并应用")
    fix.add_argument("--offline", action="store_true", help="离线重放已记录的修改")

    sub.add_parser("save", help="把最终任务状态写入 PowerContext")
    sub.add_parser("resume", help="重启后从 PowerContext 读取任务状态")

    args = parser.parse_args()
    command = getattr(workflow, f"{args.command}_cmd")
    asyncio.run(command(**{k: v for k, v in vars(args).items() if k != "command"}))


if __name__ == "__main__":
    main()
