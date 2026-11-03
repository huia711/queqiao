"""Fuzzy paper search: the model turns a description into arXiv queries, then reranks real results.

Candidates always come from the arXiv API, so the model can only choose among papers that exist; it
never supplies IDs or titles itself.
"""

from __future__ import annotations

import re
import time

import httpx

from . import arxiv, llm
from .magpie import MagpieError

DOI = re.compile(r"\b10\.\d{4,9}/[^\s\"<>]+", re.IGNORECASE)
CACHE_SECONDS = 900
_cache: dict[str, tuple[float, dict]] = {}

PLAN_SYSTEM = """你是学术检索助手，把用户对论文的描述转换成 arXiv 检索计划。
用户可能给出：不完整的标题、中文描述、参考文献条目、作者和年份、模型或 benchmark 名称。
只输出 JSON：
{"title": "如果能确定英文标题就填写，否则为空字符串",
 "authors": ["可能的作者姓氏，没有就留空"],
 "groups": [["英文关键词或短语", "..."], ["另一种说法", "..."]],
 "year_from": null, "year_to": null}
规则：
- groups 给 2-4 组，每组 2-3 个英文词或短语，同组内是 AND 关系；不同组用不同的同义说法。
- 短语要是论文摘要里可能出现的词，例如 "GUI agent"、"benchmark"、"video reasoning"。
- 不确定的内容不要写，不要编造标题或作者。"""

RANK_SYSTEM = """你是学术检索助手。给定用户的查询和一组真实的 arXiv 候选论文，
挑出最符合查询的论文并排序。只能使用候选列表里的编号。
只输出 JSON：{"results": [{"n": 候选序号, "score": 0-100, "reason": "一句中文，说明为什么匹配，尽量引用摘要里的具体点"}]}
最多 8 条，只保留 score >= 30 的；如果都不相关就返回空列表。"""


def _term(text: str) -> str:
    text = re.sub(r"[^0-9A-Za-z\- .+]", " ", str(text))
    return " ".join(text.split())[:80]


def _phrase(field: str, text: str) -> str:
    term = _term(text)
    if not term:
        return ""
    return f'{field}:"{term}"' if " " in term else f"{field}:{term}"


def _year_filter(plan: dict) -> str:
    def year(value):
        try:
            number = int(value)
        except (TypeError, ValueError):
            return None
        return number if 1991 <= number <= 2100 else None

    start, end = year(plan.get("year_from")), year(plan.get("year_to"))
    if not start and not end:
        return ""
    return f" AND submittedDate:[{start or 1991}01010000 TO {end or 2100}12312359]"


def build_queries(plan: dict, raw: str) -> list[str]:
    queries: list[str] = []
    years = _year_filter(plan)
    title = _term(plan.get("title") or "")
    if len(title) > 8:
        queries.append(_phrase("ti", title))
        words = [word for word in title.split() if len(word) > 3][:6]
        if len(words) >= 2:
            queries.append(" AND ".join(f"ti:{word}" for word in words))
    authors = [_term(name).split(" ")[-1] for name in plan.get("authors") or [] if _term(name)]
    for group in (plan.get("groups") or [])[:4]:
        if isinstance(group, str):
            group = [group]
        parts = [_phrase("all", item) for item in group[:2]]
        parts = [part for part in parts if part]
        if authors:
            parts.append(_phrase("au", authors[0]))
        if parts:
            queries.append(" AND ".join(parts) + years)
    ascii_raw = _term(raw)
    if not queries and len(ascii_raw) > 3:
        queries.append(" AND ".join(f"all:{word}" for word in ascii_raw.split()[:8]))
    return list(dict.fromkeys(query for query in queries if query))[:6]


def broad_query(plan: dict) -> str:
    """OR of every phrase in the plan, used when the focused queries find too little."""
    phrases = []
    for group in plan.get("groups") or []:
        for item in [group] if isinstance(group, str) else group:
            phrase = _phrase("abs", item)
            if phrase:
                phrases.append(phrase)
    phrases = list(dict.fromkeys(phrases))[:8]
    return " OR ".join(phrases)


async def _doi_title(client: httpx.AsyncClient, doi: str) -> str:
    try:
        response = await client.get(f"https://api.crossref.org/works/{doi}", timeout=15)
        response.raise_for_status()
        titles = response.json().get("message", {}).get("title") or []
        return titles[0] if titles else ""
    except (httpx.HTTPError, ValueError):
        return ""


