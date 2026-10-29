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

"""Source-driven availability for Memory and Topic Memory.

A Source that stops being used withdraws the evidence that supported it. This
module keeps one dependency graph per Scope and propagates a Source's
availability to every Memory entry and Topic Memory that depends on it:
unavailable content leaves native list, search and Context, its body stays
exactly readable for audit, and a restored Source makes the content available
again.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, field_validator

from powercontext.artifacts import ArtifactRef, MemoryCitation
from powercontext.builtin.artifacts.memory import InvalidMemoryCitationError, MemoryEntryNotFoundError
from powercontext.builtin.persistence.database import AsyncDatabase
from powercontext.builtin.persistence.source_lifecycle import (
    LifecycleEventRecord,
    LifecycleNode,
    SourceLifecycleRepository,
)
from powercontext.builtin.records import BaseValueNotFoundError
from powercontext.builtin.runtime.models import GetMemoryEntryRequest
from powercontext.builtin.sources import validate_scope_id
from powercontext.errors import ArtifactNotFoundError, RevisionConflictError
from powercontext.limits import MAX_SOURCE_ID_LENGTH
from powercontext.sources import SourceRef

if TYPE_CHECKING:
    from powercontext.builtin.runtime.application import BuiltinRuntime


LifecyclePurpose: TypeAlias = Literal["generation", "audit"]

LIFECYCLE_REASON = "source lifecycle"
TOPIC_MEMORY_FAMILY = "topic-memory"
_LIFECYCLE_POLICIES = ("all", "any")


class SourceLifecycleError(ValueError):
    """Report a lifecycle registration, event, or resolution that cannot be accepted."""


class SourceLifecycleEvent(BaseModel):
    """One Source availability event with a strict, validated shape."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    node_id: str
    reason: str
    revision: int = Field(gt=0)
    sequence: int = Field(gt=0)
    available: bool

    @field_validator("node_id", "reason")
    @classmethod
    def require_text(cls, value: str) -> str:
        if not value or not value.strip():
            raise ValueError("must not be empty or whitespace")  # noqa: TRY003
        return value


@dataclass(frozen=True, slots=True)
class _LifecyclePlan:
    """The availability an accepted event produces, before it is persisted."""

    nodes: Mapping[str, LifecycleNode]
    states: Mapping[str, bool]
    replayed: bool


