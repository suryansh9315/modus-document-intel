"""
Cerebras API client for the agents service.
OpenAI-compatible endpoint. Direct httpx — no SDK overhead. Supports streaming via SSE.
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
BASE_DELAY = 2.0      # seconds
MAX_RETRY_DELAY = 60.0  # cap retry-after to a reasonable bound

# Cerebras: 30 req/min shared with the workers service.
# 1.5s between agent calls → ≤ 40 req/min from agents alone; combined headroom remains.
_semaphore = asyncio.Semaphore(1)
_REQUEST_INTERVAL = 1.5  # seconds between requests


class GroqClient:
    def __init__(self, api_key: str | None = None):
        key = api_key or os.environ["CEREBRAS_API_KEY"]
        self._client = httpx.AsyncClient(
            base_url=CEREBRAS_BASE,
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

        async with _semaphore:
            for attempt in range(MAX_RETRIES):
                r = await self._client.post("/chat/completions", json=payload)
                if r.status_code == 429:
                    retry_after = r.headers.get("retry-after")
                    delay = float(retry_after) if retry_after else BASE_DELAY * (2 ** attempt)
                    delay = min(delay, MAX_RETRY_DELAY)
                    logger.warning(f"Rate limited (429), retrying in {delay:.1f}s (attempt {attempt + 1}/{MAX_RETRIES})")
                    await asyncio.sleep(delay)
                    continue
                if r.status_code >= 400 and r.status_code != 429:
                    logger.error(f"API error {r.status_code}: {r.text}")
                r.raise_for_status()
                await asyncio.sleep(_REQUEST_INTERVAL)
                return r.json()["choices"][0]["message"]["content"]

            # Final attempt without catching
            r = await self._client.post("/chat/completions", json=payload)
            r.raise_for_status()
            await asyncio.sleep(_REQUEST_INTERVAL)
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

        async with _semaphore:
            for attempt in range(MAX_RETRIES + 1):
                async with self._client.stream("POST", "/chat/completions", json=payload) as r:
                    if r.status_code == 429:
                        retry_after = r.headers.get("retry-after")
                        delay = float(retry_after) if retry_after else BASE_DELAY * (2 ** attempt)
                        delay = min(delay, MAX_RETRY_DELAY)
                        logger.warning(f"Stream rate limited (429), retrying in {delay:.1f}s (attempt {attempt + 1}/{MAX_RETRIES})")
                        await asyncio.sleep(delay)
                        continue
                    if r.status_code >= 400 and r.status_code != 429:
                        body = await r.aread()
                        logger.error(f"API error {r.status_code}: {body.decode()[:500]}")
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
                    await asyncio.sleep(_REQUEST_INTERVAL)
                    return  # successfully streamed

            raise RuntimeError(f"Stream failed after {MAX_RETRIES} retries due to rate limiting")

    async def aclose(self):
        await self._client.aclose()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        await self.aclose()


# Module-level singleton (initialized on first use)
_client: GroqClient | None = None


def get_groq_client() -> GroqClient:
    global _client
    if _client is None:
        _client = GroqClient()
    return _client
