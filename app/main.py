from __future__ import annotations

import asyncio
import hmac
import re
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote

import httpx
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import access, arxiv, assist, auth, digest, export, notes, search, zotero
from .compile import has_tex
from .config import settings
from .fonts import cjk_fonts
from .jobs import store
from .magpie import MagpieError, list_models
from .pipeline import runner_for


@asynccontextmanager
async def lifespan(_app: FastAPI):
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    store.load()
    store.on_done = assist.auto_guide
    scheduler = asyncio.create_task(digest.scheduler())
    yield
    scheduler.cancel()


app = FastAPI(title="鹊桥 Queqiao", lifespan=lifespan)

GATEWAY_SECRET_HEADER = "x-hub-node-secret"


def base_path() -> str:
    """Normalized URL prefix for deployments behind a path-based reverse proxy."""
    value = (settings.base_path or "").strip().strip("/")
    return f"/{value}" if value else ""


PUBLIC_PATHS = {"/login", "/logout", "/favicon.ico"}


def safe_next(target: str | None) -> str:
    """App-relative path to return to after login; anything else falls back to the home page."""
    target = (target or "").strip()
    if not target.startswith("/") or target.startswith("//") or "\\" in target or len(target) > 2000:
        return "/"
    if any(ord(char) < 32 for char in target) or target.split("?", 1)[0] in PUBLIC_PATHS:
        return "/"
    return target


def client_key(request: Request) -> str:
    forwarded = request.headers.get("cf-connecting-ip") or request.headers.get("x-forwarded-for", "")
    forwarded = forwarded.split(",", 1)[0].strip()
    return forwarded or (request.client.host if request.client else "unknown")


# (method, path regex, permission). Anything not listed that is not a GET/HEAD needs "write".
PERMISSIONS: list[tuple[str, re.Pattern, str]] = [
    ("POST", re.compile(r"^/api/arxiv$"), "translate"),
    ("POST", re.compile(r"^/api/upload$"), "translate"),
    ("POST", re.compile(r"^/api/jobs/[0-9a-f]{12}/retry$"), "translate"),
    ("POST", re.compile(r"^/api/search$"), "ai"),
    ("POST", re.compile(r"^/api/jobs/[0-9a-f]{12}/ask$"), "ai"),
    ("POST", re.compile(r"^/api/jobs/[0-9a-f]{12}/(guide|references)$"), "ai"),
    ("POST", re.compile(r"^/api/digest/run$"), "admin"),
    ("PUT", re.compile(r"^/api/digest/profile$"), "admin"),
    ("DELETE", re.compile(r"^/api/digest/profile$"), "admin"),
    ("DELETE", re.compile(r"^/api/jobs/[0-9a-f]{12}$"), "admin"),
    ("PUT", re.compile(r"^/api/settings$"), "admin"),
    ("POST", re.compile(r"^/api/settings/test$"), "admin"),
    ("POST", re.compile(r"^/api/zotero/test$"), "admin"),
    ("POST", re.compile(r"^/api/jobs/[0-9a-f]{12}/zotero$"), "admin"),
    ("GET", re.compile(r"^/api/access/codes$"), "admin"),
    ("POST", re.compile(r"^/api/access/codes"), "admin"),
]


def permission_for(method: str, path: str) -> str | None:
    if path in ("/api/access/me",):
        return None
    for verb, pattern, permission in PERMISSIONS:
        if method == verb and pattern.match(path):
            return permission
    return None if method in ("GET", "HEAD", "OPTIONS") else "write"


@app.middleware("http")
async def access_guard(request: Request, call_next):
    """Enforce the gateway secret, the optional login password and invite-code permissions."""
    secret = settings.gateway_secret
    if secret:
        given = request.headers.get(GATEWAY_SECRET_HEADER, "")
        if not hmac.compare_digest(given, secret):
            return JSONResponse({"detail": "forbidden"}, status_code=403)
    path = request.scope.get("path", "/")
    if auth.enabled() and path not in PUBLIC_PATHS and not path.startswith("/static/"):
        if not auth.session_valid(request.cookies.get(auth.COOKIE)):
            if path.startswith("/api/"):
                return JSONResponse({"detail": "需要登录"}, status_code=401)
            target = path + (f"?{request.url.query}" if request.url.query else "")
            return RedirectResponse(
                f"{base_path()}/login?next={quote(target, safe='')}", status_code=303
            )
    identity = access.identify(request.headers.get(access.HEADER))
    request.state.identity = identity
    if path.startswith("/api/"):
        permission = permission_for(request.method, path)
        if permission:
            try:
                access.require(identity, permission)
            except access.AccessDenied as exc:
                return JSONResponse({"detail": exc.message, "code": "invite"}, status_code=exc.status)
    return await call_next(request)


