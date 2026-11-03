from __future__ import annotations

import asyncio
import json
import re
import shutil
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Awaitable, Callable

from .config import settings

ACTIVE_STATES = {"queued", "running"}
ARTIFACT_NAMES = {
    "original": "original.pdf",
    "translated": "translated.pdf",
    "dual": "dual.pdf",
    "source": "translated-source.zip",
    "log": "build.log",
}


@dataclass
class Job:
    id: str
    kind: str
    model: str
    title: str = ""
    arxiv_id: str | None = None
    source_name: str | None = None
    status: str = "queued"
    stage: str = "排队中"
    progress: float = 0.0
    message: str = ""
    error: str | None = None
    warnings: list[str] = field(default_factory=list)
    pipeline: str | None = None
    artifacts: list[str] = field(default_factory=list)
    meta: dict = field(default_factory=dict)
    usage: dict = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    finished_at: float | None = None

    @property
    def directory(self) -> Path:
        return settings.jobs_dir / self.id

    def public(self) -> dict:
        data = asdict(self)
        data["artifact_urls"] = {name: f"/api/jobs/{self.id}/files/{name}" for name in self.artifacts}
        notes_root = self.directory / "notes"
        data["notes"] = {
            target: (notes_root / f"{target}.annotated.pdf").is_file() for target in ("translated", "original")
        }
        return data


class JobStore:
    def __init__(self) -> None:
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()
        self._semaphore: asyncio.Semaphore | None = None
        self._tasks: dict[str, asyncio.Task] = {}

    def load(self) -> None:
        settings.jobs_dir.mkdir(parents=True, exist_ok=True)
        for meta_path in sorted(settings.jobs_dir.glob("*/job.json")):
            try:
                raw = json.loads(meta_path.read_text("utf-8"))
                job = Job(**raw)
            except (OSError, ValueError, TypeError):
                continue
            if job.status in ACTIVE_STATES:
                job.status = "failed"
                job.error = "服务重启时任务被中断，请重新提交。"
                job.stage = "已中断"
                self._write(job)
            self._jobs[job.id] = job

    def _write(self, job: Job) -> None:
        job.directory.mkdir(parents=True, exist_ok=True)
        tmp = job.directory / "job.json.tmp"
        tmp.write_text(json.dumps(asdict(job), ensure_ascii=False, indent=2), "utf-8")
        tmp.replace(job.directory / "job.json")

    def create(self, kind: str, model: str, **fields) -> Job:
        job = Job(id=uuid.uuid4().hex[:12], kind=kind, model=model, **fields)
        with self._lock:
            self._jobs[job.id] = job
            self._write(job)
        return job

    def get(self, job_id: str) -> Job | None:
        if not re.fullmatch(r"[0-9a-f]{12}", job_id or ""):
            return None
        return self._jobs.get(job_id)

    def list(self) -> list[Job]:
        return sorted(self._jobs.values(), key=lambda job: job.created_at, reverse=True)

    def find_reusable_arxiv(self, arxiv_id: str, model: str) -> Job | None:
        for job in self.list():
            if (
                job.kind == "arxiv"
                and job.model == model
                and arxiv_id in (job.arxiv_id, job.meta.get("requested_id"))
                and job.status in ACTIVE_STATES | {"done"}
            ):
                return job
        return None

    def update(self, job: Job, **fields) -> None:
        with self._lock:
            for key, value in fields.items():
                setattr(job, key, value)
            job.updated_at = time.time()
            self._write(job)

    def add_artifact(self, job: Job, name: str) -> None:
        if name not in job.artifacts:
            self.update(job, artifacts=[*job.artifacts, name])

    def warn(self, job: Job, message: str) -> None:
        if message not in job.warnings:
            self.update(job, warnings=[*job.warnings, message])

    def delete(self, job: Job) -> None:
        task = self._tasks.pop(job.id, None)
        if task and not task.done():
            task.cancel()
        with self._lock:
            self._jobs.pop(job.id, None)
        trash = job.directory.with_name(f".deleted-{job.id}")
        if job.directory.exists():
            job.directory.rename(trash)
            shutil.rmtree(trash, ignore_errors=True)

    def submit(self, job: Job, runner: Callable[[Job], Awaitable[None]]) -> None:
        if self._semaphore is None:
            self._semaphore = asyncio.Semaphore(settings.max_jobs)
        semaphore = self._semaphore

        async def wrapped() -> None:
            async with semaphore:
                self.update(job, status="running", stage="准备中", progress=1.0)
                try:
                    await runner(job)
                except asyncio.CancelledError:
                    self.update(job, status="failed", error="任务已取消", stage="已取消", finished_at=time.time())
                    raise
                except Exception as exc:
                    self.update(
                        job,
                        status="failed",
                        error=str(exc) or exc.__class__.__name__,
                        stage="失败",
                        finished_at=time.time(),
                    )
                else:
                    if job.status == "running":
                        self.update(job, status="done", stage="完成", progress=100.0, finished_at=time.time())
                finally:
                    self._tasks.pop(job.id, None)

        self._tasks[job.id] = asyncio.create_task(wrapped())

    def artifact_path(self, job: Job, name: str) -> Path | None:
        filename = ARTIFACT_NAMES.get(name)
        if not filename or name not in job.artifacts:
            return None
        path = job.directory / filename
        return path if path.is_file() else None


store = JobStore()
