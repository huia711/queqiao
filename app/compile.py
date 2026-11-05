"""Prepare translated LaTeX for CJK typesetting and compile it with latexmk."""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import signal
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .fonts import cjk_fonts
from .latex import BEGIN_DOCUMENT, DOCUMENTCLASS, RenderedFile, find_uncommented, is_commented, read_tex

MARKER = "% queqiao: CJK support"
PDFTEX_ONLY = re.compile(
    r"^[ \t]*\\(?:pdfoutput|pdfminorversion|pdfsuppresswarningpagegroup|pdfinclusioncopyfonts|pdfobjcompresslevel|"
    r"pdfcompresslevel|pdfpagewidth|pdfpageheight|pdfinfoomitdate|pdftrailerid|pdfglyphtounicode|pdfgentounicode)\b[^\n]*",
    re.MULTILINE,
)
PDFTEX_PACKAGES = {"inputenc", "fontenc", "pdfx", "cmap", "mmap", "glyphtounicode", "pdfcomment", "epstopdf", "CJK", "CJKutf8"}
USEPACKAGE = re.compile(r"\\usepackage\s*(\[[^\]]*\])?\s*\{([^}]*)\}")
CJK_BEGIN = re.compile(r"\\begin\s*\{CJK\*?\}\s*\{[^}]*\}\s*\{[^}]*\}")
CJK_END = re.compile(r"\\end\s*\{CJK\*?\}")
CTEX_CLASSES = {"ctexart", "ctexrep", "ctexbook", "ctexbeamer"}
# soul's \hl/\ul/\st cannot hyphenate CJK text ("Reconstruction failed"); xeCJKfntef draws the same
# effects and handles Chinese. Homebrew TeX Live has no font map for FontAwesome, so name the .otf.
XELATEX_COMPAT = r"""\usepackage{xeCJKfntef}
\makeatletter
\def\queqiao@hlcolor{yellow}
\def\queqiao@soul{%
  \def\sethlcolor##1{\def\queqiao@hlcolor{##1}}%
  \DeclareRobustCommand\hl[1]{\CJKunderline[thickness=1.05em,depth=-0.8em,format=\color{\queqiao@hlcolor},skip=false]{##1}}%
  \DeclareRobustCommand\ul[1]{\CJKunderline{##1}}%
  \DeclareRobustCommand\st[1]{\CJKsout{##1}}%
}
\@ifpackageloaded{soul}{\RequirePackage{xcolor}\AtBeginDocument{\queqiao@soul}}{}
\@ifpackageloaded{fontawesome}{\renewfontfamily{\FA}{FontAwesome}[Extension=.otf]}{}
\makeatother"""
FILE_LINE_ERROR = re.compile(r"^(.+?\.tex):(\d+): (.*)$")
FATAL_HINTS = ("Emergency stop", "Fatal error", "==> Fatal", "job aborted")
AUX_SUFFIXES = {".aux", ".fdb_latexmk", ".fls", ".out", ".toc", ".xdv", ".pdf", ".log", ".run.xml", ".bcf", ".lof", ".lot", ".nav", ".snm", ".vrb"}


@dataclass
class CompileResult:
    ok: bool
    pdf: Path | None
    engine: str
    log_tail: str
    errors: list[tuple[Path, int, str]]
    clean: bool = False
    reverted: list = field(default_factory=list)


def cjk_preamble(engine: str) -> str:
    fonts = cjk_fonts()
    serif = fonts["serif"]
    lines = [MARKER]
    if engine == "xelatex":
        lines.append(r"\usepackage{xeCJK}")
        if serif:
            # Templates may set \defaultfontfeatures{Extension=.otf} (e.g. XCharter), which would turn the
            # CJK family name into a missing file name; keep that out of our CJK font setup only.
            lines.append(r"\begingroup\defaultfontfeatures{}")
            lines.append(rf"\setCJKmainfont{{{serif}}}[AutoFakeBold=2.5,AutoFakeSlant=0.2,ItalicFont={{{fonts['kai'] or serif}}}]")
            lines.append(rf"\setCJKsansfont{{{fonts['sans'] or serif}}}[AutoFakeBold=2.5]")
            lines.append(rf"\setCJKmonofont{{{fonts['sans'] or serif}}}")
            lines.append(r"\endgroup")
        lines.append(r"\xeCJKsetup{CJKspace=true}")
        lines.append(XELATEX_COMPAT)
    else:
        lines.append(r"\usepackage{luatexja-fontspec}")
        if serif:
            lines.append(rf"\setmainjfont{{{serif}}}[AutoFakeBold=2.5,ItalicFont={{{fonts['kai'] or serif}}}]")
            lines.append(rf"\setsansjfont{{{fonts['sans'] or serif}}}[AutoFakeBold=2.5]")
    lines.append(r"\linespread{1.18}\selectfont")
    lines.append(r"\providecommand{\queqiaoCJKready}{}")
    return "\n".join(lines) + "\n"