class StripBasePath:
    """ASGI wrapper: accept both /queqiao/... (proxy keeps the prefix) and /... (proxy strips it)."""

    def __init__(self, inner):
        self.inner = inner

    async def __call__(self, scope, receive, send):
        prefix = base_path()
        if prefix and scope.get("type") in ("http", "websocket"):
            path = scope.get("path", "")
            if path == prefix or path.startswith(prefix + "/"):
                scope = dict(scope)
                scope["path"] = path[len(prefix):] or "/"
                raw = scope.get("raw_path")
                if raw:
                    scope["raw_path"] = raw[len(prefix.encode()):] or b"/"
        await self.inner(scope, receive, send)


def page(name: str) -> HTMLResponse:
    """Serve an HTML page; under a prefix, root-relative URLs are rewritten to include it."""
    html = (web_dir / name).read_text("utf-8")
    prefix = base_path()
    if prefix:
        for attribute in ('href="/', 'src="/', 'action="/'):
            html = html.replace(attribute, attribute[:-1] + prefix + "/")
        html = html.replace("<head>", f'<head>\n  <script>window.QQ_BASE = "{prefix}";</script>', 1)
    return HTMLResponse(html, headers={"Cache-Control": "no-store"})


class ArxivRequest(BaseModel):
    input: str
    model: str | None = None
    force: bool = False


class SettingsRequest(BaseModel):
    magpie_base_url: str | None = None
    magpie_api_key: str | None = None
    default_model: str | None = None
    assist_model: str | None = None
    preferred_models: list[str] | str | None = None
    concurrency: int | None = None
    pdf_qps: int | None = None
    zotero_user_id: str | None = None
    zotero_api_key: str | None = None
    zotero_collection: str | None = None


def pick_model(requested: str | None) -> str:
    model = (requested or "").strip() or settings.default_model
    if len(model) > 200 or any(char.isspace() for char in model):
        raise HTTPException(400, "模型名称不合法")
    return model


@app.get("/api/health")
async def health() -> dict:
    magpie_ok = False
    model_count = 0
    try:
        model_count = len(await list_models())
        magpie_ok = True
    except (httpx.HTTPError, ValueError):
        pass
    return {
        "magpie": {"ok": magpie_ok, "base_url": settings.magpie_base_url, "models": model_count},
        "latex": has_tex(),
        "fonts": cjk_fonts(),
        "default_model": settings.default_model,
        "assist_model": settings.helper_model,
        "data_dir": str(settings.data_dir),
        "auth": auth.enabled(),
        "access_mode": "invite" if access.invite_mode() else "open",
        "zotero": zotero.configured(),
        "digest_hour": settings.digest_hour,
    }


@app.get("/api/models")
async def models() -> dict:
    try:
        items = await list_models()
    except (httpx.HTTPError, ValueError) as exc:
        raise HTTPException(502, f"无法连接 Magpie：{exc}") from exc
    return {"default": settings.default_model, "models": items}


@app.get("/api/settings")
async def get_settings(request: Request) -> dict:
    identity = request.state.identity
    if access.invite_mode() and not (identity and identity.admin):
        return {"settings": {}, "restricted": True}
    return {"settings": settings.public(), "data_dir": str(settings.data_dir), "settings_file": str(settings.settings_file)}


@app.put("/api/settings")
async def put_settings(request: SettingsRequest) -> dict:
    changes = request.model_dump(exclude_unset=True)
    try:
        updated = settings.update(changes)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"settings": updated, "data_dir": str(settings.data_dir), "settings_file": str(settings.settings_file)}


