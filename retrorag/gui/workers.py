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
    doc_ids: frozenset = frozenset()      # documents this job works on (for busy indicators)
    verb: str = ""                        # e.g. "Running OCR"
    dropped: bool = False                 # taken off the queue before it started


class JobContext:
    def __init__(self, runner: "JobRunner", job: Job):
        self.runner, self.job = runner, job

    def progress(self, i: int, n: int, msg: str = "") -> None:
        self.runner.progress.emit(i, n, msg)

    def log(self, msg: str) -> None:
        self.runner.log.emit(str(msg))

    def cancelled(self) -> bool:
        return self.runner._cancel.is_set()

    def run_process(self, target: str, **kwargs):
        """Run module:function in a separate process (keeps the window responsive); None if cancelled."""
        from ..proc import Cancelled, run_in_process
        try:
            return run_in_process(target, kwargs, progress=self.progress, log=self.log, cancelled=self.cancelled)
        except Cancelled:
            self.log("Cancelled.")
            return None


class JobRunner(QObject):
    started = Signal(str)
    job_started = Signal(object)             # Job
    job_ended = Signal(object)               # Job
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
        self.current: Job | None = None
        self.pending_doc_ids: set = set()
        self._deliver.connect(self._on_deliver)
        self._thread = threading.Thread(target=self._loop, daemon=True, name="retrorag-jobs")
        self._thread.start()

    def submit(self, name: str, fn, on_done=None, on_error=None, doc_ids=(), verb: str = "") -> int:
        job = Job(name, fn, on_done, on_error, self._next_id, frozenset(doc_ids), verb or name)
        self._next_id += 1
        self.pending_doc_ids |= set(job.doc_ids)
        self._q.put(job)
        self.queue_changed.emit(self._q.qsize() + int(self.busy))
        return job.id

    def queued(self) -> list[Job]:
        """Jobs waiting to start, in order."""
        with self._q.mutex:
            return [j for j in self._q.queue if j is not None and not j.dropped]

    def doc_ids_in_use(self) -> set:
        """Documents the running job, or one waiting to start, works on."""
        cur = self.current
        return set(cur.doc_ids if cur else ()).union(*(j.doc_ids for j in self.queued()))

    def cancel_for(self, doc_id: int) -> None:
        """Cancel what's happening to one document: the running job if it's that one's (which, like Cancel, also
        empties the queue), else just its jobs waiting to start."""
        cur = self.current
        if cur is not None and doc_id in cur.doc_ids:
            self.cancel()
            return
        for j in self.queued():
            if doc_id in j.doc_ids:
                j.dropped = True
                self.log.emit(f"Cancelled queued job: {j.name}")
        self.pending_doc_ids = set().union(*(j.doc_ids for j in self.queued()), cur.doc_ids if cur else ())
        self.queue_changed.emit(len(self.queued()) + int(self.busy))

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
            if job.dropped:
                continue
            self._cancel.clear()
            self.busy = True
            self.current = job
            self.started.emit(job.name)
            self.job_started.emit(job)
            result, err = None, ""
            try:
                result = job.fn(JobContext(self, job))
            except Exception as e:
                err = f"{e}"
                self.log.emit(f"ERROR in '{job.name}': {e}\n{traceback.format_exc(limit=4)}")
            self.busy = False
            self.current = None
            self.pending_doc_ids -= set(job.doc_ids)
            self.job_ended.emit(job)
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
