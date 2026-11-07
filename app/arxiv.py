from __future__ import annotations

import asyncio
import re
import time
import xml.etree.ElementTree as ET
from pathlib import Path

import httpx

NEW_STYLE_ID = re.compile(r"(?<![\d.])(\d{4}\.\d{4,5})(v\d+)?(?!\d)")
OLD_STYLE_ID = re.compile(r"(?<![A-Za-z])([a-z][a-z\-]+(?:\.[A-Z]{2})?/\d{7})(v\d+)?")
ATOM = {"a": "http://www.w3.org/2005/Atom", "arxiv": "http://arxiv.org/schemas/atom"}
USER_AGENT = "queqiao/0.2 (self-hosted paper translator)"
MAX_DOWNLOAD_BYTES = 300 * 1024 * 1024
API_URL = "https://export.arxiv.org/api/query"
# arXiv asks API clients to space out requests; all queries share this gate.
API_INTERVAL = 1.0
_api_lock = asyncio.Lock()
_api_last = 0.0
# A brief network drop (VPN switch, Wi-Fi roaming) should not fail a whole translation job.
RETRY_DELAYS = (2, 5, 12)
RETRY_STATUS = {429, 500, 502, 503, 504}


def _retryable(exc: Exception) -> bool:
    if isinstance(exc, httpx.TransportError):
        return True
    return isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code in RETRY_STATUS


class ArxivError(RuntimeError):
    pass


def parse_arxiv_id(text: str) -> str | None:
    text = (text or "").strip()
    match = NEW_STYLE_ID.search(text)
    if match:
        return match.group(1) + (match.group(2) or "")
    match = OLD_STYLE_ID.search(text)
    if match:
        return match.group(1) + (match.group(2) or "")
    return None


def base_id(arxiv_id: str) -> str:
    return re.sub(r"v\d+$", "", arxiv_id)


def http_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        headers={"User-Agent": USER_AGENT},
        timeout=httpx.Timeout(90, connect=15),
        follow_redirects=True,
    )


def parse_entry(entry: ET.Element) -> dict | None:
    entry_id = (entry.findtext("a:id", default="", namespaces=ATOM) or "").strip()
    if "/abs/" not in entry_id:
        return None
    resolved = entry_id.split("/abs/", 1)[1]
    primary = entry.find("arxiv:primary_category", ATOM)
    return {
        "arxiv_id": resolved,
        "title": " ".join((entry.findtext("a:title", default="", namespaces=ATOM) or "").split()),
        "authors": [
            " ".join((author.findtext("a:name", default="", namespaces=ATOM) or "").split())
            for author in entry.findall("a:author", ATOM)
        ],
        "abstract": " ".join((entry.findtext("a:summary", default="", namespaces=ATOM) or "").split()),
        "published": (entry.findtext("a:published", default="", namespaces=ATOM) or "")[:10],
        "updated": (entry.findtext("a:updated", default="", namespaces=ATOM) or "")[:10],
        "category": primary.get("term") if primary is not None else "",
        "abs_url": f"https://arxiv.org/abs/{resolved}",
    }


async def api_query(client: httpx.AsyncClient, params: dict) -> tuple[list[dict], int]:
    """Call the arXiv Atom API (rate limited); returns entries and the total hit count."""
    global _api_last
    for delay in (*RETRY_DELAYS, None):
        try:
            async with _api_lock:
                wait = API_INTERVAL - (time.monotonic() - _api_last)
                if wait > 0:
                    await asyncio.sleep(wait)
                try:
                    response = await client.get(API_URL, params=params)
                finally:
                    _api_last = time.monotonic()
            response.raise_for_status()
            break
        except (httpx.TransportError, httpx.HTTPStatusError) as exc:
            if delay is None or not _retryable(exc):
                raise
            await asyncio.sleep(delay)
    root = ET.fromstring(response.content)
    total_text = root.findtext("{http://a9.com/-/spec/opensearch/1.1/}totalResults") or "0"
    entries = [item for item in (parse_entry(entry) for entry in root.findall("a:entry", ATOM)) if item]
    return entries, int(total_text) if total_text.isdigit() else len(entries)


async def fetch_metadata(client: httpx.AsyncClient, arxiv_id: str) -> dict:
    entries, _ = await api_query(client, {"id_list": arxiv_id, "max_results": 1})
    if not entries:
        raise ArxivError(f"未找到 arXiv 论文 {arxiv_id}")
    return entries[0]


async def fetch_many(client: httpx.AsyncClient, ids: list[str]) -> dict[str, dict]:
    """Metadata for many IDs, keyed by the base ID (without version)."""
    found: dict[str, dict] = {}
    unique = list(dict.fromkeys(base_id(item) for item in ids if item))
    for start in range(0, len(unique), 50):
        chunk = unique[start : start + 50]
        entries, _ = await api_query(client, {"id_list": ",".join(chunk), "max_results": len(chunk)})
        for entry in entries:
            found[base_id(entry["arxiv_id"])] = entry
    return found


async def search(
    client: httpx.AsyncClient, query: str, max_results: int = 10, sort: str = "relevance", start: int = 0
) -> tuple[list[dict], int]:
    params = {
        "search_query": query,
        "max_results": max_results,
        "start": start,
        "sortBy": sort,
        "sortOrder": "descending",
    }
    return await api_query(client, params)


async def download(client: httpx.AsyncClient, url: str, dest: Path) -> str:
    for delay in (*RETRY_DELAYS, None):
        try:
            return await _download_once(client, url, dest)
        except (httpx.TransportError, httpx.HTTPStatusError) as exc:
            if delay is None or not _retryable(exc):
                raise
            await asyncio.sleep(delay)
    raise ArxivError(f"下载失败：{url}")


async def _download_once(client: httpx.AsyncClient, url: str, dest: Path) -> str:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".part")
    total = 0
    async with client.stream("GET", url) as response:
        if response.status_code == 404:
            raise ArxivError(f"arXiv 返回 404：{url}")
        response.raise_for_status()
        content_type = response.headers.get("content-type", "")
        with tmp.open("wb") as handle:
            async for chunk in response.aiter_bytes():
                total += len(chunk)
                if total > MAX_DOWNLOAD_BYTES:
                    raise ArxivError("下载内容超过 300MB，已停止")
                handle.write(chunk)
    tmp.replace(dest)
    return content_type
