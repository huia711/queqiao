"""Small helpers for non-translation model calls (search, Q&A, guides, digests)."""

from __future__ import annotations

import json
import re

from .config import settings
from .magpie import MagpieClient

JSON_BLOCK = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


async def complete(
    system: str,
    user: str,
    *,
    model: str | None = None,
    temperature: float = 0.3,
    use_cache: bool = True,
) -> str:
    client = MagpieClient(model or settings.helper_model, concurrency=4)
    try:
        return await client.chat(system, user, use_cache=use_cache, temperature=temperature)
    finally:
        await client.close()


def parse_json(text: str):
    """Best-effort JSON extraction from a model reply (fenced block, bare object or array)."""
    text = (text or "").strip()
    candidates = [text]
    fenced = JSON_BLOCK.search(text)
    if fenced:
        candidates.insert(0, fenced.group(1).strip())
    for opener, closer in (("{", "}"), ("[", "]")):
        start, end = text.find(opener), text.rfind(closer)
        if start != -1 and end > start:
            candidates.append(text[start : end + 1])
    for candidate in candidates:
        try:
            return json.loads(candidate)
        except ValueError:
            continue
    raise ValueError("模型没有返回有效的 JSON")


async def complete_json(system: str, user: str, *, model: str | None = None, temperature: float = 0.2, use_cache: bool = True):
    reply = await complete(system, user, model=model, temperature=temperature, use_cache=use_cache)
    try:
        return parse_json(reply)
    except ValueError:
        retry = await complete(
            system,
            user + "\n\n只输出 JSON，不要任何解释或 Markdown。",
            model=model,
            temperature=0,
            use_cache=False,
        )
        return parse_json(retry)
