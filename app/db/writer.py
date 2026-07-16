"""Single-writer thread per discovery database path."""

from __future__ import annotations

import concurrent.futures
import queue
import threading
from collections.abc import Callable
from pathlib import Path
from typing import TypeVar

from sqlalchemy.orm import Session, sessionmaker

from app.db.engine import register_engine_dispose_hook
from app.db.schema import bootstrap_schema

T = TypeVar("T")


class DiscoveryWriterStoppedError(RuntimeError):
    """Raised when a write is submitted to a writer that is shutting down."""


def discovery_write(path: Path, fn: Callable[[Session], T]) -> T:
    """Run ``fn(session)`` on the path's writer thread; commit on success."""
    resolved = path.expanduser().resolve()
    with _writers_lock:
        writer = _writers.get(resolved)
        if writer is None:
            writer = _PathWriter(resolved)
            _writers[resolved] = writer
    return writer.submit(fn)


def stop_discovery_writer(path: Path, *, timeout_s: float = 30.0) -> None:
    """Stop the writer thread for ``path`` if one was started."""
    resolved = path.expanduser().resolve()
    with _writers_lock:
        writer = _writers.get(resolved)
    if writer is None:
        return
    writer.stop(timeout_s=timeout_s)
    with _writers_lock:
        if _writers.get(resolved) is writer:
            del _writers[resolved]


class _PathWriter:
    """Serialize mutating sessions for one database path onto a dedicated thread."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._jobs: queue.Queue[object] = queue.Queue()
        self._start_lock = threading.Lock()
        self._thread = threading.Thread(
            target=self._run,
            name=f"sqlite-writer-{path.name}",
            daemon=True,
        )
        self._thread_ident: int | None = None
        self._closed = False
        self._started = False
        self._stopping = False

    def start(self) -> None:
        with self._start_lock:
            self._ensure_started_locked()

    def stop(self, *, timeout_s: float = 30.0) -> None:
        with self._start_lock:
            if self._closed:
                return
            if not self._started:
                self._closed = True
                return
            self._stopping = True
            self._jobs.put(_STOP)
        self._thread.join(timeout=timeout_s)
        with self._start_lock:
            if self._thread.is_alive():
                raise TimeoutError(
                    f"Expected discovery writer for {self._path} to stop within {timeout_s}s, got still running",
                )
            # Timeout-recovery may leave a second _STOP (and rarely a late job)
            # in the queue after the worker already exited on the first sentinel.
            self._drain_remaining_jobs()
            self._closed = True
            self._started = False
            self._stopping = False
            self._thread_ident = None

    def submit(self, fn: Callable[[Session], T]) -> T:
        if self._thread_ident is not None and threading.get_ident() == self._thread_ident:
            return self._execute(fn)
        future: concurrent.futures.Future[T] = concurrent.futures.Future()
        with self._start_lock:
            if self._closed or self._stopping:
                raise DiscoveryWriterStoppedError(
                    f"Expected discovery writer for {self._path} to accept writes, got shutting down",
                )
            self._ensure_started_locked()
            self._jobs.put((fn, future))
        return future.result()

    def _drain_remaining_jobs(self) -> None:
        while True:
            try:
                item = self._jobs.get_nowait()
            except queue.Empty:
                return
            if item is _STOP:
                continue
            assert isinstance(item, tuple)
            _fn, future = item
            assert isinstance(future, concurrent.futures.Future)
            if future.set_running_or_notify_cancel():
                future.set_exception(
                    DiscoveryWriterStoppedError(
                        f"Expected discovery writer for {self._path} to run queued write, got stopped",
                    ),
                )

    def _ensure_started_locked(self) -> None:
        if self._closed:
            raise DiscoveryWriterStoppedError(
                f"Expected discovery writer for {self._path} to accept writes, got closed",
            )
        if self._started:
            return
        self._thread.start()
        self._started = True
        self._stopping = False

    def _execute(self, fn: Callable[[Session], T]) -> T:
        engine = bootstrap_schema(self._path)
        factory = sessionmaker(bind=engine, expire_on_commit=False)
        with factory() as session:
            try:
                result = fn(session)
                session.commit()
                return result
            except Exception:
                session.rollback()
                raise

    def _run(self) -> None:
        self._thread_ident = threading.get_ident()
        while True:
            item = self._jobs.get()
            if item is _STOP:
                self._drain_remaining_jobs()
                return
            assert isinstance(item, tuple)
            fn, future = item
            assert callable(fn)
            assert isinstance(future, concurrent.futures.Future)
            if not future.set_running_or_notify_cancel():
                continue
            try:
                future.set_result(self._execute(fn))
            except Exception as exc:
                future.set_exception(exc)


_STOP = object()
_writers: dict[Path, _PathWriter] = {}
_writers_lock = threading.Lock()

register_engine_dispose_hook(stop_discovery_writer)
