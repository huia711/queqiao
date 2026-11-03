"""Markdown / HTML export of a paper's reading record (guide, highlights, notes, Q&A)."""

from __future__ import annotations

import html
import re
import time

from . import assist, notes
from .jobs import Job

KIND_LABEL = {"Highlight": "高亮", "FreeText": "批注", "Ink": "画笔", "Stamp": "图片", "Text": "便签", "Underline": "下划线", "StrikeOut": "删除线", "Squiggly": "波浪线"}


def _title(job: Job) -> str:
    return (job.meta or {}).get("title") or job.title or job.source_name or job.id


def markdown(job: Job, reader_url: str = "") -> str:
    meta = job.meta or {}
    lines = [f"# {_title(job)}", ""]
    authors = ", ".join(meta.get("authors") or [])
    if authors:
        lines.append(f"- 作者：{authors}")
    if job.arxiv_id:
        lines.append(f"- arXiv：[{job.arxiv_id}](https://arxiv.org/abs/{job.arxiv_id})" + (f" · {meta.get('category')}" if meta.get("category") else ""))
    if meta.get("published"):
        lines.append(f"- 首发：{meta.get('published')}" + (f"；更新：{meta.get('updated')}" if meta.get("updated") not in (None, "", meta.get("published")) else ""))
    if reader_url:
        lines.append(f"- 鹊桥阅读：{reader_url}")
    lines.append(f"- 导出时间：{time.strftime('%Y-%m-%d %H:%M')}")
    lines.append("")

    guide = assist.read_state(job, "guide")
    if guide.get("status") == "done":
        data = guide.get("data") or {}
        lines += ["## 导读", ""]
        if data.get("tldr"):
            lines += [f"> {data['tldr']}", ""]
        for key, label in (("problem", "问题"), ("method", "方法")):
            if data.get(key):
                lines += [f"**{label}**：{data[key]}", ""]
        for key, label in (("contributions", "贡献"), ("results", "关键结果"), ("limitations", "局限"), ("questions", "值得思考")):
            values = [str(item) for item in data.get(key) or [] if item]
            if values:
                lines += [f"**{label}**", *[f"- {value}" for value in values], ""]
        setup = data.get("setup") or {}
        if isinstance(setup, dict) and any(setup.values()):
            names = {"data": "数据/环境", "baselines": "基线", "metrics": "指标", "compute": "规模/算力"}
            lines += ["**实验设置**", *[f"- {names.get(k, k)}：{v}" for k, v in setup.items() if v], ""]
        figures = data.get("figures") or []
        if figures:
            lines += ["**关键图表**", *[f"- {item['label']}（p.{item['page']}）：{item.get('why', '')}" for item in figures], ""]

    summary = notes.summary(job)
    for target, label in (("translated", "译文笔记"), ("original", "原文笔记")):
        items = summary.get(target, {}).get("notes") or []
        if not items:
            continue
        lines += [f"## {label}", ""]
        for note in items:
            head = f"- **p.{note['page']}** · {KIND_LABEL.get(note['kind'], note['kind'])}"
            if note.get("quote"):
                head += f"：“{note['quote']}”"
            lines.append(head)
            if note.get("text"):
                lines.append(f"  - {note['text']}")
        lines.append("")

    history = assist.qa_history(job)
    if history:
        lines += ["## 研读问答", ""]
        for item in history:
            where = f"（p.{item['page']}）" if item.get("page") else ""
            lines.append(f"### {item.get('question', '')[:80]}{where}")
            if item.get("selection"):
                lines += ["", f"> {item['selection']}"]
            lines += ["", item.get("answer", ""), ""]
    return "\n".join(lines).rstrip() + "\n"


def _inline(text: str) -> str:
    text = html.escape(text)
    text = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", text)
    text = re.sub(r"\[([^\]]+)\]\((https?://[^)\s]+)\)", r'<a href="\2">\1</a>', text)
    return text


def markdown_to_html(text: str) -> str:
    """Just enough Markdown for Zotero notes: headings, quotes, bullet lists, bold, links."""
    out: list[str] = []
    in_list = False
    for raw in text.splitlines():
        line = raw.rstrip()
        bullet = re.match(r"^(\s*)- (.*)", line)
        if bullet:
            if not in_list:
                out.append("<ul>")
                in_list = True
            out.append(f"<li>{_inline(bullet.group(2))}</li>")
            continue
        if in_list:
            out.append("</ul>")
            in_list = False
        if not line:
            continue
        heading = re.match(r"^(#{1,3}) (.*)", line)
        if heading:
            level = len(heading.group(1))
            out.append(f"<h{level}>{_inline(heading.group(2))}</h{level}>")
        elif line.startswith("> "):
            out.append(f"<blockquote>{_inline(line[2:])}</blockquote>")
        else:
            out.append(f"<p>{_inline(line)}</p>")
    if in_list:
        out.append("</ul>")
    return "\n".join(out)
