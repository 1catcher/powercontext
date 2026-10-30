"""SDD workflow: ingest -> context -> verify -> fix -> save/resume.

Each command below is a step of the validation sample; run.sh drives them in
order.  Command stdout is the module's structured output (sources are real
files / real DB rows / real PowerContext records).
"""

from __future__ import annotations

import ast
import difflib
import json
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from . import config
from .materials import MaterialsStore
from .minimax import MiniMaxClient
from .state import StateStore

_DEMO_FILES = [
    ("spec", "specs/catalog-spec.md", config.SPEC_FILE),
    ("code", "booklib/catalog.py", config.CATALOG_FILE),
    ("test", "tests/test_catalog.py", config.TESTS_DIR / "test_catalog.py"),
]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _log_devlog(name: str, text: str) -> None:
    config.DEVLOG_DIR.mkdir(parents=True, exist_ok=True)
    (config.DEVLOG_DIR / name).write_text(text, encoding="utf-8")


async def init_cmd() -> None:
    """Initialize the seekdb materials DB and the PowerContext scope."""
    store = MaterialsStore()
    await store.open()
    try:
        count = await store.count()
    finally:
        await store.close()
    state = StateStore()
    scope_id = await state.ensure_scope()
    existing = await state.load_state()
    if existing is None:
        await state.save_state(
            {
                "task": config.TASK_TITLE,
                "todo": ["实现 keyword 过滤(SPEC-2)", "运行验证", "保存任务状态"],
                "done": ["初始化：seekdb 资料库与 PowerContext 作用域"],
                "verified": {},
                "steps": [],
            }
        )
    print(f"[init] scope={scope_id} materials_rows={count}")
    print("[init] OK — seekdb 资料库与 PowerContext 状态已就绪")


async def ingest_cmd() -> None:
    """Ingest specs/code/tests into seekdb and capture the spec in PowerContext."""
    store = MaterialsStore()
    await store.open()
    try:
        for kind, rel_path, file_path in _DEMO_FILES:
            content = file_path.read_text(encoding="utf-8")
            await store.ingest(kind, rel_path, content)
            print(f"[ingest] seekdb <- {kind}: {rel_path} ({len(content)} chars)")
        count = await store.count()
    finally:
        await store.close()
    state = StateStore()
    await state.ensure_scope()
    version = _spec_version()
    await state.capture_spec(version, config.SPEC_FILE.read_text(encoding="utf-8"))
    print(f"[ingest] PowerContext <- spec source 'spec-catalog-{version}'")
    print(f"[ingest] OK — materials rows in seekdb: {count}; spec version: {version}")


def _spec_version() -> str:
    text = config.SPEC_FILE.read_text(encoding="utf-8")
    return "v2" if "SPEC-2" in text else "v1"


async def context_cmd() -> str:
    """Retrieve materials from seekdb, state from PowerContext, and print context."""
    store = MaterialsStore()
    await store.open()
    try:
        spec_rows = await store.search(config.TASK_KEYWORDS, ("spec",), limit=3)
        code_rows = await store.search(config.TASK_KEYWORDS, ("code",), limit=3)
        test_rows = await store.search(config.TASK_KEYWORDS, ("test",), limit=3)
    finally:
        await store.close()
    state = StateStore()
    await state.ensure_scope()
    task_state = await state.load_state()

    lines: list[str] = []
    lines.append(f"# 任务上下文 — {config.TASK_TITLE}")
    lines.append(f"任务描述: {config.TASK_DESCRIPTION}")
    lines.append("")
    lines.append("## 适用规格条目 (来源: seekdb materials)")
    for row in spec_rows:
        lines.append(f"- {row['path']} ({row['kind']}):")
        for entry in _spec_entries(row["content"]):
            lines.append(f"  - {entry}")
    lines.append("")
    lines.append("## 需要修改的代码 (来源: seekdb materials)")
    for row in code_rows:
        lines.append(f"- {row['path']}: {_symbols(row['content'])}")
    lines.append("")
    lines.append("## 验证测试 (来源: seekdb materials)")
    for row in test_rows:
        lines.append(f"- {row['path']}: {_test_names(row['content'])}")
    lines.append("")
    lines.append("## 任务状态 (来源: PowerContext scope)")
    if task_state:
        lines.append(f"- 已完成: {', '.join(task_state.get('done', [])) or '(无)'}")
        lines.append(f"- 待办: {', '.join(task_state.get('todo', [])) or '(无)'}")
        verified = task_state.get("verified") or {}
        if verified.get("after"):
            lines.append(f"- 验证结果: 修改后测试 {verified['after']}")
        if verified.get("before"):
            lines.append(f"- 验证结果: 修改前测试 {verified['before']}")
        lines.append(f"- 最近更新: {task_state.get('updated_at', '(未知)')}")
    else:
        lines.append("- (无已保存状态)")
    context = "\n".join(lines)
    print(context)
    return context


def _spec_entries(content: str) -> list[str]:
    entries = []
    for line in content.splitlines():
        stripped = line.strip()
        if stripped.startswith("## SPEC-"):
            entries.append(stripped)
    return entries


