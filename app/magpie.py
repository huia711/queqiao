from __future__ import annotations

import asyncio
import hashlib
import json
import re
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import httpx

from .config import settings

THINK_BLOCK = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
CODE_FENCE = re.compile(r"^\s*```[a-zA-Z]*\s*\n(.*?)\n?```\s*$", re.DOTALL)


class MagpieError(RuntimeError):
    def __init__(self, message: str, retryable: bool = True):
        super().__init__(message)
        self.retryable = retryable


@dataclass
class Usage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    requests: int = 0
    cache_hits: int = 0

    def as_dict(self) -> dict:
        return {
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "requests": self.requests,
            "cache_hits": self.cache_hits,
        }


class TranslationCache:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS translations (key TEXT PRIMARY KEY, value TEXT NOT NULL, created REAL NOT NULL)"
        )
        self._conn.commit()

    @staticmethod
    def make_key(model: str, system: str, text: str) -> str:
        digest = hashlib.sha256()
        for part in (model, system, text):
            digest.update(part.encode("utf-8"))
            digest.update(b"\x00")
        return digest.hexdigest()

    def get(self, key: str) -> str | None:
        with self._lock:
            row = self._conn.execute("SELECT value FROM translations WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None

    def put(self, key: str, value: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO translations (key, value, created) VALUES (?, ?, ?)",
                (key, value, time.time()),
            )
            self._conn.commit()


_cache: TranslationCache | None = None


def get_cache() -> TranslationCache:
    global _cache
    if _cache is None:
        _cache = TranslationCache(settings.cache_db)
    return _cache


def clean_completion(text: str) -> str:
    text = THINK_BLOCK.sub("", text).strip()
    fenced = CODE_FENCE.match(text)
    if fenced:
        text = fenced.group(1)
    return text.strip("\n")


class MagpieClient:
    def __init__(self, model: str, concurrency: int | None = None):
        self.model = model
        self.usage = Usage()
        self._semaphore = asyncio.Semaphore(concurrency or settings.concurrency)
        self._client = httpx.AsyncClient(
            base_url=settings.magpie_base_url,
            headers={"Authorization": f"Bearer {settings.magpie_api_key}"},
            timeout=httpx.Timeout(settings.request_timeout, connect=10),
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def chat(self, system: str, user: str, *, use_cache: bool = True, temperature: float = 0.2) -> str:
        cache = get_cache()
        key = cache.make_key(self.model, system, user)
        if use_cache:
            cached = cache.get(key)
            if cached is not None:
                self.usage.cache_hits += 1
                return cached
        payload = {
            "model": self.model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "temperature": temperature,
            "stream": False,
        }
        last_error: Exception | None = None
        for attempt in range(4):
            try:
                async with self._semaphore:
                    response = await self._client.post("/chat/completions", json=payload)
                if response.status_code >= 400:
                    retryable = response.status_code in (408, 409, 429) or response.status_code >= 500
                    raise MagpieError(f"Magpie HTTP {response.status_code}: {response.text[:300]}", retryable)
                data = response.json()
                choices = data.get("choices") or []
                if not choices:
                    raise MagpieError(f"Magpie returned no choices: {json.dumps(data)[:300]}")
                content = choices[0].get("message", {}).get("content") or ""
                usage = data.get("usage") or {}
                self.usage.prompt_tokens += int(usage.get("prompt_tokens") or 0)
                self.usage.completion_tokens += int(usage.get("completion_tokens") or 0)
                self.usage.requests += 1
                result = clean_completion(content)
                if not result.strip():
                    raise MagpieError("Magpie returned an empty translation")
                if use_cache:
                    cache.put(key, result)
                return result
            except (httpx.HTTPError, MagpieError, ValueError) as exc:
                last_error = exc
                if isinstance(exc, MagpieError) and not exc.retryable:
                    break
                await asyncio.sleep(1.5 * (attempt + 1))
        if isinstance(last_error, MagpieError):
            raise last_error
        raise MagpieError(str(last_error) if last_error else "Magpie request failed")


async def list_models() -> list[dict]:
    async with httpx.AsyncClient(
        base_url=settings.magpie_base_url,
        headers={"Authorization": f"Bearer {settings.magpie_api_key}"},
        timeout=10,
    ) as client:
        response = await client.get("/models")
        response.raise_for_status()
        data = response.json().get("data") or []
    models = []
    for item in data:
        model_id = item.get("id")
        if not model_id:
            continue
        models.append(
            {
                "id": model_id,
                "name": item.get("magpie_label") or item.get("display_name") or model_id,
            }
        )
    preferred = {name: index for index, name in enumerate(settings.preferred_models)}
    models.sort(key=lambda m: (preferred.get(m["id"], len(preferred)), m["id"]))
    for model in models:
        model["recommended"] = model["id"] in preferred
    return models
