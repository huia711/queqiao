from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import time
import zipfile
from pathlib import Path

from . import arxiv
from .compile import compile_project, has_tex
from .config import ROOT_DIR, settings
from .jobs import Job, store
from .latex import LatexError, apply_translations, collect_project, extract_source, plan_segments, translate_segments
from .magpie import MagpieClient, MagpieError

BABELDOC_STAGE_NAMES = {
    "Parse PDF and Create Intermediate Representation": "解析 PDF",
    "DetectScannedFile": "检测扫描件",
    "Parse Page Layout": "分析版面",
    "Parse Paragraphs": "识别段落",
    "Parse Formulas and Styles": "识别公式与样式",
    "Remove Char Descent": "处理字符",
    "Automatic Term Extraction": "抽取术语",
    "Translate Paragraphs": "翻译段落",
    "Typesetting": "排版",
    "Add Fonts": "嵌入字体",
    "Generate drawing instructions": "生成绘制指令",
    "Subset font": "字体子集化",
    "Save PDF": "保存 PDF",
}


def load_glossary(path: Path | None) -> list[tuple[str, str]]:
    if not path or not path.is_file():
        return []
    entries: list[tuple[str, str]] = []
    import csv

    with path.open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            source = (row.get("source") or "").strip()
            target = (row.get("target") or "").strip()
            if source and target:
                entries.append((source, target))
    return entries[:400]


def default_glossary_path() -> Path:
    return ROOT_DIR / "glossary" / "ai.csv"


def job_glossary(job: Job) -> Path | None:
    custom = job.directory / "glossary.csv"
    if custom.is_file():
        return custom
    default = default_glossary_path()
    return default if default.is_file() else None


def zip_tree(source: Path, dest: Path) -> None:
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(source.rglob("*")):
            if path.is_file() and path.suffix not in (".aux", ".log", ".fls", ".fdb_latexmk", ".xdv", ".synctex.gz"):
                archive.write(path, path.relative_to(source))


async def run_arxiv(job: Job) -> None:
    workdir = job.directory
    async with arxiv.http_client() as client:
        store.update(job, stage="读取论文信息", progress=2)
        meta = await arxiv.fetch_metadata(client, job.arxiv_id)
        store.update(job, meta={**job.meta, **meta}, title=meta.get("title") or job.title, arxiv_id=meta["arxiv_id"])
        store.update(job, stage="下载原文 PDF", progress=4)
        await arxiv.download(client, f"https://arxiv.org/pdf/{job.arxiv_id}", workdir / "original.pdf")
        store.add_artifact(job, "original")
        store.update(job, stage="下载 LaTeX 源码", progress=7)
        source_ok = False
        try:
            await arxiv.download(client, f"https://arxiv.org/e-print/{job.arxiv_id}", workdir / "source.bin")
            source_ok = True
        except Exception as exc:
            store.warn(job, f"源码下载失败，改用 PDF 版式翻译：{exc}")
    if source_ok and has_tex():
        try:
            await translate_latex_job(job)
            return
        except MagpieError:
            raise
        except Exception as exc:
            store.warn(job, f"LaTeX 源码翻译未成功，改用 PDF 版式翻译：{exc}")
    elif source_ok:
        store.warn(job, "本机未安装 latexmk/xelatex，改用 PDF 版式翻译")
    await translate_pdf_job(job, workdir / "original.pdf", progress_base=10)


class LatexCompileError(RuntimeError):
    pass