class ScopedSourceLifecycleService:
    """Register evidence nodes and propagate Source availability in one Scope."""

    def __init__(self, runtime: BuiltinRuntime, scope_id: str, database: AsyncDatabase | None) -> None:
        self._runtime = runtime
        self.scope_id = validate_scope_id(scope_id)
        self._db = database
        self._repository = SourceLifecycleRepository()

    async def register_source(self, node_id: str, source_ref: SourceRef, /, *, revision: int) -> None:
        """Register one Source as the root of the Scope's evidence graph."""

        node = _require_node_id(node_id)
        if revision < 1:
            raise SourceLifecycleError("revision must be a positive integer")
        await self._require_readable_source(source_ref)
        async with self._runtime._scope_operation(self.scope_id), self._runtime._locked(self.scope_id):
            async with self._database().transaction() as connection:
                nodes = await self._nodes(connection)
                existing = nodes.get(node)
                if existing is not None:
                    _require_same_binding(existing, "source", source_ref, (), "all", revision)
                    return
                await self._repository.insert_node(
                    connection,
                    LifecycleNode(
                        scope_id=self.scope_id,
                        node_id=node,
                        kind="source",
                        policy="all",
                        parents=(),
                        reference=source_ref,
                        revision=revision,
                        sequence=0,
                        available=True,
                    ),
                )

    async def register_memory(
        self,
        node_id: str,
        citation: MemoryCitation,
        /,
        *,
        parents: tuple[str, ...],
        policy: str = "all",
    ) -> None:
        """Register one Memory entry with the evidence that supports it."""

        node = _require_node_id(node_id)
        parent_ids = _require_parents(parents, node)
        normalized = _require_policy(policy)
        await self._require_readable_memory(citation)
        await self._register_derived(node, "memory", citation, parent_ids, normalized)

    async def register_topic(
        self,
        node_id: str,
        artifact_ref: ArtifactRef,
        /,
        *,
        parents: tuple[str, ...],
        policy: str = "all",
    ) -> None:
        """Register one Topic Memory with the evidence that supports it."""

        node = _require_node_id(node_id)
        parent_ids = _require_parents(parents, node)
        normalized = _require_policy(policy)
        await self._require_readable_topic(artifact_ref)
        await self._register_derived(node, "topic", artifact_ref, parent_ids, normalized)

    async def apply(self, event: SourceLifecycleEvent, /) -> list[str]:
        """Apply one Source event and return every available node, ordered by identifier."""

        async with self._runtime._scope_operation(self.scope_id), self._runtime._locked(self.scope_id):
            plan = await self._plan(event)
            if plan.replayed:
                return _available_ids(plan.nodes)
            await self._propagate_native(plan.nodes, plan.states)
            await self._commit(event, plan)
            return sorted(node_id for node_id, available in plan.states.items() if available)

    async def resolve(
        self,
        node_id: str,
        /,
        *,
        purpose: str,
    ) -> SourceRef | MemoryCitation | ArtifactRef:
        """Return one node's original reference, honoring the requested purpose."""

        if purpose not in ("generation", "audit"):
            raise SourceLifecycleError("purpose is not a known lifecycle purpose")
        node = _require_node_id(node_id)
        async with self._database().transaction() as connection:
            nodes = await self._nodes(connection)
        target = nodes.get(node)
        if target is None:
            raise SourceLifecycleError("node is not registered in this Scope")
        if purpose == "generation" and not target.available:
            raise SourceLifecycleError("node lost its support and cannot be used for generation")
        return target.reference

    async def unavailable_topic_ids(self) -> frozenset[str]:
        """Return the Topic Memory identifiers that lost their support."""

        async with self._database().transaction() as connection:
            return await self._repository.unavailable_artifact_ids(connection, self.scope_id, ("topic",))

    async def _register_derived(
        self,
        node: str,
        kind: str,
        reference: SourceRef | MemoryCitation | ArtifactRef,
        parents: tuple[str, ...],
        policy: str,
    ) -> None:
        async with self._runtime._scope_operation(self.scope_id), self._runtime._locked(self.scope_id):
            entry_id: str | None = None
            async with self._database().transaction() as connection:
                nodes = await self._nodes(connection)
                existing = nodes.get(node)
                if existing is not None:
                    _require_same_binding(existing, kind, reference, parents, policy, existing.revision)
                else:
                    for parent in parents:
                        if parent not in nodes:
                            raise SourceLifecycleError("a parent node is not registered yet")
                    available = _derive_availability(_current_states(nodes), parents, policy)
                    await self._repository.insert_node(
                        connection,
                        LifecycleNode(
                            scope_id=self.scope_id,
                            node_id=node,
                            kind=kind,  # type: ignore[arg-type]
                            policy=policy,  # type: ignore[arg-type]
                            parents=parents,
                            reference=reference,
                            revision=0,
                            sequence=0,
                            available=available,
                        ),
                    )
                    nodes = await self._nodes(connection)
                if kind == "memory" and isinstance(reference, MemoryCitation):
                    entry_id = reference.entry_id
            if entry_id is not None:
                await self._reconcile_memory_entry(nodes, _current_states(nodes), entry_id)

    async def _plan(self, event: SourceLifecycleEvent) -> _LifecyclePlan:
        """Validate one event and compute the availability it produces."""

        async with self._database().transaction() as connection:
            nodes = await self._nodes(connection)
            source = nodes.get(event.node_id)
            if source is None or source.kind != "source":
                raise SourceLifecycleError("event does not name a registered Source")
            if source.revision != event.revision:
                raise SourceLifecycleError("revision does not match the Source's lifecycle version")
            recorded = await self._repository.event_at(
                connection,
                self.scope_id,
                event.node_id,
                event.sequence,
            )
            if recorded is not None:
                if not recorded.matches(
                    revision=event.revision,
                    available=event.available,
                    reason=event.reason,
                ):
                    raise SourceLifecycleError("the sequence already names a different event")
                return _LifecyclePlan(nodes=nodes, states=_current_states(nodes), replayed=True)
            latest = await self._repository.latest_event(connection, self.scope_id, event.node_id)
            if latest is not None and event.sequence <= latest.sequence:
                raise SourceLifecycleError("sequence is not newer than the accepted event")
            states = _propagate(nodes, event.node_id, event.available)
        return _LifecyclePlan(nodes=nodes, states=states, replayed=False)

    async def _commit(self, event: SourceLifecycleEvent, plan: _LifecyclePlan) -> None:
        """Persist the accepted event and the availability it produced."""

        async with self._database().transaction() as connection:
            await self._repository.update_source_progress(
                connection,
                self.scope_id,
                event.node_id,
                available=event.available,
                sequence=event.sequence,
            )
            changed = {
                node_id: available
                for node_id, available in plan.states.items()
                if node_id != event.node_id and plan.nodes[node_id].available != available
            }
            if changed:
                await self._repository.update_availability(connection, self.scope_id, changed)
            await self._repository.insert_event(
                connection,
                LifecycleEventRecord(
                    scope_id=self.scope_id,
                    node_id=event.node_id,
                    sequence=event.sequence,
                    revision=event.revision,
                    available=event.available,
                    reason=event.reason,
                ),
            )

    async def _propagate_native(self, nodes: Mapping[str, LifecycleNode], states: Mapping[str, bool]) -> None:
        """Move every affected native Memory entry to the state its evidence allows."""

        for entry_id in _memory_entry_ids(nodes):
            await self._reconcile_memory_entry(nodes, states, entry_id)

    async def _reconcile_memory_entry(
        self,
        nodes: Mapping[str, LifecycleNode],
        states: Mapping[str, bool],
        entry_id: str,
    ) -> None:
        """Keep one native Memory entry usable while any node that claims it has support."""

        owners = [
            node_id
            for node_id, node in nodes.items()
            if node.kind == "memory"
            and isinstance(node.reference, MemoryCitation)
            and node.reference.entry_id == entry_id
        ]
        if not owners:
            return
        citation = nodes[owners[0]].reference
        if not isinstance(citation, MemoryCitation):
            return
        await self._set_entry_availability(citation, available=any(states[node_id] for node_id in owners))

    async def _set_entry_availability(self, citation: MemoryCitation, /, *, available: bool) -> None:
        """Deactivate or reactivate the native Memory entry one citation names."""

        async with self._runtime._context(self.scope_id) as context:
            service = context.artifacts.memory
            current = await _head_or_none(service, context.artifacts.memory_artifact_id)
            if current is None:
                return
            entry = next(
                (item for item in await service.entries(current) if item.entry_id == citation.entry_id),
                None,
            )
            if entry is None:
                return
            if available:
                await service.reactivate(current, entries=(entry,), reason=LIFECYCLE_REASON)
            else:
                await service.forget(current, entries=(entry,), reason=LIFECYCLE_REASON)

    async def _nodes(self, connection) -> dict[str, LifecycleNode]:
        nodes = await self._repository.nodes(connection, self.scope_id)
        return {node.node_id: node for node in nodes}

    def _database(self) -> AsyncDatabase:
        if self._db is None:
            raise SourceLifecycleError("lifecycle persistence is not configured")
        return self._db

    async def _require_readable_source(self, source_ref: SourceRef) -> None:
        try:
            await self._runtime.records.for_scope(self.scope_id).get_source(
                source_ref.source_type,
                source_ref.source_id,
            )
        except (BaseValueNotFoundError, ArtifactNotFoundError, RevisionConflictError) as error:
            raise SourceLifecycleError("the Source is not readable") from error

    async def _require_readable_memory(self, citation: MemoryCitation) -> None:
        try:
            await self._runtime.memory.for_scope(self.scope_id).get(
                GetMemoryEntryRequest(citation=citation),
            )
        except (
            ArtifactNotFoundError,
            InvalidMemoryCitationError,
            MemoryEntryNotFoundError,
            RevisionConflictError,
        ) as error:
            raise SourceLifecycleError("the Memory entry is not readable") from error

    async def _require_readable_topic(self, artifact_ref: ArtifactRef) -> None:
        try:
            await self._runtime.records.for_scope(self.scope_id).get_artifact_revision(
                TOPIC_MEMORY_FAMILY,
                artifact_ref.artifact_id,
                artifact_ref.revision,
            )
        except (BaseValueNotFoundError, ArtifactNotFoundError, RevisionConflictError) as error:
            raise SourceLifecycleError("the Topic Memory revision is not readable") from error


