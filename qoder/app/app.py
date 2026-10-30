"""ReSearch 科研资料工作台 — 后端服务。

核心功能（均真实使用指定技术）：
- seekdb：资料的唯一存储与检索。资料入库到 materials 表（FULLTEXT 索引），
  检索走数据库内 MATCH..AGAINST（不支持时降级 LIKE），带 kind 结构化过滤；
- PowerContext：任务进展与学习状态持久化。每次保存追加一个版本化 Source
  （Source 不可变），读取时取最新版本，重启服务后状态可复用。

依赖：仅 pymysql（seekdb 走 MySQL 协议）；HTTP 服务与 PowerContext 调用用
标准库实现，便于评审复现。
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pymysql

APP_PORT = int(os.environ.get("APP_PORT", "8080"))
SEEKDB_HOST = os.environ.get("SEEKDB_HOST", "127.0.0.1")
SEEKDB_PORT = int(os.environ.get("SEEKDB_PORT", "2881"))
SEEKDB_USER = os.environ.get("SEEKDB_USER", "root")
SEEKDB_PASSWORD = os.environ.get("SEEKDB_PASSWORD", "")
SEEKDB_DB = os.environ.get("SEEKDB_DB", "research_workbench")
POWERCONTEXT_URL = os.environ.get(
    "POWERCONTEXT_SERVER_URL", "http://127.0.0.1:8600"
).rstrip("/")
SCOPE_TITLE = "research-workbench"
STATE_SOURCE_PREFIX = "workbench-state"

ROOT = Path(__file__).resolve().parent
STATIC_DIR = ROOT / "static"
ACCESS_LOG = ROOT / "state" / "access.log"

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
)
log = logging.getLogger("research-workbench")

_TASK_STATE_RE = re.compile(rf"{re.escape(STATE_SOURCE_PREFIX)}-(\d+)")

_sql_lock = threading.Lock()


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def audit(entry: dict) -> None:
    """Append one line of technology-usage evidence (seekdb SQL / PC writes)."""
    try:
        ACCESS_LOG.parent.mkdir(parents=True, exist_ok=True)
        with _sql_lock, ACCESS_LOG.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({**entry, "at": now()}, ensure_ascii=False) + "\n")
    except OSError:
        pass


def db_connect() -> pymysql.connections.Connection:
    return pymysql.connect(
        host=SEEKDB_HOST,
        port=SEEKDB_PORT,
        user=SEEKDB_USER,
        password=SEEKDB_PASSWORD,
        database=SEEKDB_DB,
        charset="utf8mb4",
        autocommit=True,
        connect_timeout=5,
    )


def init_database() -> None:
    """Create the seekdb database, table and FULLTEXT index (idempotent)."""
    conn = pymysql.connect(
        host=SEEKDB_HOST,
        port=SEEKDB_PORT,
        user=SEEKDB_USER,
        password=SEEKDB_PASSWORD,
        charset="utf8mb4",
        autocommit=True,
    )
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"CREATE DATABASE IF NOT EXISTS `{SEEKDB_DB}`"
                " DEFAULT CHARACTER SET utf8mb4"
            )
    finally:
        conn.close()
    conn = db_connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "CREATE TABLE IF NOT EXISTS materials ("
                " id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,"
                " kind VARCHAR(32) NOT NULL,"
                " title VARCHAR(256) NOT NULL,"
                " content LONGTEXT NOT NULL,"
                " created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP"
                ")"
            )
        audit({"op": "seekdb.ddl", "sql": "CREATE TABLE IF NOT EXISTS materials"})
    finally:
        conn.close()


def ensure_fulltext() -> bool:
    """Create the FULLTEXT index (IK 中文分词优先，降级默认解析器)。"""
    attempts = [
        ("CREATE FULLTEXT INDEX ft_materials_content"
         " ON materials(content) WITH PARSER ik"),
        ("CREATE FULLTEXT INDEX ft_materials_content ON materials(content)"),
    ]
    for sql in attempts:
        try:
            conn = db_connect()
            try:
                with conn.cursor() as cur:
                    cur.execute(sql)
                audit({"op": "seekdb.ddl", "sql": sql})
                return True
            finally:
                conn.close()
        except Exception as error:  # noqa: BLE001
            log.warning("fulltext index attempt failed (%s): %s", sql[:60], error)
    log.warning("fulltext index unavailable — 检索降级为 LIKE")
    return False


def _fulltext_available() -> bool:
    try:
        conn = db_connect()
        try:
            with conn.cursor() as cur:
                cur.execute("SHOW INDEX FROM materials")
                names = {row[2] for row in cur.fetchall()}
            return "ft_materials_content" in names
        finally:
            conn.close()
    except Exception:  # noqa: BLE001
        return False


def ingest(kind: str, title: str, content: str) -> dict:
    conn = db_connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO materials(kind, title, content) VALUES(%s, %s, %s)",
                (kind, title, content),
            )
            row_id = cur.lastrowid
        audit(
            {
                "op": "seekdb.insert",
                "sql": "INSERT INTO materials(kind,title,content) VALUES(?,?,?)",
                "params": [kind, title, f"<{len(content)} chars>"],
            }
        )
        return {"id": row_id, "kind": kind, "title": title, "chars": len(content)}
    finally:
        conn.close()


def search(query: str, kind: str | None = None, limit: int = 20) -> dict:
    if not query.strip():
        raise ValueError("query is empty")
    conn = db_connect()
    try:
        with conn.cursor() as cur:
            if _fulltext_available():
                sql = (
                    "SELECT id, kind, title, content FROM materials"
                    " WHERE MATCH(content) AGAINST(%s)"
                )
                params: list = [query]
                mode = "MATCH..AGAINST"
            else:
                terms = [t for t in re.split(r"\s+", query.strip()) if t]
                sql = (
                    "SELECT id, kind, title, content FROM materials WHERE "
                    + " OR ".join(["content LIKE %s"] * len(terms))
                )
                params = [f"%{t}%" for t in terms]
                mode = "LIKE"
            if kind:
                sql += " AND kind = %s"
                params.append(kind)
            sql += f" ORDER BY id DESC LIMIT {int(limit)}"
            cur.execute(sql, params)
            rows = cur.fetchall()
        audit(
            {
                "op": "seekdb.select",
                "sql": sql,
                "params": [query, kind, limit],
                "rows": len(rows),
            }
        )
    finally:
        conn.close()
    results = []
    for row_id, row_kind, title, content in rows:
        results.append(
            {
                "id": row_id,
                "kind": row_kind,
                "title": title,
                "snippet": _snippet(content, query),
            }
        )
    return {"mode": mode, "count": len(results), "results": results}


def _snippet(content: str, query: str, width: int = 160) -> str:
    terms = [t for t in re.split(r"\s+", query) if t]
    positions = [
        match.start()
        for term in terms
        for match in re.finditer(re.escape(term), content, re.IGNORECASE)
    ]
    if not positions:
        return content[:width]
    start = max(0, min(positions) - width // 3)
    snippet = content[start : start + width]
    prefix = "…" if start > 0 else ""
    suffix = "…" if start + width < len(content) else ""
    return f"{prefix}{snippet}{suffix}"


def list_materials(limit: int = 50) -> dict:
    conn = db_connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, kind, title, CHAR_LENGTH(content) AS size, created_at"
                f" FROM materials ORDER BY id DESC LIMIT {int(limit)}"
            )
            rows = cur.fetchall()
        audit({"op": "seekdb.select", "sql": "SELECT ... FROM materials", "rows": len(rows)})
    finally:
        conn.close()
    return {
        "count": len(rows),
        "items": [
            {
                "id": row[0],
                "kind": row[1],
                "title": row[2],
                "chars": row[3],
                "created_at": str(row[4]),
            }
            for row in rows
        ],
    }


# ---------- PowerContext ----------


def _pc_request(method: str, path: str, body: dict | None = None) -> dict:
    url = f"{POWERCONTEXT_URL}{path}"
    data = None if body is None else json.dumps(body).encode("utf-8")
    request = urllib.request.Request(
        url, data=data, method=method, headers={"content-type": "application/json"}
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            payload = response.read().decode("utf-8")
            return json.loads(payload) if payload else {}
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", "replace")
        raise RuntimeError(
            f"PowerContext HTTP {error.code} {method} {path}: {detail[:300]}"
        ) from error


def pc_ensure_scope() -> str:
    page = _pc_request("GET", "/v1/scopes")
    for item in page.get("items", []):
        if item.get("title") == SCOPE_TITLE:
            return item["scope_id"]
    descriptor = _pc_request(
        "POST",
        "/v1/scopes",
        {
            "title": SCOPE_TITLE,
            "summary": "ReSearch 科研资料工作台：任务进展与学习状态",
            "idempotency_key": f"{SCOPE_TITLE}:init",
        },
    )
    return descriptor["scope_id"]


def pc_state_ids(scope_id: str) -> list[tuple[int, str]]:
    page = _pc_request("GET", f"/v1/scopes/{scope_id}/sources")
    ids = []
    for item in page.get("items", []):
        match = _TASK_STATE_RE.fullmatch(item.get("source_id", ""))
        if match:
            ids.append((int(match.group(1)), item["source_id"]))
    return ids


def pc_save_state(state: dict) -> str:
    scope_id = pc_ensure_scope()
    ids = pc_state_ids(scope_id)
    seq = max((seq for seq, _ in ids), default=0) + 1
    source_id = f"{STATE_SOURCE_PREFIX}-{seq:04d}"
    state = {**state, "updated_at": now()}
    _pc_request(
        "POST",
        "/v1/sources/content",
        {
            "scope_id": scope_id,
            "source_id": source_id,
            "content": json.dumps(state, ensure_ascii=False, indent=2),
            "metadata": {"module": "research-workbench", "kind": "task-state"},
        },
    )
    audit({"op": "powercontext.write", "source_id": source_id, "scope": scope_id})
    return source_id


def pc_load_state() -> dict | None:
    try:
        scope_id = pc_ensure_scope()
        ids = pc_state_ids(scope_id)
    except Exception as error:  # noqa: BLE001
        log.warning("PowerContext 不可用: %s", error)
        return None
    if not ids:
        return None
    _, latest_id = max(ids, key=lambda pair: pair[0])
    try:
        record = _pc_request(
            "GET", f"/v1/scopes/{scope_id}/sources/content/{latest_id}"
        )
        content = record.get("content")
        if isinstance(content, str):
            return json.loads(content)
        return content
    except Exception as error:  # noqa: BLE001
        log.warning("读取 PowerContext 状态失败: %s", error)
        return None


# ---------- HTTP ----------

class Handler(BaseHTTPRequestHandler):
    server_version = "ResearchWorkbench/1.0"

    def _send(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("content-type", "application/json; charset=utf-8")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _error(self, status: int, message: str) -> None:
        self._send(status, {"error": message})

    def _body(self) -> dict:
        length = int(self.headers.get("content-length", "0"))
        raw = self.rfile.read(length)
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError("request body must be UTF-8") from exc
        return json.loads(text) if text else {}

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        query = urllib.parse.parse_qs(self.path.split("?", 1)[1] if "?" in self.path else "")
        if path == "/" or path.startswith("/index.html"):
            page = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
            body = page.encode("utf-8")
            self.send_response(200)
            self.send_header("content-type", "text/html; charset=utf-8")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if path.startswith("/static/"):
            name = os.path.basename(path)
            target = STATIC_DIR / name
            if not target.is_file():
                return self._error(404, "not found")
            body = target.read_bytes()
            content_type = "text/javascript" if name.endswith(".js") else "text/css"
            self.send_response(200)
            self.send_header("content-type", f"{content_type}; charset=utf-8")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if path == "/api/search":
            try:
                result = search(
                    query.get("q", [""])[0], query.get("kind", [None])[0]
                )
            except ValueError:
                return self._error(400, "query is empty")
            return self._send(200, result)
        if path == "/api/materials":
            return self._send(200, list_materials())
        if path == "/api/state":
            state = pc_load_state()
            return self._send(200, {"state": state, "ok": state is not None})
        if path == "/api/health":
            return self._send(200, {"ok": True})
        return self._error(404, "not found")

    def do_POST(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        try:
            body = self._body()
        except (ValueError, json.JSONDecodeError) as exc:
            return self._error(400, f"invalid request body: {exc}")
        if path == "/api/ingest":
            kind = str(body.get("kind", "note"))[:32]
            title = str(body.get("title", ""))[:256]
            content = str(body.get("content", ""))
            if not title or not content:
                return self._error(400, "title and content are required")
            row = ingest(kind, title, content)
            ensure_fulltext()
            return self._send(200, {"ok": True, "row": row})
        if path == "/api/state":
            allowed = {"todo", "notes", "progress"}
            state = {key: value for key, value in body.items() if key in allowed}
            if not state:
                return self._error(400, "nothing to save")
            source_id = pc_save_state(state)
            return self._send(200, {"ok": True, "source_id": source_id})
        return self._error(404, "not found")

    def log_message(self, fmt: str, *args) -> None:  # noqa: A003
        log.info("%s %s", self.address_string(), fmt % args)


def main() -> None:
    init_database()
    ensure_fulltext()
    server = ThreadingHTTPServer(("0.0.0.0", APP_PORT), Handler)
    log.info(
        "ReSearch 工作台已启动: http://127.0.0.1:%s "
        "(seekdb=%s:%s/%s, PowerContext=%s)",
        APP_PORT, SEEKDB_HOST, SEEKDB_PORT, SEEKDB_DB, POWERCONTEXT_URL,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    sys.exit(main())
