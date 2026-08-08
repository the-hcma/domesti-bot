"""Hermetic tests for sensor collection SQLite store."""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import func, select

from app.db.models import SensorSample
from app.db.session import discovery_session
from app.device_enums import SensorChartWindow, SensorCollectionKey
from app.sensor_collection_store import (
    count_sensor_samples_to_prune,
    insert_sensor_sample,
    list_sensor_samples,
    load_sensor_collection_config,
    load_sensor_collection_retention,
    save_sensor_collection_config,
    save_sensor_collection_retention,
)


def test_save_and_load_sensor_collection_config(tmp_path: Path) -> None:
    db = tmp_path / "ui.sqlite"
    saved = save_sensor_collection_config(
        db,
        device_id="aa:bb:cc:dd:ee:ff",
        enabled=True,
        family_id="ep1",
        interval_s=15,
        sensor_key=SensorCollectionKey.TEMPERATURE_C,
    )
    assert saved.enabled is True
    assert saved.interval_s == 15
    loaded = load_sensor_collection_config(
        db,
        device_id="aa:bb:cc:dd:ee:ff",
        sensor_key=SensorCollectionKey.TEMPERATURE_C,
    )
    assert loaded is not None
    assert loaded.enabled is True
    assert loaded.interval_s == 15


def test_save_sensor_collection_config_rejects_unknown_interval(tmp_path: Path) -> None:
    db = tmp_path / "ui.sqlite"
    with pytest.raises(ValueError, match="Expected sensor collection interval"):
        save_sensor_collection_config(
            db,
            device_id="aa:bb:cc:dd:ee:ff",
            enabled=True,
            family_id="ep1",
            interval_s=7,
            sensor_key=SensorCollectionKey.OCCUPANCY,
        )


def test_list_sensor_samples_filters_by_chart_window(tmp_path: Path) -> None:
    db = tmp_path / "ui.sqlite"
    device_id = "aa:bb:cc:dd:ee:ff"
    key = SensorCollectionKey.HUMIDITY_PCT
    now = 1_700_000_000.0
    insert_sensor_sample(
        db,
        device_id=device_id,
        family_id="ep1",
        recorded_at=now - 10,
        sensor_key=key,
        unit="%",
        value=40.0,
        now=now,
    )
    insert_sensor_sample(
        db,
        device_id=device_id,
        family_id="ep1",
        recorded_at=now - 120,
        sensor_key=key,
        unit="%",
        value=41.0,
        now=now,
    )
    insert_sensor_sample(
        db,
        device_id=device_id,
        family_id="ep1",
        recorded_at=now - 3500,
        sensor_key=key,
        unit="%",
        value=42.0,
        now=now,
    )

    minute = list_sensor_samples(
        db,
        device_id=device_id,
        sensor_key=key,
        window=SensorChartWindow.LAST_MINUTE,
        now=now,
    )
    assert [p.value for p in minute] == [40.0]

    five = list_sensor_samples(
        db,
        device_id=device_id,
        sensor_key=key,
        window=SensorChartWindow.LAST_5_MINUTES,
        now=now,
    )
    assert [p.value for p in five] == [41.0, 40.0]

    hour = list_sensor_samples(
        db,
        device_id=device_id,
        sensor_key=key,
        window=SensorChartWindow.LAST_HOUR,
        now=now,
    )
    assert [p.value for p in hour] == [42.0, 41.0, 40.0]


def test_list_sensor_samples_last_week_window(tmp_path: Path) -> None:
    db = tmp_path / "ui.sqlite"
    device_id = "aa:bb:cc:dd:ee:ff"
    key = SensorCollectionKey.OCCUPANCY
    now = 1_700_000_000.0
    insert_sensor_sample(
        db,
        device_id=device_id,
        family_id="ep1",
        recorded_at=now - 100_000,
        sensor_key=key,
        unit=None,
        value=1.0,
        now=now,
    )
    insert_sensor_sample(
        db,
        device_id=device_id,
        family_id="ep1",
        recorded_at=now - 700_000,
        sensor_key=key,
        unit=None,
        value=0.0,
        now=now,
    )
    week = list_sensor_samples(
        db,
        device_id=device_id,
        sensor_key=key,
        window=SensorChartWindow.LAST_WEEK,
        now=now,
    )
    assert [p.value for p in week] == [1.0]
    assert SensorChartWindow.LAST_WEEK.duration_s() == 604_800.0


