"""Reading assistant: ask about a selection, reading guide card, reference TL;DRs.

Guides and reference overviews run as background tasks (they can take a minute) and are cached in
``data/jobs/<id>/assist/``. Every model call sees the extracted text of the original paper.
"""

from __future__ import annotations

import asyncio
import difflib
import json
import re
import time

import httpx

from . import arxiv, llm, papertext
from .config import settings
from .jobs import Job
from .magpie import MagpieError

MAX_QA = 200
MAX_REFERENCES = 80
_tasks: dict[str, asyncio.Task] = {}
_title_cache: dict[str, str] = {}

ASK_SYSTEM = """你是论文研读助手，帮助中文读者读懂一篇英文学术论文。
你会拿到论文全文（每页以 [p.N] 标记）、读者选中的文字（可能是中文译文）和问题。
要求：
- 用简洁的中文回答，术语保留英文；先给结论，再展开，通常不超过 300 字。
- 引用论文内容时标注页码，例如 [p.3]。
- 论文里没有的信息要明确说“论文未提及”；可以补充背景知识，但要标注“（背景知识）”。
- 可以使用 **加粗**、列表和行内公式，不要使用标题。"""

MODE_PROMPTS = {
    "explain": "请解释这段话：它在说什么、涉及哪些概念或符号，以及它在论文论证中起什么作用。",
    "plain": "请把这段话改写成通俗、口语化的中文，让非这个小方向的研究者也能看懂；必要时给一个小例子。",
    "critique": "请从审稿人的角度看这段话：它的论点是否有充分的证据支撑？有哪些隐含假设或可能的问题？",
}

GUIDE_SYSTEM = """你是资深的 AI 方向研究者，为一篇论文写中文导读卡片，帮助读者快速判断读什么、怎么读。
只输出 JSON：
{"tldr": "一句话概括论文做了什么、效果如何",
 "problem": "它解决什么问题，现有方法哪里不足（2-3 句）",
 "contributions": ["主要贡献，每条一句"],
 "method": "核心方法的关键机制（3-5 句，具体到做法）",
 "setup": {"data": "数据集/环境/任务", "baselines": "对比方法", "metrics": "指标", "compute": "模型规模或算力，没有就留空"},
 "results": ["最有判断价值的结果，保留数字和对比条件，每条标注页码 [p.N]"],
 "limitations": ["局限：包括作者承认的和你看到的（后者标注“（推断）”）"],
 "figures": [{"label": "Figure 1 或 Table 2，必须来自给定的图表列表", "why": "为什么值得看"}],
 "questions": ["读完值得思考或验证的问题，2-3 条"]}
要求：只依据论文内容，不要夸大；figures 选 3-5 个最关键的图表。"""

REF_PARSE_SYSTEM = """把论文的参考文献列表解析成 JSON。只输出 JSON：
{"refs": [{"title": "英文标题", "authors": "第一作者姓名，多人加 et al.", "year": "2023", "venue": "会议或期刊，没有就留空", "arxiv": "原文中出现的 arXiv 编号，没有就留空"}]}
按原顺序输出所有条目；只摘录，不要编造或补全信息。"""

REF_ROLE_SYSTEM = """你在阅读一篇论文。给定论文正文和它的参考文献列表，说明正文是如何使用每篇文献的。
只输出 JSON：{"roles": [{"n": 编号, "role": "一句中文，例如“作为主要对比基线”“沿用其评测协议”“作为动机：指出其无法处理…”", "weight": "core / related / minor"}]}
weight 的标准（要严格）：
- core：本文的方法直接建立在其上，或它是主要对比基线、被改进的对象。通常只有 3-8 篇，不超过总数的五分之一。
- related：沿用的评测基准、数据集、工具，或在相关工作里有实质讨论的工作。
- minor：只在背景或一长串引用里顺带提到。
正文里找不到依据时 role 留空，weight 记为 minor。"""

REF_TLDR_SYSTEM = """根据每篇论文的摘要，用中文写出 TL;DR 和核心动机。只输出 JSON：
{"items": [{"n": 编号, "tldr": "一句话：做了什么、得到什么结果", "motivation": "一句话：它想解决的核心问题或不足"}]}
只依据摘要，术语保留英文。"""