@app.post("/api/settings/test")
async def test_settings(request: SettingsRequest) -> dict:
    """Check a Magpie address/key without saving it."""
    base_url = (request.magpie_base_url or settings.magpie_base_url).rstrip("/")
    key = request.magpie_api_key or settings.magpie_api_key
    if key.startswith("••••••"):
        key = settings.magpie_api_key
    try:
        async with httpx.AsyncClient(base_url=base_url, headers={"Authorization": f"Bearer {key}"}, timeout=8) as client:
            response = await client.get("/models")
        response.raise_for_status()
        count = len(response.json().get("data") or [])
    except (httpx.HTTPError, ValueError) as exc:
        return {"ok": False, "error": f"{exc.__class__.__name__}: {str(exc)[:200]}"}
    return {"ok": True, "models": count}


@app.post("/api/arxiv")
async def submit_arxiv(request: ArxivRequest) -> dict:
    arxiv_id = arxiv.parse_arxiv_id(request.input)
    if not arxiv_id:
        raise HTTPException(400, "无法识别 arXiv 地址或 ID")
    model = pick_model(request.model)
    if not request.force:
        existing = store.find_reusable_arxiv(arxiv_id, model)
        if existing:
            return existing.public()
    job = store.create("arxiv", model, arxiv_id=arxiv_id, title=f"arXiv:{arxiv_id}", meta={"requested_id": arxiv_id})
    store.submit(job, runner_for(job))
    return job.public()


@app.get("/api/arxiv/lookup")
async def lookup_arxiv(id: str) -> dict:
    """An existing translation of this paper (any model), so visitors can read without submitting."""
    arxiv_id = arxiv.parse_arxiv_id(id)
    if not arxiv_id:
        raise HTTPException(400, "无法识别 arXiv 地址或 ID")
    wanted = arxiv.base_id(arxiv_id)
    matches = [
        job
        for job in store.list()
        if job.kind == "arxiv"
        and wanted in (arxiv.base_id(job.arxiv_id or ""), arxiv.base_id(job.meta.get("requested_id", "")))
        and job.status != "failed"
    ]
    matches.sort(key=lambda job: (job.model != settings.default_model, job.status != "done"))
    return {"job": matches[0].public() if matches else None}


# ---------------------------------------------------------------------------
# Invite codes
# ---------------------------------------------------------------------------


class CodeRequest(BaseModel):
    name: str
    role: str = "member"
    translate: int | None = None
    ai: int | None = None


@app.get("/api/access/me")
async def access_me(request: Request) -> dict:
    return access.describe(request.state.identity)


@app.get("/api/access/codes")
async def access_codes() -> dict:
    return {"codes": [access.public_entry(entry) for entry in access.load_codes()], "mode": settings.access_mode}


@app.post("/api/access/codes")
async def access_create(request: CodeRequest) -> dict:
    try:
        code, entry = access.create_code(request.name, request.role, request.translate, request.ai)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"code": code, "entry": entry}


@app.post("/api/access/codes/{code_id}/revoke")
async def access_revoke(code_id: str, request: Request) -> dict:
    identity = request.state.identity
    if identity and identity.id == code_id:
        raise HTTPException(400, "不能停用当前正在使用的邀请码")
    if not access.revoke(code_id):
        raise HTTPException(404, "未找到该邀请码")
    return {"ok": True}


# ---------------------------------------------------------------------------
# Fuzzy search
# ---------------------------------------------------------------------------


class SearchRequest(BaseModel):
    q: str


@app.post("/api/search")
async def search_papers(request: SearchRequest) -> dict:
    try:
        result = await search.search(request.q)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except (arxiv.ArxivError, httpx.HTTPError) as exc:
        raise HTTPException(502, f"arXiv 检索失败：{exc}") from exc
    translated = {}
    for job in store.list():
        if job.kind == "arxiv" and job.arxiv_id and job.status != "failed":
            translated.setdefault(arxiv.base_id(job.arxiv_id), job.public())
    for item in result["results"]:
        existing = translated.get(arxiv.base_id(item["arxiv_id"]))
        item["job"] = {"id": existing["id"], "status": existing["status"], "arxiv_id": existing["arxiv_id"], "kind": "arxiv"} if existing else None
    return result


# ---------------------------------------------------------------------------
# Reading assistant
# ---------------------------------------------------------------------------


