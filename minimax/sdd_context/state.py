"""PowerContext task-state store.

Task progress, todos and verification results are saved as durable Sources in
a PowerContext Scope.  PowerContext Sources are append-only journals (capturing
the same identity with different content raises ``source_conflict``), so every
save writes a new versioned Source ``sdd-task-state-000N`` and reads take the
latest one.  After a module restart the same records are read back and fed
into context building, which is the state-reuse acceptance item.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone

from powercontext.client.client import PowerContextClient
from powercontext.client.errors import ServerResponseError
from powercontext.http._generated.models import (
    CaptureContentSourceRequest,
    CreateScopeRequest,
)

from . import config


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class StateStore:
    """Client-side wrapper around one PowerContext scope.

    The PowerContextClient is kept open for the whole process lifetime:
    entering the client's async context manager closes the underlying httpx
    client permanently, so methods must not use per-call ``async with``.
    """

    def __init__(self, url: str = config.POWERCONTEXT_URL) -> None:
        self._client = PowerContextClient(url)
        self.scope_id: str | None = None

    async def ensure_scope(self) -> str:
        page = await self._client.list_scopes()
        for descriptor in page.items:
            if descriptor.title == config.SCOPE_TITLE:
                self.scope_id = descriptor.scope_id
                return self.scope_id
        descriptor = await self._client.create_scope(
            CreateScopeRequest(
                title=config.SCOPE_TITLE,
                summary="MiniMax SDD 上下文模块演示：booklib 关键词过滤任务",
                idempotency_key=f"{config.SCOPE_TITLE}:init",
            )
        )
        self.scope_id = descriptor.scope_id
        return self.scope_id

    async def _state_source_ids(self) -> list[tuple[int, str]]:
        """Existing task-state sources as (sequence, source_id) pairs."""
        assert self.scope_id is not None
        page = await self._client.list_sources(self.scope_id)
        prefix = config.STATE_SOURCE_ID
        ids: list[tuple[int, str]] = []
        for item in page.items:
            source_id = item.source_id
            if source_id == prefix:
                ids.append((0, source_id))
                continue
            match = re.fullmatch(rf"{re.escape(prefix)}-(\d+)", source_id)
            if match:
                ids.append((int(match.group(1)), source_id))
        return ids

    async def save_state(self, state: dict) -> str:
        """Append the current task state as a new versioned Source."""
        assert self.scope_id is not None
        state = {**state, "updated_at": _now()}
        ids = await self._state_source_ids()
        seq = max((seq for seq, _ in ids), default=0) + 1
        source_id = f"{config.STATE_SOURCE_ID}-{seq:04d}"
        await self._client.capture_content_source(
            CaptureContentSourceRequest(
                scope_id=self.scope_id,
                source_id=source_id,
                content=json.dumps(state, ensure_ascii=False, indent=2),
                metadata={
                    "module": "minimax-sdd",
                    "kind": "task-state",
                    "sequence": seq,
                },
            )
        )
        return source_id

    async def capture_spec(self, version: str, spec_text: str) -> str:
        """Capture one spec version; duplicate captures get a new suffix."""
        assert self.scope_id is not None
        base = f"{config.SPEC_SOURCE_PREFIX}-{version}"
        source_id = base
        for attempt in range(1, 100):
            try:
                await self._client.capture_content_source(
                    CaptureContentSourceRequest(
                        scope_id=self.scope_id,
                        source_id=source_id,
                        content=spec_text,
                        metadata={
                            "module": "minimax-sdd",
                            "kind": "spec",
                            "version": version,
                        },
                    )
                )
                return source_id
            except ServerResponseError as error:
                if error.code != "source_conflict":
                    raise
                source_id = f"{base}-{attempt}"
        raise RuntimeError(f"cannot capture spec source after 99 attempts: {base}")

    async def load_state(self) -> dict | None:
        """Read the latest task state back from PowerContext (restart reuse)."""
        assert self.scope_id is not None
        try:
            ids = await self._state_source_ids()
        except Exception:  # noqa: BLE001
            return None
        if not ids:
            return None
        _, latest_id = max(ids, key=lambda pair: pair[0])
        try:
            record = await self._client.get_source(
                self.scope_id, "content", latest_id
            )
        except Exception:  # noqa: BLE001
            return None
        if record is None or record.content is None:
            return None
        if isinstance(record.content, str):
            return json.loads(record.content)
        return record.content

    async def list_source_ids(self) -> list[str]:
        assert self.scope_id is not None
        page = await self._client.list_sources(self.scope_id)
        return [item.source_id for item in page.items]
