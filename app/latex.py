"""Translate arXiv LaTeX sources through Magpie while protecting every non-prose construct.

Pipeline: extract archive -> find the main file and its \\input tree -> scan bodies into prose
segments (math, citations, labels and unknown macros are masked as ⟦n⟧ atoms) -> batch-translate
-> validate each fragment (placeholders, braces, commands) -> render translations back in place.
"""

from __future__ import annotations

import asyncio
import bisect
import gzip
import hashlib
import io
import json
import re
import tarfile
import zipfile
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Callable

from .magpie import MagpieClient, MagpieError, get_cache

ProgressFn = Callable[[int, int], None]

PROMPT_VERSION = "latex-v3"
PLACEHOLDER_RE = re.compile(r"⟦\s*(\d+)\s*⟧")
BLANK_LINE = re.compile(r"\n[ \t]*\n(?:[ \t]*\n)*")
CS_NAME = re.compile(r"[A-Za-z@]+\*?")
CONTROL_SEQUENCE = re.compile(r"\\(?:[A-Za-z@]+|.)", re.DOTALL)
CITE_OR_REF = re.compile(r"^(?:[A-Za-z]*cite[A-Za-z]*|[A-Za-z]*ref|labelcref)$")
SIMPLE_ARG = re.compile(r"^[^\s\\{}$]*$")
PARAM_ARG = re.compile(r"^[^\s{}]*$")
PROSE_WORD = re.compile(r"[A-Za-z]{3,}")
SEG_RE = re.compile(r"<seg\s+id\s*=\s*[\"']?(\d+)[\"']?\s*>(.*?)</seg>", re.DOTALL)
CJK_CHAR = r"[\u2e80-\u9fff\uf900-\ufaff\uff00-\uffef\u3000-\u303f]"
MACRO_BEFORE_CJK = re.compile(r"(\\[A-Za-z@]+)(?=" + CJK_CHAR + ")")
CONTROL_WORD_END = re.compile(r"\\[A-Za-z@]+$")
BACKSLASH_NON_ASCII = re.compile(r"(?<!\\)\\[^\x00-\x7f]")
IF_TOKEN = re.compile(r"\\(?:if[A-Za-z@]*|fi)(?![A-Za-z@])")
REGISTER_ASSIGN = re.compile(
    r"\s*=\s*(?:[-+]?(?:\d+(?:\.\d*)?|\.\d+)\s*(?:pt|em|ex|cm|mm|in|bp|sp|pc|dd|cc|mu|fill?l?)?"
    r"(?:\s*plus\s*[-+]?[\d.]+\s*[a-z]*)?(?:\s*minus\s*[-+]?[\d.]+\s*[a-z]*)?|\\[A-Za-z@]+)"
)
NUMERIC_ARG = re.compile(r"\s*[-+]?(?:\d+(?:\.\d*)?|\.\d+)\s*(?:pt|em|ex|cm|mm|in|bp|sp|pc|dd|cc|mu|fill?l?)?")
MAX_EXTRACT_BYTES = 800 * 1024 * 1024

SYSTEM_PROMPT = """你是资深的学术论文译者，负责把 LaTeX 源码里的英文翻译成准确、流畅、符合中文学术写作习惯的简体中文。

必须遵守：
1. 只翻译自然语言。LaTeX 命令（如 \\textbf{…}、\\emph{…}）及花括号结构原样保留，命令名不翻译、不删除、不新增；只翻译花括号里的英文。
2. 形如 ⟦0⟧、⟦1⟧ 的占位符代表公式、引用、脚注、标签、链接等不可改动的内容。每个占位符必须原样保留、恰好出现一次；可以按中文语序调整位置，但不能改编号、合并或删除。
3. 保留转义写法（\\%、\\&、\\_、\\#、~）。不要输出 Markdown、代码块或任何解释。
4. 人名、模型名、数据集名、方法名和通用缩写可保留英文；同一术语全文译法一致。
5. 输入由若干 <seg id="N">…</seg> 组成。逐段翻译，按原 id 输出 <seg id="N">译文</seg>，不要合并、拆分或遗漏任何片段。"""

MATH_ENVS = {
    "equation", "equation*", "align", "align*", "alignat", "alignat*", "flalign", "flalign*",
    "gather", "gather*", "multline", "multline*", "eqnarray", "eqnarray*", "displaymath", "math",
    "dmath", "dmath*", "dgroup", "dgroup*", "darray", "IEEEeqnarray", "IEEEeqnarray*", "empheq",
    "subeqnarray", "xalignat", "xxalignat", "mathpar", "subequations",
}

PROTECTED_ENVS = {
    "verbatim", "verbatim*", "Verbatim", "BVerbatim", "LVerbatim", "lstlisting", "lstlisting*", "minted",
    "comment", "alltt", "tabular", "tabular*", "tabularx", "tabulary", "tabu", "longtable", "longtable*",
    "supertabular", "xtabular", "longtabu", "NiceTabular", "tblr", "longtblr", "array", "tikzpicture",
    "tikzcd", "pgfpicture", "circuitikz", "forest", "picture", "pspicture", "axis", "groupplot",
    "algorithmic", "algorithmic*", "thebibliography", "filecontents", "filecontents*", "spverbatim",
    "tcblisting", "pycode", "sagesilent", "CCSXML", "asy", "gnuplot", "lrbox", "dot2tex", "algorithm2e",
    "tikzfigure",
}

ENV_ARG_SPECS = {
    "minipage": "OOOG", "wrapfigure": "OGOG", "wraptable": "OGOG", "subfigure": "OG", "subtable": "OG",
    "multicols": "GO", "multicols*": "GO", "adjustbox": "G", "list": "GG", "column": "OG", "overpic": "OG",
    "otherlanguage": "G", "otherlanguage*": "G", "spacing": "G", "tcolorbox": "O", "mdframed": "O",
    "threeparttable": "O", "tablenotes": "O", "sidewaysfigure": "O", "sidewaystable": "O", "figure": "O",
    "figure*": "O", "table": "O", "table*": "O", "itemize": "O", "enumerate": "O", "description": "O",
    "theorem": "O", "lemma": "O", "proof": "O", "definition": "O", "proposition": "O", "corollary": "O",
    "remark": "O", "example": "O", "exercise": "O", "problem": "O", "solution": "O", "columns": "O",
    "frame": "OOGG", "block": "G", "alertblock": "G", "exampleblock": "G", "varwidth": "G",
    "center": "", "flushleft": "", "flushright": "", "quote": "", "quotation": "", "abstract": "",
    "algorithm": "O", "algorithm*": "O",
}

