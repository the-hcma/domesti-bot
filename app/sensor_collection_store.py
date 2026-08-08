"""Persist Automations → Data collection config and sensor samples in SQLite."""

from __future__ import annotations

import math
import time
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.db.models import SensorCollectionConfig, SensorCollectionSettings, SensorSample
from app.db.session import discovery_session, discovery_write
from app.device_enums import SensorChartWindow, SensorCollectionKey

DEFAULT_SENSOR_COLLECTION_INTERVAL_S = 30
DEFAULT_SENSOR_SAMPLE_RETENTION_DAYS = 60.0
SENSOR_COLLECTION_INTERVAL_PRESETS_S: tuple[int, ...] = (5, 15, 30, 60, 300)
_SENSOR_COLLECTION_SETTINGS_ID = 1


@dataclass(frozen=True)
class SensorCollectionConfigRecord:
    device_id: str
    enabled: bool
    family_id: str
    interval_s: int
    sensor_key: SensorCollectionKey


@dataclass(frozen=True)
class SensorCollectionRetentionRecord:
    """How long collected samples are kept before prune."""

    max_age_days: float
    unlimited: bool

    @property
    def max_age_s(self) -> float:
        return self.max_age_days * 86_400.0


@dataclass(frozen=True)
class SensorSampleRecord:
    device_id: str
    family_id: str
    recorded_at: float
    sensor_key: SensorCollectionKey
    unit: str | None
    value: float


def count_sensor_samples_to_prune(
    path: Path,
    *,
    max_age_days: float,
    unlimited: bool,
    now: float | None = None,
) -> int:
    """Return how many samples the proposed retention would delete.

    Unlimited policies never prune. Limited policies count rows older than
    ``now - max_age_days`` (wall clock when ``now`` is omitted).
    """
    if unlimited:
        return 0
    if max_age_days <= 0:
        raise ValueError(f"Expected retention max_age_days > 0 when limited, got {max_age_days}")
    if not math.isfinite(max_age_days):
        raise ValueError(f"Expected finite retention max_age_days, got {max_age_days}")
    end = time.time() if now is None else now
    cutoff = end - max_age_days * 86_400.0
    with discovery_session(path) as session:
        counted = session.scalar(
            select(func.count()).select_from(SensorSample).where(SensorSample.recorded_at < cutoff)
        )
        return int(counted or 0)


def default_sensor_collection_retention() -> SensorCollectionRetentionRecord:
    """Return the default two-month sample retention policy."""
    return SensorCollectionRetentionRecord(
        max_age_days=DEFAULT_SENSOR_SAMPLE_RETENTION_DAYS,
        unlimited=False,
    )


def insert_sensor_sample(
    path: Path,
    *,
    device_id: str,
    family_id: str,
    recorded_at: float,
    sensor_key: SensorCollectionKey,
    unit: str | None,
    value: float,
    now: float | None = None,
) -> None:
    """Append one sample and prune by the configured retention policy.

    ``now`` is the prune reference clock (defaults to wall time). Production
    callers leave it unset; hermetic tests that insert synthetic ``recorded_at``
    values should pass the same synthetic clock as ``now``.
    """
    prune_at = time.time() if now is None else now

    def _write(session: Session) -> None:
        session.add(
            SensorSample(
                device_id=device_id,
                family_id=family_id,
                recorded_at=recorded_at,
                sensor_key=sensor_key.value,
                unit=unit,
                value=value,
            )
        )
        retention = _retention_from_session(session)
        if retention.unlimited:
            return
        cutoff = prune_at - retention.max_age_s
        session.execute(delete(SensorSample).where(SensorSample.recorded_at < cutoff))

    discovery_write(path, _write)


def list_sensor_collection_configs(path: Path) -> list[SensorCollectionConfigRecord]:
    """Return every persisted per-sensor collection config row."""
    with discovery_session(path) as session:
        rows = session.scalars(select(SensorCollectionConfig)).all()
        return [_config_from_row(row) for row in rows]


def list_sensor_samples(
    path: Path,
    *,
    device_id: str,
    sensor_key: SensorCollectionKey,
    window: SensorChartWindow,
    now: float | None = None,
) -> list[SensorSampleRecord]:
    """Return samples for ``device_id`` / ``sensor_key`` within ``window`` (oldest first)."""
    end = time.time() if now is None else now
    start = end - window.duration_s()
    with discovery_session(path) as session:
        rows = session.scalars(
            select(SensorSample)
            .where(
                SensorSample.device_id == device_id,
                SensorSample.sensor_key == sensor_key.value,
                SensorSample.recorded_at >= start,
                SensorSample.recorded_at <= end,
            )
            .order_by(SensorSample.recorded_at.asc())
        ).all()
        return [_sample_from_row(row) for row in rows]


def load_sensor_collection_config(
    path: Path,
    *,
    device_id: str,
    sensor_key: SensorCollectionKey,
) -> SensorCollectionConfigRecord | None:
    """Return one config row, or ``None`` when never configured."""
    with discovery_session(path) as session:
        row = session.get(SensorCollectionConfig, (device_id, sensor_key.value))
        if row is None:
            return None
        return _config_from_row(row)


def load_sensor_collection_retention(path: Path) -> SensorCollectionRetentionRecord:
    """Return the configured sample retention policy (defaults to two months)."""
    with discovery_session(path) as session:
        return _retention_from_session(session)


