"""HTTP routes for Automations → Data sensor collection."""

from __future__ import annotations

import math
import time
from http import HTTPStatus
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query, Request

from app.api.schemas import (
    SensorCollectionConfigIn,
    SensorCollectionRetentionIn,
    SensorCollectionRetentionOut,
    SensorCollectionRetentionPrunePreviewOut,
    SensorCollectionSampleOut,
    SensorCollectionSamplesOut,
    SensorCollectionSensorOut,
    SensorCollectionSensorsOut,
)
from app.api.settings_routes import discovery_cache_path_from_request
from app.device_display import format_device_display
from app.device_enums import DeviceFamilyId, SensorChartWindow, SensorCollectionKey
from app.sensor_collection import (
    build_sensor_collection_rows,
    list_collectible_sensors,
)
from app.sensor_collection_store import (
    SENSOR_COLLECTION_INTERVAL_PRESETS_S,
    count_sensor_samples_to_prune,
    list_sensor_samples,
    load_sensor_collection_retention,
    save_sensor_collection_config,
    save_sensor_collection_retention,
)
from app.server_runtime import runtime

router = APIRouter(prefix="/v1/sensor-collection", tags=["sensor-collection"])


@router.get("/retention", response_model=SensorCollectionRetentionOut)
async def get_sensor_collection_retention(
    request: Request,
) -> SensorCollectionRetentionOut:
    """Return the sample retention policy (default two months)."""
    cache_path = _require_discovery_cache(request)
    retention = load_sensor_collection_retention(cache_path)
    return SensorCollectionRetentionOut(
        max_age_days=retention.max_age_days,
        unlimited=retention.unlimited,
    )