def _state_path(job: Job, kind: str):
    return papertext.assist_dir(job) / f"{kind}.json"


def read_state(job: Job, kind: str) -> dict:
    path = _state_path(job, kind)
    try:
        state = json.loads(path.read_text("utf-8"))
    except (OSError, ValueError):
        return {"status": "none"}
    key = f"{job.id}:{kind}"
    if state.get("status") == "running" and key not in _tasks:
        state = {**state, "status": "failed", "error": "服务重启时任务被中断，请重新生成"}
    return state


def _write_state(job: Job, kind: str, state: dict) -> None:
    path = _state_path(job, kind)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps({**state, "updated": time.time()}, ensure_ascii=False, indent=1), "utf-8")
    temporary.replace(path)


def start(job: Job, kind: str, force: bool = False) -> dict:
    """Start building the guide or reference overview unless it is cached or already running."""
    key = f"{job.id}:{kind}"
    state = read_state(job, kind)
    if key in _tasks or (state.get("status") == "done" and not force):
        return state
    builder = {"guide": build_guide, "references": build_references}[kind]
    model = settings.helper_model
    _write_state(job, kind, {"status": "running", "stage": "准备中", "model": model})

    async def run() -> None:
        try:
            data = await builder(job, model, lambda stage: _write_state(job, kind, {"status": "running", "stage": stage, "model": model}))
            _write_state(job, kind, {"status": "done", "model": model, "data": data})
        except Exception as exc:  # noqa: BLE001 - reported to the reader
            _write_state(job, kind, {"status": "failed", "model": model, "error": str(exc)[:400] or exc.__class__.__name__})
        finally:
            _tasks.pop(key, None)

    _tasks[key] = asyncio.create_task(run())
    return read_state(job, kind)


def _require_text(job: Job) -> list[str]:
    pages = papertext.page_texts(job)
    if not any(page.strip() for page in pages):
        raise ValueError("还没有可读取的原文（PDF 尚未下载，或是扫描件）")
    return pages


def _paper_header(job: Job) -> str:
    meta = job.meta or {}
    title = meta.get("title") or job.title or ""
    authors = ", ".join((meta.get("authors") or [])[:8])
    return f"论文：{title}\n作者：{authors}\n"


# ---------------------------------------------------------------------------
# Questions about a selection
# ---------------------------------------------------------------------------


def qa_history(job: Job) -> list[dict]:
    try:
        items = json.loads((papertext.assist_dir(job) / "qa.json").read_text("utf-8"))
    except (OSError, ValueError):
        return []
    return items if isinstance(items, list) else []


def _save_qa(job: Job, item: dict) -> None:
    items = [*qa_history(job), item][-MAX_QA:]
    path = papertext.assist_dir(job) / "qa.json"
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(items, ensure_ascii=False, indent=1), "utf-8")
    temporary.replace(path)


def delete_qa(job: Job, item_id: str) -> None:
    items = [item for item in qa_history(job) if item.get("id") != item_id]
    path = papertext.assist_dir(job) / "qa.json"
    path.write_text(json.dumps(items, ensure_ascii=False, indent=1), "utf-8")


async def ask(job: Job, selection: str, question: str, mode: str, page: int | None, side: str, thread: list[dict], asker: str) -> dict:
    selection = (selection or "").strip()[:3000]
    question = (question or "").strip()[:1500]
    if mode not in MODE_PROMPTS and not question:
        raise ValueError("请输入问题")
    pages = await asyncio.to_thread(_require_text, job)
    body = papertext.body_text(pages)
    prompt = MODE_PROMPTS.get(mode) if mode in MODE_PROMPTS and not question else question
    parts = [_paper_header(job), "【论文全文】", body, ""]
    for turn in (thread or [])[-6:]:
        q, a = str(turn.get("q", ""))[:1500], str(turn.get("a", ""))[:3000]
        if q and a:
            parts += [f"【之前的提问】{q}", f"【之前的回答】{a}", ""]
    if selection:
        where = f"（{'译文' if side == 'translated' else '原文'}第 {page} 页）" if page else ""
        parts += [f"【读者选中的文字{where}】", selection, ""]
    parts += ["【问题】", prompt]
    answer = await llm.complete(ASK_SYSTEM, "\n".join(parts), temperature=0.3)
    item = {
        "id": f"{int(time.time() * 1000):x}",
        "time": time.time(),
        "mode": mode if mode in MODE_PROMPTS and not question else "ask",
        "question": prompt,
        "selection": selection,
        "page": page,
        "side": side,
        "answer": answer,
        "model": settings.helper_model,
        "by": asker,
    }
    await asyncio.to_thread(_save_qa, job, item)
    return item