class SourceLifecycleApplication:
    """Select the scoped Source lifecycle service."""

    def __init__(self, runtime: BuiltinRuntime, database: AsyncDatabase | None = None) -> None:
        self._runtime = runtime
        self._database = database

    def for_scope(self, scope_id: str, /) -> ScopedSourceLifecycleService:
        return ScopedSourceLifecycleService(self._runtime, scope_id, self._database)


async def _head_or_none(service, artifact_id: str):
    try:
        return await service.head(artifact_id)
    except ArtifactNotFoundError:
        return None


def _require_node_id(node_id: str) -> str:
    if not isinstance(node_id, str) or not node_id or not node_id.strip():
        raise SourceLifecycleError("node_id must be a non-empty identifier")
    if len(node_id) > MAX_SOURCE_ID_LENGTH:
        raise SourceLifecycleError("node_id is longer than the permitted identifier")
    return node_id


def _require_parents(parents: tuple[str, ...], node: str) -> tuple[str, ...]:
    if not parents:
        raise SourceLifecycleError("a derived node needs at least one parent")
    if len(set(parents)) != len(parents):
        raise SourceLifecycleError("a derived node cannot name the same parent twice")
    for parent in parents:
        _require_node_id(parent)
        if parent == node:
            raise SourceLifecycleError("a derived node cannot depend on itself")
    return tuple(parents)


