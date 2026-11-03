from __future__ import annotations

import asyncio
import hmac
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import arxiv, notes
from .compile import has_tex
from .config import settings
from .fonts import cjk_fonts
from .jobs import store
from .magpie import list_models
from .pipeline import runner_for


@asynccontextmanager
async def lifespan(_app: FastAPI):
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    store.load()
    yield


app = FastAPI(title="鹊桥 Queqiao", lifespan=lifespan)

GATEWAY_SECRET_HEADER = "x-hub-node-secret"


def base_path() -> str:
    """Normalized URL prefix for deployments behind a path-based reverse proxy."""
    value = (settings.base_path or "").strip().strip("/")
    return f"/{value}" if value else ""


@app.middleware("http")
async def gateway_guard(request: Request, call_next):
    """When a gateway secret is configured, only accept requests that came through the gateway."""
    secret = settings.gateway_secret
    if secret:
        given = request.headers.get(GATEWAY_SECRET_HEADER, "")
        if not hmac.compare_digest(given, secret):
            return JSONResponse({"detail": "forbidden"}, status_code=403)
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
    preferred_models: list[str] | str | None = None
    concurrency: int | None = None
    pdf_qps: int | None = None


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
        "data_dir": str(settings.data_dir),
    }


@app.get("/api/models")
async def models() -> dict:
    try:
        items = await list_models()
    except (httpx.HTTPError, ValueError) as exc:
        raise HTTPException(502, f"无法连接 Magpie：{exc}") from exc
    return {"default": settings.default_model, "models": items}


@app.get("/api/settings")
async def get_settings() -> dict:
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


@app.get("/history")
async def history_page() -> RedirectResponse:
    return RedirectResponse(f"{base_path()}/library")


@app.get("/about")
async def about_page() -> HTMLResponse:
    return page("index.html")


@app.get("/abs/{arxiv_path:path}")
async def abs_redirect(arxiv_path: str) -> RedirectResponse:
    return RedirectResponse(f"{base_path()}/arxiv/{arxiv_path}")


@app.exception_handler(HTTPException)
async def http_error(_request: Request, exc: HTTPException) -> JSONResponse:
    return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)


app.mount("/static", StaticFiles(directory=web_dir), name="static")

# uvicorn serves `asgi`; it accepts requests with or without the base-path prefix.
asgi = StripBasePath(app)