# ---------------------------------------------------------------------------
# Reading guide
# ---------------------------------------------------------------------------


async def build_guide(job: Job, model: str, progress) -> dict:
    progress("提取论文文字")
    pages = await asyncio.to_thread(_require_text, job)
    caps = papertext.captions(pages)
    listing = "\n".join(f"- {item['label']}（p.{item['page']}）：{item['caption'][:160]}" for item in caps) or "（未识别到图表标题）"
    appendix = papertext.appendix_text(pages, 20_000)
    user = "\n".join(
        [
            _paper_header(job),
            "【图表列表】",
            listing,
            "",
            "【论文全文】",
            papertext.body_text(pages),
            *(["", "【附录节选】", appendix] if appendix else []),
        ]
    )
    progress("大模型阅读全文")
    data = await llm.complete_json(GUIDE_SYSTEM, user, model=model, use_cache=False)
    if not isinstance(data, dict):
        raise ValueError("模型返回的导读格式不正确")
    known = {_figure_key(item["label"]): item for item in caps}
    figures = []
    for item in data.get("figures") or []:
        if not isinstance(item, dict):
            continue
        match = known.get(_figure_key(str(item.get("label", ""))))
        if match and all(existing["label"] != match["label"] for existing in figures):
            figures.append({"label": match["label"], "page": match["page"], "caption": match["caption"], "why": str(item.get("why", ""))})
    data["figures"] = figures
    data["figure_index"] = caps
    data["pages"] = len(pages)
    return data


def _figure_key(label: str) -> str:
    """'Figure 3: …', 'Fig. 3', '图 3' -> 'figure3'; 'Table 2', '表 2' -> 'table2'."""
    match = re.search(r"(fig(?:ure)?\.?|table|图|表)\s*(\d+[a-z]?)", label or "", re.IGNORECASE)
    if not match:
        return ""
    kind = "table" if match.group(1).lower() in ("table", "表") else "figure"
    return f"{kind}{match.group(2).lower()}"


# ---------------------------------------------------------------------------
# References overview
# ---------------------------------------------------------------------------


