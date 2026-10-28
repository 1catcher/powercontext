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

"""Capture the raw visible events of one Pi session branch as citable Sources."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any
from urllib.parse import quote

from pydantic import BaseModel, JsonValue, field_validator

from powercontext.limits import MAX_SOURCE_ID_LENGTH
from powercontext.sources.connectors import ConnectorRunCompletion, ConnectorRunSession, ConnectorRunStatus
from powercontext.sources.models import Source, SourceMaterialization
from powercontext.sources.projections import TEXT_EVIDENCE_PROJECTION_KEY, TextEvidence

PI_EVENT_SOURCE_NAME = "pi-event"
PI_EVENT_SOURCE_VERSION = "1"
PI_SESSION_CONNECTOR_NAME = "pi-session-history"
PI_SESSION_CONNECTOR_VERSION = "1"
PI_SESSION_HEADER_TYPE = "session"
PI_SESSION_FORMAT_VERSION = 3
PI_CHECKPOINT_VERSION = 1

_MESSAGE_TYPE = "message"
_CUSTOM_MESSAGE_TYPE = "custom_message"
_COMPACTION_TYPE = "compaction"
_BRANCH_SUMMARY_TYPE = "branch_summary"
_SUMMARY_TYPES = (_COMPACTION_TYPE, _BRANCH_SUMMARY_TYPE)
_TOOL_RESULT_ROLE = "toolResult"
_BASH_EXECUTION_ROLE = "bashExecution"
_UNKNOWN_TOOL_NAME = "unknown"


class PiSessionInput(BaseModel):
    """One Pi session archive addressed by its bytes and optional leaf node."""

    content: bytes
    leaf_id: str | None = None


class PiEventInput(BaseModel):
    """One visible event of a Pi session, with its complete original record."""

    session_id: str
    event_id: str
    parent_id: str | None
    event_type: str
    role: str
    content: str
    record: dict[str, JsonValue]


class PiEventSource(Source):
    """A captured Pi event persisted as one immutable, citable Source."""

    source_id: str
    session_id: str
    event_id: str
    parent_id: str | None
    event_type: str
    role: str
    content: str
    record: dict[str, JsonValue]

    @field_validator("source_id")
    @classmethod
    def validate_source_id(cls, value: str) -> str:
        if not value or value.strip() != value or len(value) > MAX_SOURCE_ID_LENGTH:
            raise ValueError("Pi event source_id must be a bounded non-empty trimmed string")
        return value


def pi_event_source_id(session_id: str, event_id: str) -> str:
    """Build the stable identity of one Pi event."""

    if not session_id or not session_id.strip():
        raise ValueError("Pi session_id must be a non-empty string")
    if not event_id or not event_id.strip():
        raise ValueError("Pi event_id must be a non-empty string")
    source_id = f"pi:{quote(session_id, safe='')}:{quote(event_id, safe='')}"
    if len(source_id) > MAX_SOURCE_ID_LENGTH:
        raise ValueError(f"Pi event source_id must not exceed {MAX_SOURCE_ID_LENGTH} characters")
    return source_id


def parse_pi_session(value: PiSessionInput) -> tuple[PiEventInput, ...]:
    """Parse the selected Pi branch into its visible events, or fail atomically."""

    records = _read_records(value.content)
    if not records:
        raise ValueError("Pi session must contain a session header")
    header = records[0]
    if header.get("type") != PI_SESSION_HEADER_TYPE:
        raise ValueError("Pi session must start with a session header record")
    if header.get("version") != PI_SESSION_FORMAT_VERSION:
        raise ValueError("Pi session header must declare the supported format version")
    session_id = header.get("id")
    if not isinstance(session_id, str) or not session_id:
        raise ValueError("Pi session header must carry a non-empty id")

    nodes: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for record in records:
        node_id = record.get("id")
        if not isinstance(node_id, str) or not node_id:
            raise ValueError("Pi session records must carry a non-empty id")
        if node_id in nodes:
            raise ValueError(f"Pi session record id {node_id!r} is duplicated")
        parent_id = record.get("parentId")
        if parent_id is not None:
            if not isinstance(parent_id, str) or not parent_id:
                raise ValueError("Pi session parentId must be null or a non-empty string")
            if parent_id not in nodes:
                raise ValueError(f"Pi session parent {parent_id!r} was never defined")
        nodes[node_id] = record
        order.append(node_id)

    if value.leaf_id is None:
        leaf_id = order[-1]
    else:
        leaf_id = value.leaf_id
        if leaf_id not in nodes:
            raise ValueError(f"Pi session leaf {leaf_id!r} was not found")

    events: list[PiEventInput] = []
    for node_id in reversed(_ancestor_chain(nodes, leaf_id)):
        record = nodes[node_id]
        visible = _visible_event(session_id, node_id, record)
        if visible is None:
            continue
        event_type, role, content = visible
        events.append(
            PiEventInput(
                session_id=session_id,
                event_id=node_id,
                parent_id=record.get("parentId"),
                event_type=event_type,
                role=role,
                content=content,
                record=record,
            )
        )
    return tuple(events)


def _read_records(content: bytes) -> list[dict[str, Any]]:
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise ValueError("Pi session must be valid UTF-8") from error
    records: list[dict[str, Any]] = []
    for number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as error:
            raise ValueError(f"Pi session line {number} is not valid JSON") from error
        if not isinstance(record, dict):
            raise ValueError(f"Pi session line {number} must be a JSON object")
        records.append(record)
    return records


def _ancestor_chain(nodes: dict[str, dict[str, Any]], leaf_id: str) -> list[str]:
    chain: list[str] = []
    visited: set[str] = set()
    current: str | None = leaf_id
    while current is not None:
        if current in visited:
            raise ValueError(f"Pi session node {current!r} forms a cycle")
        visited.add(current)
        chain.append(current)
        parent = nodes[current].get("parentId")
        current = parent if isinstance(parent, str) else None
    return chain


def _visible_event(session_id: str, node_id: str, record: dict[str, Any]) -> tuple[str, str, str] | None:
    event_type = record.get("type")
    if not isinstance(event_type, str) or not event_type:
        return None
    if event_type == _MESSAGE_TYPE:
        message = record.get("message")
        if not isinstance(message, dict):
            return None
        role = message.get("role")
        if not isinstance(role, str) or not role:
            return None
        body = _message_body(role, message)
    elif event_type == _CUSTOM_MESSAGE_TYPE:
        role = "custom"
        body = _visible_text(record.get("content"))
    elif event_type in _SUMMARY_TYPES:
        role = event_type
        summary = record.get("summary")
        body = summary if isinstance(summary, str) else ""
    else:
        # Control nodes keep the graph connected but carry no textual evidence.
        return None
    if not body:
        return None
    return event_type, role, f"{role}: {body}"


def _message_body(role: str, message: dict[str, Any]) -> str:
    if role == _TOOL_RESULT_ROLE:
        tool_name = message.get("toolName")
        if not isinstance(tool_name, str) or not tool_name:
            tool_name = _UNKNOWN_TOOL_NAME
        tool_call_id = message.get("toolCallId")
        if not isinstance(tool_call_id, str) or not tool_call_id:
            tool_call_id = _UNKNOWN_TOOL_NAME
        return f"[tool_result {tool_name} {tool_call_id}] {_visible_text(message.get('content'))}"
    if role == _BASH_EXECUTION_ROLE:
        command = message.get("command")
        output = message.get("output")
        return f"$ {command if isinstance(command, str) else ''}\n{output if isinstance(output, str) else ''}"
    return _visible_text(message.get("content"))


def _visible_text(content: object) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if isinstance(block, str):
            parts.append(block)
            continue
        if not isinstance(block, dict):
            continue
        block_type = block.get("type")
        if block_type == "text":
            text = block.get("text")
            if isinstance(text, str):
                parts.append(text)
        elif block_type == "toolCall":
            parts.append(_tool_call_text(block))
    return "\n".join(parts)


def _tool_call_text(block: dict[str, Any]) -> str:
    name = block.get("name")
    call_id = block.get("id")
    arguments = block.get("arguments")
    encoded = json.dumps(
        arguments if arguments is not None else {},
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return f"[tool_call {name if isinstance(name, str) else ''} {call_id if isinstance(call_id, str) else ''}] {encoded}"


class PiEventTextEvidenceProjection:
    """Project one captured Pi event into the standard text evidence shape."""

    name: str = TEXT_EVIDENCE_PROJECTION_KEY.name
    version: str = TEXT_EVIDENCE_PROJECTION_KEY.version
    source_class: type[PiEventSource] = PiEventSource
    output_class: type[TextEvidence] = TextEvidence

    def project(self, source: PiEventSource, /) -> TextEvidence:
        return TextEvidence(
            source_type=PI_EVENT_SOURCE_NAME,
            source_id=source.source_id,
            content=source.content,
            metadata={
                "session_id": source.session_id,
                "event_id": source.event_id,
                "parent_id": source.parent_id,
                "event_type": source.event_type,
                "role": source.role,
            },
        )


class PiEventSourceDefinition:
    """Bind Pi event capture to a durable Source Definition version."""

    name: str = PI_EVENT_SOURCE_NAME
    version: str = PI_EVENT_SOURCE_VERSION
    input_class: type[PiEventInput] = PiEventInput
    source_class: type[PiEventSource] = PiEventSource
    projections: tuple[PiEventTextEvidenceProjection, ...] = (PiEventTextEvidenceProjection(),)

    async def resolve(self, value: PiEventInput, /) -> PiEventSource:
        source_id = pi_event_source_id(value.session_id, value.event_id)
        return PiEventSource(
            name=source_id,
            source_id=source_id,
            session_id=value.session_id,
            event_id=value.event_id,
            parent_id=value.parent_id,
            event_type=value.event_type,
            role=value.role,
            content=value.content,
            record=value.record,
            materialization=SourceMaterialization.CAPTURED,
        )

    async def read(self, source: PiEventSource, /) -> PiEventInput:
        return PiEventInput(
            session_id=source.session_id,
            event_id=source.event_id,
            parent_id=source.parent_id,
            event_type=source.event_type,
            role=source.role,
            content=source.content,
            record=source.record,
        )


PI_EVENT_SOURCE_DEFINITION = PiEventSourceDefinition()


class PiSessionConnector:
    """Import one Pi session branch incrementally, resuming from a durable checkpoint."""

    name: str = PI_SESSION_CONNECTOR_NAME
    version: str = PI_SESSION_CONNECTOR_VERSION
    source_definitions: frozenset[str] = frozenset({PI_EVENT_SOURCE_NAME})

    def __init__(self, path: Path | str, leaf_id: str | None = None) -> None:
        self.path = Path(path)
        self.leaf_id = leaf_id

    async def run(self, session: ConnectorRunSession, /) -> ConnectorRunCompletion:
        raw = self.path.read_bytes()
        events = parse_pi_session(PiSessionInput(content=raw, leaf_id=self.leaf_id))
        imported = _read_checkpoint(session.checkpoint)
        digests = dict(imported)
        for event in events:
            source_id = pi_event_source_id(event.session_id, event.event_id)
            digest = _record_digest(event.record)
            known = imported.get(source_id)
            if known is not None:
                if known != digest:
                    raise ValueError(f"Pi event {event.event_id!r} was rewritten after it was imported")
                continue
            await session.submit(source_id, PI_EVENT_SOURCE_NAME, event)
            digests[source_id] = digest
        return ConnectorRunCompletion(
            status=ConnectorRunStatus.COMPLETE,
            checkpoint={"version": PI_CHECKPOINT_VERSION, "events": digests},
        )


def _read_checkpoint(checkpoint: JsonValue | None) -> dict[str, str]:
    if checkpoint is None:
        return {}
    if not isinstance(checkpoint, dict):
        raise ValueError("Pi session checkpoint must be a JSON object")
    version = checkpoint.get("version")
    events = checkpoint.get("events")
    if version != PI_CHECKPOINT_VERSION or not isinstance(events, dict):
        raise ValueError("Pi session checkpoint has an unsupported format")
    imported: dict[str, str] = {}
    for source_id, digest in events.items():
        if not isinstance(source_id, str) or not isinstance(digest, str):
            raise ValueError("Pi session checkpoint must map event identities to digests")
        imported[source_id] = digest
    return imported


def _record_digest(record: dict[str, JsonValue]) -> str:
    canonical = json.dumps(record, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


__all__ = [
    "PI_EVENT_SOURCE_DEFINITION",
    "PiEventInput",
    "PiEventSource",
    "PiEventSourceDefinition",
    "PiEventTextEvidenceProjection",
    "PiSessionConnector",
    "PiSessionInput",
    "pi_event_source_id",
    "parse_pi_session",
]