class AskRequest(BaseModel):
    selection: str = ""
    question: str = ""
    mode: str = "ask"
    page: int | None = None
    side: str = "translated"
    thread: list[dict] = []


class ForceRequest(BaseModel):
    force: bool = False


def job_or_404(job_id: str):
    job = store.get(job_id)
    if not job:
        raise HTTPException(404, "任务不存在")
    return job


@app.get("/api/jobs/{job_id}/assist")
async def job_assist(job_id: str) -> dict:
    job = job_or_404(job_id)
    return {
        "guide": assist.read_state(job, "guide"),
        "references": assist.read_state(job, "references"),
        "qa": assist.qa_history(job),
        "model": settings.helper_model,
    }


@app.post("/api/jobs/{job_id}/guide")
async def job_guide(job_id: str, request: ForceRequest) -> dict:
    return assist.start(job_or_404(job_id), "guide", request.force)


@app.post("/api/jobs/{job_id}/references")
async def job_references(job_id: str, request: ForceRequest) -> dict:
    return assist.start(job_or_404(job_id), "references", request.force)


@app.post("/api/jobs/{job_id}/ask")
async def job_ask(job_id: str, body: AskRequest, request: Request) -> dict:
    job = job_or_404(job_id)
    identity = request.state.identity
    try:
        return await assist.ask(
            job, body.selection, body.question, body.mode, body.page, body.side, body.thread, identity.name if identity else ""
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except MagpieError as exc:
        raise HTTPException(502, f"模型调用失败：{exc}") from exc


@app.delete("/api/jobs/{job_id}/qa/{item_id}")
async def job_qa_delete(job_id: str, item_id: str) -> dict:
    await asyncio.to_thread(assist.delete_qa, job_or_404(job_id), item_id)
    return {"ok": True}


def public_reader_url(request: Request, job) -> str:
    proto = request.headers.get("x-forwarded-proto", request.url.scheme)
    host = request.headers.get("host") or request.url.netloc
    path = f"/arxiv/{job.arxiv_id}?job={job.id}" if job.kind == "arxiv" and job.arxiv_id else f"/job/{job.id}"
    return f"{proto}://{host}{base_path()}{path}"


@app.get("/api/jobs/{job_id}/export.md")
async def job_export(job_id: str, request: Request) -> PlainTextResponse:
    job = job_or_404(job_id)
    text = await asyncio.to_thread(export.markdown, job, public_reader_url(request, job))
    stem = (job.arxiv_id or job.title or job.id).replace("/", "_")
    return PlainTextResponse(
        text,
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(stem)}.notes.md", "Cache-Control": "no-store"},
    )


# ---------------------------------------------------------------------------
# Zotero
# ---------------------------------------------------------------------------


@app.post("/api/zotero/test")
async def zotero_test() -> dict:
    try:
        return await zotero.test()
    except (zotero.ZoteroError, httpx.HTTPError) as exc:
        return {"ok": False, "error": str(exc)[:300]}


@app.post("/api/jobs/{job_id}/zotero")
async def job_zotero(job_id: str, request: Request) -> dict:
    job = job_or_404(job_id)
    try:
        return await zotero.sync(job, public_reader_url(request, job))
    except zotero.ZoteroError as exc:
        raise HTTPException(400, str(exc)) from exc
    except httpx.HTTPError as exc:
        raise HTTPException(502, f"无法连接 Zotero：{exc}") from exc


# ---------------------------------------------------------------------------
# Daily digest
# ---------------------------------------------------------------------------


@app.get("/api/digest")
async def digest_index() -> dict:
    return {"dates": digest.dates(), "status": digest.status(), "profile": digest.profile(), "hour": settings.digest_hour}


@app.get("/api/digest/{day}")
async def digest_day(day: str) -> dict:
    report = digest.load(day)
    if not report:
        raise HTTPException(404, "这一天没有日报")
    translated = {}
    for job in store.list():
        if job.kind == "arxiv" and job.arxiv_id and job.status != "failed":
            translated.setdefault(arxiv.base_id(job.arxiv_id), {"id": job.id, "status": job.status, "arxiv_id": job.arxiv_id, "kind": "arxiv"})
    for item in report.get("items", []):
        item["job"] = translated.get(item["arxiv_id"])
    return report


