"""HTTP routes for persisted Automations participants and geofences."""

from __future__ import annotations

from http import HTTPStatus
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request

from app.api.schemas import GeofenceOut, ParticipantOut
from app.api.settings_routes import discovery_cache_path_from_request
from app.rules_store import (
    GeofenceRecord,
    ParticipantRecord,
    delete_geofence,
    list_geofences,
    list_participants,
    save_geofence,
)

router = APIRouter(prefix="/v1/rules", tags=["rules"])


@router.delete("/geofences/{geofence_id}", status_code=HTTPStatus.NO_CONTENT)
async def delete_geofence_route(geofence_id: str, request: Request) -> None:
    """Remove one geofence row."""
    cache_path = _require_discovery_cache(request)
    delete_geofence(cache_path, geofence_id)


@router.get("/geofences", response_model=list[GeofenceOut])
async def get_geofences(request: Request) -> list[GeofenceOut]:
    """Return persisted geofence definitions."""
    cache_path = discovery_cache_path_from_request(request)
    if cache_path is None:
        return []
    return [_geofence_to_schema(row) for row in list_geofences(cache_path)]


@router.get("/participants", response_model=list[ParticipantOut])
async def get_participants(request: Request) -> list[ParticipantOut]:
    """Return persisted participant roster rows."""
    cache_path = discovery_cache_path_from_request(request)
    if cache_path is None:
        return []
    return [_participant_to_schema(row) for row in list_participants(cache_path)]


@router.put("/geofences/{geofence_id}", response_model=GeofenceOut)
async def put_geofence(
    geofence_id: str,
    body: GeofenceOut,
    request: Request,
) -> GeofenceOut:
    """Create or update one geofence row."""
    cache_path = _require_discovery_cache(request)
    if body.geofence_id != geofence_id:
        raise HTTPException(
            status_code=HTTPStatus.UNPROCESSABLE_ENTITY,
            detail=(
                f"Expected geofence_id in path to match body, got {geofence_id!r} "
                f"and {body.geofence_id!r}"
            ),
        )
    saved = save_geofence(
        cache_path,
        GeofenceRecord(
            geofence_id=body.geofence_id,
            label=body.label.strip(),
            center_lat=body.center_lat,
            center_lon=body.center_lon,
            radius_m=body.radius_m,
            enabled=body.enabled,
            owntracks_rid=body.owntracks_rid,
        ),
    )
    return _geofence_to_schema(saved)


def _geofence_to_schema(record: GeofenceRecord) -> GeofenceOut:
    return GeofenceOut(
        geofence_id=record.geofence_id,
        label=record.label,
        center_lat=record.center_lat,
        center_lon=record.center_lon,
        radius_m=record.radius_m,
        enabled=record.enabled,
        owntracks_rid=record.owntracks_rid,
    )


def _participant_to_schema(record: ParticipantRecord) -> ParticipantOut:
    return ParticipantOut(
        participant_id=record.participant_id,
        display_name=record.display_name,
        tracking_device_label=record.tracking_device_label,
        enabled=record.enabled,
    )


def _require_discovery_cache(request: Request) -> Path:
    cache_path = discovery_cache_path_from_request(request)
    if cache_path is None:
        raise HTTPException(
            status_code=HTTPStatus.CONFLICT,
            detail=(
                "Cannot persist Automations data: server started with "
                "--no-discovery-cache. Restart with a discovery cache path."
            ),
        )
    return cache_path
