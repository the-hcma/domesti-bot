"""Hermetic tests for on-demand my-tracks location request coordination."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from cryptography.fernet import Fernet

from app.api.schemas import (
    RuleConditionsOut,
    RuleDeviceActionOut,
    RuleOut,
    UsersInsideGeofenceCondition,
)
from app.device_enums import DeviceFamilyId, RuleDeviceActionType
from app.db.secrets import save_mytracks_relay_api_key_to_db
from app.location_history_retention import default_location_history_retention
from app.location_request_coordinator import (
    DEFERRED_EDGE_FRACTION,
    LOCATION_REQUEST_COOLDOWN_S,
    DeferredAccuracyEdgeSnapshot,
    LocationRequestContext,
    LocationRequestCoordinator,
    _cooldown_until_from_result,
    _location_request_context_log_value,
)
from app.mytracks_service import RequestLocationResult
from app.mytracks_store import (
    MyTracksConfigSave,
    MyTracksPairingSave,
    load_remote_request_location_enabled,
    save_mytracks_config,
    save_mytracks_pairing,
    set_remote_request_location_enabled,
)
from app.presence_store import UserLocationRecord, upsert_user_location
from app.rules_store import GeofenceRecord, UserRecord, replace_geofences, replace_users


@pytest.fixture(autouse=True)
def _fernet_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DOMESTI_BOT_SECRETS_KEY", Fernet.generate_key().decode("ascii"))


def _seed_db(db: Path) -> None:
    replace_users(
        db,
        [
            UserRecord(
                user_id="henrique",
                first_name="Henrique",
                last_name="",
                display_name="Henrique",
                tracking_device_label="Pixel",
                enabled=True,
                home_wifi_bssid="aa:bb:cc:dd:ee:ff",
            ),
        ],
    )
    replace_geofences(
        db,
        [
            GeofenceRecord(
                geofence_id="house",
                label="House",
                center_lat=41.194072,
                center_lon=-73.888325,
                radius_m=250,
                enabled=True,
                owntracks_rid=None,
            ),
        ],
    )
    save_mytracks_config(
        db,
        MyTracksConfigSave(domain="https://tracks.example.com", username="admin"),
    )
    save_mytracks_pairing(
        db,
        MyTracksPairingSave(
            domain="https://tracks.example.com",
            username="admin",
            domesti_public_base_url="https://domesti.example.com",
            user_location_update_url="https://domesti.example.com/v1/webhooks/location_update",
            user_location_test_url="https://domesti.example.com/v1/webhooks/location_update/test",
        ),
    )
    save_mytracks_relay_api_key_to_db(db, "relay-secret")
    set_remote_request_location_enabled(db, enabled=True)


def _write_edge_rule(path: Path) -> None:
    rule = RuleOut(
        accuracy_edge_grace_s=120,
        conditions=RuleConditionsOut(
            all=[
                UsersInsideGeofenceCondition(
                    type="users_inside_geofence",
                    geofence_id="house",
                    user_ids=["henrique"],
                ),
            ],
        ),
        cooldown_s=0,
        device_actions=[
            RuleDeviceActionOut(
                family_id=DeviceFamilyId.KASA,
                device_id="Garage",
                action=RuleDeviceActionType.TURN_ON,
            ),
        ],
        enabled=True,
        id="arrive-home",
        label="Arrive home",
        min_location_accuracy_m=50,
        notification_emails=[],
        notify_on_fire=False,
        trigger="edge_true",
    )
    payload = {
        "version": 1,
        "device_id_resolution": "preferred_label",
        "settings_location": {
            "lat": 41.194072,
            "lon": -73.8883254,
            "timezone": "America/New_York",
            "home_label": "Home",
        },
        "rules": [rule.model_dump(mode="json")],
    }
    path.write_text(json.dumps(payload), encoding="utf-8")


def _write_scheduled_rule_for_stale_watchdog(path: Path) -> None:
    rule = RuleOut(
        conditions=RuleConditionsOut(
            all=[
                UsersInsideGeofenceCondition(
                    type="users_inside_geofence",
                    geofence_id="house",
                    user_ids=["henrique"],
                ),
            ],
        ),
        cooldown_s=0,
        device_actions=[],
        enabled=True,
        id="morning-lights",
        label="Morning lights",
        min_location_accuracy_m=50,
        notification_emails=[],
        notify_on_fire=False,
        schedule_cron="0 8 * * *",
        trigger="scheduled",
    )
    payload = {
        "version": 1,
        "device_id_resolution": "preferred_label",
        "settings_location": {
            "lat": 41.194072,
            "lon": -73.8883254,
            "timezone": "America/New_York",
            "home_label": "Home",
        },
        "rules": [rule.model_dump(mode="json")],
    }
    path.write_text(json.dumps(payload), encoding="utf-8")


@pytest.mark.asyncio
async def test_accuracy_streak_requests_fresh_location(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle = tmp_path / "rules.json"
    db = tmp_path / "discovery.sqlite"
    _write_edge_rule(bundle)
    _seed_db(db)
    monkeypatch.setenv("DOMESTI_AUTOMATION_RULES_FILE", str(bundle))
    monkeypatch.setattr(
        "app.location_request_coordinator.ACCURACY_STREAK_COUNT",
        2,
    )

    now = 1_700_000_000.0
    for offset, accuracy_m in [(0.0, 120), (10.0, 130)]:
        upsert_user_location(
            db,
            UserLocationRecord(
                user_id="henrique",
                lat=41.194085,
                lon=-73.888365,
                accuracy_m=accuracy_m,
                fix_at=now + offset, reported_at=now + offset,
                source="test",
            ),
            retention=default_location_history_retention(),
        )

    coordinator = LocationRequestCoordinator(cache_path=db, now_fn=lambda: now + 10.0)
    request_mock = AsyncMock(
        return_value=RequestLocationResult(status="accepted"),
    )
    with patch(
        "app.location_request_coordinator.request_user_location",
        request_mock,
    ):
        await coordinator._maybe_request_async(
            "henrique",
            context=LocationRequestContext(
                deferred_edges=(),
                location=UserLocationRecord(
                    user_id="henrique",
                    lat=41.194085,
                    lon=-73.888365,
                    accuracy_m=130,
                    fix_at=now + 10.0, reported_at=now + 10.0,
                    source="test",
                ),
                now=now + 10.0,
            ),
        )

    request_mock.assert_awaited_once()
    assert request_mock.await_args is not None
    assert request_mock.await_args.kwargs["reason"] == "accuracy_streak"


@pytest.mark.asyncio
async def test_coordinator_skips_when_remote_requests_disabled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle = tmp_path / "rules.json"
    db = tmp_path / "discovery.sqlite"
    _write_edge_rule(bundle)
    _seed_db(db)
    set_remote_request_location_enabled(db, enabled=False)
    monkeypatch.setenv("DOMESTI_AUTOMATION_RULES_FILE", str(bundle))
    assert load_remote_request_location_enabled(db) is False

    now = 1_700_000_000.0
    coordinator = LocationRequestCoordinator(cache_path=db, now_fn=lambda: now)
    request_mock = AsyncMock(return_value=RequestLocationResult(status="accepted"))
    with patch(
        "app.location_request_coordinator.request_user_location",
        request_mock,
    ):
        await coordinator._maybe_request_async(
            "henrique",
            context=LocationRequestContext(
                deferred_edges=(),
                location=UserLocationRecord(
                    user_id="henrique",
                    lat=41.194085,
                    lon=-73.888365,
                    accuracy_m=120,
                    fix_at=now, reported_at=now,
                    source="test",
                ),
                now=now,
            ),
        )
    request_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_coordinator_skips_when_wifi_home_bssid_matches(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle = tmp_path / "rules.json"
    db = tmp_path / "discovery.sqlite"
    _write_edge_rule(bundle)
    _seed_db(db)
    monkeypatch.setenv("DOMESTI_AUTOMATION_RULES_FILE", str(bundle))

    now = 1_700_000_000.0
    location = UserLocationRecord(
        user_id="henrique",
        lat=41.194085,
        lon=-73.888365,
        accuracy_m=120,
        fix_at=now, reported_at=now,
        source="test",
        connection_type="w",
        wifi_bssid="aa:bb:cc:dd:ee:ff",
    )
    coordinator = LocationRequestCoordinator(cache_path=db, now_fn=lambda: now)
    request_mock = AsyncMock(return_value=RequestLocationResult(status="accepted"))
    with patch(
        "app.location_request_coordinator.request_user_location",
        request_mock,
    ):
        await coordinator._maybe_request_async(
            "henrique",
            context=LocationRequestContext(
                deferred_edges=(),
                location=location,
                now=now,
            ),
        )
    request_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_deferred_edge_trigger_respects_grace_fraction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle = tmp_path / "rules.json"
    db = tmp_path / "discovery.sqlite"
    _write_edge_rule(bundle)
    _seed_db(db)
    monkeypatch.setenv("DOMESTI_AUTOMATION_RULES_FILE", str(bundle))

    now = 1_700_000_000.0
    grace_s = 120.0
    expires_at = now + grace_s * 0.9
    location = UserLocationRecord(
        user_id="henrique",
        lat=41.194085,
        lon=-73.888365,
        accuracy_m=120,
        fix_at=now, reported_at=now,
        source="test",
    )
    coordinator = LocationRequestCoordinator(cache_path=db, now_fn=lambda: now)
    request_mock = AsyncMock(return_value=RequestLocationResult(status="accepted"))
    with patch(
        "app.location_request_coordinator.request_user_location",
        request_mock,
    ):
        await coordinator._maybe_request_async(
            "henrique",
            context=LocationRequestContext(
                deferred_edges=(
                    DeferredAccuracyEdgeSnapshot(
                        event="entered",
                        expires_at=expires_at,
                        geofence_id="house",
                        observed_at=now,
                        rule_id="arrive-home",
                        user_id="henrique",
                    ),
                ),
                location=location,
                now=now,
            ),
        )
    request_mock.assert_not_awaited()

    request_mock.reset_mock()
    later = now + grace_s * DEFERRED_EDGE_FRACTION + 1.0
    with patch(
        "app.location_request_coordinator.request_user_location",
        request_mock,
    ):
        await coordinator._maybe_request_async(
            "henrique",
            context=LocationRequestContext(
                deferred_edges=(
                    DeferredAccuracyEdgeSnapshot(
                        event="entered",
                        expires_at=now + grace_s,
                        geofence_id="house",
                        observed_at=now,
                        rule_id="arrive-home",
                        user_id="henrique",
                    ),
                ),
                location=location,
                now=later,
            ),
        )
    request_mock.assert_awaited_once()
    assert request_mock.await_args is not None
    assert request_mock.await_args.kwargs["reason"] == "deferred_edge"


def test_cooldown_until_from_result_prefers_server_timestamp() -> None:
    now = 1_700_000_000.0
    server_until = now + 25.0
    assert _cooldown_until_from_result(now=now, cooldown_until_epoch=server_until) == server_until


def test_cooldown_until_from_result_falls_back_to_local_default() -> None:
    now = 1_700_000_000.0
    assert _cooldown_until_from_result(now=now, cooldown_until_epoch=None) == (
        now + LOCATION_REQUEST_COOLDOWN_S
    )


def test_location_request_context_log_value_uses_not_applicable_for_missing() -> None:
    assert _location_request_context_log_value(None) == "<not applicable>"
    assert _location_request_context_log_value("") == "<not applicable>"
    assert _location_request_context_log_value("  ") == "<not applicable>"
    assert _location_request_context_log_value("arrive-home") == "arrive-home"


@pytest.mark.asyncio
async def test_accepted_log_uses_not_applicable_for_missing_rule_and_geofence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    import logging

    bundle = tmp_path / "rules.json"
    db = tmp_path / "discovery.sqlite"
    _write_scheduled_rule_for_stale_watchdog(bundle)
    _seed_db(db)
    monkeypatch.setenv("DOMESTI_AUTOMATION_RULES_FILE", str(bundle))

    coordinator = LocationRequestCoordinator(cache_path=db, now_fn=lambda: 1_700_000_000.0)
    request_mock = AsyncMock(return_value=RequestLocationResult(status="accepted"))
    with (
        caplog.at_level(logging.INFO, logger="mytracks"),
        patch(
            "app.location_request_coordinator.request_user_location",
            request_mock,
        ),
    ):
        await coordinator._maybe_request_async(
            "henrique",
            context=LocationRequestContext(
                deferred_edges=(),
                location=UserLocationRecord(
                    user_id="henrique",
                    lat=41.194085,
                    lon=-73.888365,
                    accuracy_m=12,
                    fix_at=1_700_000_000.0, reported_at=1_700_000_000.0,
                    source="test",
                ),
                now=1_700_000_000.0,
            ),
            reason="stale_watchdog",
            require_edge_rules=False,
            rule_id=None,
            geofence_id=None,
        )

    assert any(
        "rule_id=<not applicable> geofence_id=<not applicable>" in record.message
        for record in caplog.records
    )


@pytest.mark.asyncio
async def test_accepted_response_records_server_cooldown_until(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle = tmp_path / "rules.json"
    db = tmp_path / "discovery.sqlite"
    _write_edge_rule(bundle)
    _seed_db(db)
    monkeypatch.setenv("DOMESTI_AUTOMATION_RULES_FILE", str(bundle))
    monkeypatch.setattr("app.location_request_coordinator.ACCURACY_STREAK_COUNT", 1)

    now = 1_700_000_000.0
    upsert_user_location(
        db,
        UserLocationRecord(
            user_id="henrique",
            lat=41.194085,
            lon=-73.888365,
            accuracy_m=120,
            fix_at=now, reported_at=now,
            source="test",
        ),
        retention=default_location_history_retention(),
    )

    coordinator = LocationRequestCoordinator(cache_path=db, now_fn=lambda: now + 1.0)
    server_until = now + 31.0
    request_mock = AsyncMock(
        return_value=RequestLocationResult(
            status="accepted",
            cooldown_until_epoch=server_until,
        ),
    )
    with patch(
        "app.location_request_coordinator.request_user_location",
        request_mock,
    ):
        await coordinator._maybe_request_async(
            "henrique",
            context=LocationRequestContext(
                deferred_edges=(),
                location=UserLocationRecord(
                    user_id="henrique",
                    lat=41.194085,
                    lon=-73.888365,
                    accuracy_m=120,
                    fix_at=now + 1.0, reported_at=now + 1.0,
                    source="test",
                ),
                now=now + 1.0,
            ),
        )

    assert coordinator._user_in_local_cooldown("henrique", now=now + 30.0)
    assert not coordinator._user_in_local_cooldown("henrique", now=server_until)
