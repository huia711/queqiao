"""Plain text of a paper for the reading assistant: pages, captions and the reference list."""

from __future__ import annotations

import json
import re
from pathlib import Path

from .jobs import Job

CAPTION = re.compile(r"^\s*((?:Figure|Fig\.|Table|图|表)\s*\d+[a-z]?)\s*[.:：|]?\s*(.{0,220})", re.IGNORECASE)
REFERENCE_HEADING = re.compile(r"^\s*(?:\d+\.?\s*)?(References|Bibliography|REFERENCES|BIBLIOGRAPHY|Literature Cited)\s*$", re.MULTILINE)
APPENDIX_HEADING = re.compile(r"^\s*(?:Appendix|APPENDIX|Supplementary Material|A\s+[A-Z][A-Za-z ]{3,60})\s*$", re.MULTILINE)


def assist_dir(job: Job) -> Path:
    path = job.directory / "assist"
    path.mkdir(parents=True, exist_ok=True)
    return path


def source_pdf(job: Job) -> Path | None:
    path = job.directory / "original.pdf"
    return path if path.is_file() else None


def page_texts(job: Job) -> list[str]:
    """Text of every page of the original PDF (cached next to the job)."""
    pdf = source_pdf(job)
    if not pdf:
        return []
    cache = assist_dir(job) / "pages.json"
    stamp = pdf.stat().st_mtime
    try:
        cached = json.loads(cache.read_text("utf-8"))
        if cached.get("mtime") == stamp:
            return cached["pages"]
    except (OSError, ValueError, KeyError):
        pass
    import pymupdf

    with pymupdf.open(pdf) as document:
        pages = [page.get_text("text") or "" for page in document]
    cache.write_text(json.dumps({"mtime": stamp, "pages": pages}, ensure_ascii=False), "utf-8")
    return pages


def _reference_start(pages: list[str]) -> tuple[int, int] | None:
    """(page index, offset) of the last References heading in the paper."""
    for index in range(len(pages) - 1, -1, -1):
        matches = list(REFERENCE_HEADING.finditer(pages[index]))
        if matches:
            return index, matches[-1].start()
    return None


def body_text(pages: list[str], limit: int = 160_000) -> str:
    """Page-tagged body text without the reference list, trimmed to ``limit`` characters."""
    start = _reference_start(pages)
    parts = []
    for index, text in enumerate(pages):
        if start and index == start[0]:
            text = text[: start[1]]
        elif start and index > start[0]:
            text = ""
        text = re.sub(r"[ \t]+", " ", text).strip()
        if text:
            parts.append(f"[p.{index + 1}]\n{text}")
    joined = "\n\n".join(parts)
    if len(joined) > limit:
        joined = joined[:limit] + "\n\n[…后文省略…]"
    return joined


def appendix_text(pages: list[str], limit: int = 30_000) -> str:
    start = _reference_start(pages)
    if not start:
        return ""
    tail = pages[start[0]][start[1] :] + "\n" + "\n".join(pages[start[0] + 1 :])
    match = APPENDIX_HEADING.search(tail)
    return tail[match.start() :][:limit] if match else ""


def captions(pages: list[str]) -> list[dict]:
    found: dict[str, dict] = {}
    for index, text in enumerate(pages):
        for line in text.splitlines():
            match = CAPTION.match(line)
            if not match or len(match.group(2).strip()) < 8:
                continue
            label = re.sub(r"\s+", " ", match.group(1)).replace("Fig.", "Figure").title()
            found.setdefault(label, {"label": label, "page": index + 1, "caption": match.group(2).strip()})
    return list(found.values())[:80]


def _clean_bbl(text: str) -> str:
    text = re.sub(r"(?<!\\)%.*", "", text)
    text = re.sub(r"\\bibitem(\[[^\]]*\])?\{[^}]*\}", "\n\n[ref]", text)
    text = re.sub(r"\\newblock", " ", text)
    text = re.sub(r"\\(?:em|it|bf|sc|textit|textbf|emph|url|href|doi)\b", " ", text)
    text = re.sub(r"\\[a-zA-Z]+\*?", " ", text)
    text = re.sub(r"[{}~]", " ", text)
    return re.sub(r"[ \t]+", " ", text)


def reference_text(job: Job, pages: list[str], limit: int = 60_000) -> str:
    """The paper's reference list: cleaned .bbl from the LaTeX source if present, else PDF text."""
    source = job.directory / "src"
    if source.is_dir():
        bbls = sorted(source.rglob("*.bbl"), key=lambda path: path.stat().st_size, reverse=True)
        if bbls:
            try:
                cleaned = _clean_bbl(bbls[0].read_text("utf-8", errors="replace"))
            except OSError:
                cleaned = ""
            if cleaned.count("[ref]") >= 3:
                return cleaned[:limit]
    start = _reference_start(pages)
    if not start:
        return ""
    tail = pages[start[0]][start[1] :] + "\n" + "\n".join(pages[start[0] + 1 :])
    match = APPENDIX_HEADING.search(tail, 200)
    if match:
        tail = tail[: match.start()]
    return tail[:limit]
