# Copyright (c) 2026 OceanBase.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Evidence-dependency graph persistence for Source-driven availability."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, TypeAdapter
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from powercontext.artifacts import ArtifactRef, MemoryCitation
from powercontext.builtin.persistence.codec import dump_model, load_model, stored_bytes
from powercontext.builtin.persistence.errors import InvalidRepositoryArgumentError
from powercontext.builtin.persistence.tables import (
    SOURCE_LIFECYCLE_EVENTS_TABLE,
    SOURCE_LIFECYCLE_NODES_TABLE,
)
from powercontext.limits import MAX_SCOPE_ID_LENGTH, MAX_SOURCE_ID_LENGTH
from powercontext.sources import SourceRef

LifecycleKind: TypeAlias = Literal["source", "memory", "topic"]
LifecyclePolicy: TypeAlias = Literal["all", "any"]
LifecycleReference: TypeAlias = SourceRef | MemoryCitation | ArtifactRef

_REFERENCE_MODELS: Mapping[LifecycleKind, type[BaseModel]] = {
    "source": SourceRef,
    "memory": MemoryCitation,
    "topic": ArtifactRef,
}
_PARENTS_ADAPTER = TypeAdapter(tuple[str, ...])


class LifecycleNode(BaseModel):
    """One node of a scope's evidence dependency graph."""

    model_config = ConfigDict(frozen=True)

    scope_id: str
    node_id: str
    kind: LifecycleKind
    policy: LifecyclePolicy = "all"
    parents: tuple[str, ...] = ()
    reference: LifecycleReference
    revision: int = 0
    sequence: int = 0
    available: bool = True

    def supports(self, parent_available: Mapping[str, bool]) -> bool:
        """Return whether this node's whole body still has enough support."""

        support = [bool(parent_available.get(parent, False)) for parent in self.parents]
        return all(support) if self.policy == "all" else any(support)


class LifecycleEventRecord(BaseModel):
    """One accepted Source availability event, kept for replay detection."""

    model_config = ConfigDict(frozen=True)

    scope_id: str
    node_id: str
    sequence: int
    revision: int
    available: bool
    reason: str

    def matches(self, *, revision: int, available: bool, reason: str) -> bool:
        return self.revision == revision and self.available == available and self.reason == reason