# S=star, O=optional [..], G=brace group, C=control sequence or group, P=\def parameter text, L=\let target.
DEFINITION_SPECS = {
    "def": "CPG", "gdef": "CPG", "edef": "CPG", "xdef": "CPG", "let": "CL", "futurelet": "CCC",
    "newcommand": "SCOOG", "renewcommand": "SCOOG", "providecommand": "SCOOG", "DeclareRobustCommand": "SCOOG",
    "newenvironment": "SGOOGG", "renewenvironment": "SGOOGG", "NewDocumentCommand": "CGG",
    "RenewDocumentCommand": "CGG", "ProvideDocumentCommand": "CGG", "DeclareDocumentCommand": "CGG",
    "NewDocumentEnvironment": "GGGG", "RenewDocumentEnvironment": "GGGG", "DeclareMathOperator": "SCG",
    "newtheorem": "SGOGO", "theoremstyle": "G", "newcounter": "GO", "setcounter": "GG", "addtocounter": "GG",
    "stepcounter": "G", "refstepcounter": "G", "newlength": "C", "setlength": "CG", "addtolength": "CG",
    "settowidth": "CG", "settoheight": "CG", "settodepth": "CG", "newcolumntype": "GOG", "definecolor": "OGGG",
    "colorlet": "OGG", "crefname": "GGG", "Crefname": "GGG", "crefformat": "GG", "Crefformat": "GG",
    "creflabelformat": "GG", "newacronym": "OGGG", "newglossaryentry": "GG", "acrodef": "GOG",
    "captionsetup": "OG", "setlist": "OG", "hypersetup": "G", "graphicspath": "G",
    "DeclareGraphicsExtensions": "G", "tikzset": "G", "pgfplotsset": "G", "usetikzlibrary": "G", "lstset": "G",
    "sisetup": "G", "algrenewcommand": "SCOOG", "algnewcommand": "SCOOG", "SetKwInOut": "GG", "SetKwInput": "GG",
    "SetKwFunction": "GG", "SetKwProg": "GGGG", "SetKw": "GG", "newif": "C", "pgfmathsetmacro": "CG",
    "usepackage": "OG", "RequirePackage": "OG", "newsavebox": "C", "sbox": "CG", "savebox": "COOG",
    "newfloat": "GGGO", "floatname": "GG", "DeclareCaptionFormat": "GG", "DeclareCaptionLabelFormat": "GG",
    "lstdefinelanguage": "OGOG", "lstdefinestyle": "GG", "tcbset": "G", "newtcolorbox": "OGOOG",
    "newmdenv": "OG", "renewcaptionname": "GGG", "addto": "CG", "apptocmd": "CGGG", "pretocmd": "CGGG",
    "patchcmd": "CGGGG", "AtBeginDocument": "G", "AtEndDocument": "G", "pdfstringdefDisableCommands": "G",
}

# Commands that end the current segment; their listed arguments are left untouched.
HARD_SPECS = {
    "maketitle": "", "tableofcontents": "", "listoffigures": "", "listoftables": "", "appendix": "",
    "newpage": "", "clearpage": "", "cleardoublepage": "", "pagebreak": "O", "bibliography": "G",
    "bibliographystyle": "G", "printbibliography": "O", "include": "G", "includeonly": "G", "subfile": "G",
    "import": "GG", "subimport": "SGG", "inputfrom": "GG", "includepdf": "OG", "onecolumn": "",
    "balance": "", "FloatBarrier": "", "par": "", "printAffiliationsAndNotice": "G",
    "IEEEpeerreviewmaketitle": "", "frontmatter": "", "mainmatter": "", "backmatter": "", "printglossary": "O",
    "printglossaries": "", "printindex": "", "lstinputlisting": "OG", "inputminted": "OGG", "end": "G",
    "bibitem": "OG", "item": "O", "centering": "", "raggedright": "", "raggedleft": "", "noindent": "",
    "indent": "", "medskip": "", "bigskip": "", "smallskip": "", "vfill": "", "hline": "", "toprule": "O",
    "midrule": "O", "bottomrule": "O", "vspace": "SG",
    "addvspace": "G", "subfloat": "O", "State": "O", "Statex": "", "STATE": "", "Require": "",
    "Ensure": "", "else": "", "fi": "", "or": "", "makeatother": "", "documentclass": "OG",
}

# Commands masked entirely; every listed argument becomes part of one atom.
ATOM_SPECS = {
    "label": "G", "url": "G", "nolinkurl": "G", "path": "G", "texttt": "G", "email": "G", "orcid": "G",
    "includegraphics": "SOOG", "includesvg": "OG", "includeinkscape": "OG",
    "thanks": "G", "author": "OG", "affil": "OG", "affiliation": "OG", "institute": "G", "address": "G",
    "inst": "G", "authornote": "G", "authornotemark": "O", "institution": "G", "titlenote": "G",
    "hspace": "SG", "color": "OG", "pagecolor": "OG", "rule": "OGG", "footnotemark": "O", "SI": "OGG",
    "si": "OG", "num": "OG", "qty": "OGG", "unit": "OG", "SIrange": "OGGG", "numrange": "OGG",
    "qtyrange": "OGGG", "ang": "OG", "ensuremath": "G", "textsuperscript": "G", "textsubscript": "G",
    "index": "G", "phantom": "G", "hphantom": "G", "vphantom": "G", "pagestyle": "G", "thispagestyle": "G",
    "pagenumbering": "G", "linespread": "G", "fontsize": "GG", "addcontentsline": "GGG",
    "addtocontents": "GG", "markboth": "GG", "markright": "G", "icmltitlerunning": "G", "icmlsetsymbol": "GG",
    "icmlauthor": "GG", "icmlaffiliation": "GG", "icmlcorrespondingauthor": "GG", "icmlkeywords": "G",
    "IEEEauthorblockN": "G", "IEEEauthorblockA": "G", "IEEEPARstart": "GG", "acmConference": "OGGG",
    "acmBooktitle": "G", "acmDOI": "G", "acmISBN": "G", "acmPrice": "G", "acmYear": "G", "copyrightyear": "G",
    "setcopyright": "G", "ccsdesc": "OG", "received": "OG", "acmJournal": "G", "acmVolume": "G",
    "acmNumber": "G", "acmArticle": "G", "acmMonth": "G", "settopmatter": "G", "crefrange": "SGG",
    "Crefrange": "SGG", "cpagerefrange": "SGG", "gls": "SOG", "Gls": "SOG", "glspl": "SOG", "Glspl": "SOG",
    "glsentrytext": "G", "acrshort": "SOG", "acrlong": "SOG", "acrfull": "SOG", "ac": "SOG", "acs": "SOG",
    "acl": "SOG", "acf": "SOG", "acp": "SOG", "Ac": "SOG", "Acp": "SOG", "acused": "G", "nolinebreak": "O",
    "linebreak": "O", "newline": "", "nocite": "G", "pgfplotstableread": "OGG", "faIcon": "OG",
    "textcolor": "OG", "colorbox": "OG", "fcolorbox": "OGG", "href": "G", "hyperref": "O", "hyperlink": "G",
    "hypertarget": "G", "resizebox": "SGG", "scalebox": "GO", "rotatebox": "OG", "raisebox": "GOO",
    "makebox": "OO", "framebox": "OO", "parbox": "OOOG", "multicolumn": "GG", "multirow": "OGOG",
    "cellcolor": "OG", "rowcolor": "OG", "captionof": "SG", "sethlcolor": "G", "tnote": "G",
    "ifthenelse": "GGG", "small": "", "footnotesize": "", "scriptsize": "", "tiny": "", "large": "",
    "Large": "", "LARGE": "", "huge": "", "Huge": "", "normalsize": "", "hfill": "", "xspace": "",
    "protect": "", "ldots": "", "dots": "", "textbackslash": "", "LaTeX": "", "TeX": "", "today": "",
}

