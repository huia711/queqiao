"""Server-side persistence of reader annotations.

Every annotation the reader makes (highlight, text note, ink, stamp) is written into the PDF itself by
PDF.js (``saveDocument``) and uploaded here.  We keep that annotated copy next to the pristine file,
keep a few previous revisions as backups, and extract a JSON index of the notes for the sidebar.
"""

from __future__ import annotations

import json
import shutil
import threading
import time
from pathlib import Path

from .jobs import Job

NOTE_TARGETS = ("translated", "original")
KEEP_REVISIONS = 20
MAX_NOTES_BYTES = 200 * 1024 * 1024
NOTE_SUBTYPES = {"Highlight", "FreeText", "Ink", "Stamp", "Text", "Underline", "StrikeOut", "Squiggly", "Square", "Circle", "Line", "Polygon", "PolyLine", "Caret"}
_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


class NotesError(RuntimeError):
    pass


def _lock(job: Job, target: str) -> threading.Lock:
    key = f"{job.id}:{target}"
    with _locks_guard:
        return _locks.setdefault(key, threading.Lock())


def notes_dir(job: Job) -> Path:
    return job.directory / "notes"


def annotated_path(job: Job, target: str) -> Path:
    return notes_dir(job) / f"{target}.annotated.pdf"


def index_path(job: Job, target: str) -> Path:
    return notes_dir(job) / f"{target}.notes.json"


def _flatten_text(value) -> str:
    if value is None:
        return ""
    return " ".join(str(value).split())


def extract_notes(pdf: Path) -> list[dict]:
    """List every markup annotation with its page, kind, text and the text it covers."""
    import pymupdf

    notes: list[dict] = []
    with pymupdf.open(pdf) as document:
        for page in document:
            for annot in page.annots() or []:
                subtype = annot.type[1] if annot.type else ""
                if subtype not in NOTE_SUBTYPES:
                    continue
                info = annot.info or {}
                rect = annot.rect
                quoted = ""
                if subtype in ("Highlight", "Underline", "StrikeOut", "Squiggly"):
                    vertices = annot.vertices or []
                    pieces = []
                    for index in range(0, len(vertices) - 3, 4):
                        quad = pymupdf.Quad(vertices[index : index + 4])
                        pieces.append(page.get_textbox(quad.rect))
                    quoted = _flatten_text(" ".join(pieces)) or _flatten_text(page.get_textbox(rect))
                colors = annot.colors or {}
                stroke = colors.get("stroke") or colors.get("fill")
                notes.append(
                    {
                        "page": page.number + 1,
                        "kind": subtype,
                        "text": _flatten_text(info.get("content")),
                        "quote": quoted[:600],
                        "color": "#%02x%02x%02x" % tuple(int(max(0, min(1, c)) * 255) for c in stroke[:3]) if stroke and len(stroke) >= 3 else None,
                        "top": round(rect.y0 / max(page.rect.height, 1), 4),
                        "modified": info.get("modDate") or "",
                    }
                )
    notes.sort(key=lambda note: (note["page"], note["top"]))
    return notes


def summary(job: Job) -> dict:
    result = {}
    for target in NOTE_TARGETS:
        path = annotated_path(job, target)
        if not path.is_file():
            result[target] = {"exists": False, "count": 0, "updated": None, "notes": []}
            continue
        notes: list[dict] = []
        index = index_path(job, target)
        if index.is_file():
            try:
                notes = json.loads(index.read_text("utf-8"))
            except ValueError:
                notes = []
        result[target] = {
            "exists": True,
            "count": len(notes),
            "updated": path.stat().st_mtime,
            "notes": notes,
        }
    return result


def save(job: Job, target: str, data: bytes) -> dict:
    if target not in NOTE_TARGETS:
        raise NotesError("只能保存原文或译文的笔记")
    if not data.startswith(b"%PDF-"):
        raise NotesError("上传的笔记文件不是 PDF")
    if len(data) > MAX_NOTES_BYTES:
        raise NotesError("笔记文件过大")
    directory = notes_dir(job)
    directory.mkdir(parents=True, exist_ok=True)
    with _lock(job, target):
        temporary = directory / f".{target}.{time.time_ns()}.tmp"
        temporary.write_bytes(data)
        try:
            notes = extract_notes(temporary)
        except Exception as exc:
            temporary.unlink(missing_ok=True)
            raise NotesError(f"无法解析笔记 PDF：{exc}") from exc
        current = annotated_path(job, target)
        if current.is_file():
            history = directory / "history"
            history.mkdir(exist_ok=True)
            stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(current.stat().st_mtime))
            shutil.copyfile(current, history / f"{target}.{stamp}.pdf")
            revisions = sorted(history.glob(f"{target}.*.pdf"))
            for stale in revisions[:-KEEP_REVISIONS]:
                stale.unlink(missing_ok=True)
        temporary.replace(current)
        index_temporary = index_path(job, target).with_suffix(".json.tmp")
        index_temporary.write_text(json.dumps(notes, ensure_ascii=False, indent=1), "utf-8")
        index_temporary.replace(index_path(job, target))
    return {"target": target, "count": len(notes), "updated": current.stat().st_mtime, "notes": notes}


def clear(job: Job, target: str) -> None:
    if target not in NOTE_TARGETS:
        raise NotesError("未知的笔记目标")
    with _lock(job, target):
        current = annotated_path(job, target)
        if current.is_file():
            history = notes_dir(job) / "history"
            history.mkdir(parents=True, exist_ok=True)
            stamp = time.strftime("%Y%m%d-%H%M%S")
            shutil.move(str(current), history / f"{target}.{stamp}.cleared.pdf")
        index_path(job, target).unlink(missing_ok=True)