def _norm_title(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()


async def _resolve_title(client: httpx.AsyncClient, title: str) -> dict | None:
    norm = _norm_title(title)
    if len(norm) < 12:
        return None
    if norm in _title_cache:
        cached = _title_cache[norm]
        return json.loads(cached) if cached else None
    words = [word for word in norm.split() if len(word) > 2][:10]
    query = " AND ".join(f"ti:{word}" for word in words)
    best = None
    try:
        entries, _ = await arxiv.search(client, query, max_results=5)
    except (httpx.HTTPError, ValueError):
        return None
    for entry in entries:
        ratio = difflib.SequenceMatcher(None, norm, _norm_title(entry["title"])).ratio()
        if ratio >= 0.9 and (best is None or ratio > best[0]):
            best = (ratio, entry)
    _title_cache[norm] = json.dumps(best[1]) if best else ""
    return best[1] if best else None


def _chunks(text: str, size: int) -> list[str]:
    blocks = re.split(r"\n\s*\n|(?=\[ref\])", text)
    chunks, current = [], ""
    for block in blocks:
        if len(current) + len(block) > size and current:
            chunks.append(current)
            current = ""
        current += block + "\n\n"
    if current.strip():
        chunks.append(current)
    return chunks


async def build_references(job: Job, model: str, progress) -> dict:
    progress("提取参考文献")
    pages = await asyncio.to_thread(_require_text, job)
    raw = papertext.reference_text(job, pages)
    if len(raw.strip()) < 200:
        raise ValueError("没有找到参考文献列表")
    chunks = _chunks(raw, 14_000)
    progress(f"解析参考文献（{len(chunks)} 段）")
    parsed = await asyncio.gather(*(llm.complete_json(REF_PARSE_SYSTEM, chunk, model=model) for chunk in chunks), return_exceptions=True)
    refs: list[dict] = []
    for part in parsed:
        if isinstance(part, Exception):
            continue
        for item in (part.get("refs") if isinstance(part, dict) else part) or []:
            if isinstance(item, dict) and str(item.get("title", "")).strip():
                refs.append(item)
    if not refs:
        raise MagpieError("模型没有解析出参考文献", retryable=False)
    truncated = len(refs) > MAX_REFERENCES
    refs = refs[:MAX_REFERENCES]
    for index, item in enumerate(refs, 1):
        item["n"] = index
        candidate = arxiv.parse_arxiv_id(str(item.get("arxiv") or ""))
        item["arxiv"] = candidate if candidate and arxiv.base_id(candidate) in raw else ""
    progress("分析每篇文献在本文中的作用")
    listing = "\n".join(f"[{item['n']}] {item.get('authors', '')} ({item.get('year', '')}). {item['title']}" for item in refs)
    roles_task = asyncio.create_task(
        llm.complete_json(REF_ROLE_SYSTEM, f"{_paper_header(job)}\n【参考文献】\n{listing}\n\n【正文】\n{papertext.body_text(pages, 120_000)}", model=model)
    )
    resolved: dict[int, dict] = {}
    async with arxiv.http_client() as client:
        with_ids = {item["n"]: item["arxiv"] for item in refs if item["arxiv"]}
        if with_ids:
            found = await arxiv.fetch_many(client, list(with_ids.values()))
            for number, arxiv_id in with_ids.items():
                if arxiv.base_id(arxiv_id) in found:
                    resolved[number] = found[arxiv.base_id(arxiv_id)]
        pending = [item for item in refs if item["n"] not in resolved]
        for done, item in enumerate(pending, 1):
            if done % 5 == 1:
                progress(f"在 arXiv 上查找文献（{done}/{len(pending)}）")
            entry = await _resolve_title(client, item["title"])
            if entry:
                resolved[item["n"]] = entry
    progress(f"生成 TL;DR（{len(resolved)} 篇有摘要）")
    numbers = sorted(resolved)
    batches = [numbers[start : start + 12] for start in range(0, len(numbers), 12)]

    async def summarize(batch: list[int]):
        text = "\n\n".join(f"[{number}] {resolved[number]['title']}\n{resolved[number]['abstract'][:1500]}" for number in batch)
        return await llm.complete_json(REF_TLDR_SYSTEM, text, model=model)

    summaries: dict[int, dict] = {}
    for result in await asyncio.gather(*(summarize(batch) for batch in batches), return_exceptions=True):
        if isinstance(result, Exception):
            continue
        for item in (result.get("items") if isinstance(result, dict) else result) or []:
            try:
                summaries[int(item.get("n"))] = item
            except (TypeError, ValueError, AttributeError):
                continue
    roles: dict[int, dict] = {}
    try:
        reply = await roles_task
        for item in (reply.get("roles") if isinstance(reply, dict) else reply) or []:
            try:
                roles[int(item.get("n"))] = item
            except (TypeError, ValueError, AttributeError):
                continue
    except (MagpieError, ValueError):
        pass
    items = []
    for item in refs:
        number = item["n"]
        entry = resolved.get(number)
        summary = summaries.get(number, {})
        role = roles.get(number, {})
        items.append(
            {
                "n": number,
                "title": entry["title"] if entry else item["title"],
                "authors": item.get("authors", ""),
                "year": str(item.get("year") or (entry["published"][:4] if entry else "")),
                "venue": item.get("venue", ""),
                "arxiv_id": arxiv.base_id(entry["arxiv_id"]) if entry else None,
                "tldr": summary.get("tldr") if entry else None,
                "motivation": summary.get("motivation") if entry else None,
                "role": role.get("role") or "",
                "weight": role.get("weight") if role.get("weight") in ("core", "related", "minor") else "related",
            }
        )
    return {"items": items, "total": len(items), "on_arxiv": len(resolved), "truncated": truncated}


async def auto_guide(job: Job) -> None:
    """Generate the reading guide after a translation finishes (best effort)."""
    if read_state(job, "guide").get("status") in ("done", "running"):
        return
    try:
        start(job, "guide")
    except Exception:  # noqa: BLE001 - optional feature
        pass