# Commands kept visible in the prose; their brace arguments are translated in place.
PROSE_COMMANDS = {
    "textbf", "textit", "emph", "underline", "textsc", "textsl", "textrm", "textsf", "textmd", "textup",
    "textnormal", "mbox", "text", "footnotetext", "subtitle", "keywords", "uline", "sout", "hl", "fbox",
    "textls", "underbar", "enquote", "bfseries", "itshape", "em", "bf", "it", "sc", "rmfamily", "sffamily",
    "ttfamily", "upshape", "mdseries", "normalfont", "scshape", "slshape",
}

# Commands whose braced argument is prose translated as its own segment: (spec before the group, token kind).
# "hard" commands also end the surrounding segment; "atom" commands stay inside it as one placeholder.
NESTED_PROSE = {
    "section": ("SO", "hard"), "subsection": ("SO", "hard"), "subsubsection": ("SO", "hard"),
    "paragraph": ("SO", "hard"), "subparagraph": ("SO", "hard"), "chapter": ("SO", "hard"),
    "part": ("SO", "hard"), "caption": ("SO", "hard"), "subcaption": ("SO", "hard"), "title": ("O", "hard"),
    "icmltitle": ("", "hard"), "subcaptionbox": ("SO", "hard"), "frametitle": ("O", "hard"),
    "framesubtitle": ("O", "hard"), "footnote": ("O", "atom"), "tablefootnote": ("O", "atom"),
    "marginpar": ("O", "atom"),
}

NUMERIC_COMMANDS = {
    "vskip", "hskip", "kern", "mkern", "penalty", "looseness", "tolerance", "emergencystretch",
    "hyphenpenalty", "exhyphenpenalty", "parindent", "parskip", "baselineskip", "columnsep", "itemsep",
    "topsep", "parsep", "widowpenalty", "clubpenalty", "interlinepenalty", "displaywidowpenalty",
    "brokenpenalty", "lineskip", "lineskiplimit", "hbadness", "vbadness", "hfuzz", "vfuzz", "abovedisplayskip",
    "belowdisplayskip", "abovecaptionskip", "belowcaptionskip", "textfloatsep", "floatsep", "intextsep",
}

VERB_COMMANDS = {"verb", "lstinline", "mintinline", "Verb"}


class LatexError(RuntimeError):
    pass


@dataclass
class Segment:
    file: Path
    start: int
    end: int
    masked: str
    atoms: list[tuple[int, int]]
    translated: str | None = None
    problem: str | None = None


@dataclass
class TexFile:
    path: Path
    mode: str
    text: str = ""
    segments: list[Segment] = field(default_factory=list)


@dataclass
class Project:
    root: Path
    main: Path
    files: list[TexFile]

    @property
    def segments(self) -> list[Segment]:
        return [segment for tex in self.files for segment in tex.segments]


# ---------------------------------------------------------------------------
# Source archive handling
# ---------------------------------------------------------------------------


def safe_target(root: Path, name: str) -> Path | None:
    normalized = name.replace("\\", "/")
    if normalized.startswith("/"):
        return None
    parts = [part for part in PurePosixPath(normalized).parts if part not in ("", ".")]
    if not parts or any(part == ".." for part in parts):
        return None
    target = root.joinpath(*parts)
    try:
        target.resolve().relative_to(root.resolve())
    except ValueError:
        return None
    return target