def adapt_preamble(preamble: str) -> str:
    preamble = PDFTEX_ONLY.sub(lambda match: "% queqiao removed: " + match.group(0).strip(), preamble)

    def rewrite(match: re.Match) -> str:
        if is_commented(preamble, match.start()):
            return match.group(0)
        options, names = match.group(1) or "", match.group(2)
        kept = [name.strip() for name in names.split(",") if name.strip() and name.strip() not in PDFTEX_PACKAGES]
        if not kept:
            return "% queqiao removed: " + match.group(0)
        if len(kept) == len([n for n in names.split(",") if n.strip()]):
            return match.group(0)
        return f"\\usepackage{options}{{{','.join(kept)}}}"

    preamble = USEPACKAGE.sub(rewrite, preamble)
    preamble = CJK_BEGIN.sub("", preamble)
    return CJK_END.sub("", preamble)


def inject_cjk(main: Path, engine: str) -> None:
    text = read_tex(main)
    if MARKER in text:
        text = re.sub(re.escape(MARKER) + r".*?\\providecommand\{\\queqiaoCJKready\}\{\}\n", "", text, flags=re.DOTALL)
    documentclass = find_uncommented(DOCUMENTCLASS, text)
    begin = find_uncommented(BEGIN_DOCUMENT, text)
    if not documentclass or not begin:
        raise RuntimeError("主文件缺少 \\documentclass 或 \\begin{document}")
    class_match = re.compile(r"\\documentclass\s*(?:\[[^\]]*\])?\s*\{([^}]*)\}").match(text, documentclass.start())
    class_name = class_match.group(1).strip() if class_match else ""
    head = text[: begin.start()]
    tail = text[begin.start() :]
    head = adapt_preamble(head)
    tail = CJK_END.sub("", CJK_BEGIN.sub("", tail))
    if class_name not in CTEX_CLASSES:
        head = head.rstrip() + "\n" + cjk_preamble(engine)
    main.write_text(head + tail, "utf-8")


def sanitize_tree(root: Path) -> None:
    for path in root.rglob("*.tex"):
        text = read_tex(path)
        updated = PDFTEX_ONLY.sub(lambda match: "% queqiao removed: " + match.group(0).strip(), text)
        if updated != text:
            path.write_text(updated, "utf-8")


def tail_of(path: Path, limit: int = 6000) -> str:
    if not path.is_file():
        return ""
    data = path.read_bytes()[-limit:]
    return data.decode("utf-8", errors="replace")


def parse_errors(log_path: Path, workdir: Path) -> list[tuple[Path, int, str]]:
    """Extract file:line:message errors (from -file-line-error) from a TeX log."""
    if not log_path.is_file():
        return []
    errors: list[tuple[Path, int, str]] = []
    seen: set[tuple[Path, int]] = set()
    for line in log_path.read_text("utf-8", errors="replace").splitlines():
        match = FILE_LINE_ERROR.match(line.strip())
        if not match:
            continue
        name, number, message = match.group(1), int(match.group(2)), match.group(3)
        path = Path(name)
        if not path.is_absolute():
            path = (workdir / name).resolve()
        key = (path, number)
        if key not in seen:
            seen.add(key)
            errors.append((path, number, message[:200]))
    return errors