class SourceLifecycleRepository:
    """Persist dependency nodes and the Source events that move them."""

    async def nodes(self, connection: AsyncConnection, scope_id: str, /) -> tuple[LifecycleNode, ...]:
        """Return every node registered in one scope, ordered by node identifier."""

        _require_identifier("scope_id", scope_id, MAX_SCOPE_ID_LENGTH)
        rows = (
            await connection.execute(
                select(SOURCE_LIFECYCLE_NODES_TABLE)
                .where(SOURCE_LIFECYCLE_NODES_TABLE.c.scope_id == scope_id)
                .order_by(SOURCE_LIFECYCLE_NODES_TABLE.c.node_id)
            )
        ).mappings().all()
        return tuple(_decode_node(scope_id, row) for row in rows)

    async def insert_node(self, connection: AsyncConnection, node: LifecycleNode, /) -> bool:
        """Insert one node, reporting whether this call created it."""

        _require_identifier("scope_id", node.scope_id, MAX_SCOPE_ID_LENGTH)
        _require_identifier("node_id", node.node_id, MAX_SOURCE_ID_LENGTH)
        existing = await self._select_node(connection, node.scope_id, node.node_id)
        if existing is not None:
            return False
        await connection.execute(
            SOURCE_LIFECYCLE_NODES_TABLE.insert().values(
                scope_id=node.scope_id,
                node_id=node.node_id,
                kind=node.kind,
                policy=node.policy,
                parents=_PARENTS_ADAPTER.dump_json(node.parents),
                reference=dump_model(node.reference, kind="source-lifecycle-node", name=node.node_id),
                revision=node.revision,
                sequence=node.sequence,
                available=node.available,
            )
        )
        return True

    async def update_availability(
        self,
        connection: AsyncConnection,
        scope_id: str,
        states: Mapping[str, bool],
        /,
    ) -> None:
        """Persist the propagated availability of the nodes that changed."""

        _require_identifier("scope_id", scope_id, MAX_SCOPE_ID_LENGTH)
        for node_id, available in states.items():
            _require_identifier("node_id", node_id, MAX_SOURCE_ID_LENGTH)
            await connection.execute(
                update(SOURCE_LIFECYCLE_NODES_TABLE)
                .where(
                    SOURCE_LIFECYCLE_NODES_TABLE.c.scope_id == scope_id,
                    SOURCE_LIFECYCLE_NODES_TABLE.c.node_id == node_id,
                )
                .values(available=available)
            )

    async def update_source_progress(
        self,
        connection: AsyncConnection,
        scope_id: str,
        node_id: str,
        /,
        *,
        available: bool,
        sequence: int,
    ) -> None:
        """Record one Source's accepted event sequence and resulting availability."""

        _require_identifier("scope_id", scope_id, MAX_SCOPE_ID_LENGTH)
        _require_identifier("node_id", node_id, MAX_SOURCE_ID_LENGTH)
        result = await connection.execute(
            update(SOURCE_LIFECYCLE_NODES_TABLE)
            .where(
                SOURCE_LIFECYCLE_NODES_TABLE.c.scope_id == scope_id,
                SOURCE_LIFECYCLE_NODES_TABLE.c.node_id == node_id,
            )
            .values(available=available, sequence=sequence)
        )
        if result.rowcount != 1:
            raise InvalidRepositoryArgumentError("node_id", "is not a registered lifecycle node")

    async def latest_event(
        self,
        connection: AsyncConnection,
        scope_id: str,
        node_id: str,
        /,
    ) -> LifecycleEventRecord | None:
        """Return the highest-sequence event accepted for one Source."""

        _require_identifier("scope_id", scope_id, MAX_SCOPE_ID_LENGTH)
        _require_identifier("node_id", node_id, MAX_SOURCE_ID_LENGTH)
        row = (
            await connection.execute(
                select(SOURCE_LIFECYCLE_EVENTS_TABLE)
                .where(
                    SOURCE_LIFECYCLE_EVENTS_TABLE.c.scope_id == scope_id,
                    SOURCE_LIFECYCLE_EVENTS_TABLE.c.node_id == node_id,
                )
                .order_by(SOURCE_LIFECYCLE_EVENTS_TABLE.c.sequence.desc())
                .limit(1)
            )
        ).mappings().one_or_none()
        if row is None:
            return None
        return LifecycleEventRecord(
            scope_id=scope_id,
            node_id=node_id,
            sequence=int(row["sequence"]),
            revision=int(row["revision"]),
            available=bool(row["available"]),
            reason=str(row["reason"]),
        )

    async def event_at(
        self,
        connection: AsyncConnection,
        scope_id: str,
        node_id: str,
        sequence: int,
        /,
    ) -> LifecycleEventRecord | None:
        """Return the event accepted for one Source at exactly one sequence."""

        _require_identifier("scope_id", scope_id, MAX_SCOPE_ID_LENGTH)
        _require_identifier("node_id", node_id, MAX_SOURCE_ID_LENGTH)
        row = (
            await connection.execute(
                select(SOURCE_LIFECYCLE_EVENTS_TABLE).where(
                    SOURCE_LIFECYCLE_EVENTS_TABLE.c.scope_id == scope_id,
                    SOURCE_LIFECYCLE_EVENTS_TABLE.c.node_id == node_id,
                    SOURCE_LIFECYCLE_EVENTS_TABLE.c.sequence == sequence,
                )
            )
        ).mappings().one_or_none()
        if row is None:
            return None
        return LifecycleEventRecord(
            scope_id=scope_id,
            node_id=node_id,
            sequence=int(row["sequence"]),
            revision=int(row["revision"]),
            available=bool(row["available"]),
            reason=str(row["reason"]),
        )

    async def insert_event(self, connection: AsyncConnection, record: LifecycleEventRecord, /) -> bool:
        """Persist one accepted event, reporting whether this call created it."""

        existing = (
            await connection.execute(
                select(SOURCE_LIFECYCLE_EVENTS_TABLE.c.sequence).where(
                    SOURCE_LIFECYCLE_EVENTS_TABLE.c.scope_id == record.scope_id,
                    SOURCE_LIFECYCLE_EVENTS_TABLE.c.node_id == record.node_id,
                    SOURCE_LIFECYCLE_EVENTS_TABLE.c.sequence == record.sequence,
                )
            )
        ).one_or_none()
        if existing is not None:
            return False
        await connection.execute(
            SOURCE_LIFECYCLE_EVENTS_TABLE.insert().values(
                scope_id=record.scope_id,
                node_id=record.node_id,
                sequence=record.sequence,
                revision=record.revision,
                available=record.available,
                reason=record.reason,
            )
        )
        return True

    async def unavailable_artifact_ids(
        self,
        connection: AsyncConnection,
        scope_id: str,
        kinds: Sequence[LifecycleKind] = ("topic",),
        /,
    ) -> frozenset[str]:
        """Return identifiers of registered nodes that lost their support.

        Retrieval reads this to keep withdrawn content out of native search and
        Context while exact revision reads stay available for audit.
        """

        _require_identifier("scope_id", scope_id, MAX_SCOPE_ID_LENGTH)
        selected = frozenset(kinds)
        if not selected:
            return frozenset()
        rows = (
            await connection.execute(
                select(SOURCE_LIFECYCLE_NODES_TABLE.c.kind, SOURCE_LIFECYCLE_NODES_TABLE.c.reference).where(
                    SOURCE_LIFECYCLE_NODES_TABLE.c.scope_id == scope_id,
                    SOURCE_LIFECYCLE_NODES_TABLE.c.available.is_(False),
                    SOURCE_LIFECYCLE_NODES_TABLE.c.kind.in_(tuple(sorted(selected))),
                )
            )
        ).all()
        identifiers: set[str] = set()
        for kind, payload in rows:
            node = _decode_reference(str(kind), payload, name="<retrieval-filter>")
            identifiers.add(getattr(node, "artifact_id", None) or getattr(node, "source_id", ""))
        return frozenset(identifier for identifier in identifiers if identifier)

    async def _select_node(
        self,
        connection: AsyncConnection,
        scope_id: str,
        node_id: str,
        /,
    ) -> LifecycleNode | None:
        row = (
            await connection.execute(
                select(SOURCE_LIFECYCLE_NODES_TABLE).where(
                    SOURCE_LIFECYCLE_NODES_TABLE.c.scope_id == scope_id,
                    SOURCE_LIFECYCLE_NODES_TABLE.c.node_id == node_id,
                )
            )
        ).mappings().one_or_none()
        return None if row is None else _decode_node(scope_id, row)