@app.post("/api/digest/run")
async def digest_run(request: ForceRequest) -> dict:
    return digest.start(force=request.force)


@app.put("/api/digest/profile")
async def digest_profile(request: Request) -> dict:
    try:
        return digest.save_profile(await request.json())
    except (ValueError, TypeError) as exc:
        raise HTTPException(400, str(exc)) from exc


@app.delete("/api/digest/profile")
async def digest_profile_reset() -> dict:
    return digest.reset_profile()


@app.post("/api/upload")
async def submit_upload(
    file: UploadFile = File(...),
    model: str | None = Form(None),
    pages: str | None = Form(None),
    glossary: UploadFile | None = File(None),
) -> dict:
    chosen = pick_model(model)
    name = Path(file.filename or "document.pdf").name
    if not name.lower().endswith(".pdf"):
        raise HTTPException(400, "目前只支持 PDF 文件")
    if pages and not all(part.strip().replace("-", "").isdigit() for part in pages.split(",")):
        raise HTTPException(400, "页码范围格式应为 1-3,5")
    job = store.create("upload", chosen, title=Path(name).stem, source_name=name, meta={"pages": pages or ""})
    limit = settings.max_upload_mb * 1024 * 1024
    target = job.directory / "original.pdf"
    size = 0
    with target.open("wb") as handle:
        while chunk := await file.read(1024 * 1024):
            size += len(chunk)
            if size > limit:
                handle.close()
                store.delete(job)
                raise HTTPException(413, f"文件超过 {settings.max_upload_mb}MB")
            handle.write(chunk)
    with target.open("rb") as handle:
        if handle.read(5) != b"%PDF-":
            store.delete(job)
            raise HTTPException(400, "文件不是有效的 PDF")
    if glossary is not None and glossary.filename:
        data = await glossary.read(2 * 1024 * 1024)
        (job.directory / "glossary.csv").write_bytes(data)
    store.add_artifact(job, "original")
    store.submit(job, runner_for(job))
    return job.public()


@app.get("/api/jobs")
async def jobs() -> list[dict]:
    return [job.public() for job in store.list()]


@app.get("/api/jobs/{job_id}")
async def job_detail(job_id: str) -> dict:
    job = store.get(job_id)
    if not job:
        raise HTTPException(404, "任务不存在")
    return job.public()


@app.post("/api/jobs/{job_id}/retry")
async def retry_job(job_id: str) -> dict:
    job = store.get(job_id)
    if not job:
        raise HTTPException(404, "任务不存在")
    if job.status in ("queued", "running"):
        return job.public()
    # A retry rebuilds the translated PDF, so notes drawn on the old layout would no longer line up.
    await asyncio.to_thread(notes.clear, job, "translated")
    store.update(job, status="queued", stage="排队中", progress=0, error=None, warnings=[], finished_at=None)
    store.submit(job, runner_for(job))
    return job.public()


@app.delete("/api/jobs/{job_id}")
async def delete_job(job_id: str) -> dict:
    job = store.get(job_id)
    if not job:
        raise HTTPException(404, "任务不存在")
    await asyncio.to_thread(store.delete, job)
    return {"ok": True}


@app.get("/api/jobs/{job_id}/files/{name}")
async def job_file(job_id: str, name: str, download: bool = False, clean: bool = False):
    job = store.get(job_id)
    if not job:
        raise HTTPException(404, "任务不存在")
    path = store.artifact_path(job, name)
    if not path:
        raise HTTPException(404, "文件不存在")
    annotated = notes.annotated_path(job, name) if name in notes.NOTE_TARGETS else None
    with_notes = bool(annotated and annotated.is_file() and not clean)
    if with_notes:
        path = annotated
    stem = (job.arxiv_id or job.title or job.id).replace("/", "_")
    suffix = ".notes" if with_notes else ""
    filenames = {
        "original": f"{stem}{suffix}.pdf",
        "translated": f"{stem}.zh{suffix}.pdf",
        "dual": f"{stem}.dual.pdf",
        "source": f"{stem}.zh-source.zip",
        "log": f"{stem}.build.log",
    }
    media = {"source": "application/zip", "log": "text/plain; charset=utf-8"}.get(name, "application/pdf")
    disposition = "attachment" if download else "inline"
    return FileResponse(
        path,
        media_type=media,
        filename=filenames[name],
        content_disposition_type=disposition,
        headers={"Cache-Control": "no-store"},
    )


