"""Daily arXiv digest tuned to a research profile.

Each run pulls recent submissions for the profile's arXiv queries, lets the model screen and summarise
them against the stated interests, and stores the result as ``data/digest/YYYY-MM-DD.json``. Papers
already reported on earlier days are skipped. The default profile follows the owner's daily research
radar (GUI / computer-use / memory / CLI agents, harnesses, benchmarks and agent safety).
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from datetime import UTC, datetime, timedelta

import httpx

from . import arxiv, llm
from .config import settings
from .magpie import MagpieError

BATCH = 15
MAX_CANDIDATES = 240
KEEP_SEEN = 5000
# Keep the reading list short: at most this many "must read" and "watch" items per day.
MAX_MUST = 6
MAX_WATCH = 18
_task: asyncio.Task | None = None
_progress: dict = {}

DEFAULT_PROFILE = {
    "interests": """核心方向：
A. Agent 能力与系统：GUI Agent / Computer-Use Agent / Web Agent / OS Agent；Memory Agent（长程记忆）；CLI / terminal / code / SWE agent；tool-use agent；harness（planning / reasoning / reflection）；面向 (GUI) agent 的 benchmark / environment / evaluation；self-improvement / test-time training / RSI / harness evolving。
B. Agent 安全：GUI / Computer-Use / Browser-Use agent 的安全与新攻击（prompt injection、indirect prompt injection、视觉注入等）；护栏模型；harness 防御；安全与对齐。
C. 高影响力上游：LLM / VLM / Agent 新训练范式（agent RL、RLVR、test-time training）、VLM reasoning、新 benchmark 与评测协议、能改变社区默认做法的方法学论文。
筛选：高影响力优先于完全相关；重视揭示现有方法重要不足、可迁移的新机制或新评测方式、能改变判断的实验证据、开放的模型/数据/环境。
降权：纯工程补丁、只换 backbone 或小幅调参、与主题只共享关键词、缺乏技术细节的宣传。""",
    "topics": [
        {"name": "GUI / Computer-Use / Web Agent", "query": 'abs:"GUI agent" OR abs:"computer-use agent" OR abs:"computer use agent" OR abs:"web agent" OR abs:"browser agent" OR abs:"OS agent" OR abs:"mobile agent"'},
        {"name": "Memory Agent", "query": '(abs:"agent memory" OR abs:"memory agent" OR abs:"long-term memory" OR abs:"long-horizon memory") AND abs:agent'},
        {"name": "CLI / SWE / Tool-use Agent", "query": 'abs:"terminal agent" OR abs:"software engineering agent" OR abs:"SWE-bench" OR abs:"coding agent" OR abs:"tool-use agent" OR abs:"tool use agent" OR abs:"agent harness"'},
        {"name": "Self-improvement / Agent RL", "query": '(abs:"self-improving" OR abs:"self-evolving" OR abs:"test-time training" OR abs:"agentic reinforcement learning" OR abs:"agentic RL" OR abs:RLVR) AND (abs:agent OR abs:agents)'},
        {"name": "Agent Benchmark / Environment", "query": '(abs:benchmark OR abs:environment OR abs:evaluation) AND (abs:"LLM agents" OR abs:"language agents" OR abs:"agentic tasks" OR abs:"computer use")'},
        {"name": "Agent Safety", "query": 'abs:"prompt injection" OR abs:"agent safety" OR abs:"agent security" OR abs:"guardrail model" OR (abs:jailbreak AND abs:agent)'},
        {"name": "VLM Reasoning", "query": '(abs:"vision-language model" OR abs:VLM OR abs:MLLM OR abs:"multimodal large language model") AND (abs:reasoning OR abs:"reinforcement learning")'},
    ],
    "categories": ["cs.AI", "cs.CL", "cs.CV", "cs.LG", "cs.CR", "cs.SE", "cs.HC", "cs.MA", "cs.RO", "cs.IR"],
    "days": 3,
}

SCREEN_SYSTEM = """你是研究者的“每日科研前沿雷达”。根据下面的研究兴趣，筛选一批新的 arXiv 论文。
【研究兴趣与筛选标准】
{interests}

