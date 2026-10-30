"""seekdb materials store.

Project materials (specs, code, tests) are ingested into an embedded seekdb
instance and retrieved by SQL when building task context.  The engine plumbing
reuses PowerContext's own embedded-seekdb profile so no extra database
dependency is introduced.
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy import text

from powercontext.builtin.persistence.seekdb.profile import SeekDBConfig, SeekDBProfile

from . import config

_KINDS = ("spec", "code", "test")


def _build_tables() -> tuple[sa.Table, ...]:
    metadata = sa.MetaData()
    materials = sa.Table(
        "materials",
        metadata,
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("path", sa.String(512), nullable=False),
        sa.Column("content", sa.Text, nullable=False),
        sa.UniqueConstraint("kind", "path", name="uk_materials_kind_path"),
    )
    return (materials,)


def _kinds_clause(kinds: tuple[str, ...], prefix: str) -> tuple[str, dict]:
    placeholders = ", ".join(f":{prefix}k{i}" for i in range(len(kinds)))
    params = {f"{prefix}k{i}": kind for i, kind in enumerate(kinds)}
    return f"kind IN ({placeholders})", params


class MaterialsStore:
    """Async facade over the embedded seekdb materials database."""

    def __init__(self) -> None:
        self._profile: SeekDBProfile | None = None
        self._fulltext_ok = False

    async def open(self) -> None:
        config.STATE_DIR.mkdir(parents=True, exist_ok=True)
        profile_context = SeekDBProfile.open(
            SeekDBConfig(path=config.MATERIALS_DB, echo=False),
            tables=_build_tables(),
        )
        self._profile = await profile_context.__aenter__()
        self._context = profile_context
        async with self._profile.database.transaction() as connection:
            await self._create_fulltext_index(connection)

    async def _create_fulltext_index(self, connection) -> None:
        # Full-text index makes MATCH..AGAINST retrieval possible; if this
        # seekdb build rejects it we degrade gracefully to LIKE queries.
        try:
            await connection.execute(
                text(
                    "CREATE FULLTEXT INDEX ft_materials_content "
                    "ON materials (content)"
                )
            )
            self._fulltext_ok = True
        except Exception as error:  # noqa: BLE001
            self._fulltext_ok = False
            print(f"[seekdb] fulltext index unavailable ({error}); using LIKE fallback")

    async def close(self) -> None:
        if self._profile is not None:
            await self._profile.database.close()
            self._profile = None

    async def ingest(self, kind: str, path: str, content: str) -> None:
        if kind not in _KINDS:
            raise ValueError(f"unknown kind: {kind}")
        assert self._profile is not None
        async with self._profile.database.transaction() as connection:
            await connection.execute(
                text(
                    "INSERT INTO materials (kind, path, content) "
                    "VALUES (:kind, :path, :content) "
                    "ON DUPLICATE KEY UPDATE content = VALUES(content)"
                ),
                {"kind": kind, "path": path, "content": content},
            )

    async def count(self) -> int:
        assert self._profile is not None
        async with self._profile.database.transaction() as connection:
            result = await connection.execute(text("SELECT COUNT(*) FROM materials"))
            return int(result.scalar_one())

    async def search(self, terms: list[str], kinds: tuple[str, ...], limit: int) -> list[dict]:
        """Retrieve materials matching the terms; returns rows with sources."""
        assert self._profile is not None
        rows: list[dict] = []
        if self._fulltext_ok:
            query = " ".join(terms)
            kinds_sql, kinds_params = _kinds_clause(kinds, "fk")
            sql = (
                "SELECT kind, path, content FROM materials "
                f"WHERE {kinds_sql} AND MATCH (content) AGAINST (:q) LIMIT {int(limit)}"
            )
            try:
                async with self._profile.database.transaction() as connection:
                    result = await connection.execute(
                        text(sql), {**kinds_params, "q": query}
                    )
                    rows = [dict(row._mapping) for row in result]
            except Exception as error:  # noqa: BLE001
                print(f"[seekdb] MATCH query failed ({error}); falling back to LIKE")
        if not rows:
            rows = await self._search_like(terms, kinds, limit)
        return rows

    async def _search_like(self, terms: list[str], kinds: tuple[str, ...], limit: int) -> list[dict]:
        assert self._profile is not None
        kinds_sql, kinds_params = _kinds_clause(kinds, "lk")
        conditions = []
        params = dict(kinds_params)
        for i, term in enumerate(terms):
            key = f"lt{i}"
            conditions.append(f"content LIKE :{key}")
            params[key] = f"%{term}%"
        sql = (
            "SELECT kind, path, content FROM materials "
            f"WHERE {kinds_sql} AND ({' OR '.join(conditions)}) LIMIT {int(limit)}"
        )
        async with self._profile.database.transaction() as connection:
            result = await connection.execute(text(sql), params)
            return [dict(row._mapping) for row in result]
