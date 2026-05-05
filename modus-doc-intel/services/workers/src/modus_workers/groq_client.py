"""
Cerebras API client using direct httpx (no SDK bloat).
OpenAI-compatible endpoint. Supports both sync completion and async streaming.
Includes retry with exponential backoff for transient errors.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import AsyncIterator

import httpx

CEREBRAS_BASE = "https://api.cerebras.ai/v1"
PRIMARY_MODEL = "llama3.1-8b"     # 8K context window
FAST_MODEL = "llama3.1-8b"

logger = logging.getLogger(__name__)

MAX_RETRIES = 8
BASE_DELAY = 2.0  # seconds
MAX_RETRY_DELAY = 60.0  # cap retry-after to a reasonable bound


class GroqClient:
    def __init__(self, api_key: str | None = None):
        key = api_key or os.environ["CEREBRAS_API_KEY"]
        self._client = httpx.AsyncClient(
            base_url=CEREBRAS_BASE,
            headers={"Authorization": f"Bearer {key}"},
            timeout=120.0,
        )

    async def complete_with_usage(
        self,
        messages: list[dict],
        model: str = PRIMARY_MODEL,
        response_format: dict | None = None,
        max_tokens: int = 4096,
    ) -> tuple[str, int]:
        """Like complete() but also returns total tokens consumed."""
        payload: dict = {
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens,
        }
        if response_format:
            payload["response_format"] = response_format

        for attempt in range(MAX_RETRIES):
            r = await self._client.post("/chat/completions", json=payload)
            if r.status_code == 429:
                retry_after = r.headers.get("retry-after")
                delay = float(retry_after) if retry_after else BASE_DELAY * (2 ** attempt)
                delay = min(delay, MAX_RETRY_DELAY)
                logger.warning(f"Rate limited (429), retrying in {delay:.1f}s (attempt {attempt + 1}/{MAX_RETRIES})")
                await asyncio.sleep(delay)
                continue
            r.raise_for_status()
            body = r.json()
            content = body["choices"][0]["message"]["content"]
            tokens = body.get("usage", {}).get("total_tokens", 0)
            return content, tokens

        # Final attempt without catching
        r = await self._client.post("/chat/completions", json=payload)
        r.raise_for_status()
        body = r.json()
        return body["choices"][0]["message"]["content"], body.get("usage", {}).get("total_tokens", 0)

    async def complete(
        self,
        messages: list[dict],
        model: str = PRIMARY_MODEL,
        response_format: dict | None = None,
        max_tokens: int = 4096,
    ) -> str:
        content, _ = await self.complete_with_usage(
            messages, model=model, response_format=response_format, max_tokens=max_tokens
        )
        return content

    async def stream(
        self,
        messages: list[dict],
        model: str = PRIMARY_MODEL,
        max_tokens: int = 4096,
    ) -> AsyncIterator[str]:
        payload = {
            "model": model,
            "messages": messages,
            "stream": True,
            "max_tokens": max_tokens,
        }
        async with self._client.stream(
            "POST", "/chat/completions", json=payload
        ) as r:
            r.raise_for_status()
            async for line in r.aiter_lines():
                if line.startswith("data: "):
                    data_str = line[6:].strip()
                    if data_str == "[DONE]":
                        break
                    try:
                        data = json.loads(data_str)
                        delta = data["choices"][0]["delta"].get("content", "")
                        if delta:
                            yield delta
                    except (json.JSONDecodeError, KeyError, IndexError):
                        continue

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        await self.aclose()