def extract_source(archive: Path, dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    raw = archive.read_bytes()
    if raw[:2] == b"\x1f\x8b":
        raw = gzip.decompress(raw)
    if raw[:4] == b"%PDF":
        raise LatexError("该论文没有提供 LaTeX 源码（只有 PDF）")
    buffer = io.BytesIO(raw)
    written = 0
    if tarfile.is_tarfile(buffer):
        buffer.seek(0)
        with tarfile.open(fileobj=buffer, mode="r:*") as archive_tar:
            for member in archive_tar.getmembers():
                if not member.isfile():
                    continue
                target = safe_target(dest, member.name)
                if target is None:
                    continue
                written += member.size
                if written > MAX_EXTRACT_BYTES:
                    raise LatexError("源码包解压后超过 800MB，已停止")
                handle = archive_tar.extractfile(member)
                if handle is None:
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                with handle:
                    target.write_bytes(handle.read())
        return
    buffer.seek(0)
    if zipfile.is_zipfile(buffer):
        with zipfile.ZipFile(buffer) as archive_zip:
            for info in archive_zip.infolist():
                if info.is_dir():
                    continue
                target = safe_target(dest, info.filename)
                if target is None:
                    continue
                written += info.file_size
                if written > MAX_EXTRACT_BYTES:
                    raise LatexError("源码包解压后超过 800MB，已停止")
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(archive_zip.read(info))
        return
    text = raw.decode("utf-8", errors="replace")
    if "\\documentclass" in text or "\\begin{document}" in text:
        (dest / "main.tex").write_text(text, "utf-8")
        return
    raise LatexError("无法识别的 arXiv 源码格式")


def read_tex(path: Path) -> str:
    data = path.read_bytes()
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode("latin-1")


def is_commented(text: str, pos: int) -> bool:
    index = text.rfind("\n", 0, pos) + 1
    while index < pos:
        char = text[index]
        if char == "\\":
            index += 2
            continue
        if char == "%":
            return True
        index += 1
    return False


def find_uncommented(pattern: re.Pattern, text: str, last: bool = False) -> re.Match | None:
    found = None
    for match in pattern.finditer(text):
        if is_commented(text, match.start()):
            continue
        if not last:
            return match
        found = match
    return found


BEGIN_DOCUMENT = re.compile(r"\\begin\s*\{document\}")
END_DOCUMENT = re.compile(r"\\end\s*\{document\}")
DOCUMENTCLASS = re.compile(r"\\documentclass\s*(?:\[[^\]]*\])?\s*\{")
INPUT_BRACED = re.compile(r"\\(?:input|include|subfile)\s*\{([^{}]+)\}")
INPUT_BARE = re.compile(r"\\input\s+([^\s{}%\\]+)")
IMPORT_CMD = re.compile(r"\\(?:sub)?import\*?\s*\{([^{}]*)\}\s*\{([^{}]+)\}")
TITLE_CMD = re.compile(r"\\(?:title|icmltitle)(?![A-Za-z])\s*(?:\[[^\]]*\])?\s*\{")


def readme_toplevel(root: Path) -> Path | None:
    readme = root / "00README.json"
    if readme.is_file():
        try:
            data = json.loads(readme.read_text("utf-8"))
            for source in data.get("sources", []):
                if source.get("usage") == "toplevel" and source.get("filename"):
                    candidate = safe_target(root, source["filename"])
                    if candidate and candidate.is_file():
                        return candidate
        except (OSError, ValueError, AttributeError):
            pass
    legacy = root / "00README.XXX"
    if legacy.is_file():
        for line in legacy.read_text("utf-8", errors="replace").splitlines():
            parts = line.split()
            if len(parts) >= 2 and parts[1].lower() in ("toplevelfile", "toplevel"):
                candidate = safe_target(root, parts[0])
                if candidate and candidate.is_file():
                    return candidate
    return None


def find_main_tex(root: Path) -> Path:
    preferred = readme_toplevel(root)
    if preferred:
        return preferred
    candidates = []
    for path in root.rglob("*.tex"):
        relative = path.relative_to(root)
        if any(part.startswith((".", "__MACOSX")) for part in relative.parts):
            continue
        text = read_tex(path)
        if not (find_uncommented(DOCUMENTCLASS, text) and find_uncommented(BEGIN_DOCUMENT, text)):
            continue
        score = 0.0
        stem = path.stem.lower()
        if stem in ("main", "paper", "ms", "manuscript", "article", "arxiv", "main_arxiv"):
            score += 6
        if any(word in stem for word in ("supp", "appendix", "rebuttal", "response", "cover", "letter", "poster")):
            score -= 8
        if path.parent == root:
            score += 3
        score += min(len(INPUT_BRACED.findall(text)), 12) * 0.8
        score += min(len(text) / 6000, 8)
        if path.with_suffix(".bbl").is_file():
            score += 4
        candidates.append((score, path))
    if not candidates:
        raise LatexError("源码中没有找到包含 \\documentclass 的主 tex 文件")
    candidates.sort(key=lambda item: item[0], reverse=True)
    return candidates[0][1]


def resolve_tex(name: str, directories: list[Path], root: Path) -> Path | None:
    name = name.strip().strip('"')
    if not name:
        return None
    for directory in directories:
        for candidate in (name, f"{name}.tex"):
            try:
                resolved = (directory / candidate).resolve()
                resolved.relative_to(root.resolve())
            except (ValueError, OSError):
                continue
            if resolved.is_file() and resolved.suffix.lower() == ".tex":
                return resolved
    return None


def find_inputs(text: str) -> list[tuple[int, list[str]]]:
    found: list[tuple[int, list[str]]] = []
    for pattern in (INPUT_BRACED, INPUT_BARE):
        for match in pattern.finditer(text):
            if not is_commented(text, match.start()):
                found.append((match.start(), [match.group(1)]))
    for match in IMPORT_CMD.finditer(text):
        if not is_commented(text, match.start()):
            directory, name = match.group(1), match.group(2)
            found.append((match.start(), [str(PurePosixPath(directory) / name), name]))
    found.sort(key=lambda item: item[0])
    return found


def collect_project(root: Path) -> Project:
    main = find_main_tex(root).resolve()
    base = main.parent
    files: list[TexFile] = []
    seen: set[Path] = set()

    def visit(path: Path, mode: str) -> None:
        if path in seen or len(seen) > 400:
            return
        seen.add(path)
        text = read_tex(path)
        files.append(TexFile(path=path, mode=mode, text=text))
        begin = find_uncommented(BEGIN_DOCUMENT, text)
        for position, names in find_inputs(text):
            if mode == "preamble":
                child_mode = "preamble"
            elif begin is not None:
                child_mode = "body" if position > begin.start() else "preamble"
            else:
                child_mode = "body"
            for name in names:
                child = resolve_tex(name, [base, path.parent], root)
                if child:
                    visit(child, child_mode)
                    break

    visit(main, "main")
    return Project(root=root, main=main, files=files)


# ---------------------------------------------------------------------------
# Scanner: splits LaTeX into prose and protected spans
# ---------------------------------------------------------------------------


def skip_group(text: str, index: int, end: int) -> int:
    opener = text[index]
    depth = 0
    brace_depth = 0
    i = index
    while i < end:
        char = text[i]
        if char == "\\":
            i += 2
            continue
        if char == "%":
            newline = text.find("\n", i, end)
            i = end if newline == -1 else newline + 1
            continue
        if opener == "{":
            if char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    return i + 1
        else:
            if char == "{":
                brace_depth += 1
            elif char == "}":
                brace_depth -= 1
            elif char == "[" and brace_depth == 0:
                depth += 1
            elif char == "]" and brace_depth == 0:
                depth -= 1
                if depth == 0:
                    return i + 1
        i += 1
    return end


def skip_inline_space(text: str, index: int, end: int) -> int:
    newlines = 0
    while index < end and text[index] in " \t\n":
        if text[index] == "\n":
            newlines += 1
            if newlines > 1:
                break
        index += 1
    return index


def skip_control_sequence(text: str, index: int, end: int) -> int:
    match = CONTROL_SEQUENCE.match(text, index, end)
    return match.end() if match else index + 1


def skip_spec(text: str, index: int, end: int, spec: str) -> int:
    i = index
    for step in spec:
        j = skip_inline_space(text, i, end)
        if j >= end:
            break
        char = text[j]
        if step == "S":
            if char == "*":
                i = j + 1
        elif step == "O":
            if char == "[":
                i = skip_group(text, j, end)
        elif step in "GC":
            if char == "{":
                i = skip_group(text, j, end)
            elif char == "\\":
                i = skip_control_sequence(text, j, end)
            elif step == "G" and char not in "}%":
                i = j + 1
            else:
                break
        elif step == "P":
            k = j
            while k < end and text[k] not in "{\n":
                k += 1
            i = k
        elif step == "L":
            k = j
            if text[k] == "=":
                k = skip_inline_space(text, k + 1, end)
            if k < end and text[k] == "\\":
                i = skip_control_sequence(text, k, end)
            elif k < end:
                i = k + 1
    return i


def find_env_end(text: str, index: int, end: int, env: str) -> int:
    pattern = re.compile(r"\\(begin|end)\s*\{" + re.escape(env) + r"\}")
    depth = 1
    for match in pattern.finditer(text, index, end):
        if is_commented(text, match.start()):
            continue
        depth += 1 if match.group(1) == "begin" else -1
        if depth == 0:
            return match.end()
    return end


def find_iffalse_end(text: str, index: int, end: int) -> int:
    depth = 1
    for match in IF_TOKEN.finditer(text, index, end):
        if is_commented(text, match.start()):
            continue
        depth += -1 if match.group(0) == "\\fi" else 1
        if depth == 0:
            return match.end()
    return end


def find_math_end(text: str, index: int, end: int) -> int:
    if text.startswith("$$", index):
        j = index + 2
        while j < end - 1:
            if text[j] == "\\":
                j += 2
                continue
            if text[j] == "$" and text[j + 1] == "$":
                return j + 2
            j += 1
        return index + 2
    j = index + 1
    while j < end:
        char = text[j]
        if char == "\\":
            j += 2
            continue
        if char == "$":
            return j + 1
        if char == "\n" and BLANK_LINE.match(text, j, end):
            return index + 1
        j += 1
    return index + 1


def find_bracket_math_end(text: str, index: int, end: int, closer: str) -> int:
    j = index + 2
    while j < end - 1:
        if text[j] == "\\":
            if text[j + 1] == closer:
                return j + 2
            j += 2
            continue
        j += 1
    return end


class Scanner:
    """Tokenize a LaTeX range into text / atom / hard / drop / par tokens.

    atom: masked as ⟦n⟧ inside a segment.  hard: ends the segment and is left untouched.
    drop: a comment.  par: a blank line.  Prose arguments of title-, caption- and footnote-like
    commands are collected in ``nested`` and segmented separately.
    """

    def __init__(self, text: str, start: int, end: int):
        self.text = text
        self.start = start
        self.end = end
        self.nested: list[tuple[int, int]] = []

    def scan(self) -> list[tuple[str, int, int]]:
        text, end = self.text, self.end
        tokens: list[tuple[str, int, int]] = []
        pending = self.start
        i = self.start

        def emit(kind: str, start: int, stop: int) -> None:
            nonlocal pending
            if start > pending:
                tokens.append(("text", pending, start))
            tokens.append((kind, start, stop))
            pending = stop

        while i < end:
            char = text[i]
            if char == "%":
                newline = text.find("\n", i, end)
                stop = end if newline == -1 else newline
                emit("drop", i, stop)
                i = stop
                continue
            if char == "\n":
                blank = BLANK_LINE.match(text, i, end)
                if blank:
                    emit("par", i, blank.end())
                    i = blank.end()
                    continue
                i += 1
                continue
            if char == "$":
                stop = find_math_end(text, i, end)
                emit("atom", i, stop)
                i = stop
                continue
            if char == "\\":
                kind, stop = self.command(i)
                if kind != "text":
                    emit(kind, i, stop)
                i = max(stop, i + 1)
                continue
            i += 1
        if end > pending:
            tokens.append(("text", pending, end))
        return tokens

    def command(self, i: int) -> tuple[str, int]:
        text, end = self.text, self.end
        following = text[i + 1] if i + 1 < end else ""
        if following == "[":
            return "atom", find_bracket_math_end(text, i, end, "]")
        if following == "(":
            return "atom", find_bracket_math_end(text, i, end, ")")
        if following == "\\":
            return "atom", skip_spec(text, i + 2, end, "SO")
        if not (following.isascii() and (following.isalpha() or following == "@")):
            return "text", min(i + 2, end)
        match = CS_NAME.match(text, i + 1, end)
        name = match.group(0)
        name_end = match.end()
        base = name.rstrip("*")
        if base == "begin":
            return self.begin_env(name_end)
        if base == "iffalse":
            return "hard", find_iffalse_end(text, name_end, end)
        if base == "makeatletter":
            closing = re.compile(r"\\makeatother(?![A-Za-z])").search(text, name_end, end)
            return "hard", closing.end() if closing else end
        if base in VERB_COMMANDS:
            return "atom", self.verb_end(base, name_end)
        if base in DEFINITION_SPECS:
            return "hard", skip_spec(text, name_end, end, DEFINITION_SPECS[base])
        assignment = REGISTER_ASSIGN.match(text, name_end, end)
        if assignment and assignment.end() > name_end:
            return "atom", assignment.end()
        if base in NUMERIC_COMMANDS:
            numeric = NUMERIC_ARG.match(text, name_end, end)
            return "atom", numeric.end() if numeric and re.search(r"\d", numeric.group(0)) else name_end
        if base == "input":
            j = skip_inline_space(text, name_end, end)
            if j < end and text[j] == "{":
                return "hard", skip_group(text, j, end)
            bare = re.compile(r"[^\s{}%\\]+").match(text, j, end)
            return "hard", bare.end() if bare else name_end
        if base == "twocolumn":
            j = skip_inline_space(text, name_end, end)
            if j < end and text[j] == "[":
                group_end = skip_group(text, j, end)
                self.nested.append((j + 1, group_end - 1))
                return "hard", group_end
            return "hard", name_end
        if base in NESTED_PROSE:
            spec, kind = NESTED_PROSE[base]
            head_end = skip_spec(text, name_end, end, spec)
            j = skip_inline_space(text, head_end, end)
            if j < end and text[j] == "{":
                group_end = skip_group(text, j, end)
                self.nested.append((j + 1, group_end - 1))
                return kind, group_end
            return kind, head_end
        if base in HARD_SPECS:
            return "hard", skip_spec(text, name_end, end, HARD_SPECS[base])
        if base.startswith("if") and base != "ifthenelse":
            return "hard", name_end
        if base in ATOM_SPECS:
            return "atom", skip_spec(text, name_end, end, ATOM_SPECS[base])
        if CITE_OR_REF.match(base):
            return "atom", skip_spec(text, name_end, end, "SOOG")
        if base in PROSE_COMMANDS:
            return "text", name_end
        j = skip_inline_space(text, name_end, end)
        if j < end and text[j] == "{":
            group_end = skip_group(text, j, end)
            if SIMPLE_ARG.match(text[j + 1 : group_end - 1].strip()):
                stop = group_end
                while stop < end and text[stop] == "{":
                    stop = skip_group(text, stop, end)
                return "atom", stop
            return "text", name_end
        return "atom", name_end

    def verb_end(self, base: str, index: int) -> int:
        text, end = self.text, self.end
        i = index
        if base == "lstinline":
            i = skip_spec(text, i, end, "O")
        if base == "mintinline":
            i = skip_spec(text, i, end, "OG")
        if i >= end:
            return end
        delimiter = text[i]
        if delimiter == "{":
            return skip_group(text, i, end)
        close = text.find(delimiter, i + 1, end)
        newline = text.find("\n", i + 1, end)
        if close == -1 or (newline != -1 and newline < close):
            return i + 1
        return close + 1

    def begin_env(self, name_end: int) -> tuple[str, int]:
        text, end = self.text, self.end
        match = re.compile(r"\s*\{([^{}]*)\}").match(text, name_end, end)
        if not match:
            return "text", name_end
        env = match.group(1).strip()
        after = match.end()
        if env in MATH_ENVS:
            return "atom", find_env_end(text, after, end, env)
        if env in PROTECTED_ENVS:
            return "hard", find_env_end(text, after, end, env)
        if env in ("algorithm", "algorithm*"):
            stop = find_env_end(text, after, end, env)
            if not re.search(r"\\begin\s*\{algorithmic", text[after:stop]):
                return "hard", stop
        if env in ENV_ARG_SPECS:
            return "hard", skip_spec(text, after, end, ENV_ARG_SPECS[env])
        stop = skip_spec(text, after, end, "O")
        while stop < end and text[stop] == "{":
            group_end = skip_group(text, stop, end)
            if not PARAM_ARG.match(text[stop + 1 : group_end - 1]):
                break
            stop = group_end
        return "hard", stop


def build_segment(path: Path, text: str, tokens: list[tuple[str, int, int]]) -> Segment | None:
    def blank(token: tuple[str, int, int]) -> bool:
        return token[0] == "text" and not text[token[1] : token[2]].strip()

    # Edge atoms stay inside the segment so the translator can reorder them (e.g. "如 \cite{x} 所示").
    # A trailing comment is dropped from the segment: without its newline it could swallow moved text.
    while tokens and blank(tokens[0]):
        tokens = tokens[1:]
    while tokens and (tokens[-1][0] == "drop" or blank(tokens[-1])):
        tokens = tokens[:-1]
    if not tokens:
        return None
    start, end = tokens[0][1], tokens[-1][2]
    first = text[tokens[0][1] : tokens[0][2]]
    start += len(first) - len(first.lstrip())
    last = text[tokens[-1][1] : tokens[-1][2]]
    end -= len(last) - len(last.rstrip())
    pieces: list[str] = []
    atoms: list[tuple[int, int]] = []
    consumed = start
    for kind, token_start, token_end in tokens:
        token_start, token_end = max(token_start, consumed), min(token_end, end)
        if token_end <= token_start:
            continue
        if kind == "drop":
            # Keep comments (and the newline ending them) verbatim so the spliced text cannot
            # be swallowed by a comment, and line structure such as `{%` survives translation.
            if token_end < end and text[token_end] == "\n":
                token_end += 1
            pieces.append(f"⟦{len(atoms)}⟧")
            atoms.append((token_start, token_end))
            consumed = token_end
            continue
        if kind == "text":
            pieces.append(text[token_start:token_end])
        else:
            pieces.append(f"⟦{len(atoms)}⟧")
            atoms.append((token_start, token_end))
        consumed = token_end
    masked = "".join(pieces)
    prose = PLACEHOLDER_RE.sub(" ", masked)
    prose = re.sub(r"\\[A-Za-z@]+\*?", " ", prose)
    if not PROSE_WORD.search(prose):
        return None
    return Segment(file=path, start=start, end=end, masked=masked, atoms=atoms)


def segments_in_range(path: Path, text: str, start: int, end: int, depth: int = 0) -> list[Segment]:
    segments: list[Segment] = []
    current: list[tuple[str, int, int]] = []
    scanner = Scanner(text, start, end)
    for token in scanner.scan():
        if token[0] in ("par", "hard"):
            segment = build_segment(path, text, current)
            if segment:
                segments.append(segment)
            current = []
        else:
            current.append(token)
    segment = build_segment(path, text, current)
    if segment:
        segments.append(segment)
    if depth < 6:
        for inner_start, inner_end in scanner.nested:
            segments.extend(segments_in_range(path, text, inner_start, inner_end, depth + 1))
    return segments


def title_segments(path: Path, text: str, start: int, end: int) -> list[Segment]:
    segments: list[Segment] = []
    for match in TITLE_CMD.finditer(text, start, end):
        if is_commented(text, match.start()):
            continue
        group_start = match.end() - 1
        group_end = skip_group(text, group_start, end)
        segments.extend(segments_in_range(path, text, group_start + 1, group_end - 1))
    return segments


def tidy_segments(segments: list[Segment]) -> list[Segment]:
    """Sort, drop duplicates and partial overlaps; nested (fully contained) segments are kept."""
    ordered = sorted(segments, key=lambda segment: (segment.start, -segment.end))
    kept: list[Segment] = []
    stack: list[Segment] = []
    for segment in ordered:
        while stack and stack[-1].end <= segment.start:
            stack.pop()
        if stack and (segment.end > stack[-1].end or (segment.start, segment.end) == (stack[-1].start, stack[-1].end)):
            continue
        kept.append(segment)
        stack.append(segment)
    return kept


def plan_segments(project: Project) -> None:
    for tex in project.files:
        text = tex.text
        begin = find_uncommented(BEGIN_DOCUMENT, text)
        if tex.mode == "preamble":
            segments = title_segments(tex.path, text, 0, len(text))
        elif begin is not None:
            finish = find_uncommented(END_DOCUMENT, text, last=True)
            body_end = finish.start() if finish and finish.start() > begin.end() else len(text)
            segments = title_segments(tex.path, text, 0, begin.start())
            segments += segments_in_range(tex.path, text, begin.end(), body_end)
        elif tex.mode == "body":
            segments = segments_in_range(tex.path, text, 0, len(text))
        else:
            segments = []
        tex.segments = tidy_segments(segments)


# ---------------------------------------------------------------------------
# Validation of translated fragments
# ---------------------------------------------------------------------------


def brace_profile(text: str) -> tuple[int, int]:
    depth = 0
    lowest = 0
    i = 0
    while i < len(text):
        char = text[i]
        if char == "\\":
            i += 2
            continue
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            lowest = min(lowest, depth)
        i += 1
    return depth, lowest


def count_unescaped(text: str, char: str) -> int:
    count = 0
    i = 0
    while i < len(text):
        if text[i] == "\\":
            i += 2
            continue
        if text[i] == char:
            count += 1
        i += 1
    return count


def escape_unescaped(text: str, char: str, replacement: str) -> str:
    out: list[str] = []
    i = 0
    while i < len(text):
        if text[i] == "\\":
            out.append(text[i : i + 2])
            i += 2
            continue
        out.append(replacement if text[i] == char else text[i])
        i += 1
    return "".join(out)


def command_counts(text: str) -> Counter:
    return Counter(match.group(1) for match in re.finditer(r"\\([A-Za-z@]+)", text))


def normalize_output(text: str) -> str:
    text = text.replace("\r\n", "\n").strip("\n")
    return PLACEHOLDER_RE.sub(lambda match: f"⟦{int(match.group(1))}⟧", text)


def check_translation(segment: Segment, output: str | None) -> str | None:
    if output is None or not output.strip():
        return "输出为空"
    ids = sorted(int(value) for value in PLACEHOLDER_RE.findall(output))
    if ids != list(range(len(segment.atoms))):
        return "占位符缺失、重复或编号被改动"
    leftover = PLACEHOLDER_RE.sub("", output)
    if "⟦" in leftover or "⟧" in leftover:
        return "占位符格式被破坏"
    source_depth, source_low = brace_profile(segment.masked)
    out_depth, out_low = brace_profile(output)
    if out_depth != source_depth or out_low < source_low:
        return "花括号不配对"
    source_counts = command_counts(segment.masked)
    out_counts = command_counts(output)
    for name, count in out_counts.items():
        if count > source_counts.get(name, 0):
            return f"出现了原文没有的命令 \\{name}"
    for name, count in source_counts.items():
        if out_counts.get(name, 0) < count and name not in PROSE_COMMANDS:
            return f"命令 \\{name} 丢失"
    if BACKSLASH_NON_ASCII.search(output):
        return "反斜杠后接了非 ASCII 字符"
    if count_unescaped(output, "$") % 2:
        return "美元符号不成对"
    stripped = output.rstrip()
    if stripped.endswith("\\") and not stripped.endswith("\\\\"):
        return "结尾有多余的反斜杠"
    if len(output) > 6 * len(segment.masked) + 200:
        return "输出异常冗长"
    return None


def polish_output(segment: Segment, output: str) -> str:
    output = BLANK_LINE.sub("\n", output)
    for char, replacement in (("%", "\\%"), ("&", "\\&"), ("#", "\\#"), ("_", "\\_")):
        if count_unescaped(segment.masked, char) == 0 and count_unescaped(output, char):
            output = escape_unescaped(output, char, replacement)
    if count_unescaped(segment.masked, "^") == 0 and count_unescaped(output, "^"):
        output = escape_unescaped(output, "^", "\\^{}")
    return output


def parse_reply(reply: str) -> dict[int, str]:
    parsed: dict[int, str] = {}
    for match in SEG_RE.finditer(reply or ""):
        parsed.setdefault(int(match.group(1)), normalize_output(match.group(2)))
    return parsed


# ---------------------------------------------------------------------------
# Translation driver
# ---------------------------------------------------------------------------


@dataclass
class TranslationStats:
    total: int = 0
    translated: int = 0
    cached: int = 0
    failed: int = 0
    failures: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "segments_total": self.total,
            "segments_translated": self.translated,
            "segments_cached": self.cached,
            "segments_failed": self.failed,
            "failure_samples": self.failures[:8],
        }


