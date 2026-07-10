"""Concurrency tests for QueuePool readers and the single-writer thread."""

from __future__ import annotations

import concurrent.futures
import threading
import time
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from app.db import writer as writer_mod
from app.db.engine import dispose_engine, get_engine
from app.db.models import RuleUserLastLocation
from app.db.session import discovery_session, discovery_write
from app.db.writer import (
    DiscoveryWriterStoppedError,
    _PathWriter,
    _STOP,
    stop_discovery_writer,
)
from app.location_history_retention import default_location_history_retention
from app.presence_store import UserLocationRecord, upsert_user_location
from app.rules_store import UserRecord, replace_users, user_exists


def test_concurrent_reads_and_writes_no_interface_error(tmp_path: Path) -> None:
    db = tmp_path / "discovery.sqlite"
    replace_users(
        db,
        [
            UserRecord(
                user_id="henrique",
                first_name="Henrique",
                last_name="",
                display_name="Henrique",
                tracking_device_label="Phone",
                enabled=True,
            ),
            UserRecord(
                user_id="kristen",
                first_name="Kristen",
                last_name="",
                display_name="Kristen",
                tracking_device_label="Phone",
                enabled=True,
            ),
        ],
    )
    retention = default_location_history_retention()
    errors: list[BaseException] = []

    def _read(user_id: str) -> bool:
        try:
            return user_exists(db, user_id)
        except BaseException as exc:
            errors.append(exc)
            raise

    def _write(user_id: str, seq: int) -> bool:
        try:
            return upsert_user_location(
                db,
                UserLocationRecord(
                    user_id=user_id,
                    lat=41.0 + seq * 0.0001,
                    lon=-73.0,
                    accuracy_m=10,
                    fix_at=1_700_000_000.0 + seq,
                    reported_at=1_700_000_000.0 + seq,
                    source="test",
                ),
                retention=retention,
            )
        except BaseException as exc:
            errors.append(exc)
            raise

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        futures = []
        for i in range(40):
            user_id = "henrique" if i % 2 == 0 else "kristen"
            futures.append(pool.submit(_read, user_id))
            futures.append(pool.submit(_write, user_id, i))
        results = [future.result(timeout=60) for future in futures]

    assert errors == []
    assert all(isinstance(value, bool) for value in results)
    dispose_engine(db)


def test_nested_discovery_write_on_writer_thread(tmp_path: Path) -> None:
    db = tmp_path / "discovery.sqlite"
    get_engine(db)

    def _outer(session: Session) -> int:
        del session

        def _inner(_inner_session: Session) -> int:
            return 7

        return discovery_write(db, _inner) + 1

    assert discovery_write(db, _outer) == 8
    dispose_engine(db)


def test_overlapping_writes_serialize(tmp_path: Path) -> None:
    db = tmp_path / "discovery.sqlite"
    replace_users(
        db,
        [
            UserRecord(
                user_id="henrique",
                first_name="Henrique",
                last_name="",
                display_name="Henrique",
                tracking_device_label="Phone",
                enabled=True,
            ),
        ],
    )
    retention = default_location_history_retention()

    def _write(seq: int) -> None:
        upsert_user_location(
            db,
            UserLocationRecord(
                user_id="henrique",
                lat=40.0 + seq,
                lon=-73.0,
                accuracy_m=10,
                fix_at=1_700_000_000.0 + seq,
                reported_at=1_700_000_000.0 + seq,
                source="test",
            ),
            retention=retention,
        )

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(_write, 1)
        second = pool.submit(_write, 2)
        first.result(timeout=30)
        second.result(timeout=30)

    with discovery_session(db) as session:
        row = session.get(RuleUserLastLocation, "henrique")
        assert row is not None
        assert (row.lat, row.reported_at) in {
            (41.0, 1_700_000_001.0),
            (42.0, 1_700_000_002.0),
        }
    dispose_engine(db)


