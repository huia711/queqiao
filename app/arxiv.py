from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from pathlib import Path

import httpx

NEW_STYLE_ID = re.compile(r"(?<![\d.])(\d{4}\.\d{4,5})(v\d+)?(?!\d)")
OLD_STYLE_ID = re.compile(r"(?<![A-Za-z])([a-z][a-z\-]+(?:\.[A-Z]{2})?/\d{7})(v\d+)?")
ATOM = {"a": "http://www.w3.org/2005/Atom", "arxiv": "http://arxiv.org/schemas/atom"}
USER_AGENT = "queqiao/0.2 (self-hosted paper translator)"
MAX_DOWNLOAD_BYTES = 300 * 1024 * 1024


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


async def fetch_metadata(client: httpx.AsyncClient, arxiv_id: str) -> dict:
    response = await client.get(
        "https://export.arxiv.org/api/query", params={"id_list": arxiv_id, "max_results": 1}
    )
    response.raise_for_status()
    root = ET.fromstring(response.content)
    entry = root.find("a:entry", ATOM)
    if entry is None:
        raise ArxivError(f"未找到 arXiv 论文 {arxiv_id}")
    entry_id = (entry.findtext("a:id", default="", namespaces=ATOM) or "").strip()
    if "/abs/" not in entry_id:
        raise ArxivError(f"未找到 arXiv 论文 {arxiv_id}")
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


async def download(client: httpx.AsyncClient, url: str, dest: Path) -> str:
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
