"""Background jobs: every run is a job that finishes inline or can be polled, waited on and cancelled."""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import anyio
from anyio.abc import TaskGroup

log = logging.getLogger(__name__)

Work = Callable[["Job"], Awaitable[dict[str, Any]]]
Reporter = Callable[[int, int, str], Awaitable[None]]
FINAL = {"completed", "partial", "failed", "timed_out", "cancelled"}


@dataclass
class Job:
    id: str
    kind: str
    status: str = "queued"
    progress: str | None = None
    result: dict[str, Any] | None = None
    error: str | None = None
    done: anyio.Event = field(default_factory=anyio.Event)
    scope: anyio.CancelScope = field(default_factory=anyio.CancelScope)
    reporters: list[Reporter] = field(default_factory=list)

    async def report(self, done: int, total: int, message: str) -> None:
        self.progress = message
        for reporter in list(self.reporters):
            try:
                await reporter(done, total, message)
            except Exception:
                self.reporters.remove(reporter)

    def snapshot(self) -> dict[str, Any]:
        return {
            "job_id": self.id,
            "kind": self.kind,
            "status": self.status,
            "progress": self.progress,
            "error": self.error,
            "result": self.result,
        }


class Jobs:
    def __init__(self, root: Callable[[], Path]):
        self._root = root
        self._jobs: dict[str, Job] = {}
        self.tasks: TaskGroup | None = None

    def _summary(self, job_id: str) -> Path:
        return self._root() / job_id / "summary.json"

    async def submit(self, job_id: str, kind: str, work: Work, wait_seconds: float, reporter: Reporter | None) -> dict:
        if self.tasks is None:
            raise RuntimeError("the job runner is not started")
        job = Job(job_id, kind)
        self._jobs[job_id] = job
        self.tasks.start_soon(self._run, job, work)
        return await self.wait(job_id, wait_seconds, reporter)

    async def _run(self, job: Job, work: Work) -> None:
        job.status = "running"
        try:
            with job.scope:
                job.result = await work(job)
                job.status = job.result.get("status", "completed")
            if job.scope.cancel_called:
                job.status = "cancelled"
        except anyio.get_cancelled_exc_class():
            job.status = "cancelled"
            raise
        except Exception as error:
            log.exception("job %s failed", job.id)
            job.status, job.error = "failed", str(error)
        finally:
            self._persist(job)
            job.done.set()

    def _persist(self, job: Job) -> None:
        path = self._summary(job.id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(job.snapshot(), indent=1))

    async def wait(self, job_id: str, seconds: float, reporter: Reporter | None = None) -> dict:
        job = self._jobs.get(job_id)
        if job is None:
            return self.get(job_id)
        if reporter:
            job.reporters.append(reporter)
        try:
            with anyio.move_on_after(seconds):
                await job.done.wait()
        finally:
            if reporter in job.reporters:
                job.reporters.remove(reporter)
        return job.snapshot()

    def get(self, job_id: str) -> dict:
        job = self._jobs.get(job_id)
        if job is not None:
            return job.snapshot()
        path = self._summary(job_id)
        if not path.exists():
            raise KeyError(job_id)
        return json.loads(path.read_text())

    def cancel(self, job_id: str) -> dict:
        job = self._jobs.get(job_id)
        if job is None:
            return self.get(job_id)
        if job.status not in FINAL:
            job.scope.cancel()
        return job.snapshot()

    async def stop(self) -> None:
        """Cancel every unfinished job and wait until each has cleaned up."""
        pending = [job for job in self._jobs.values() if job.status not in FINAL]
        for job in pending:
            job.scope.cancel()
        for job in pending:
            await job.done.wait()

    def list(self, limit: int) -> list[dict]:
        live = {j.id: j.snapshot() for j in self._jobs.values()}
        root = self._root()
        stored = sorted((p.name for p in root.iterdir() if p.is_dir()), reverse=True) if root.exists() else []
        ids = sorted(set(live) | set(stored), reverse=True)[:limit]
        entries = []
        for job_id in ids:
            try:
                entry = live.get(job_id) or self.get(job_id)
            except KeyError:
                continue
            entries.append({k: entry.get(k) for k in ("job_id", "kind", "status", "progress", "error")})
        return entries
