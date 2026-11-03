"""Send a paper and its reading record to Zotero through the Zotero Web API.

The item is matched against the existing library by arXiv ID / title first, so papers already in
Zotero get a child note instead of a duplicate entry. The note is updated in place on later syncs.
Credentials: user ID and a key with write access from https://www.zotero.org/settings/keys.
"""

from __future__ import annotations

import re

import httpx

from . import arxiv, export
from .config import settings
from .jobs import Job, store

API = "https://api.zotero.org"
COLLECTION_KEY = re.compile(r"^[A-Z0-9]{8}$")


class ZoteroError(RuntimeError):
    pass


def configured() -> bool:
    return bool(settings.zotero_user_id.strip() and settings.zotero_api_key.strip())


def _client() -> httpx.AsyncClient:
    if not configured():
        raise ZoteroError("还没有配置 Zotero：请在设置页填写 User ID 和 API Key")
    user = settings.zotero_user_id.strip()
    if not user.isdigit():
        raise ZoteroError("Zotero User ID 应该是一串数字（在 zotero.org/settings/keys 页面可以看到）")
    return httpx.AsyncClient(
        base_url=f"{API}/users/{user}",
        headers={"Zotero-API-Key": settings.zotero_api_key.strip(), "Zotero-API-Version": "3"},
        timeout=30,
    )


def _check(response: httpx.Response) -> httpx.Response:
    if response.status_code in (401, 403):
        raise ZoteroError("Zotero 拒绝访问：请确认 API Key 有效，并且勾选了“Allow write access”")
    if response.status_code >= 400:
        raise ZoteroError(f"Zotero 返回 HTTP {response.status_code}：{response.text[:200]}")
    return response


def _created_key(response: httpx.Response) -> str:
    data = _check(response).json()
    success = data.get("successful") or {}
    if "0" in success:
        return success["0"]["key"]
    failed = (data.get("failed") or {}).get("0") or {}
    raise ZoteroError(f"Zotero 创建条目失败：{failed.get('message', data)}")


async def test() -> dict:
    async with _client() as client:
        response = _check(await client.get(f"{API}/keys/current"))
        access = response.json().get("access", {}).get("user", {})
        if not access.get("write"):
            raise ZoteroError("这个 API Key 没有写入权限，请在 Zotero 网站上勾选“Allow write access”")
        items = _check(await client.get("/items/top", params={"limit": 1, "format": "keys"}))
        return {"ok": True, "username": response.json().get("username"), "items": int(items.headers.get("Total-Results", "0") or 0)}


async def _collection(client: httpx.AsyncClient) -> str | None:
    wanted = settings.zotero_collection.strip()
    if not wanted:
        return None
    if COLLECTION_KEY.match(wanted):
        return wanted
    start = 0
    while True:
        response = _check(await client.get("/collections", params={"limit": 100, "start": start}))
        batch = response.json()
        for item in batch:
            if item.get("data", {}).get("name") == wanted:
                return item["key"]
        if len(batch) < 100:
            break
        start += 100
    return _created_key(await client.post("/collections", json=[{"name": wanted}]))


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (text or "").lower())


async def _find_existing(client: httpx.AsyncClient, job: Job) -> str | None:
    meta = job.meta or {}
    title = meta.get("title") or job.title or ""
    wanted_id = arxiv.base_id(job.arxiv_id) if job.arxiv_id else ""
    queries = [query for query in (wanted_id, title[:120]) if query]
    for query in queries:
        response = _check(await client.get("/items/top", params={"q": query, "qmode": "everything", "limit": 25}))
        for item in response.json():
            data = item.get("data", {})
            if data.get("itemType") in ("note", "attachment"):
                continue
            blob = " ".join(str(data.get(field, "")) for field in ("url", "archiveID", "extra", "DOI", "number"))
            if wanted_id and wanted_id in blob:
                return item["key"]
            if title and _norm(data.get("title")) == _norm(title):
                return item["key"]
    return None


def _creators(names: list[str]) -> list[dict]:
    creators = []
    for name in names[:60]:
        parts = name.rsplit(" ", 1)
        if len(parts) == 2:
            creators.append({"creatorType": "author", "firstName": parts[0], "lastName": parts[1]})
        else:
            creators.append({"creatorType": "author", "name": name})
    return creators


def _item(job: Job, collection: str | None) -> dict:
    meta = job.meta or {}
    tags = [{"tag": "鹊桥"}]
    if job.arxiv_id:
        base = arxiv.base_id(job.arxiv_id)
        item = {
            "itemType": "preprint",
            "title": meta.get("title") or job.title,
            "creators": _creators(meta.get("authors") or []),
            "abstractNote": meta.get("abstract", ""),
            "date": meta.get("published", ""),
            "repository": "arXiv",
            "archiveID": f"arXiv:{base}",
            "url": f"https://arxiv.org/abs/{base}",
            "DOI": f"10.48550/arXiv.{base}",
            "tags": tags + ([{"tag": meta["category"]}] if meta.get("category") else []),
        }
    else:
        item = {"itemType": "document", "title": job.title or job.source_name or job.id, "tags": tags}
    if collection:
        item["collections"] = [collection]
    return item


async def _upsert_note(client: httpx.AsyncClient, parent: str, note_key: str | None, html: str) -> str:
    if note_key:
        response = await client.get(f"/items/{note_key}")
        if response.status_code == 200 and not response.json().get("data", {}).get("deleted"):
            version = response.json().get("version")
            _check(await client.patch(f"/items/{note_key}", json={"note": html}, headers={"If-Unmodified-Since-Version": str(version)}))
            return note_key
    return _created_key(await client.post("/items", json=[{"itemType": "note", "parentItem": parent, "note": html, "tags": [{"tag": "鹊桥笔记"}]}]))


async def sync(job: Job, reader_url: str) -> dict:
    """Create or update the Zotero item for ``job`` with a note holding the reading record."""
    meta = dict(job.meta or {})
    record = meta.get("zotero") or {}
    html = "<h1>鹊桥研读记录</h1>\n" + export.markdown_to_html(export.markdown(job, reader_url).split("\n", 1)[1])
    async with _client() as client:
        parent = record.get("item")
        if parent:
            response = await client.get(f"/items/{parent}")
            if response.status_code != 200 or response.json().get("data", {}).get("deleted"):
                parent = None
        created = False
        if not parent:
            parent = await _find_existing(client, job)
        if not parent:
            parent = _created_key(await client.post("/items", json=[_item(job, await _collection(client))]))
            created = True
        same_parent = record.get("item") == parent
        note = await _upsert_note(client, parent, record.get("note") if same_parent else None, html)
        if reader_url and (not same_parent or not record.get("link")):
            link = {"itemType": "attachment", "linkMode": "linked_url", "parentItem": parent, "title": "鹊桥双语对照阅读", "url": reader_url, "tags": []}
            try:
                record["link"] = _created_key(await client.post("/items", json=[link]))
            except ZoteroError:
                record["link"] = None
        user = settings.zotero_user_id.strip()
    record.update({"item": parent, "note": note, "user": user})
    store.update(job, meta={**job.meta, "zotero": record})
    return {"ok": True, "item": parent, "note": note, "created": created, "url": f"https://www.zotero.org/users/{user}/items/{parent}", "open": f"zotero://select/library/items/{parent}"}