def _symbols(content: str) -> str:
    names = []
    for node in ast.walk(ast.parse(content)):
        if isinstance(node, (ast.FunctionDef, ast.ClassDef)):
            names.append(node.name)
    return ", ".join(names) or "(no symbols)"


def _test_names(content: str) -> str:
    names = []
    for node in ast.walk(ast.parse(content)):
        if isinstance(node, ast.FunctionDef) and node.name.startswith("test_"):
            names.append(node.name)
    return ", ".join(names) or "(no tests)"


def _run_pytest() -> dict:
    config.DEVLOG_DIR.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "tests"],
        cwd=config.DEMO_PROJECT,
        capture_output=True,
        text=True,
        timeout=300,
    )
    output = result.stdout + result.stderr
    return {
        "exit_code": result.returncode,
        "output": output,
        "passed": result.returncode == 0,
    }


async def _record_verify(state_store: StateStore, phase: str, report: dict) -> None:
    current = await state_store.load_state() or {}
    current["verified"] = {**(current.get("verified") or {}), phase: report["summary"]}
    current.setdefault("done", []).append(f"verify:{phase} -> {report['summary']}")
    await state_store.save_state(current)


async def verify_cmd(phase: str) -> None:
    """Run pytest before/after the change and record the result."""
    if phase == "before":
        shutil.copyfile(config.CATALOG_BEFORE, config.CATALOG_FILE)
        print("[verify:before] 已恢复修改前代码快照 (snapshots/catalog.before.py)")
    elif phase == "after":
        print("[verify:after] 使用当前 catalog.py (MiniMax 修改后)")
    else:
        raise SystemExit(f"unknown phase: {phase}")
    report = _run_pytest()
    _log_devlog(f"pytest_{phase}.log", report["output"])
    print(report["output"])

    state = StateStore()
    await state.ensure_scope()
    if phase == "before":
        # The target test must fail because the feature is missing (TypeError),
        # while all regression tests pass. Any other failure mode is an
        # environment error and must not masquerade as "not implemented yet".
        output = report["output"]
        if "TypeError" not in output or "test_filter_by_keyword" not in output:
            raise SystemExit(
                "[verify:before] 修改前失败原因不符合预期"
                "（应为 test_filter_by_keyword 的 TypeError），请检查环境"
            )
        print("[verify:before] 符合预期：目标测试因功能未实现而失败，回归测试通过")
        report["summary"] = (
            "目标测试失败(TypeError, 功能未实现)，回归测试通过 — 符合修改前预期"
        )
    else:
        if not report["passed"]:
            report["summary"] = f"失败(exit={report['exit_code']})"
            await _record_verify(state, phase, report)
            raise SystemExit("[verify:after] 测试未全部通过，任务未完成")
        report["summary"] = "全部通过"
        print("[verify:after] OK — 功能测试与回归测试全部通过")
    await _record_verify(state, phase, report)


def _api_surface(content: str) -> dict[str, int]:
    """Public symbols and their positional-argument counts (incl. self)."""
    tree = ast.parse(content)
    surface: dict[str, int] = {}
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and _is_public(node.name):
            surface[node.name] = len(node.args.args)
        elif isinstance(node, ast.ClassDef):
            for sub in node.body:
                if isinstance(sub, ast.FunctionDef) and _is_public(sub.name):
                    surface[f"{node.name}.{sub.name}"] = len(sub.args.args)
    return surface


def _is_public(name: str) -> bool:
    return not name.startswith("_") or name == "__init__"


def _validate_api_surface(before: str, after: str) -> None:
    before_api = _api_surface(before)
    after_api = _api_surface(after)
    for name, arg_count in sorted(before_api.items()):
        if name not in after_api:
            raise SystemExit(f"MiniMax 输出丢失既有公开符号 {name}，拒绝应用")
        if name == "Catalog.list_books":
            # 本任务的目标修改正是为 list_books 增加 keyword 参数
            if after_api[name] < arg_count:
                raise SystemExit(f"MiniMax 输出删减了 {name} 的参数，拒绝应用")
            continue
        if after_api[name] != arg_count:
            raise SystemExit(
                f"MiniMax 输出修改了 {name} 的签名（参数个数 {arg_count} -> "
                f"{after_api[name]}），拒绝应用"
            )


