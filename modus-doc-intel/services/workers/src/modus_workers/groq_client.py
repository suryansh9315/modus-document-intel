"""
Groq API client using direct httpx (no SDK bloat).
Supports both sync completion and async streaming.
Includes retry with exponential backoff for rate limits.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import AsyncIterator

import httpx

GROQ_BASE = "https://api.groq.com/openai/v1"
PRIMARY_MODEL = "llama-3.3-70b-versatile"   # 128K ctx, high quality
FAST_MODEL = "llama-3.1-8b-instant"         # routing, extraction, verification

logger = logging.getLogger(__name__)

MAX_RETRIES = 8
BASE_DELAY = 2.0  # seconds
MAX_RETRY_DELAY = 60.0  # cap retry-after to avoid 1-hour waits


class GroqClient:
    def __init__(self, api_key: str | None = None):
        key = api_key or os.environ["GROQ_API_KEY"]
        self._client = httpx.AsyncClient(
            base_url=GROQ_BASE,
            headers={"Authorization": f"Bearer {key}"},
            timeout=120.0,
        )

    async def complete(
        self,
        messages: list[dict],
        model: str = PRIMARY_MODEL,
        response_format: dict | None = None,
        max_tokens: int = 4096,
    ) -> str:
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
                # Rate limited — extract retry-after or use exponential backoff
                retry_after = r.headers.get("retry-after")
                delay = float(retry_after) if retry_after else BASE_DELAY * (2 ** attempt)
                delay = min(delay, MAX_RETRY_DELAY)  # cap to avoid multi-minute waits
                logger.warning(f"Rate limited (429), retrying in {delay:.1f}s (attempt {attempt + 1}/{MAX_RETRIES})")
                await asyncio.sleep(delay)
                continue
            r.raise_for_status()
            return r.json()["choices"][0]["message"]["content"]

        # Final attempt without catching
        r = await self._client.post("/chat/completions", json=payload)
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"]

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