def latest_sensor_sample(
    path: Path,
    *,
    device_id: str,
    sensor_key: SensorCollectionKey,
) -> SensorSampleRecord | None:
    """Return the newest sample for a sensor, if any."""
    with discovery_session(path) as session:
        row = session.scalars(
            select(SensorSample)
            .where(
                SensorSample.device_id == device_id,
                SensorSample.sensor_key == sensor_key.value,
            )
            .order_by(SensorSample.recorded_at.desc())
            .limit(1)
        ).first()
        if row is None:
            return None
        return _sample_from_row(row)


def prune_sensor_samples(path: Path, *, now: float | None = None) -> int:
    """Delete samples older than the configured retention; return rows deleted."""
    end = time.time() if now is None else now
    deleted = {"n": 0}

    def _write(session: Session) -> None:
        retention = _retention_from_session(session)
        if retention.unlimited:
            deleted["n"] = 0
            return
        cutoff = end - retention.max_age_s
        stale = session.scalars(select(SensorSample).where(SensorSample.recorded_at < cutoff)).all()
        deleted["n"] = len(stale)
        for row in stale:
            session.delete(row)

    discovery_write(path, _write)
    return deleted["n"]


def save_sensor_collection_config(
    path: Path,
    *,
    device_id: str,
    enabled: bool,
    family_id: str,
    interval_s: int,
    sensor_key: SensorCollectionKey,
) -> SensorCollectionConfigRecord:
    """Upsert enable + interval for one sensor."""
    if interval_s not in SENSOR_COLLECTION_INTERVAL_PRESETS_S:
        raise ValueError(
            f"Expected sensor collection interval in {SENSOR_COLLECTION_INTERVAL_PRESETS_S}, got {interval_s}"
        )
    now = time.time()

    def _write(session: Session) -> None:
        row = session.get(SensorCollectionConfig, (device_id, sensor_key.value))
        if row is None:
            session.add(
                SensorCollectionConfig(
                    device_id=device_id,
                    enabled=1 if enabled else 0,
                    family_id=family_id,
                    interval_s=interval_s,
                    sensor_key=sensor_key.value,
                    updated_at=now,
                )
            )
            return
        row.enabled = 1 if enabled else 0
        row.family_id = family_id
        row.interval_s = interval_s
        row.updated_at = now

    discovery_write(path, _write)
    return SensorCollectionConfigRecord(
        device_id=device_id,
        enabled=enabled,
        family_id=family_id,
        interval_s=interval_s,
        sensor_key=sensor_key,
    )


def save_sensor_collection_retention(
    path: Path,
    *,
    max_age_days: float,
    unlimited: bool,
) -> SensorCollectionRetentionRecord:
    """Upsert global sample retention and prune immediately when limited.

    When ``unlimited`` is true, keep the previously stored limited
    ``max_age_days`` (so toggling keep-forever off restores the operator's
    prior setting). Fall back to a positive request value, then the default.
    """
    if not unlimited and max_age_days <= 0:
        raise ValueError(f"Expected retention max_age_days > 0 when limited, got {max_age_days}")
    if not math.isfinite(max_age_days):
        raise ValueError(f"Expected finite retention max_age_days, got {max_age_days}")
    now = time.time()
    saved: dict[str, SensorCollectionRetentionRecord] = {}

    def _write(session: Session) -> None:
        row = session.get(SensorCollectionSettings, _SENSOR_COLLECTION_SETTINGS_ID)
        if unlimited:
            if row is not None and float(row.retention_max_age_days) > 0:
                days = float(row.retention_max_age_days)
            elif max_age_days > 0:
                days = float(max_age_days)
            else:
                days = DEFAULT_SENSOR_SAMPLE_RETENTION_DAYS
        else:
            days = float(max_age_days)
        record = SensorCollectionRetentionRecord(max_age_days=days, unlimited=unlimited)
        saved["record"] = record
        if row is None:
            session.add(
                SensorCollectionSettings(
                    id=_SENSOR_COLLECTION_SETTINGS_ID,
                    retention_max_age_days=record.max_age_days,
                    retention_unlimited=1 if unlimited else 0,
                    updated_at=now,
                )
            )
        else:
            row.retention_max_age_days = record.max_age_days
            row.retention_unlimited = 1 if unlimited else 0
            row.updated_at = now
        if unlimited:
            return
        cutoff = now - record.max_age_s
        session.execute(delete(SensorSample).where(SensorSample.recorded_at < cutoff))

    discovery_write(path, _write)
    return saved["record"]


def _config_from_row(row: SensorCollectionConfig) -> SensorCollectionConfigRecord:
    return SensorCollectionConfigRecord(
        device_id=row.device_id,
        enabled=bool(row.enabled),
        family_id=row.family_id,
        interval_s=int(row.interval_s),
        sensor_key=SensorCollectionKey(row.sensor_key),
    )


def _retention_from_session(session: Session) -> SensorCollectionRetentionRecord:
    row = session.get(SensorCollectionSettings, _SENSOR_COLLECTION_SETTINGS_ID)
    if row is None:
        return default_sensor_collection_retention()
    if bool(row.retention_unlimited):
        return SensorCollectionRetentionRecord(
            max_age_days=float(row.retention_max_age_days),
            unlimited=True,
        )
    return SensorCollectionRetentionRecord(
        max_age_days=float(row.retention_max_age_days),
        unlimited=False,
    )


def _sample_from_row(row: SensorSample) -> SensorSampleRecord:
    return SensorSampleRecord(
        device_id=row.device_id,
        family_id=row.family_id,
        recorded_at=float(row.recorded_at),
        sensor_key=SensorCollectionKey(row.sensor_key),
        unit=row.unit,
        value=float(row.value),
    )
