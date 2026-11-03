"""Runtime settings.

Values come from (highest priority first): environment variables, ``settings.json`` in the data
directory (written by the in-app settings page), then built-in defaults. Settings that can be changed
from the UI are re-read on every use, so edits take effect without restarting the service.
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent

DEFAULT_PREFERRED_MODELS = [
    "workbuddy/glm-5.3-flashx",
    "workbuddy/deepseek-v4.1-flash",
    "antigravity/gemini-3.8-flash",
    "workbuddy/glm-5.3-flash-ioa",
    "codex/gpt-5.6-luna",
]

# key -> (environment variable, default, type)
FIELDS: dict[str, tuple[str, object, type]] = {
    "magpie_base_url": ("MAGPIE_BASE_URL", "http://127.0.0.1:3425/v1", str),
    "magpie_api_key": ("MAGPIE_API_KEY", "magpie", str),
    "default_model": ("QUEQIAO_DEFAULT_MODEL", "workbuddy/glm-5.3-flashx", str),
    "preferred_models": ("QUEQIAO_PREFERRED_MODELS", DEFAULT_PREFERRED_MODELS, list),
    "concurrency": ("QUEQIAO_CONCURRENCY", 8, int),
    "pdf_qps": ("QUEQIAO_PDF_QPS", 8, int),
    "max_jobs": ("QUEQIAO_MAX_JOBS", 2, int),
    "max_upload_mb": ("QUEQIAO_MAX_UPLOAD_MB", 50, int),
    "chunk_chars": ("QUEQIAO_CHUNK_CHARS", 2400, int),
    "latex_timeout": ("QUEQIAO_LATEX_TIMEOUT", 600, int),
    "request_timeout": ("QUEQIAO_REQUEST_TIMEOUT", 180, int),
    "cjk_serif_font": ("QUEQIAO_CJK_SERIF_FONT", "", str),
    "cjk_sans_font": ("QUEQIAO_CJK_SANS_FONT", "", str),
    "cjk_kai_font": ("QUEQIAO_CJK_KAI_FONT", "", str),
    # Path prefix when served behind a reverse proxy (e.g. "/queqiao").
    "base_path": ("QUEQIAO_BASE_PATH", "", str),
    # Shared secret a fronting gateway must send in X-Hub-Node-Secret; empty disables the check.
    "gateway_secret": ("QUEQIAO_GATEWAY_SECRET", "", str),
    # Optional access password (PBKDF2 hash from `python -m app.auth`) and session signing key.
    "auth_hash": ("QUEQIAO_AUTH_HASH", "", str),
    "auth_secret": ("QUEQIAO_AUTH_SECRET", "", str),
    "auth_max_age": ("QUEQIAO_AUTH_MAX_AGE", 43200, int),
    # "open" (everything allowed, for local use) or "invite" (writes and model calls need an invite code).
    "access_mode": ("QUEQIAO_ACCESS_MODE", "open", str),
    # Model for search, Q&A, reading guides, reference summaries and the daily digest; empty = default_model.
    "assist_model": ("QUEQIAO_ASSIST_MODEL", "", str),
    # Hour (0-23, server local time) to build the daily arXiv digest automatically; -1 disables it.
    "digest_hour": ("QUEQIAO_DIGEST_HOUR", -1, int),
    # Zotero Web API (https://www.zotero.org/settings/keys).
    "zotero_user_id": ("QUEQIAO_ZOTERO_USER_ID", "", str),
    "zotero_api_key": ("QUEQIAO_ZOTERO_API_KEY", "", str),
    "zotero_collection": ("QUEQIAO_ZOTERO_COLLECTION", "", str),
}

# Settings the in-app settings page may change.
EDITABLE = (
    "magpie_base_url",
    "magpie_api_key",
    "default_model",
    "assist_model",
    "preferred_models",
    "concurrency",
    "pdf_qps",
    "zotero_user_id",
    "zotero_api_key",
    "zotero_collection",
)
SECRET_KEYS = ("magpie_api_key", "zotero_api_key")


def _default_data_dir() -> Path:
    configured = os.environ.get("QUEQIAO_DATA_DIR")
    if configured:
        return Path(configured).expanduser()
    return ROOT_DIR / "data"


def _coerce(value: object, kind: type) -> object:
    if kind is int:
        return int(value)
    if kind is list:
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return [str(item).strip() for item in value if str(item).strip()]
    return str(value).strip()


class Settings:
    def __init__(self) -> None:
        self.data_dir = _default_data_dir()
        self.web_dir = Path(os.environ.get("QUEQIAO_WEB_DIR", str(ROOT_DIR / "web"))).expanduser()
        self._lock = threading.Lock()
        self._file_cache: tuple[float, dict] | None = None

    @property
    def settings_file(self) -> Path:
        return self.data_dir / "settings.json"

    @property
    def jobs_dir(self) -> Path:
        return self.data_dir / "jobs"

    @property
    def cache_db(self) -> Path:
        return self.data_dir / "cache.sqlite3"

    def _stored(self) -> dict:
        path = self.settings_file
        try:
            mtime = path.stat().st_mtime
        except OSError:
            return {}
        with self._lock:
            if self._file_cache and self._file_cache[0] == mtime:
                return self._file_cache[1]
            try:
                data = json.loads(path.read_text("utf-8"))
            except (OSError, ValueError):
                data = {}
            if not isinstance(data, dict):
                data = {}
            self._file_cache = (mtime, data)
            return data

    def source(self, key: str) -> str:
        env_name = FIELDS[key][0]
        if os.environ.get(env_name, "").strip():
            return "env"
        if key in self._stored():
            return "file"
        return "default"

    def __getattr__(self, key: str):
        if key not in FIELDS:
            raise AttributeError(key)
        env_name, default, kind = FIELDS[key]
        raw = os.environ.get(env_name)
        if raw is not None and raw.strip():
            try:
                value = _coerce(raw, kind)
            except ValueError:
                value = default
        else:
            stored = self._stored()
            try:
                value = _coerce(stored[key], kind) if key in stored else default
            except (TypeError, ValueError):
                value = default
        if key == "magpie_base_url":
            value = str(value).rstrip("/")
        return value

    def update(self, changes: dict) -> dict:
        """Persist editable settings; empty values reset a key to its default."""
        stored = dict(self._stored())
        for key, value in changes.items():
            if key not in EDITABLE:
                continue
            kind = FIELDS[key][2]
            if value is None or value == "" or value == []:
                stored.pop(key, None)
                continue
            if key in SECRET_KEYS and str(value).startswith("••••••"):
                continue
            coerced = _coerce(value, kind)
            if kind is int and not 1 <= int(coerced) <= 64:
                raise ValueError(f"{key} 必须在 1–64 之间")
            if key == "magpie_base_url" and not str(coerced).startswith(("http://", "https://")):
                raise ValueError("Magpie 地址需要以 http:// 或 https:// 开头")
            stored[key] = coerced
        self.data_dir.mkdir(parents=True, exist_ok=True)
        temporary = self.settings_file.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(stored, ensure_ascii=False, indent=2), "utf-8")
        temporary.replace(self.settings_file)
        with self._lock:
            self._file_cache = None
        return self.public()

    def public(self) -> dict:
        """Editable settings for the UI (API keys are masked)."""
        result = {}
        for key in EDITABLE:
            value = getattr(self, key)
            if key in SECRET_KEYS:
                value = "" if value in ("", "magpie") else "••••••" + str(value)[-4:]
            result[key] = {"value": value, "source": self.source(key), "default": FIELDS[key][1]}
        return result

    @property
    def helper_model(self) -> str:
        return (self.assist_model or "").strip() or self.default_model


settings = Settings()