对每篇论文给出：
- score：0-100，综合研究价值、与兴趣的相关性和证据强度（仅凭标题和摘要判断，保持克制）。
- tier："must"（优先精读：与主线直接相关且方法、问题定义或证据有明显价值，或上游影响足够大）/ "watch"（值得关注）/ "brief"（简讯）/ "skip"（不相关或价值低）。
- area：所属方向，从“Agent 能力”“Agent 安全”“上游进展”中选一个。
- tldr：一句中文，说清问题和核心机制，不要只翻译摘要。
- why：一句中文，对这位研究者的意义（问题定义 / 方法 / 实验设计 / 评测资源 / 安全启示）；skip 可留空。
- caveat：从摘要看最需要核实的地方（可留空）。
避免“重磅”“颠覆”等词。不要因为新术语或刷分就判定为 must；must 应当很少，一批里通常不超过 1-2 篇。
只输出 JSON：{{"items": [{{"n": 序号, "score": 0, "tier": "skip", "area": "", "tldr": "", "why": "", "caveat": ""}}]}}"""

OVERVIEW_SYSTEM = """你是研究者的“每日科研前沿雷达”。根据今天入选的论文写一段中文总览（120-220 字）：
概括最值得注意的进展，指出应优先阅读的 1-3 篇及原因（用论文短名指代）。
高价值信息有限时直接说明，不要用空泛的趋势判断填充；这些判断只基于摘要，要克制。只输出这段文字。"""


def _dir():
    path = settings.data_dir / "digest"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _read(path, default):
    try:
        return json.loads(path.read_text("utf-8"))
    except (OSError, ValueError):
        return default


def _write(path, data) -> None:
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=1), "utf-8")
    temporary.replace(path)


def profile() -> dict:
    stored = _read(_dir() / "profile.json", {})
    merged = {**DEFAULT_PROFILE, **(stored if isinstance(stored, dict) else {})}
    merged["custom"] = bool(stored)
    return merged


def save_profile(data: dict) -> dict:
    interests = str(data.get("interests") or "").strip()[:6000]
    topics = []
    for item in data.get("topics") or []:
        name = " ".join(str(item.get("name", "")).split())[:60]
        query = " ".join(str(item.get("query", "")).split())[:600]
        if name and query:
            topics.append({"name": name, "query": query})
    if not interests or not topics:
        raise ValueError("研究兴趣和至少一个检索主题不能为空")
    categories = [c.strip() for c in data.get("categories") or [] if re.fullmatch(r"[a-z\-]+(\.[A-Za-z\-]+)?", str(c).strip())]
    days = max(1, min(7, int(data.get("days") or 3)))
    _write(_dir() / "profile.json", {"interests": interests, "topics": topics[:12], "categories": categories, "days": days})
    return profile()


def reset_profile() -> dict:
    (_dir() / "profile.json").unlink(missing_ok=True)
    return profile()


def dates() -> list[str]:
    return sorted((path.stem for path in _dir().glob("20??-??-??.json")), reverse=True)


def load(day: str) -> dict | None:
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day or ""):
        return None
    data = _read(_dir() / f"{day}.json", None)
    return data if isinstance(data, dict) else None


def status() -> dict:
    return {"running": bool(_task and not _task.done()), **_progress}


def _seen() -> dict:
    data = _read(_dir() / "seen.json", {})
    return data if isinstance(data, dict) else {}


def _date_window(days: int) -> tuple[str, str]:
    end = datetime.now(UTC)
    start = end - timedelta(days=days)
    return start.strftime("%Y%m%d%H%M"), end.strftime("%Y%m%d%H%M")


async def _collect(prof: dict) -> tuple[list[dict], list[str], dict]:
    start, end = _date_window(int(prof.get("days") or 3))
    window = f" AND submittedDate:[{start} TO {end}]"
    categories = prof.get("categories") or []
    category_filter = f" AND ({' OR '.join(f'cat:{c}' for c in categories)})" if categories else ""
    pool: dict[str, dict] = {}
    problems: list[str] = []
    counts: dict[str, int] = {}
    async with arxiv.http_client() as client:
        for topic in prof["topics"]:
            _progress["stage"] = f"检索：{topic['name']}"
            query = f"({topic['query']}){category_filter}{window}"
            try:
                entries, total = await arxiv.search(client, query, max_results=60, sort="submittedDate")
            except (httpx.HTTPError, ValueError) as exc:
                problems.append(f"{topic['name']} 检索失败：{str(exc)[:120]}")
                continue
            counts[topic["name"]] = total
            for entry in entries:
                key = arxiv.base_id(entry["arxiv_id"])
                if key in pool:
                    pool[key].setdefault("topics", []).append(topic["name"])
                else:
                    pool[key] = {**entry, "topics": [topic["name"]]}
    return list(pool.values())[:MAX_CANDIDATES], problems, {"window": [start, end], "hits": counts}


async def _screen(papers: list[dict], interests: str, model: str) -> dict[int, dict]:
    system = SCREEN_SYSTEM.format(interests=interests)
    results: dict[int, dict] = {}
    batches = [list(range(start, min(start + BATCH, len(papers)))) for start in range(0, len(papers), BATCH)]
    semaphore = asyncio.Semaphore(4)
    done = 0

    async def run(batch: list[int]) -> None:
        nonlocal done
        listing = "\n\n".join(
            f"[{index}] {papers[index]['title']}\n分类：{papers[index]['category']}；作者：{', '.join(papers[index]['authors'][:4])}\n{papers[index]['abstract'][:1100]}"
            for index in batch
        )
        async with semaphore:
            try:
                reply = await llm.complete_json(system, listing, model=model)
            except (MagpieError, ValueError):
                reply = {}
        for item in (reply.get("items") if isinstance(reply, dict) else reply) or []:
            try:
                number = int(item.get("n"))
            except (TypeError, ValueError, AttributeError):
                continue
            if number in batch:
                results[number] = item
        done += 1
        _progress["stage"] = f"大模型筛选（{done}/{len(batches)} 批）"

    await asyncio.gather(*(run(batch) for batch in batches))
    return results


async def build(day: str | None = None, force: bool = False) -> dict:
    day = day or time.strftime("%Y-%m-%d")
    prof = profile()
    model = settings.helper_model
    started = time.time()
    _progress.update({"stage": "准备中", "day": day, "error": None, "started": started})
    seen = _seen()
    candidates, problems, window = await _collect(prof)
    fresh = [paper for paper in candidates if force or seen.get(arxiv.base_id(paper["arxiv_id"]), day) == day]
    skipped = len(candidates) - len(fresh)
    screened = await _screen(fresh, prof["interests"], model) if fresh else {}
    items = []
    for index, paper in enumerate(fresh):
        verdict = screened.get(index)
        if not verdict:
            continue
        tier = verdict.get("tier") if verdict.get("tier") in ("must", "watch", "brief", "skip") else "skip"
        try:
            score = max(0, min(100, int(verdict.get("score") or 0)))
        except (TypeError, ValueError):
            score = 0
        if tier == "skip":
            continue
        version = re.search(r"v(\d+)$", paper["arxiv_id"])
        items.append(
            {
                "arxiv_id": arxiv.base_id(paper["arxiv_id"]),
                "version": int(version.group(1)) if version else 1,
                "title": paper["title"],
                "authors": paper["authors"][:8],
                "author_count": len(paper["authors"]),
                "category": paper["category"],
                "published": paper["published"],
                "updated": paper["updated"],
                "event": "新论文" if paper["published"] == paper["updated"] else "版本更新",
                "abstract": paper["abstract"],
                "topics": paper.get("topics", []),
                "tier": tier,
                "score": score,
                "area": str(verdict.get("area") or "")[:20],
                "tldr": str(verdict.get("tldr") or "")[:300],
                "why": str(verdict.get("why") or "")[:300],
                "caveat": str(verdict.get("caveat") or "")[:300],
            }
        )
    order = {"must": 0, "watch": 1, "brief": 2}
    items.sort(key=lambda item: -item["score"])
    counts = {"must": 0, "watch": 0}
    for item in items:
        if item["tier"] == "must" and counts["must"] >= MAX_MUST:
            item["tier"] = "watch"
        if item["tier"] == "watch" and counts["watch"] >= MAX_WATCH:
            item["tier"] = "brief"
        if item["tier"] in counts:
            counts[item["tier"]] += 1
    items.sort(key=lambda item: (order[item["tier"]], -item["score"]))
    overview = ""
    if items:
        _progress["stage"] = "撰写今日总览"
        top = items[:12]
        listing = "\n".join(f"- [{item['tier']}] {item['title']}：{item['tldr']}（{item['why']}）" for item in top)
        try:
            overview = await llm.complete(OVERVIEW_SYSTEM, listing, model=model, use_cache=False)
        except MagpieError as exc:
            problems.append(f"总览生成失败：{str(exc)[:120]}")
    if len(fresh) and not screened:
        raise MagpieError("大模型筛选全部失败，请检查 Magpie 是否可用", retryable=False)
    for paper in fresh:
        seen.setdefault(arxiv.base_id(paper["arxiv_id"]), day)
    if len(seen) > KEEP_SEEN:
        seen = dict(sorted(seen.items(), key=lambda pair: pair[1])[-KEEP_SEEN:])
    _write(_dir() / "seen.json", seen)
    report = {
        "day": day,
        "generated": time.time(),
        "seconds": round(time.time() - started),
        "model": model,
        "window": window["window"],
        "hits": window["hits"],
        "candidates": len(candidates),
        "already_reported": skipped,
        "screened": len(screened),
        "overview": overview,
        "items": items,
        "problems": problems,
    }
    _write(_dir() / f"{day}.json", report)
    return report


def start(force: bool = False) -> dict:
    global _task
    if _task and not _task.done():
        return status()

    async def run() -> None:
        try:
            await build(force=force)
            _progress.update({"stage": "完成", "error": None})
        except Exception as exc:  # noqa: BLE001 - surfaced in the UI
            _progress.update({"stage": "失败", "error": str(exc)[:300] or exc.__class__.__name__})

    _task = asyncio.create_task(run())
    return status()


async def scheduler() -> None:
    """Build today's digest once the configured hour has passed; retry every 30 minutes on failure."""
    while True:
        try:
            hour = int(settings.digest_hour)
            today = time.strftime("%Y-%m-%d")
            if 0 <= hour <= 23 and time.localtime().tm_hour >= hour and not load(today) and not (_task and not _task.done()):
                start()
                while _task and not _task.done():
                    await asyncio.sleep(10)
                if not load(today):
                    await asyncio.sleep(1800)
                    continue
        except Exception:  # noqa: BLE001 - never let the scheduler die
            pass
        await asyncio.sleep(300)
