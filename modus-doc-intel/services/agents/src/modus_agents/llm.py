"""
Two LLM clients for the agents service:
- CerebrasClient  → llama-3.1-3b  (FAST_MODEL)  — extraction, local analysis, contradiction
- GroqPrimaryClient → llama-4-scout (PRIMARY_MODEL) — global reasoning, query synthesis

Each client has its own httpx session and independent rate limiter.
Workers ingestion uses Cerebras only via its own groq_client.py — no shared state.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import AsyncIterator

import httpx

CEREBRAS_BASE = "https://api.cerebras.ai/v1"
GROQ_BASE     = "https://api.groq.com/openai/v1"

FAST_MODEL    = "llama3.1-8b"                                   # Cerebras — 6K TPM, 30 RPM
PRIMARY_MODEL = "meta-llama/llama-4-scout-17b-16e-instruct"     # Groq    — 30K TPM, 30 RPM, 1K RPD

logger = logging.getLogger(__name__)

MAX_RETRIES     = 8
BASE_DELAY      = 2.0   # seconds
MAX_RETRY_DELAY = 60.0  # cap retry-after header value

# --- Cerebras rate limiter (llama-3.1-3b: 30 RPM) ---
_cerebras_semaphore = asyncio.Semaphore(1)
_cerebras_interval  = 2.0   # seconds between requests

# --- Groq rate limiter (llama-4-scout: 30 RPM) ---
_groq_semaphore  = asyncio.Semaphore(1)
_groq_interval   = 2.0   # seconds between requests


class CerebrasClient:
    """Cerebras API client — used for FAST_MODEL calls in the agents query pipeline."""

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
        model: str = FAST_MODEL,
        response_format: dict | None = None,
        max_tokens: int = 2048,
    ) -> str:
        payload: dict = {
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens,
        }
        if response_format:
            payload["response_format"] = response_format

        async with _cerebras_semaphore:
            for attempt in range(MAX_RETRIES):
                r = await self._client.post("/chat/completions", json=payload)
                if r.status_code == 429:
                    retry_after = r.headers.get("retry-after")
                    delay = float(retry_after) if retry_after else BASE_DELAY * (2 ** attempt)
                    delay = min(delay, MAX_RETRY_DELAY)
                    logger.warning(f"Cerebras rate limited (429), retrying in {delay:.1f}s (attempt {attempt + 1}/{MAX_RETRIES})")
                    await asyncio.sleep(delay)
                    continue
                if r.status_code >= 400:
                    logger.error(f"Cerebras API error {r.status_code}: {r.text[:500]}")
                r.raise_for_status()
                await asyncio.sleep(_cerebras_interval)
                return r.json()["choices"][0]["message"]["content"]

            # Final attempt
            r = await self._client.post("/chat/completions", json=payload)
            r.raise_for_status()
            await asyncio.sleep(_cerebras_interval)
            return r.json()["choices"][0]["message"]["content"]

    async def stream(
        self,
        messages: list[dict],
        model: str = FAST_MODEL,
        max_tokens: int = 2048,
    ) -> AsyncIterator[str]:
        payload = {
            "model": model,
            "messages": messages,
            "stream": True,
            "max_tokens": max_tokens,
        }
        async with _cerebras_semaphore:
            for attempt in range(MAX_RETRIES + 1):
                async with self._client.stream("POST", "/chat/completions", json=payload) as r:
                    if r.status_code == 429:
                        retry_after = r.headers.get("retry-after")
                        delay = float(retry_after) if retry_after else BASE_DELAY * (2 ** attempt)
                        delay = min(delay, MAX_RETRY_DELAY)
                        logger.warning(f"Cerebras stream rate limited, retrying in {delay:.1f}s")
                        await asyncio.sleep(delay)
                        continue
                    if r.status_code >= 400:
                        body = await r.aread()
                        logger.error(f"Cerebras stream error {r.status_code}: {body.decode()[:500]}")
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
                    await asyncio.sleep(_cerebras_interval)
                    return

        raise RuntimeError(f"Cerebras stream failed after {MAX_RETRIES} retries")

    async def aclose(self):
        await self._client.aclose()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        await self.aclose()


class GroqPrimaryClient:
    """Groq API client — used for PRIMARY_MODEL calls (global reasoning, query synthesis)."""

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

        msg_chars = sum(len(m.get("content", "")) for m in payload["messages"])
        logger.info(f"[groq] sending request: model={payload['model']} msgs={len(payload['messages'])} chars={msg_chars}")
        async with _groq_semaphore:
            for attempt in range(MAX_RETRIES):
                r = await self._client.post("/chat/completions", json=payload)
                logger.info(f"[groq] response: status={r.status_code} attempt={attempt}")
                if r.status_code == 429:
                    retry_after = r.headers.get("retry-after")
                    delay = float(retry_after) if retry_after else BASE_DELAY * (2 ** attempt)
                    delay = min(delay, MAX_RETRY_DELAY)
                    logger.warning(f"Groq rate limited (429), retrying in {delay:.1f}s (attempt {attempt + 1}/{MAX_RETRIES})")
                    await asyncio.sleep(delay)
                    continue
                if r.status_code == 400:
                    try:
                        code = r.json().get("error", {}).get("code", "")
                    except Exception:
                        code = ""
                    if code == "json_validate_failed" and "response_format" in payload:
                        try:
                            failed_gen = r.json().get("error", {}).get("failed_generation", "")
                            if failed_gen:
                                logger.warning(f"Groq json_validate_failed — returning failed_generation directly ({len(failed_gen)} chars)")
                                await asyncio.sleep(_groq_interval)
                                return failed_gen
                        except Exception:
                            pass
                        logger.warning("Groq json_validate_failed — no failed_generation, retrying without JSON mode")
                        payload.pop("response_format")
                        msgs = payload["messages"]
                        if msgs and msgs[0].get("role") == "system":
                            msgs[0]["content"] += "\nRespond with valid JSON only. No markdown, no explanation. Start with { end with }."
                        else:
                            msgs.insert(0, {"role": "system", "content": "Respond with valid JSON only. No markdown, no explanation. Start with { end with }."})
                        await asyncio.sleep(_groq_interval)
                        continue
                if r.status_code >= 400:
                    logger.error(f"Groq API error {r.status_code}: {r.text[:500]}")
                r.raise_for_status()
                await asyncio.sleep(_groq_interval)
                return r.json()["choices"][0]["message"]["content"]

            # Final attempt
            r = await self._client.post("/chat/completions", json=payload)
            r.raise_for_status()
            await asyncio.sleep(_groq_interval)
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
        async with _groq_semaphore:
            for attempt in range(MAX_RETRIES + 1):
                async with self._client.stream("POST", "/chat/completions", json=payload) as r:
                    if r.status_code == 429:
                        retry_after = r.headers.get("retry-after")
                        delay = float(retry_after) if retry_after else BASE_DELAY * (2 ** attempt)
                        delay = min(delay, MAX_RETRY_DELAY)
                        logger.warning(f"Groq stream rate limited, retrying in {delay:.1f}s")
                        await asyncio.sleep(delay)
                        continue
                    if r.status_code >= 400:
                        body = await r.aread()
                        logger.error(f"Groq stream error {r.status_code}: {body.decode()[:500]}")
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
                    await asyncio.sleep(_groq_interval)
                    return

        raise RuntimeError(f"Groq stream failed after {MAX_RETRIES} retries")

    async def aclose(self):
        await self._client.aclose()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        await self.aclose()


# Module-level singletons — initialised on first use
_cerebras_client: CerebrasClient | None = None
_groq_primary_client: GroqPrimaryClient | None = None


def get_cerebras_client() -> CerebrasClient:
    global _cerebras_client
    if _cerebras_client is None:
        _cerebras_client = CerebrasClient()
    return _cerebras_client


def get_groq_primary_client() -> GroqPrimaryClient:
    global _groq_primary_client
    if _groq_primary_client is None:
        _groq_primary_client = GroqPrimaryClient()
    return _groq_primary_client