def test_stop_drains_jobs_after_stop_sentinel(tmp_path: Path) -> None:
    """Jobs behind ``_STOP`` must fail fast — not hang on ``future.result()``."""
    db = tmp_path / "discovery.sqlite"
    writer = _PathWriter(db.resolve())
    started = threading.Event()
    release = threading.Event()

    def _blocking(_session: Session) -> str:
        started.set()
        assert release.wait(timeout=10)
        return "done"

    def _late(_session: Session) -> str:
        return "should-not-run"

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        blocked = pool.submit(writer.submit, _blocking)
        assert started.wait(timeout=5)
        late: concurrent.futures.Future[str] = concurrent.futures.Future()
        with writer._start_lock:
            writer._stopping = True
            writer._jobs.put(_STOP)
            writer._jobs.put((_late, late))
        release.set()
        assert blocked.result(timeout=5) == "done"
        with pytest.raises(DiscoveryWriterStoppedError):
            late.result(timeout=5)
        writer._thread.join(timeout=5)


def test_stop_timeout_raises_without_resetting(tmp_path: Path) -> None:
    db = tmp_path / "discovery.sqlite"
    writer = _PathWriter(db.resolve())
    started = threading.Event()
    release = threading.Event()

    def _blocking(_session: Session) -> str:
        started.set()
        assert release.wait(timeout=10)
        return "done"

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        blocked = pool.submit(writer.submit, _blocking)
        assert started.wait(timeout=5)
        with pytest.raises(TimeoutError, match="still running"):
            writer.stop(timeout_s=0.05)
        assert writer._started
        assert writer._stopping
        assert writer._thread.is_alive()
        release.set()
        assert blocked.result(timeout=5) == "done"
        writer.stop(timeout_s=5)
    # Successful stop retires this instance; a later discovery_write must mint a
    # fresh writer rather than reviving the closed one (and must not hit a stray
    # _STOP left in the old queue).
    with pytest.raises(DiscoveryWriterStoppedError):
        writer.submit(lambda _s: "stale")
    assert discovery_write(db, lambda _s: "after-recovery") == "after-recovery"


def test_stop_discovery_writer_keeps_registration_on_timeout(tmp_path: Path) -> None:
    db = tmp_path / "discovery.sqlite"
    resolved = db.resolve()
    path_writer = _PathWriter(resolved)
    with writer_mod._writers_lock:
        writer_mod._writers[resolved] = path_writer
    started = threading.Event()
    release = threading.Event()

    def _blocking(_session: Session) -> str:
        started.set()
        assert release.wait(timeout=10)
        return "done"

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        blocked = pool.submit(path_writer.submit, _blocking)
        assert started.wait(timeout=5)
        with pytest.raises(TimeoutError, match="still running"):
            stop_discovery_writer(db, timeout_s=0.05)
        with writer_mod._writers_lock:
            assert writer_mod._writers.get(resolved) is path_writer
        release.set()
        assert blocked.result(timeout=5) == "done"
        stop_discovery_writer(db)
        with writer_mod._writers_lock:
            assert resolved not in writer_mod._writers


def test_submit_while_stopping_raises(tmp_path: Path) -> None:
    db = tmp_path / "discovery.sqlite"
    writer = _PathWriter(db.resolve())
    started = threading.Event()
    release = threading.Event()

    def _blocking(_session: Session) -> str:
        started.set()
        assert release.wait(timeout=10)
        return "done"

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        blocked = pool.submit(writer.submit, _blocking)
        assert started.wait(timeout=5)
        stopping = pool.submit(lambda: writer.stop(timeout_s=10.0))
        deadline = time.monotonic() + 5.0
        while not writer._stopping and time.monotonic() < deadline:
            time.sleep(0.01)
        assert writer._stopping
        with pytest.raises(DiscoveryWriterStoppedError):
            writer.submit(lambda _session: "nope")
        release.set()
        assert blocked.result(timeout=5) == "done"
        stopping.result(timeout=15)