def _require_policy(policy: str) -> str:
    if policy not in _LIFECYCLE_POLICIES:
        raise SourceLifecycleError("policy is not a known lifecycle policy")
    return policy


def _require_same_binding(
    existing: LifecycleNode,
    kind: str,
    reference: SourceRef | MemoryCitation | ArtifactRef,
    parents: tuple[str, ...],
    policy: str,
    revision: int,
) -> None:
    if (
        existing.kind != kind
        or existing.reference != reference
        or existing.parents != parents
        or existing.policy != policy
        or (kind == "source" and existing.revision != revision)
    ):
        raise SourceLifecycleError("the node is already registered with a different binding")


def _current_states(nodes: Mapping[str, LifecycleNode]) -> dict[str, bool]:
    return {node_id: node.available for node_id, node in nodes.items()}


def _memory_entry_ids(nodes: Mapping[str, LifecycleNode]) -> tuple[str, ...]:
    return tuple(
        sorted(
            {
                node.reference.entry_id
                for node in nodes.values()
                if node.kind == "memory" and isinstance(node.reference, MemoryCitation)
            },
        ),
    )


def _available_ids(nodes: Mapping[str, LifecycleNode]) -> list[str]:
    return sorted(node_id for node_id, node in nodes.items() if node.available)


def _derive_availability(states: Mapping[str, bool], parents: tuple[str, ...], policy: str) -> bool:
    support = [states[parent] for parent in parents if parent in states]
    if len(support) != len(parents):
        return False
    return all(support) if policy == "all" else any(support)


def _propagate(nodes: Mapping[str, LifecycleNode], source_id: str, available: bool) -> dict[str, bool]:
    """Return every node's availability after one Source changed."""

    states = _current_states(nodes)
    states[source_id] = available
    for node_id in _topological_order(nodes):
        node = nodes[node_id]
        if node.kind == "source":
            continue
        states[node_id] = _derive_availability(states, node.parents, node.policy)
    return states


def _topological_order(nodes: Mapping[str, LifecycleNode]) -> list[str]:
    """Order the graph so every parent is decided before its children."""

    pending = {node_id: {parent for parent in node.parents if parent in nodes} for node_id, node in nodes.items()}
    order: list[str] = []
    decided: set[str] = set()
    while len(order) < len(nodes):
        ready = sorted(node_id for node_id, parents in pending.items() if not parents and node_id not in decided)
        if not ready:
            raise SourceLifecycleError("the evidence graph contains a cycle")
        for node_id in ready:
            order.append(node_id)
            decided.add(node_id)
            for parents in pending.values():
                parents.discard(node_id)
    return order


__all__ = [
    "LifecyclePurpose",
    "ScopedSourceLifecycleService",
    "SourceLifecycleApplication",
    "SourceLifecycleError",
    "SourceLifecycleEvent",
]