@router.post(
    "/retention/prune-preview",
    response_model=SensorCollectionRetentionPrunePreviewOut,
)
async def post_sensor_collection_retention_prune_preview(
    body: SensorCollectionRetentionIn,
    request: Request,
) -> SensorCollectionRetentionPrunePreviewOut:
    """Count samples that would be deleted if ``body`` were saved now."""
    cache_path = _require_discovery_cache(request)
    try:
        samples_to_prune = count_sensor_samples_to_prune(
            cache_path,
            max_age_days=body.max_age_days,
            unlimited=body.unlimited,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=HTTPStatus.UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc
    return SensorCollectionRetentionPrunePreviewOut(samples_to_prune=samples_to_prune)


@router.put("/retention", response_model=SensorCollectionRetentionOut)
async def put_sensor_collection_retention(
    body: SensorCollectionRetentionIn,
    request: Request,
) -> SensorCollectionRetentionOut:
    """Update sample retention and prune immediately when age-limited."""
    cache_path = _require_discovery_cache(request)
    try:
        saved = save_sensor_collection_retention(
            cache_path,
            max_age_days=body.max_age_days,
            unlimited=body.unlimited,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=HTTPStatus.UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc
    return SensorCollectionRetentionOut(
        max_age_days=saved.max_age_days,
        unlimited=saved.unlimited,
    )


@router.get("/samples", response_model=SensorCollectionSamplesOut)
async def get_sensor_collection_samples(
    request: Request,
    device_id: str = Query(..., min_length=1),
    sensor_key: SensorCollectionKey = Query(...),
    window: SensorChartWindow = Query(default=SensorChartWindow.LAST_5_MINUTES),
    as_of: float | None = Query(
        default=None,
        description=(
            "Unix epoch seconds for the chart window end. "
            "Omit for server now; pass a past time to shift the window (period nav)."
        ),
    ),
) -> SensorCollectionSamplesOut:
    """Return persisted samples for a chart time window."""
    cache_path = _require_discovery_cache(request)
    now = time.time()
    if as_of is None:
        window_end = now
    else:
        if not math.isfinite(as_of):
            raise HTTPException(
                status_code=HTTPStatus.UNPROCESSABLE_ENTITY,
                detail="Expected finite as_of unix epoch seconds",
            )
        # Clamp to server now so clients cannot request a future window end.
        window_end = min(as_of, now)
    points = list_sensor_samples(
        cache_path,
        device_id=device_id.strip(),
        sensor_key=sensor_key,
        window=window,
        now=window_end,
    )
    return SensorCollectionSamplesOut(
        as_of=window_end,
        device_id=device_id.strip(),
        points=[
            SensorCollectionSampleOut(
                recorded_at=point.recorded_at,
                unit=point.unit,
                value=point.value,
            )
            for point in points
        ],
        sensor_key=sensor_key,
        window=window,
    )


@router.get("/sensors", response_model=SensorCollectionSensorsOut)
async def get_sensor_collection_sensors(request: Request) -> SensorCollectionSensorsOut:
    """List collectible sensors merged with per-sensor collection config."""
    cache_path = _require_discovery_cache(request)
    rows = build_sensor_collection_rows(runtime.device_state, cache_path)
    return SensorCollectionSensorsOut(
        sensors=[
            SensorCollectionSensorOut(
                device_display=row.device_display,
                device_id=row.device_id,
                display_name=row.display_name,
                enabled=row.enabled,
                family_id=row.family_id,
                interval_s=row.interval_s,
                last_sample_at=row.last_sample_at,
                last_value=row.last_value,
                sensor_key=row.sensor_key,
                unit=row.unit,
            )
            for row in rows
        ]
    )


@router.put(
    "/sensors/{device_id}/{sensor_key}",
    response_model=SensorCollectionSensorOut,
)
async def put_sensor_collection_sensor(
    device_id: str,
    sensor_key: SensorCollectionKey,
    body: SensorCollectionConfigIn,
    request: Request,
) -> SensorCollectionSensorOut:
    """Enable/disable collection and set sample frequency for one sensor."""
    cache_path = _require_discovery_cache(request)
    if body.interval_s not in SENSOR_COLLECTION_INTERVAL_PRESETS_S:
        raise HTTPException(
            status_code=HTTPStatus.UNPROCESSABLE_ENTITY,
            detail=(
                f"Expected sensor collection interval in {SENSOR_COLLECTION_INTERVAL_PRESETS_S}, got {body.interval_s}"
            ),
        )
    trimmed_id = device_id.strip()
    if trimmed_id == "":
        raise HTTPException(
            status_code=HTTPStatus.UNPROCESSABLE_ENTITY,
            detail="Expected non-empty device_id, got empty value",
        )
    family_id = _resolve_family_id(trimmed_id, sensor_key)
    saved = save_sensor_collection_config(
        cache_path,
        device_id=trimmed_id,
        enabled=body.enabled,
        family_id=family_id.value,
        interval_s=body.interval_s,
        sensor_key=sensor_key,
    )
    rows = build_sensor_collection_rows(runtime.device_state, cache_path)
    for row in rows:
        if row.device_id == saved.device_id and row.sensor_key == saved.sensor_key:
            return SensorCollectionSensorOut(
                device_display=row.device_display,
                device_id=row.device_id,
                display_name=row.display_name,
                enabled=row.enabled,
                family_id=row.family_id,
                interval_s=row.interval_s,
                last_sample_at=row.last_sample_at,
                last_value=row.last_value,
                sensor_key=row.sensor_key,
                unit=row.unit,
            )
    return SensorCollectionSensorOut(
        device_display=format_device_display(trimmed_id, trimmed_id),
        device_id=trimmed_id,
        display_name=trimmed_id,
        enabled=saved.enabled,
        family_id=family_id,
        interval_s=saved.interval_s,
        last_sample_at=None,
        last_value=None,
        sensor_key=saved.sensor_key,
        unit=saved.sensor_key.unit_label(),
    )


def _require_discovery_cache(request: Request) -> Path:
    cache_path = discovery_cache_path_from_request(request)
    if cache_path is None:
        raise HTTPException(
            status_code=HTTPStatus.CONFLICT,
            detail=(
                "Cannot use sensor collection: server started with "
                "--no-discovery-cache. Restart with a discovery cache path."
            ),
        )
    return cache_path


def _resolve_family_id(device_id: str, sensor_key: SensorCollectionKey) -> DeviceFamilyId:
    """Prefer live catalog family; fall back to EP1 for known collection keys."""
    for sensor in list_collectible_sensors(runtime.device_state):
        if sensor.device_id == device_id and sensor.sensor_key == sensor_key:
            return sensor.family_id
    # v1 collection keys are EP1-only; accept config before discovery finishes.
    return DeviceFamilyId.EP1
