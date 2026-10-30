"""导入 demo_data 下的初始资料到 seekdb（通过工作台 /api/ingest）。

用法（工作台已启动后）:
    uv run --with pymysql python scripts/seed.py
或
    python scripts/seed.py
"""

import json
import sys
import urllib.request
from pathlib import Path

APP_URL = "http://127.0.0.1:8080"
DATA_DIR = Path(__file__).resolve().parent.parent / "demo_data"

KIND_BY_PREFIX = {
    "数据库系统复习笔记": "note",
    "论文笔记": "paper",
    "seekdb部署": "doc",
}


def main() -> int:
    count = 0
    for path in sorted(DATA_DIR.glob("*.md")):
        kind = next(
            (value for prefix, value in KIND_BY_PREFIX.items() if path.stem.startswith(prefix)),
            "note",
        )
        body = json.dumps(
            {"title": path.stem, "kind": kind, "content": path.read_text(encoding="utf-8")}
        ).encode("utf-8")
        request = urllib.request.Request(
            f"{APP_URL}/api/ingest",
            data=body,
            method="POST",
            headers={"content-type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=15) as response:
            payload = json.loads(response.read().decode("utf-8"))
        print(f"[seed] {path.name} -> id={payload['row']['id']} ({payload['row']['chars']} chars)")
        count += 1
    print(f"[seed] OK — 导入 {count} 份资料")
    return 0


if __name__ == "__main__":
    sys.exit(main())