def glossary_digest(glossary: list[tuple[str, str]]) -> str:
    payload = "\n".join(f"{source}\t{target}" for source, target in glossary)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def system_prompt(title: str, glossary: list[tuple[str, str]], extra: str = "") -> str:
    parts = [SYSTEM_PROMPT]
    if title:
        parts.append(f"论文标题（仅供理解上下文）：{title}")
    if glossary:
        parts.append("术语表（出现时使用对应译法）：\n" + "\n".join(f"{source} → {target}" for source, target in glossary))
    if extra:
        parts.append(extra)
    return "\n\n".join(parts)


def relevant_glossary(glossary: list[tuple[str, str]], text: str) -> list[tuple[str, str]]:
    lowered = text.lower()
    return [
        (source, target)
        for source, target in glossary
        if re.search(r"(?<![a-z])" + re.escape(source.lower()) + r"(?![a-z])", lowered)
    ]


def make_batches(segments: list[Segment], budget: int) -> list[list[Segment]]:
    batches: list[list[Segment]] = []
    current: list[Segment] = []
    size = 0
    for segment in segments:
        length = len(segment.masked)
        if current and (size + length > budget or len(current) >= 30):
            batches.append(current)
            current, size = [], 0
        current.append(segment)
        size += length
    if current:
        batches.append(current)
    return batches