async def translate_latex_job(job: Job) -> None:
    workdir = job.directory
    source_dir = workdir / "src"
    build_dir = workdir / "build"
    for directory in (source_dir, build_dir):
        if directory.exists():
            shutil.rmtree(directory)
    store.update(job, stage="解析 LaTeX 源码", progress=9, pipeline="latex")
    extract_source(workdir / "source.bin", source_dir)
    project = collect_project(source_dir)
    plan_segments(project)
    segments = project.segments
    if not segments:
        raise LatexError("源码中没有找到可翻译的正文")
    chars = sum(len(segment.masked) for segment in segments)
    store.update(
        job,
        stage=f"翻译正文（0/{len(segments)} 段）",
        progress=12,
        meta={**job.meta, "main_tex": str(project.main.relative_to(source_dir.resolve())), "segments": len(segments), "chars": chars},
    )
    client = MagpieClient(job.model)
    last_update = 0.0

    def progress(done: int, total: int) -> None:
        nonlocal last_update
        now = time.time()
        if now - last_update < 0.8 and done < total:
            return
        last_update = now
        store.update(job, stage=f"翻译正文（{done}/{total} 段）", progress=12 + 70 * done / max(total, 1), usage=client.usage.as_dict())

    try:
        stats = await translate_segments(
            segments,
            client,
            title=job.meta.get("title", ""),
            glossary=load_glossary(job_glossary(job)),
            budget=settings.chunk_chars,
            progress=progress,
        )
    finally:
        await client.close()
    store.update(job, usage={**client.usage.as_dict(), **stats.as_dict()})
    if stats.translated == 0:
        sample = stats.failures[0] if stats.failures else "未知原因"
        raise MagpieError(f"没有任何段落翻译成功，请检查 Magpie 模型 {job.model} 是否可用（{sample[:200]}）", retryable=False)
    if stats.failed:
        store.warn(job, f"{stats.failed} 个段落未通过格式校验，已保留英文原文")
    store.update(job, stage="生成译文源码", progress=83)
    shutil.copytree(source_dir, build_dir)
    rendered = apply_translations(project, source_dir, build_dir)
    main = build_dir / project.main.relative_to(source_dir.resolve())
    store.update(job, stage="编译中文 PDF（XeLaTeX）", progress=86)
    log_file = workdir / "build.log"
    result = await compile_project(
        build_dir,
        main,
        settings.latex_timeout,
        log_file,
        rendered=rendered,
        rerender=lambda: apply_translations(project, source_dir, build_dir),
    )
    store.add_artifact(job, "log")
    if not result.ok or not result.pdf:
        raise LatexCompileError(f"编译失败：{result.log_tail[:400]}")
    if result.reverted:
        store.warn(job, f"{len(result.reverted)} 个段落的译文会导致编译错误，已自动恢复为英文原文")
    if not result.clean:
        store.warn(job, "编译时仍有 TeX 报错，PDF 已生成，但个别位置的排版可能有偏差，详见构建日志")
    shutil.copyfile(result.pdf, workdir / "translated.pdf")
    store.add_artifact(job, "translated")
    zip_tree(build_dir, workdir / "translated-source.zip")
    store.add_artifact(job, "source")
    store.update(job, pipeline="latex", meta={**job.meta, "engine": result.engine})


async def translate_pdf_job(job: Job, pdf: Path, progress_base: float = 3) -> None:
    workdir = job.directory
    store.update(job, stage="PDF 版式翻译准备中", progress=progress_base, pipeline="babeldoc")
    out_dir = workdir / "babeldoc-out"
    tmp_dir = workdir / "babeldoc-work"
    out_dir.mkdir(parents=True, exist_ok=True)
    tmp_dir.mkdir(parents=True, exist_ok=True)
    cmd = [
        sys.executable,
        "-m",
        "app.pdf_worker",
        "--input",
        str(pdf),
        "--output-dir",
        str(out_dir),
        "--work-dir",
        str(tmp_dir),
        "--model",
        job.model,
        "--base-url",
        settings.magpie_base_url,
        "--api-key",
        settings.magpie_api_key,
        "--qps",
        str(settings.pdf_qps),
    ]
    glossary = job_glossary(job)
    if glossary:
        cmd += ["--glossary", str(glossary)]
    if job.meta.get("pages"):
        cmd += ["--pages", str(job.meta["pages"])]
    log_path = workdir / "build.log"
    with log_path.open("ab") as stderr_log:
        process = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=ROOT_DIR,
            stdout=asyncio.subprocess.PIPE,
            stderr=stderr_log,
            env={**os.environ, "PYTHONUNBUFFERED": "1"},
            limit=1024 * 1024,
        )
        store.add_artifact(job, "log")
        finish: dict | None = None
        error: str | None = None
        try:
            assert process.stdout is not None
            async for raw in process.stdout:
                try:
                    event = json.loads(raw.decode("utf-8", errors="replace"))
                except ValueError:
                    continue
                kind = event.get("type")
                if kind == "progress":
                    stage = BABELDOC_STAGE_NAMES.get(event.get("stage") or "", event.get("stage") or "处理中")
                    current, total = event.get("current"), event.get("total")
                    label = f"{stage}（{current}/{total}）" if total else stage
                    overall = float(event.get("progress") or 0)
                    store.update(job, stage=label, progress=progress_base + (99 - progress_base) * overall / 100)
                elif kind == "stage":
                    store.update(job, stage=event.get("stage") or job.stage)
                elif kind == "finish":
                    finish = event
                elif kind == "error":
                    error = event.get("error")
            await process.wait()
        except asyncio.CancelledError:
            process.kill()
            await process.wait()
            raise
    if error or not finish:
        raise RuntimeError(error or f"PDF 翻译进程异常退出（退出码 {process.returncode}）")
    outputs = finish.get("outputs") or {}
    if "translated" not in outputs:
        raise RuntimeError("BabelDOC 没有生成译文 PDF")
    for name, filename in outputs.items():
        shutil.move(str(out_dir / filename), workdir / filename)
        store.add_artifact(job, name)
    shutil.rmtree(tmp_dir, ignore_errors=True)
    shutil.rmtree(out_dir, ignore_errors=True)
    store.update(job, usage=finish.get("usage") or {}, meta={**job.meta, "babeldoc_seconds": finish.get("seconds")})


async def run_upload(job: Job) -> None:
    pdf = job.directory / "original.pdf"
    await translate_pdf_job(job, pdf)


def runner_for(job: Job):
    return run_arxiv if job.kind == "arxiv" else run_upload