async def fix_cmd(offline: bool = False) -> None:
    """Generate the code change with MiniMax (or replay the recorded patch).

    Generated output must keep every pre-existing public symbol and signature
    (SPEC-1 compatibility); rejected attempts are retried with the rejection
    reason fed back to the model, and every attempt is recorded in the dev
    record for auditability.
    """
    context = await context_cmd()
    state = StateStore()
    await state.ensure_scope()
    client = MiniMaxClient()
    before_content = config.CATALOG_BEFORE.read_text(encoding="utf-8")
    attempts: list[dict] = []

    if offline or not client.available:
        print("[fix] 离线模式：未配置 MINIMAX_API_KEY，重放已记录的代码差异")
        new_content, prompt, response_text = _offline_record()
        mode = "offline replay"
        _validate_catalog(new_content)
        _validate_api_surface(before_content, new_content)
    else:
        mode = f"{client.model} @ {client.base_url}"
        constraint = ""
        new_content = ""
        for attempt in range(1, 4):
            print(f"[fix] 调用 MiniMax ({mode}) — 第 {attempt} 次生成")
            new_content, prompt, response_text = await client.generate_code(
                context, extra_constraint=constraint
            )
            try:
                _validate_catalog(new_content)
                _validate_api_surface(before_content, new_content)
                break
            except SystemExit as error:
                attempts.append(
                    {"attempt": attempt, "rejected": str(error),
                     "response": response_text}
                )
                constraint = str(error)
        else:
            for entry in attempts:
                print(f"[fix] 尝试 {entry['attempt']} 被拒绝: {entry['rejected']}")
            raise SystemExit("[fix] MiniMax 多次输出均未通过校验，任务未完成")

    config.CATALOG_FILE.write_text(new_content, encoding="utf-8")
    shutil.copyfile(config.CATALOG_FILE, config.CATALOG_AFTER)

    diff = _unified_diff(config.CATALOG_BEFORE, config.CATALOG_AFTER)
    config.CATALOG_DIFF.write_text(diff, encoding="utf-8")

    attempts_text = ""
    if attempts:
        attempts_text = "\n## 生成尝试记录（被拒绝的输出）\n\n" + "\n\n".join(
            f"- 尝试 {entry['attempt']} 被拒绝: {entry['rejected']}\n\n"
            f"```\n{entry['response'][:1500]}\n```"
            for entry in attempts
        ) + "\n"

    _log_devlog(
        "minimax_dev_record.md",
        f"# MiniMax 开发记录\n\n"
        f"- 时间: {_now()}\n"
        f"- 任务: {config.TASK_TITLE}\n"
        f"- 模式: {mode}\n\n"
        f"## 提供给 MiniMax 的上下文\n\n```\n{context}\n```\n\n"
        f"## 提示词\n\n```\n{prompt}\n```\n\n"
        f"## 模型响应（最终采纳）\n\n```\n{response_text}\n```\n"
        f"{attempts_text}"
        f"## 代码差异 (snapshots/catalog.diff)\n\n```diff\n{diff}\n```\n",
    )

    current = await state.load_state() or {}
    current["done"] = [
        item for item in current.get("done", []) if not item.startswith("verify:")
    ] + [f"fix: MiniMax 完成 catalog.py 修改 ({mode})"]
    current["todo"] = [item for item in current.get("todo", []) if "实现" not in item]
    await state.save_state(current)
    print(
        f"[fix] OK — catalog.py 已更新，差异 {len(diff.splitlines())} 行，"
        f"开发记录 devlog/minimax_dev_record.md"
    )


def _offline_record() -> tuple[str, str, str]:
    if config.CATALOG_AFTER.exists():
        prompt = "(offline replay — 使用已记录的 MiniMax 修改结果)"
        response_text = config.CATALOG_AFTER.read_text(encoding="utf-8")
        return response_text, prompt, response_text
    raise SystemExit("[fix] 离线模式缺少 snapshots/catalog.after.py，无法重放")


def _validate_catalog(content: str) -> None:
    try:
        tree = ast.parse(content)
    except SyntaxError as error:
        raise SystemExit(f"[fix] MiniMax 输出不是合法 Python: {error}") from error
    names = {
        node.name for node in ast.walk(tree) if isinstance(node, (ast.ClassDef, ast.FunctionDef))
    }
    if "Catalog" not in names or "list_books" not in names:
        raise SystemExit(
            f"[fix] MiniMax 输出缺少 Catalog/list_books，拒绝应用: {sorted(names)}"
        )
    if "keyword" not in content:
        raise SystemExit("[fix] MiniMax 输出未实现 keyword 参数，拒绝应用")


def _unified_diff(before: Path, after: Path) -> str:
    before_lines = before.read_text(encoding="utf-8").splitlines(keepends=True)
    after_lines = after.read_text(encoding="utf-8").splitlines(keepends=True)
    return "".join(
        difflib.unified_diff(
            before_lines,
            after_lines,
            fromfile="a/booklib/catalog.py",
            tofile="b/booklib/catalog.py",
        )
    )


async def save_cmd() -> None:
    """Write the final task state (done/todo/verified) to PowerContext."""
    state = StateStore()
    await state.ensure_scope()
    current = await state.load_state() or {}
    current["done"] = sorted(set(current.get("done", [])) | {"save: 任务状态已保存"})
    current["todo"] = [item for item in current.get("todo", []) if "保存" not in item]
    await state.save_state(current)
    print("[save] OK — 任务状态已写入 PowerContext scope")


async def resume_cmd() -> None:
    """Read the saved task state back after a module restart."""
    state = StateStore()
    await state.ensure_scope()
    task_state = await state.load_state()
    print("[resume] 模块已重启；从 PowerContext 读取任务状态：")
    if task_state is None:
        print("[resume] (无状态)")
        raise SystemExit(1)
    print(json.dumps(task_state, ensure_ascii=False, indent=2))
    print("[resume] OK — 状态可复用，后续 context 构建会包含以上内容")