def _decode_node(scope_id: str, row: Mapping[str, object]) -> LifecycleNode:
    node_id = str(row["node_id"])
    kind = str(row["kind"])
    return LifecycleNode(
        scope_id=scope_id,
        node_id=node_id,
        kind=kind,  # type: ignore[arg-type]
        policy=str(row["policy"]),  # type: ignore[arg-type]
        parents=_PARENTS_ADAPTER.validate_json(stored_bytes(row["parents"], column="parents")),
        reference=_decode_reference(kind, row["reference"], name=node_id),
        revision=int(row["revision"]),  # type: ignore[arg-type]
        sequence=int(row["sequence"]),  # type: ignore[arg-type]
        available=bool(row["available"]),
    )


def _decode_reference(kind: str, payload: object, /, *, name: str) -> LifecycleReference:
    model = _REFERENCE_MODELS.get(kind)  # type: ignore[arg-type]
    if model is None:
        raise InvalidRepositoryArgumentError("kind", f"{kind!r} is not a lifecycle node kind")
    return load_model(model, payload, kind="source-lifecycle-node", name=name)  # type: ignore[return-value]


def _require_identifier(column: str, value: str, maximum: int) -> None:
    if not isinstance(value, str) or not value or value != value.strip() or len(value) > maximum:
        raise InvalidRepositoryArgumentError(column, "must be a non-empty trimmed identifier")