def _brief(paper: dict) -> dict:
    return {
        "arxiv_id": paper["arxiv_id"],
        "title": paper["title"],
        "authors": paper["authors"][:6],
        "author_count": len(paper["authors"]),
        "published": paper["published"],
        "updated": paper["updated"],
        "category": paper["category"],
        "abstract": paper["abstract"],
        "abs_url": paper["abs_url"],
    }


async def search(text: str) -> dict:
    text = " ".join((text or "").split())[:600]
    if len(text) < 2:
        raise ValueError("请输入至少两个字")
    cached = _cache.get(text)
    if cached and time.time() - cached[0] < CACHE_SECONDS:
        return cached[1]
    started = time.time()
    async with arxiv.http_client() as client:
        direct = arxiv.parse_arxiv_id(text)
        if direct:
            paper = await arxiv.fetch_metadata(client, direct)
            result = {"query": text, "mode": "id", "results": [{**_brief(paper), "reason": "输入中包含 arXiv 编号", "score": 100}]}
            _cache[text] = (time.time(), result)
            return result
        hint = text
        doi = DOI.search(text)
        if doi:
            title = await _doi_title(client, doi.group(0).rstrip(".,;)"))
            if title:
                hint = f"{text}\nDOI 对应的标题：{title}"
        plan: dict = {}
        notes: list[str] = []
        try:
            reply = await llm.complete_json(PLAN_SYSTEM, hint)
            plan = reply if isinstance(reply, dict) else {}
        except (MagpieError, ValueError) as exc:
            notes.append(f"大模型改写查询失败，改用关键词直接检索（{str(exc)[:80]}）")
        queries = build_queries(plan, text if not doi else hint)
        if not queries:
            raise ValueError("没能从输入中提取出可检索的英文关键词，请换一种说法")
        pool: dict[str, dict] = {}
        for query in queries:
            try:
                entries, _ = await arxiv.search(client, query, max_results=8)
            except (httpx.HTTPError, ValueError):
                continue
            for entry in entries:
                pool.setdefault(arxiv.base_id(entry["arxiv_id"]), entry)
        broad = broad_query(plan)
        if len(pool) < 8 and broad:
            queries.append(broad)
            try:
                entries, _ = await arxiv.search(client, broad + _year_filter(plan), max_results=15)
            except (httpx.HTTPError, ValueError):
                entries = []
            for entry in entries:
                pool.setdefault(arxiv.base_id(entry["arxiv_id"]), entry)
    candidates = list(pool.values())[:30]
    if not candidates:
        result = {"query": text, "mode": "search", "queries": queries, "results": [], "notes": notes}
        _cache[text] = (time.time(), result)
        return result
    listing = "\n\n".join(
        f"[{index}] {paper['title']} ({paper['published'][:4]}; {', '.join(paper['authors'][:3])})\n{paper['abstract'][:420]}"
        for index, paper in enumerate(candidates)
    )
    ranked: list[dict] = []
    try:
        reply = await llm.complete_json(RANK_SYSTEM, f"用户查询：{text}\n\n候选论文：\n{listing}")
        for item in (reply.get("results") if isinstance(reply, dict) else reply) or []:
            try:
                index = int(item.get("n"))
            except (TypeError, ValueError, AttributeError):
                continue
            if 0 <= index < len(candidates) and all(entry["arxiv_id"] != candidates[index]["arxiv_id"] for entry in ranked):
                ranked.append(
                    {
                        **_brief(candidates[index]),
                        "reason": str(item.get("reason") or "")[:200],
                        "score": max(0, min(100, int(item.get("score") or 0))),
                    }
                )
    except (MagpieError, ValueError) as exc:
        notes.append(f"大模型排序失败，按 arXiv 相关度展示（{str(exc)[:80]}）")
        ranked = [{**_brief(paper), "reason": "", "score": None} for paper in candidates[:8]]
    result = {
        "query": text,
        "mode": "search",
        "queries": queries,
        "candidates": len(candidates),
        "results": ranked[:8],
        "notes": notes,
        "seconds": round(time.time() - started, 1),
    }
    _cache[text] = (time.time(), result)
    if len(_cache) > 200:
        for key in sorted(_cache, key=lambda name: _cache[name][0])[:50]:
            _cache.pop(key, None)
    return result
