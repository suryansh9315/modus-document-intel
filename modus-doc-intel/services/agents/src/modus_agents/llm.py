"""
Groq API client for the agents service.
Direct httpx — no SDK overhead. Supports streaming via SSE.
"""
from __future__ import annotations

import json
import os
from typing import AsyncIterator

import httpx

GROQ_BASE = "https://api.groq.com/openai/v1"
PRIMARY_MODEL = "llama-3.3-70b-versatile"
FAST_MODEL = "llama-3.1-8b-instant"


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