def test_default_retention_is_two_months(tmp_path: Path) -> None:
    db = tmp_path / "ui.sqlite"
    retention = load_sensor_collection_retention(db)
    assert retention.unlimited is False
    assert retention.max_age_days == 60.0


def test_count_sensor_samples_to_prune_respects_proposed_window(tmp_path: Path) -> None:
    db = tmp_path / "ui.sqlite"
    device_id = "aa:bb:cc:dd:ee:ff"
    key = SensorCollectionKey.TEMPERATURE_C
    save_sensor_collection_retention(db, max_age_days=60.0, unlimited=True)
    now = 1_700_000_000.0
    insert_sensor_sample(
        db,
        device_id=device_id,
        family_id="ep1",
        recorded_at=now - 10 * 86_400,
        sensor_key=key,
        unit="°C",
        value=1.0,
        now=now,
    )
    insert_sensor_sample(
        db,
        device_id=device_id,
        family_id="ep1",
        recorded_at=now - 1 * 86_400,
        sensor_key=key,
        unit="°C",
        value=2.0,
        now=now,
    )
    assert count_sensor_samples_to_prune(db, max_age_days=7.0, unlimited=False, now=now) == 1
    assert count_sensor_samples_to_prune(db, max_age_days=7.0, unlimited=True, now=now) == 0


def test_insert_prunes_by_configured_retention(tmp_path: Path) -> None:
    db = tmp_path / "ui.sqlite"
    device_id = "aa:bb:cc:dd:ee:ff"
    key = SensorCollectionKey.TEMPERATURE_C
    save_sensor_collection_retention(db, max_age_days=2.0, unlimited=False)
    now = 1_700_000_000.0
    insert_sensor_sample(
        db,
        device_id=device_id,
        family_id="ep1",
        recorded_at=now - 3 * 86_400,
        sensor_key=key,
        unit="°C",
        value=1.0,
        now=now,
    )
    insert_sensor_sample(
        db,
        device_id=device_id,
        family_id="ep1",
        recorded_at=now,
        sensor_key=key,
        unit="°C",
        value=2.0,
        now=now,
    )
    kept = list_sensor_samples(
        db,
        device_id=device_id,
        sensor_key=key,
        window=SensorChartWindow.LAST_DAY,
        now=now,
    )
    # LAST_DAY only sees the fresh point; the 3-day-old row must be gone entirely.
    assert [p.value for p in kept] == [2.0]
    with discovery_session(db) as session:
        count = session.scalar(select(func.count()).select_from(SensorSample))
    assert count == 1


def test_unlimited_retention_preserves_prior_max_age_days(tmp_path: Path) -> None:
    db = tmp_path / "ui.sqlite"
    save_sensor_collection_retention(db, max_age_days=2.0, unlimited=False)
    unlimited = save_sensor_collection_retention(db, max_age_days=60.0, unlimited=True)
    assert unlimited.unlimited is True
    assert unlimited.max_age_days == 2.0
    restored = save_sensor_collection_retention(db, max_age_days=2.0, unlimited=False)
    assert restored.unlimited is False
    assert restored.max_age_days == 2.0


def test_unlimited_retention_keeps_old_samples(tmp_path: Path) -> None:
    db = tmp_path / "ui.sqlite"
    device_id = "aa:bb:cc:dd:ee:ff"
    key = SensorCollectionKey.OCCUPANCY
    save_sensor_collection_retention(db, max_age_days=60.0, unlimited=True)
    now = 1_700_000_000.0
    insert_sensor_sample(
        db,
        device_id=device_id,
        family_id="ep1",
        recorded_at=now - 200 * 86_400,
        sensor_key=key,
        unit=None,
        value=0.0,
        now=now,
    )
    insert_sensor_sample(
        db,
        device_id=device_id,
        family_id="ep1",
        recorded_at=now,
        sensor_key=key,
        unit=None,
        value=1.0,
        now=now,
    )
    with discovery_session(db) as session:
        count = session.scalar(select(func.count()).select_from(SensorSample))
    assert count == 2
