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

"""Rerank coarse Memory candidates with one offline HTTP scoring service."""

from __future__ import annotations

import math
from typing import TYPE_CHECKING

from powercontext.builtin.artifacts.memory.models import MemoryHit
from powercontext.builtin.artifacts.memory.reranking import MemoryRerankDecision
from powercontext.builtin.inference import InferenceUsage

if TYPE_CHECKING:
    # The caller owns the HTTP client, so httpx is only needed for typing here.
    import httpx

HTTP_RERANKER_POLICY_ID = "http-cross-encoder-v1"
HTTP_RERANKER_BATCH_SIZE = 32


class HTTPMemoryReranker:
    """Select final Memory hits from the scores returned by an HTTP service.

    The service scores a bounded batch of candidates at a time. Every global
    candidate rank is sent exactly once, the batches are merged back into one
    candidate pool, and the final selection is made from that pool, so a batch
    boundary never changes the outcome.
    """

    policy_id: str

    def __init__(
        self,
        endpoint: str,
        *,
        client: httpx.AsyncClient,
        batch_size: int = HTTP_RERANKER_BATCH_SIZE,
        policy_id: str = HTTP_RERANKER_POLICY_ID,
    ) -> None:
        if batch_size < 1:
            raise ValueError("rerank batch_size must be a positive integer")
        self._endpoint = endpoint
        self._client = client
        self._batch_size = batch_size
        self.policy_id = policy_id

    async def rerank(
        self,
        query: str,
        candidates: tuple[MemoryHit, ...],
        limit: int,
        /,
    ) -> MemoryRerankDecision:
        """Score every candidate and select the globally best original ranks."""

        if not candidates:
            raise ValueError("rerank requires at least one candidate")
        if limit < 1 or limit > len(candidates):
            raise ValueError("rerank limit must be between one and the candidate count")

        scored: list[tuple[int, float]] = []
        requests = 0
        for start in range(0, len(candidates), self._batch_size):
            chunk = candidates[start : start + self._batch_size]
            batch_ranks = tuple(range(start + 1, start + 1 + len(chunk)))
            payload = {
                "query": query,
                "candidates": [
                    {"rank": rank, "text": hit.text}
                    for rank, hit in zip(batch_ranks, chunk, strict=True)
                ],
            }
            response = await self._client.post(self._endpoint, json=payload)
            response.raise_for_status()
            requests += 1
            scored.extend(_parse_scores(response, batch_ranks))

        scored.sort(key=lambda item: (-item[1], item[0]))
        return MemoryRerankDecision(
            selected_ranks=tuple(rank for rank, _ in scored[:limit]),
            usage=InferenceUsage(requests=requests),
            discarded_rank_count=0,
        )


def _parse_scores(response: "httpx.Response", batch_ranks: tuple[int, ...]) -> list[tuple[int, float]]:
    """Validate one batch response and map its scores back to global ranks."""

    try:
        payload = response.json()
    except ValueError as error:
        raise ValueError("rerank service must answer with a JSON body") from error
    if not isinstance(payload, dict):
        raise ValueError("rerank service response must be a JSON object")
    if set(payload) != {"scores"}:
        raise ValueError("rerank service response must contain exactly the scores field")
    scores = payload["scores"]
    if not isinstance(scores, list):
        raise ValueError("rerank service scores must be a list")

    expected = set(batch_ranks)
    observed: set[int] = set()
    parsed: list[tuple[int, float]] = []
    for item in scores:
        if not isinstance(item, dict):
            raise ValueError("rerank service score entries must be JSON objects")
        if set(item) != {"rank", "score"}:
            raise ValueError("rerank service score entries must contain exactly rank and score")
        rank = item["rank"]
        if type(rank) is not int:
            raise ValueError("rerank service rank must be an integer")
        if rank not in expected:
            raise ValueError("rerank service returned a rank outside the requested batch")
        if rank in observed:
            raise ValueError("rerank service returned a duplicated rank")
        score = item["score"]
        if isinstance(score, bool) or not isinstance(score, (int, float)):
            raise ValueError("rerank service score must be a number")
        value = float(score)
        if not math.isfinite(value):
            raise ValueError("rerank service score must be a finite number")
        observed.add(rank)
        parsed.append((rank, value))
    if observed != expected:
        raise ValueError("rerank service must score every requested rank exactly once")
    return parsed


__all__ = ["HTTP_RERANKER_BATCH_SIZE", "HTTP_RERANKER_POLICY_ID", "HTTPMemoryReranker"]
