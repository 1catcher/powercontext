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

"""Coalesce concurrent embedding calls and cache the vectors they produce."""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from typing import TYPE_CHECKING, Any

from powercontext.builtin.inference.models import EmbeddingResult, EmbeddingVector, InferenceUsage

if TYPE_CHECKING:
    from powercontext.builtin.artifacts.memory.models import EmbeddingProfile
    from powercontext.builtin.inference.protocols import EmbeddingModel

COALESCING_MAX_BATCH_SIZE = 64
COALESCING_WINDOW_MS = 5.0
COALESCING_CACHE_SIZE = 1024


class CoalescingEmbeddingModel:
    """Share embedding work between concurrent callers and reuse finished vectors.

    Callers that ask for the same text inside one coalescing window wait for
    the same underlying request, and a later call can still join a request
    that is already running. Successful vectors are kept in a bounded LRU cache
    so a repeated text costs no further provider request. Every caller keeps
    its own input order, including repeated positions, and reports the usage of
    the batches it actually waited for.
    """

    def __init__(
        self,
        model: EmbeddingModel,
        *,
        max_batch_size: int = COALESCING_MAX_BATCH_SIZE,
        coalesce_ms: float = COALESCING_WINDOW_MS,
        cache_size: int = COALESCING_CACHE_SIZE,
    ) -> None:
        if type(max_batch_size) is not int or max_batch_size < 1:
            raise ValueError("max_batch_size must be a positive integer")
        if coalesce_ms < 0:
            raise ValueError("coalesce_ms must not be negative")
        if type(cache_size) is not int or cache_size < 0:
            raise ValueError("cache_size must not be negative")
        self._model = model
        self._max_batch_size = max_batch_size
        self._coalesce_ms = float(coalesce_ms)
        self._cache_size = cache_size
        self._cache: OrderedDict[str, EmbeddingVector] = OrderedDict()
        self._waiting: dict[str, asyncio.Future] = {}
        self._running: dict[str, asyncio.Future] = {}
        self._batch_of: dict[str, int] = {}
        self._batch_counter = 0
        self._flush_task: asyncio.Task | None = None

    @property
    def profile(self) -> EmbeddingProfile:
        """The deployment profile of the wrapped model."""

        return self._model.profile

    async def embed(self, texts: tuple[str, ...], /) -> EmbeddingResult:
        """Return one vector per input text, in the caller's own order."""

        if not texts:
            return EmbeddingResult(vectors=(), usage=InferenceUsage(requests=0))
        vectors: dict[str, EmbeddingVector] = {}
        waiters: list[tuple[str, asyncio.Future]] = []
        for text in dict.fromkeys(texts):
            cached = self._cached(text)
            if cached is not None:
                vectors[text] = cached
                continue
            future = self._running.get(text)
            if future is None:
                future = self._waiting.get(text)
            if future is None:
                future = asyncio.get_running_loop().create_future()
                self._waiting[text] = future
                self._schedule_flush()
            waiters.append((text, future))
        batches: dict[int, InferenceUsage] = {}
        for text, future in waiters:
            # A cancelled caller must not cancel the shared request.
            vector, batch_id, usage = await asyncio.shield(future)
            vectors[text] = vector
            batches.setdefault(batch_id, usage)
        ordered = tuple(vectors[text] for text in texts)
        return EmbeddingResult(vectors=ordered, usage=_merge_usage(batches.values()))

    def _cached(self, text: str) -> EmbeddingVector | None:
        if self._cache_size < 1:
            return None
        vector = self._cache.get(text)
        if vector is not None:
            self._cache.move_to_end(text)
        return vector

    def _remember(self, text: str, vector: EmbeddingVector) -> None:
        if self._cache_size < 1:
            return
        self._cache[text] = vector
        self._cache.move_to_end(text)
        while len(self._cache) > self._cache_size:
            self._cache.popitem(last=False)

    def _schedule_flush(self) -> None:
        if self._flush_task is None or self._flush_task.done():
            self._flush_task = asyncio.create_task(self._flush())

    async def _flush(self) -> None:
        await asyncio.sleep(self._coalesce_ms / 1000)
        while self._waiting:
            texts: list[str] = []
            for text in list(self._waiting):
                if len(texts) >= self._max_batch_size:
                    break
                texts.append(text)
            futures = []
            for text in texts:
                future = self._waiting.pop(text)
                self._running[text] = future
                self._batch_of[text] = self._batch_counter
                futures.append(future)
            batch_id = self._batch_counter
            self._batch_counter += 1
            asyncio.create_task(self._run_batch(batch_id, texts, futures))

    async def _run_batch(self, batch_id: int, texts: list[str], futures: list[asyncio.Future]) -> None:
        try:
            result = await self._model.embed(tuple(texts))
        except BaseException as error:  # noqa: BLE001 - the waiters see the provider error
            self._settle(texts, futures, error)
            return
        vectors = result.vectors
        if len(vectors) != len(texts):
            self._settle(texts, futures, ValueError("embedding vector count does not match the batch size"))
            return
        usage = result.usage
        for text, vector in zip(texts, vectors, strict=True):
            self._remember(text, vector)
            future = self._running.pop(text, None)
            self._batch_of.pop(text, None)
            if future is not None and not future.done():
                future.set_result((vector, batch_id, usage))

    def _settle(self, texts: list[str], futures: list[asyncio.Future], error: BaseException) -> None:
        for text, future in zip(texts, futures, strict=True):
            self._running.pop(text, None)
            self._batch_of.pop(text, None)
            if not future.done():
                future.set_exception(error)


def _merge_usage(usages: Any) -> InferenceUsage:
    """Sum the usage of the batches one caller waited for."""

    requests = 0
    input_tokens: int | None = None
    output_tokens: int | None = None
    for usage in usages:
        requests += usage.requests
        if usage.input_tokens is not None:
            input_tokens = (input_tokens or 0) + usage.input_tokens
        if usage.output_tokens is not None:
            output_tokens = (output_tokens or 0) + usage.output_tokens
    return InferenceUsage(requests=requests, input_tokens=input_tokens, output_tokens=output_tokens)


__all__ = [
    "COALESCING_CACHE_SIZE",
    "COALESCING_MAX_BATCH_SIZE",
    "COALESCING_WINDOW_MS",
    "CoalescingEmbeddingModel",
]
