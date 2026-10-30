"""MiniMax code-change client (Anthropic-compatible Messages API).

MiniMax exposes an Anthropic-compatible endpoint; the module talks to
``{MINIMAX_BASE_URL}/v1/messages`` with the ``x-api-key`` header and asks the
model to return the complete modified ``catalog.py`` inside a fenced code
block, which is far more robust for code generation than diff-format parsing.
"""

from __future__ import annotations

import re
from pathlib import Path

import httpx

from . import config

_FENCE = re.compile(r"```(?:python|py)?\s*\n(.*?)```", re.DOTALL)


class MiniMaxClient:
    def __init__(self, api_key: str = "", base_url: str = "", model: str = "") -> None:
        self.api_key = api_key or config.MINIMAX_API_KEY
        self.base_url = (base_url or config.MINIMAX_BASE_URL).rstrip("/")
        self.model = model or config.MINIMAX_MODEL

    @property
    def available(self) -> bool:
        return bool(self.api_key.strip())

    def build_prompt(self, context: str, extra_constraint: str = "") -> str:
        prompt = (
            "你是一名资深 Python 工程师，正在按规格驱动开发（SDD）修改一个业务工程。\n\n"
            "【开发任务】\n"
            f"{config.TASK_TITLE}\n{config.TASK_DESCRIPTION}\n\n"
            "【任务上下文（来自 seekdb 检索与 PowerContext 状态）】\n"
            f"{context}\n\n"
            "【输出要求】\n"
            "1. 只修改 booklib/catalog.py 一个文件，做最小化增量修改。\n"
            "2. 必须完整保留 SPEC-1 既有行为：插入顺序、min_pages 过滤、"
            "空目录返回空列表。\n"
            "3. 不得修改任何既有公开 API 的签名：Catalog.__init__ 等所有方法的"
            "参数个数与含义必须与修改前完全一致，只能为 list_books 新增"
            " keyword 参数。\n"
            "4. 新增 SPEC-2 要求的 keyword 参数：匹配书名或作者，不区分大小写。\n"
            "5. 输出修改后的完整 catalog.py，包裹在一个 ```python 代码块中，"
            "代码块之外不要写任何解释。\n"
        )
        if extra_constraint:
            prompt += f"\n【补充约束（上一次输出被拒绝的原因）】\n{extra_constraint}\n"
        return prompt

    async def generate_code(
        self, context: str, extra_constraint: str = ""
    ) -> tuple[str, str, str]:
        """Return (new_file_content, prompt, response_text)."""
        prompt = self.build_prompt(context, extra_constraint)
        url = f"{self.base_url}/v1/messages"
        headers = {
            "content-type": "application/json",
            "x-api-key": self.api_key,
            "anthropic-version": "2023-06-01",
        }
        body = {
            "model": self.model,
            "max_tokens": 2048,
            "messages": [{"role": "user", "content": prompt}],
        }
        async with httpx.AsyncClient(timeout=120.0) as client:
            response = await client.post(url, headers=headers, json=body)
            response.raise_for_status()
            payload = response.json()
        text = "".join(
            block.get("text", "")
            for block in payload.get("content", [])
            if block.get("type") == "text"
        )
        if not text:
            raise RuntimeError(f"MiniMax returned no text: {payload}")
        match = _FENCE.search(text)
        if match is None:
            raise RuntimeError(f"MiniMax response has no python code block: {text[:500]}")
        return match.group(1).strip() + "\n", prompt, text
