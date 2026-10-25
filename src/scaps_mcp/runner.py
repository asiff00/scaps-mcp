"""Run SCAPS scripts headless and collect their output files."""

from __future__ import annotations

import contextlib
import logging
import os
import secrets
import shutil
import signal
import subprocess
import sys
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

if sys.platform == "win32":
    import msvcrt
else:
    import fcntl

import anyio
from anyio.abc import Process

from . import processes
from .config import Settings
from .parsing import LogEntry, parse_error_log

log = logging.getLogger(__name__)

ENCODING = "cp1252"
ERROR_LOG = "SCAPSErrorLogFile.log"
LOCK_FILE = ".scaps-run.lock"
POLL_SECONDS = 1.0

Progress = Callable[[int, int], Awaitable[None]]


@dataclass
class RunOutcome:
    run_id: str
    directory: Path
    files: dict[str, Path] = field(default_factory=dict)
    errors: list[LogEntry] = field(default_factory=list)
    timed_out: bool = False
    seconds: float = 0.0


@contextlib.contextmanager
def installation_lock(home: Path):
    """Exclusive lock on a SCAPS installation, shared by every process that runs SCAPS from it."""
    with open(home / LOCK_FILE, "a+") as handle:
        _lock(handle, True)
        try:
            yield
        finally:
            _lock(handle, False)


def _lock(handle, acquire: bool) -> None:
    if sys.platform != "win32":
        fcntl.flock(handle, fcntl.LOCK_EX if acquire else fcntl.LOCK_UN)
        return
    handle.seek(0)
    if not acquire:
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        return
    while True:
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            return
        except OSError:
            continue


def new_run_id() -> str:
    return datetime.now(UTC).strftime("%Y%m%d-%H%M%S-") + secrets.token_hex(3)


class Runner:
    """Serialises SCAPS runs: one Wine prefix supports one SCAPS process at a time."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self._lock = anyio.Lock()

    @property
    def busy(self) -> bool:
        return self._lock.locked()

    def _command(self, script_name: str) -> list[str]:
        s = self.settings
        if s.native:
            return [str(s.exe), script_name]
        command = [s.wine, s.exe.name, script_name]
        return [s.xvfb_run, "-a", *command] if s.xvfb_run else command

    def _env(self) -> dict[str, str]:
        env = dict(os.environ)
        if not self.settings.native:
            env.update(WINEPREFIX=str(self.settings.wineprefix), WINEDEBUG="-all", WINEDLLOVERRIDES="mscoree,mshtml=")
        return env

    def _kill(self, process: Process) -> None:
        if process.returncode is not None:
            return
        if self.settings.native:
            process.kill()
        else:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)

    async def _stop_wine(self, env: dict[str, str]) -> None:
        if self.settings.native or not self.settings.wineserver:
            return
        await anyio.run_process([self.settings.wineserver, "-k"], env=env, stdin=subprocess.DEVNULL, check=False)

    async def run(
        self,
        script: str,
        outputs: list[str],
        *,
        run_id: str | None = None,
        timeout: float | None = None,
        progress: Progress | None = None,
    ) -> RunOutcome:
        home = self.settings.home
        run_id = run_id or new_run_id()
        directory = self.settings.runs_dir / run_id
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "input.script").write_text(script, encoding=ENCODING, newline="")
        script_path = home / "script" / f"mcp_{run_id}.script"
        outcome = RunOutcome(run_id, directory)

        async with self._lock:
            lock = installation_lock(home)
            await anyio.to_thread.run_sync(lock.__enter__)
            try:
                return await self._execute(script, outputs, script_path, outcome, timeout, progress)
            finally:
                lock.__exit__(None, None, None)

    async def _execute(
        self,
        script: str,
        outputs: list[str],
        script_path: Path,
        outcome: RunOutcome,
        timeout: float | None,
        progress: Progress | None,
    ) -> RunOutcome:
        home = self.settings.home
        results = home / "results"
        env = self._env()
        if processes.stop_stale(home, self.settings.exe.name):
            log.warning("stopped a SCAPS process left behind by an interrupted run")
        (home / ERROR_LOG).unlink(missing_ok=True)
        for name in outputs:
            (results / name).unlink(missing_ok=True)
        started = anyio.current_time()
        process: Process | None = None
        try:
            script_path.write_text(script, encoding=ENCODING, newline="")
            process = await anyio.open_process(
                self._command(script_path.name),
                cwd=home,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=not self.settings.native,
            )
            processes.remember(home, process.pid)
            with anyio.move_on_after(timeout or self.settings.timeout) as scope:
                reported = 0
                while process.returncode is None:
                    with anyio.move_on_after(POLL_SECONDS):
                        await process.wait()
                    if progress:
                        done = sum((results / name).exists() for name in outputs)
                        if done > reported:
                            reported = done
                            await progress(done, len(outputs))
            outcome.timed_out = scope.cancelled_caught
        finally:
            with anyio.CancelScope(shield=True):
                if process is not None:
                    self._kill(process)
                    await process.wait()
                    processes.forget(home)
                await self._stop_wine(env)
                outcome.seconds = round(anyio.current_time() - started, 2)
                script_path.unlink(missing_ok=True)
                self._collect(outcome, outputs)
        return outcome

    def _collect(self, outcome: RunOutcome, outputs: list[str]) -> None:
        home = self.settings.home
        log = home / ERROR_LOG
        if log.exists():
            text = log.read_text(encoding=ENCODING, errors="replace")
            (outcome.directory / ERROR_LOG).write_text(text, encoding=ENCODING)
            outcome.errors = parse_error_log(text)
        for name in outputs:
            source = home / "results" / name
            if source.exists():
                target = outcome.directory / name
                shutil.move(source, target)
                outcome.files[name] = target