def summarize(errors: list[tuple[Path, int, str]], root: Path, limit: int = 6) -> str:
    lines = []
    for path, number, message in errors[:limit]:
        try:
            name = path.relative_to(root.resolve())
        except ValueError:
            name = path.name
        lines.append(f"{name}:{number}: {message}")
    return "\n".join(lines)


async def run_latexmk(root: Path, main: Path, engine: str, timeout: int, log_file: Path) -> CompileResult:
    flag = {"xelatex": "-xelatex", "lualatex": "-lualatex"}[engine]
    cmd = [
        "latexmk",
        flag,
        "-interaction=nonstopmode",
        "-file-line-error",
        "-f",
        "-no-shell-escape",
        "-bibtex-cond1",
        main.name,
    ]
    env = {**os.environ, "openout_any": "p", "max_print_line": "200"}
    with log_file.open("ab") as log:
        log.write(f"\n===== {engine}: {' '.join(cmd)} =====\n".encode())
        log.flush()
        process = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=main.parent,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=log,
            stderr=asyncio.subprocess.STDOUT,
            env=env,
            start_new_session=True,
        )
        try:
            await asyncio.wait_for(process.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            await process.wait()
            log.write(f"\n[queqiao] {engine} 超时（{timeout}s），已终止\n".encode())
    pdf = main.with_suffix(".pdf")
    tex_log = main.with_suffix(".log")
    ok = pdf.is_file() and pdf.stat().st_size > 1000
    errors = parse_errors(tex_log, main.parent)
    log_text = tex_log.read_text("utf-8", errors="replace") if tex_log.is_file() else ""
    fatal = any(hint in log_text for hint in FATAL_HINTS)
    summary = summarize(errors, root)
    if summary:
        with log_file.open("a", encoding="utf-8") as log:
            log.write("\n[queqiao] TeX 错误摘要：\n" + summary + "\n")
    return CompileResult(
        ok=ok and not fatal,
        pdf=pdf if ok else None,
        engine=engine,
        log_tail=summary or tail_of(tex_log, 1500),
        errors=errors,
        clean=ok and not errors and not fatal,
    )


def clear_outputs(main: Path) -> None:
    for stale in main.parent.glob(main.stem + ".*"):
        if stale.suffix in AUX_SUFFIXES and stale != main:
            stale.unlink(missing_ok=True)


def blame(errors: list[tuple[Path, int, str]], rendered: dict[Path, RenderedFile]) -> list:
    culprits = []
    for path, number, _message in errors:
        info = rendered.get(path)
        if info is None:
            continue
        segment = info.culprit(number)
        if segment is not None and segment.translated is not None and segment not in culprits:
            culprits.append(segment)
    return culprits


async def compile_project(
    build_root: Path,
    main: Path,
    timeout: int,
    log_file: Path,
    rendered: dict[Path, RenderedFile] | None = None,
    rerender: Callable[[], dict[Path, RenderedFile]] | None = None,
    max_repairs: int = 4,
) -> CompileResult:
    """Compile with XeLaTeX (LuaLaTeX as fallback).

    When ``rendered``/``rerender`` are given, TeX errors are traced back to translated segments; those
    segments revert to the original English and the document is rebuilt, up to ``max_repairs`` times.
    """
    sanitize_tree(build_root)
    reverted: list = []
    last: CompileResult | None = None
    for engine in ("xelatex", "lualatex"):
        for _attempt in range(max_repairs + 1):
            clear_outputs(main)
            inject_cjk(main, engine)
            last = await run_latexmk(build_root, main, engine, timeout, log_file)
            if last.clean or rendered is None or rerender is None:
                break
            culprits = blame(last.errors, rendered)
            if not culprits:
                break
            for segment in culprits:
                segment.problem = "编译报错，已恢复英文原文"
                segment.translated = None
                reverted.append(segment)
            with log_file.open("a", encoding="utf-8") as log:
                log.write(f"\n[queqiao] {len(culprits)} 个译文段落导致编译错误，已恢复为原文后重新编译\n")
            rendered = rerender()
            sanitize_tree(build_root)
        if last and last.ok:
            break
    assert last is not None
    last.reverted = reverted
    return last


def has_tex() -> bool:
    return shutil.which("latexmk") is not None and shutil.which("xelatex") is not None