async def translate_segments(
    segments: list[Segment],
    client: MagpieClient,
    *,
    title: str,
    glossary: list[tuple[str, str]],
    budget: int,
    progress: ProgressFn | None = None,
) -> TranslationStats:
    stats = TranslationStats(total=len(segments))
    cache = get_cache()
    namespace = f"{PROMPT_VERSION}|{glossary_digest(glossary)}"
    pending: list[Segment] = []
    for segment in segments:
        cached = cache.get(cache.make_key(client.model, namespace, segment.masked))
        if cached is not None and check_translation(segment, cached) is None:
            segment.translated = cached
            stats.cached += 1
            stats.translated += 1
        else:
            pending.append(segment)
    done = stats.cached
    if progress:
        progress(done, stats.total)

    def accept(segment: Segment, output: str) -> None:
        polished = polish_output(segment, output)
        segment.translated = polished
        cache.put(cache.make_key(client.model, namespace, segment.masked), polished)
        stats.translated += 1

    async def single(segment: Segment, problem: str) -> None:
        reminder = (
            f"注意：上一次输出未通过校验（{problem}）。请完整保留全部 {len(segment.atoms)} 个占位符、"
            "所有 LaTeX 命令和花括号，只翻译其中的英文。"
        )
        prompt = system_prompt(title, relevant_glossary(glossary, segment.masked), reminder)
        for attempt in range(2):
            try:
                reply = await client.chat(
                    prompt,
                    f'<seg id="1">\n{segment.masked}\n</seg>',
                    use_cache=False,
                    temperature=0.1 + 0.3 * attempt,
                )
            except MagpieError as exc:
                problem = f"请求失败：{exc}"
                continue
            parsed = parse_reply(reply)
            output = parsed.get(1)
            if output is None and "<seg" not in reply:
                output = normalize_output(reply)
            issue = check_translation(segment, output)
            if issue is None and output is not None:
                accept(segment, output)
                return
            problem = issue or "缺少输出"
        segment.problem = problem
        stats.failed += 1
        stats.failures.append(f"{problem}：{segment.masked[:80]}")

    async def run_batch(batch: list[Segment]) -> None:
        nonlocal done
        body = "\n".join(f'<seg id="{index + 1}">\n{segment.masked}\n</seg>' for index, segment in enumerate(batch))
        prompt = system_prompt(title, relevant_glossary(glossary, body))
        reply = ""
        try:
            reply = await client.chat(prompt, body, use_cache=False)
        except MagpieError as exc:
            if not exc.retryable:
                raise
        parsed = parse_reply(reply)
        retry: list[tuple[Segment, str]] = []
        for index, segment in enumerate(batch):
            output = parsed.get(index + 1)
            issue = check_translation(segment, output)
            if issue:
                retry.append((segment, issue))
            else:
                accept(segment, output)
        if retry:
            await asyncio.gather(*(single(segment, issue) for segment, issue in retry))
        done += len(batch)
        if progress:
            progress(done, stats.total)

    await asyncio.gather(*(run_batch(batch) for batch in make_batches(pending, budget)))
    return stats


