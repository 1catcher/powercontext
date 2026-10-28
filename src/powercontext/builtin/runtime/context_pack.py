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

"""Context Pack: a chunked, checksummed representation of prepared context.

A Context Pack carries the bytes of one prepared context as an ordered list of
chunks. A Context Pack Delta carries only the part of the target that the
receiver cannot reuse from the pack it already holds, so consecutive turns
reuse the unchanged prefix instead of resending it.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

CONTEXT_PACK_SCHEMA = "powercontext.context-pack.v1"
CONTEXT_PACK_DELTA_SCHEMA = "powercontext.context-pack-delta.v1"
CONTEXT_PACK_CHUNK_BYTES = 512


class ContextPackError(Exception):
    """Raised when a Context Pack or delta violates the wire contract."""


@dataclass(frozen=True, slots=True)
class ContextPack:
    """One complete chunked context with its digest and wire size."""

    items: tuple[bytes, ...]
    digest: str
    byte_count: int

    @classmethod
    def build(cls, items: Iterable[bytes], /) -> ContextPack:
        """Build a complete pack from its ordered chunks."""

        chunks = _validate_items(items)
        digest = _compute_digest(chunks)
        byte_count = 0
        for _ in range(8):
            pack = cls(items=chunks, digest=digest, byte_count=byte_count)
            length = len(pack.to_bytes())
            if length == byte_count:
                return pack
            byte_count = length
        raise ContextPackError("context pack byte count did not converge")

    @classmethod
    def from_content(
        cls,
        content: str | bytes | None,
        *,
        chunk_bytes: int = CONTEXT_PACK_CHUNK_BYTES,
    ) -> ContextPack:
        """Chunk one content deterministically without changing its bytes."""

        if type(chunk_bytes) is not int or chunk_bytes < 1:
            raise ContextPackError("chunk_bytes must be a positive integer")
        if content is None:
            return cls.build(())
        if isinstance(content, str):
            raw = content.encode("utf-8")
        elif isinstance(content, (bytes, bytearray, memoryview)):
            raw = bytes(content)
        else:
            raise ContextPackError("context content must be text or bytes")
        return cls.build(_chunk_bytes(raw, chunk_bytes))

    def to_bytes(self) -> bytes:
        """Serialize the pack with the canonical wire encoding."""

        return _canonical_json(
            {
                "schema": CONTEXT_PACK_SCHEMA,
                "items": _encode_items(self.items),
                "digest": self.digest,
                "byte_count": self.byte_count,
            }
        )

    @classmethod
    def from_bytes(cls, raw: bytes, /) -> ContextPack:
        """Decode one complete pack and validate its digest and byte count."""

        payload = _load_object(
            raw,
            required=frozenset({"schema", "items", "digest", "byte_count"}),
            name="context pack",
        )
        if payload["schema"] != CONTEXT_PACK_SCHEMA:
            raise ContextPackError("context pack schema mismatch")
        items = _decode_items(payload["items"], field="items")
        digest = _require_digest(payload["digest"], field="digest")
        byte_count = payload["byte_count"]
        if type(byte_count) is not int or byte_count < 0:
            raise ContextPackError("context pack byte_count must be a non-negative integer")
        pack = cls(items=items, digest=digest, byte_count=byte_count)
        if _compute_digest(items) != digest:
            raise ContextPackError("context pack digest mismatch")
        if len(pack.to_bytes()) != byte_count:
            raise ContextPackError("context pack byte_count mismatch")
        return pack


@dataclass(frozen=True, slots=True)
class ContextPackDelta:
    """The part of a target pack that a receiver cannot reuse from its base."""

    base_digest: str
    target_digest: str
    prefix_items: int
    appended: tuple[bytes, ...]

    @classmethod
    def between(cls, base: ContextPack, target: ContextPack, /) -> ContextPackDelta:
        """Describe how the target pack extends the base pack."""

        if not isinstance(base, ContextPack) or not isinstance(target, ContextPack):
            raise ContextPackError("a context pack delta needs two context packs")
        prefix = 0
        limit = min(len(base.items), len(target.items))
        while prefix < limit and base.items[prefix] == target.items[prefix]:
            prefix += 1
        return cls(
            base_digest=base.digest,
            target_digest=target.digest,
            prefix_items=prefix,
            appended=tuple(target.items[prefix:]),
        )

    def to_bytes(self) -> bytes:
        """Serialize the delta with the canonical wire encoding."""

        return _canonical_json(
            {
                "schema": CONTEXT_PACK_DELTA_SCHEMA,
                "base_digest": self.base_digest,
                "target_digest": self.target_digest,
                "prefix_items": self.prefix_items,
                "appended": _encode_items(self.appended),
            }
        )

    @property
    def byte_count(self) -> int:
        """The number of bytes this delta occupies on the wire."""

        return len(self.to_bytes())

    @classmethod
    def from_bytes(cls, raw: bytes, /) -> ContextPackDelta:
        """Decode one delta and validate its declared fields."""

        payload = _load_object(
            raw,
            required=frozenset({"schema", "base_digest", "target_digest", "prefix_items", "appended"}),
            name="context pack delta",
        )
        if payload["schema"] != CONTEXT_PACK_DELTA_SCHEMA:
            raise ContextPackError("context pack delta schema mismatch")
        prefix_items = payload["prefix_items"]
        if type(prefix_items) is not int or prefix_items < 0:
            raise ContextPackError("context pack delta prefix_items must be a non-negative integer")
        base_digest = _require_digest(payload["base_digest"], field="base_digest")
        target_digest = _require_digest(payload["target_digest"], field="target_digest")
        appended = _decode_items(payload["appended"], field="appended")
        return cls(
            base_digest=base_digest,
            target_digest=target_digest,
            prefix_items=prefix_items,
            appended=appended,
        )


def apply_context_pack_delta(base: ContextPack, delta: ContextPackDelta, /) -> ContextPack:
    """Rebuild the target pack from the base pack and one delta."""

    if not isinstance(base, ContextPack):
        raise ContextPackError("a context pack delta needs a base context pack")
    if not isinstance(delta, ContextPackDelta):
        raise ContextPackError("a context pack delta needs a delta")
    if base.digest != delta.base_digest:
        raise ContextPackError("context pack delta base digest mismatch")
    if delta.prefix_items > len(base.items):
        raise ContextPackError("context pack delta prefix exceeds the base pack")
    target = ContextPack.build(base.items[: delta.prefix_items] + delta.appended)
    if target.digest != delta.target_digest:
        raise ContextPackError("context pack delta target digest mismatch")
    return target


def _canonical_json(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _encode_items(items: Iterable[bytes]) -> list[str]:
    return [base64.b64encode(item).decode("ascii") for item in items]


def _decode_items(values: object, *, field: str) -> tuple[bytes, ...]:
    if not isinstance(values, list):
        raise ContextPackError(f"context pack {field} must be a base64 string array")
    decoded: list[bytes] = []
    for value in values:
        if not isinstance(value, str):
            raise ContextPackError(f"context pack {field} must be a base64 string array")
        try:
            decoded.append(base64.b64decode(value, validate=True))
        except (binascii.Error, ValueError) as error:
            raise ContextPackError(f"context pack {field} must contain valid base64") from error
    return tuple(decoded)


def _validate_items(items: Iterable[bytes]) -> tuple[bytes, ...]:
    if isinstance(items, (bytes, bytearray, str)):
        raise ContextPackError("context pack items must be a sequence of byte chunks")
    chunks: list[bytes] = []
    for item in items:
        if not isinstance(item, (bytes, bytearray, memoryview)):
            raise ContextPackError("context pack items must be byte chunks")
        chunks.append(bytes(item))
    return tuple(chunks)


def _compute_digest(items: tuple[bytes, ...]) -> str:
    unsigned = {"schema": CONTEXT_PACK_SCHEMA, "items": _encode_items(items)}
    return "sha256:" + hashlib.sha256(_canonical_json(unsigned)).hexdigest()


def _require_digest(value: object, *, field: str) -> str:
    if not isinstance(value, str) or not value.startswith("sha256:") or len(value) != 71:
        raise ContextPackError(f"context pack {field} must be a sha256 digest")
    return value


def _load_object(raw: bytes, *, required: frozenset[str], name: str) -> dict[str, Any]:
    if not isinstance(raw, (bytes, bytearray, memoryview)):
        raise ContextPackError(f"{name} must be UTF-8 encoded JSON")
    try:
        payload = json.loads(bytes(raw).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ContextPackError(f"{name} must be UTF-8 encoded JSON") from error
    if not isinstance(payload, dict):
        raise ContextPackError(f"{name} must be a JSON object")
    if set(payload) != required:
        raise ContextPackError(f"{name} must contain exactly {sorted(required)}")
    return payload


def _chunk_bytes(raw: bytes, limit: int) -> tuple[bytes, ...]:
    chunks: list[bytes] = []
    total = len(raw)
    start = 0
    while start < total:
        end = min(start + limit, total)
        while end < total and (raw[end] & 0xC0) == 0x80:
            end -= 1
        if end == start:
            raise ContextPackError("chunk_bytes is too small to hold one character")
        chunk = raw[start:end]
        try:
            chunk.decode("utf-8")
        except UnicodeDecodeError as error:
            raise ContextPackError("context content must be valid UTF-8") from error
        chunks.append(chunk)
        start = end
    return tuple(chunks)


__all__ = [
    "CONTEXT_PACK_CHUNK_BYTES",
    "CONTEXT_PACK_DELTA_SCHEMA",
    "CONTEXT_PACK_SCHEMA",
    "ContextPack",
    "ContextPackDelta",
    "ContextPackError",
    "apply_context_pack_delta",
]