@app.get("/api/jobs/{job_id}/notes")
async def job_notes(job_id: str) -> dict:
    job = store.get(job_id)
    if not job:
        raise HTTPException(404, "任务不存在")
    return await asyncio.to_thread(notes.summary, job)


@app.put("/api/jobs/{job_id}/notes/{target}")
async def save_notes(job_id: str, target: str, request: Request) -> dict:
    job = store.get(job_id)
    if not job:
        raise HTTPException(404, "任务不存在")
    if target not in job.artifacts:
        raise HTTPException(404, "文件不存在")
    data = await request.body()
    try:
        return await asyncio.to_thread(notes.save, job, target, data)
    except notes.NotesError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.delete("/api/jobs/{job_id}/notes/{target}")
async def clear_notes(job_id: str, target: str) -> dict:
    job = store.get(job_id)
    if not job:
        raise HTTPException(404, "任务不存在")
    try:
        await asyncio.to_thread(notes.clear, job, target)
    except notes.NotesError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"ok": True}


web_dir = settings.web_dir


@app.get("/", response_class=HTMLResponse)
async def index() -> HTMLResponse:
    return page("index.html")


@app.get("/arxiv/{arxiv_path:path}")
async def arxiv_page(arxiv_path: str) -> HTMLResponse:
    return page("reader.html")


@app.get("/job/{job_id}")
async def job_page(job_id: str) -> HTMLResponse:
    return page("reader.html")


@app.get("/library")
async def library_page() -> HTMLResponse:
    return page("index.html")


@app.get("/settings")
async def settings_page() -> HTMLResponse:
    return page("index.html")


@app.get("/digest")
async def digest_page() -> HTMLResponse:
    return page("index.html")


@app.get("/history")
async def history_page() -> RedirectResponse:
    return RedirectResponse(f"{base_path()}/library")


@app.get("/about")
async def about_page() -> HTMLResponse:
    return page("index.html")


@app.get("/abs/{arxiv_path:path}")
async def abs_redirect(arxiv_path: str) -> RedirectResponse:
    return RedirectResponse(f"{base_path()}/arxiv/{arxiv_path}")


@app.get("/login")
async def login_page(request: Request):
    if not auth.enabled() or auth.session_valid(request.cookies.get(auth.COOKIE)):
        return RedirectResponse(f"{base_path()}{safe_next(request.query_params.get('next'))}", 303)
    return page("login.html")


@app.post("/login")
async def login_submit(
    request: Request, password: str = Form(""), next_path: str = Form("/", alias="next")
) -> RedirectResponse:
    target = safe_next(next_path)
    if not auth.enabled():
        return RedirectResponse(f"{base_path()}{target}", 303)
    key = client_key(request)
    retry = f"{base_path()}/login?next={quote(target, safe='')}"
    if auth.throttle.blocked(key):
        return RedirectResponse(f"{retry}&error=locked", 303)
    if not await asyncio.to_thread(auth.verify_password, password):
        auth.throttle.failed(key)
        return RedirectResponse(f"{retry}&error=1", 303)
    auth.throttle.reset(key)
    response = RedirectResponse(f"{base_path()}{target}", 303)
    response.set_cookie(
        auth.COOKIE,
        auth.issue_session(),
        max_age=int(settings.auth_max_age),
        path=f"{base_path()}/",
        httponly=True,
        samesite="lax",
        secure=request.headers.get("x-forwarded-proto", request.url.scheme) == "https",
    )
    return response


@app.get("/logout")
async def logout() -> RedirectResponse:
    response = RedirectResponse(f"{base_path()}/login", 303)
    response.delete_cookie(auth.COOKIE, path=f"{base_path()}/")
    return response


@app.exception_handler(HTTPException)
async def http_error(_request: Request, exc: HTTPException) -> JSONResponse:
    return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)


app.mount("/static", StaticFiles(directory=web_dir), name="static")

# uvicorn serves `asgi`; it accepts requests with or without the base-path prefix.
asgi = StripBasePath(app)
