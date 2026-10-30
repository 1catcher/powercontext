"""Configuration for the SDD context module.

Secrets never live in the repository: the MiniMax API key is read from the
``MINIMAX_API_KEY`` environment variable, mirroring ``.env.example``.
"""

from __future__ import annotations

import os
from pathlib import Path

MODULE_ROOT = Path(__file__).resolve().parents[1]  # minimax/
DEMO_PROJECT = MODULE_ROOT / "demo_project"
SPEC_FILE = DEMO_PROJECT / "specs" / "catalog-spec.md"
SPEC_V1 = DEMO_PROJECT / "specs" / "catalog-spec.v1.md"
SPEC_V2 = DEMO_PROJECT / "specs" / "catalog-spec.v2.md"
CATALOG_FILE = DEMO_PROJECT / "booklib" / "catalog.py"
CATALOG_BEFORE = DEMO_PROJECT / "snapshots" / "catalog.before.py"
CATALOG_AFTER = DEMO_PROJECT / "snapshots" / "catalog.after.py"
CATALOG_DIFF = DEMO_PROJECT / "snapshots" / "catalog.diff"
TESTS_DIR = DEMO_PROJECT / "tests"
STATE_DIR = MODULE_ROOT / "state"
MATERIALS_DB = STATE_DIR / "materials"
DEVLOG_DIR = MODULE_ROOT / "devlog"

TASK_TITLE = "为 booklib.Catalog.list_books 增加关键词过滤"
TASK_DESCRIPTION = (
    "本任务按 SPEC-2 为 Catalog.list_books 增加 keyword 参数："
    "书名或作者包含关键词（不区分大小写）且页数不低于 min_pages 的图书。"
    "必须保持 SPEC-1 既有行为：插入顺序、min_pages 过滤、空目录返回空列表。"
)
# Terms used to retrieve materials from seekdb for this task.
TASK_KEYWORDS = ["list_books", "keyword", "min_pages", "catalog", "filter", "关键词"]

POWERCONTEXT_URL = os.environ.get(
    "POWERCONTEXT_SERVER_URL", "http://127.0.0.1:8600"
)
SCOPE_TITLE = "minimax-sdd-booklib"

MINIMAX_API_KEY = os.environ.get("MINIMAX_API_KEY", "")
MINIMAX_BASE_URL = os.environ.get("MINIMAX_BASE_URL", "https://api.minimax.cn/anthropic")
MINIMAX_MODEL = os.environ.get("MINIMAX_MODEL", "MiniMax-M2.7")

STATE_SOURCE_ID = "sdd-task-state"  # PowerContext source id for task state
SPEC_SOURCE_PREFIX = "spec-catalog"
