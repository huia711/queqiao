from __future__ import annotations

import functools
import os
import shutil
import subprocess
from pathlib import Path

from .config import settings

SERIF_CANDIDATES = ["Source Han Serif CN", "Source Han Serif SC", "Noto Serif CJK SC", "Songti SC", "STSong"]
SANS_CANDIDATES = ["Source Han Sans SC", "Noto Sans CJK SC", "PingFang SC", "Hiragino Sans GB", "Heiti SC"]
KAI_CANDIDATES = ["FZKai-Z03S", "Kaiti SC", "STKaiti"]

# Font files that ship with macOS, used when fontconfig's fc-list is not installed.
MACOS_SYSTEM_FONTS = {
    "Songti SC": "/System/Library/Fonts/Supplemental/Songti.ttc",
    "STSong": "/System/Library/Fonts/Supplemental/Songti.ttc",
    "Hiragino Sans GB": "/System/Library/Fonts/Hiragino Sans GB.ttc",
    "Heiti SC": "/System/Library/Fonts/STHeiti Medium.ttc",
    "Kaiti SC": "/System/Library/Fonts/Supplemental/Kaiti.ttc",
    "STKaiti": "/System/Library/Fonts/Supplemental/Kaiti.ttc",
}


def _font_dirs() -> list[Path]:
    return [Path.home() / "Library/Fonts", Path("/Library/Fonts"), Path("/System/Library/Fonts")]


def _scan_font_files() -> set[str]:
    """Best-effort family detection from font file names when fc-list is unavailable."""
    found: set[str] = set()
    for name, path in MACOS_SYSTEM_FONTS.items():
        if os.path.exists(path):
            found.add(name)
    hints = {
        "SourceHanSerif": "Source Han Serif SC",
        "SourceHanSans": "Source Han Sans SC",
        "NotoSerifCJK": "Noto Serif CJK SC",
        "NotoSansCJK": "Noto Sans CJK SC",
        "FZKai": "FZKai-Z03S",
    }
    for directory in _font_dirs():
        if not directory.is_dir():
            continue
        for path in directory.iterdir():
            compact = path.name.replace(" ", "").replace("-", "")
            for hint, family in hints.items():
                if compact.startswith(hint.replace("-", "")):
                    found.add(family)
    return found


@functools.lru_cache(maxsize=1)
def installed_families() -> frozenset[str]:
    if not shutil.which("fc-list"):
        return frozenset(_scan_font_files())
    try:
        output = subprocess.run(
            ["fc-list", ":", "family"], capture_output=True, text=True, timeout=20, check=False
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return frozenset(_scan_font_files())
    families: set[str] = set()
    for line in output.splitlines():
        for name in line.split(","):
            name = name.replace("\\-", "-").strip()
            if name:
                families.add(name)
    return frozenset(families or _scan_font_files())


def _pick(preferred: str, candidates: list[str]) -> str | None:
    families = installed_families()
    if preferred:
        return preferred
    for name in candidates:
        if name in families:
            return name
    return None


@functools.lru_cache(maxsize=1)
def cjk_fonts() -> dict[str, str | None]:
    serif = _pick(settings.cjk_serif_font, SERIF_CANDIDATES)
    sans = _pick(settings.cjk_sans_font, SANS_CANDIDATES) or serif
    kai = _pick(settings.cjk_kai_font, KAI_CANDIDATES) or serif
    return {"serif": serif, "sans": sans, "kai": kai}