# ---------------------------------------------------------------------------
# Rendering translated files
# ---------------------------------------------------------------------------


@dataclass
class RenderedFile:
    path: Path
    text: str
    spans: list[tuple[Segment, int, int]]

    def culprit(self, line: int) -> Segment | None:
        """Return the translated segment most likely responsible for a TeX error reported at ``line``."""
        containing = [item for item in self.spans if item[1] <= line <= item[2]]
        if containing:
            return min(containing, key=lambda item: item[2] - item[1])[0]
        before = [item for item in self.spans if item[2] < line and line - item[2] <= 25]
        if before:
            return max(before, key=lambda item: item[2])[0]
        return None


def render_file(tex: TexFile) -> tuple[str, list[tuple[Segment, int, int]]]:
    """Rebuild a file with every translated segment substituted, including segments nested in atoms.

    Returns the new text and the (segment, first_line, last_line) span of every translated segment.
    """
    segments = sorted(tex.segments, key=lambda segment: (segment.start, -segment.end))
    starts = [segment.start for segment in segments]
    text = tex.text
    out: list[str] = []
    size = 0
    offsets: list[tuple[Segment, int, int]] = []

    def write(chunk: str, translated: bool = False) -> None:
        nonlocal size
        if not chunk:
            return
        if translated:
            chunk = MACRO_BEFORE_CJK.sub(r"\1{}", chunk)
            if out and chunk[0].isalpha() and CONTROL_WORD_END.search(out[-1]):
                out.append("{}")
                size += 2
        out.append(chunk)
        size += len(chunk)

    def maximal_within(start: int, end: int, exclude: Segment | None) -> list[Segment]:
        result: list[Segment] = []
        last_end = -1
        index = bisect.bisect_left(starts, start)
        while index < len(segments) and segments[index].start < end:
            segment = segments[index]
            index += 1
            if segment is exclude or segment.end > end or segment.start < last_end:
                continue
            result.append(segment)
            last_end = segment.end
        return result

    def render(start: int, end: int, exclude: Segment | None = None) -> None:
        cursor = start
        for segment in maximal_within(start, end, exclude):
            write(text[cursor : segment.start])
            restore(segment)
            cursor = segment.end
        write(text[cursor:end])

    def restore(segment: Segment) -> None:
        if segment.translated is None:
            render(segment.start, segment.end, exclude=segment)
            return
        begin = size
        position = 0
        for match in PLACEHOLDER_RE.finditer(segment.translated):
            write(segment.translated[position : match.start()], translated=True)
            atom_start, atom_end = segment.atoms[int(match.group(1))]
            render(atom_start, atom_end)
            position = match.end()
        write(segment.translated[position:], translated=True)
        offsets.append((segment, begin, size))

    render(0, len(text))
    rendered = "".join(out)
    newlines = [index for index, char in enumerate(rendered) if char == "\n"]

    def line_of(offset: int) -> int:
        return bisect.bisect_left(newlines, offset) + 1

    spans = [(segment, line_of(begin), line_of(max(begin, end - 1))) for segment, begin, end in offsets]
    return rendered, spans


def apply_translations(project: Project, source_root: Path, output_root: Path) -> dict[Path, RenderedFile]:
    """Write translated copies of every segmented tex file into output_root (a copy of the source tree)."""
    rendered: dict[Path, RenderedFile] = {}
    for tex in project.files:
        if not tex.segments:
            continue
        target = output_root / tex.path.relative_to(source_root.resolve())
        target.parent.mkdir(parents=True, exist_ok=True)
        text, spans = render_file(tex)
        target.write_text(text, "utf-8")
        rendered[target.resolve()] = RenderedFile(path=target.resolve(), text=text, spans=spans)
    return rendered
