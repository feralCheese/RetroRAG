"""Background job queue: long tasks (adding folders, OCR, indexing) run one at a time off the UI thread."""
from __future__ import annotations

import queue
import threading
import traceback
from dataclasses import dataclass, field
from typing import Any, Callable

from PySide6.QtCore import QObject, Signal


@dataclass
class Job:
    name: str
    fn: Callable[["JobContext"], Any]
    on_done: Callable[[Any], None] | None = None
    on_error: Callable[[str], None] | None = None
    id: int = field(default=0)


class JobContext:
    def __init__(self, runner: "JobRunner", job: Job):
        self.runner, self.job = runner, job

    def progress(self, i: int, n: int, msg: str = "") -> None:
        self.runner.progress.emit(i, n, msg)

    def log(self, msg: str) -> None:
        self.runner.log.emit(str(msg))

    def cancelled(self) -> bool:
        return self.runner._cancel.is_set()


class JobRunner(QObject):
    started = Signal(str)
    progress = Signal(int, int, str)
    log = Signal(str)
    finished = Signal(str, object, str)      # name, result, error-text
    queue_changed = Signal(int)
    _deliver = Signal(object, object, str)   # job, result, error (marshals callbacks onto the UI thread)

    def __init__(self):
        super().__init__()
        self._q: queue.Queue[Job | None] = queue.Queue()
        self._cancel = threading.Event()
        self._next_id = 1
        self.busy = False
        self._deliver.connect(self._on_deliver)
        self._thread = threading.Thread(target=self._loop, daemon=True, name="retrorag-jobs")
        self._thread.start()

    def submit(self, name: str, fn, on_done=None, on_error=None) -> int:
        job = Job(name, fn, on_done, on_error, self._next_id)
        self._next_id += 1
        self._q.put(job)
        self.queue_changed.emit(self._q.qsize() + int(self.busy))
        return job.id

    def cancel(self) -> None:
        self._cancel.set()
        # drop queued jobs too
        try:
            while True:
                j = self._q.get_nowait()
                if j is None:
                    self._q.put(None)
                    break
                self.log.emit(f"Cancelled queued job: {j.name}")
        except queue.Empty:
            pass

    def shutdown(self) -> None:
        self._cancel.set()
        self._q.put(None)

    def _loop(self) -> None:
        while True:
            job = self._q.get()
            if job is None:
                return
            self._cancel.clear()
            self.busy = True
            self.started.emit(job.name)
            result, err = None, ""
            try:
                result = job.fn(JobContext(self, job))
            except Exception as e:
                err = f"{e}"
                self.log.emit(f"ERROR in '{job.name}': {e}\n{traceback.format_exc(limit=4)}")
            self.busy = False
            self._deliver.emit(job, result, err)
            self.finished.emit(job.name, result, err)
            self.queue_changed.emit(self._q.qsize())

    def _on_deliver(self, job: Job, result, err: str) -> None:
        try:
            if err and job.on_error:
                job.on_error(err)
            elif not err and job.on_done:
                job.on_done(result)
        except Exception as e:
            self.log.emit(f"ERROR after '{job.name}': {e}")
