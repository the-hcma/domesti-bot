"""Unit tests for WiFi-at-home presence helpers."""

from __future__ import annotations

from app.api.schemas import SettingsLocationOut
from app.presence_store import _haversine_m
from app.presence_store import UserLocationRecord
from app.rules_store import GeofenceRecord
from app.wifi_home_presence import (
    WIFI_HOME_GEOFENCE_RADIUS_SCALE,
    effective_geofence_ids_containing_location,
    history_row_geofence_inside,
    home_geofence_ids,
    location_accuracy_is_low,
    location_within_wifi_home_proximity,
    wifi_home_geofence_ids,
    wifi_home_presence_applies,
)
from app.rule_evaluator import _reconstruct_geofence_seed_from_history

_MIN_ACCURACY_M = 50

# ~270 m north of the house center: outside strict 250 m radius, inside 300 m slack.
_SLACK_ZONE_LAT = 41.196497
_SLACK_ZONE_LON = -73.888325


def _settings(**overrides: object) -> SettingsLocationOut:
    base = {
        "lat": 41.194072,
        "lon": -73.8883254,
        "timezone": "America/New_York",
    }
    base.update(overrides)
    return SettingsLocationOut.model_validate(base)


def _house_geofence() -> GeofenceRecord:
    return GeofenceRecord(
        geofence_id="house",
        label="House",
        center_lat=41.194072,
        center_lon=-73.888325,
        radius_m=250,
        enabled=True,
        owntracks_rid=None,
    )


def test_location_accuracy_is_low() -> None:
    assert location_accuracy_is_low(300, _MIN_ACCURACY_M) is True
    assert location_accuracy_is_low(50, _MIN_ACCURACY_M) is False
    assert location_accuracy_is_low(20, _MIN_ACCURACY_M) is False
    assert location_accuracy_is_low(None, _MIN_ACCURACY_M) is False


def test_location_within_wifi_home_proximity() -> None:
    geofence = _house_geofence()
    slack_m = float(geofence.radius_m) * WIFI_HOME_GEOFENCE_RADIUS_SCALE
    dist_m = _haversine_m(
        _SLACK_ZONE_LAT,
        _SLACK_ZONE_LON,
        geofence.center_lat,
        geofence.center_lon,
    )
    assert dist_m > float(geofence.radius_m)
    assert dist_m <= slack_m
    assert location_within_wifi_home_proximity(
        41.194085,
        -73.888365,
        geofence,
    )
    assert location_within_wifi_home_proximity(
        _SLACK_ZONE_LAT,
        _SLACK_ZONE_LON,
        geofence,
    )
    assert not location_within_wifi_home_proximity(
        41.2000,
        -73.9000,
        geofence,
    )


def test_wifi_home_geofence_ids_disabled_returns_empty() -> None:
    settings = _settings(wifi_home_presence_enabled=False, wifi_home_geofence_id="house")
    ids = wifi_home_geofence_ids(settings, [_house_geofence()])
    assert ids == frozenset()


def test_home_geofence_ids_ignores_wifi_presence_toggle() -> None:
    settings = _settings(wifi_home_presence_enabled=False, wifi_home_geofence_id="house")
    ids = home_geofence_ids(settings, [_house_geofence()])
    assert ids == frozenset({"house"})


def test_wifi_home_geofence_ids_honors_explicit_geofence() -> None:
    settings = _settings(wifi_home_geofence_id="house")
    ids = wifi_home_geofence_ids(settings, [_house_geofence()])
    assert ids == frozenset({"house"})


def test_wifi_home_presence_applies_for_configured_home_bssid() -> None:
    settings = _settings(wifi_home_geofence_id="house")
    geofences = [_house_geofence()]
    kwargs = {
        "geofences": geofences,
        "lat": 41.2000,
        "lon": -73.9000,
        "min_accuracy_m": _MIN_ACCURACY_M,
        "home_wifi_bssid": "aa:bb:cc:dd:ee:ff",
        "observed_wifi_bssid": "AA:BB:CC:DD:EE:FF",
    }
    assert wifi_home_presence_applies(settings, "house", "w", accuracy_m=500, **kwargs)
    assert not wifi_home_presence_applies(
        settings,
        "house",
        "w",
        accuracy_m=500,
        geofences=geofences,
        lat=41.194085,
        lon=-73.888365,
        min_accuracy_m=_MIN_ACCURACY_M,
        home_wifi_bssid="aa:bb:cc:dd:ee:ff",
        observed_wifi_bssid="11:22:33:44:55:66",
    )


def test_wifi_home_presence_configured_home_bssid_skips_geo_fallback() -> None:
    settings = _settings(wifi_home_geofence_id="house")
    geofences = [_house_geofence()]
    kwargs = {
        "geofences": geofences,
        "lat": _SLACK_ZONE_LAT,
        "lon": _SLACK_ZONE_LON,
        "min_accuracy_m": _MIN_ACCURACY_M,
        "home_wifi_bssid": "aa:bb:cc:dd:ee:ff",
        "observed_wifi_bssid": None,
    }
    assert not wifi_home_presence_applies(settings, "house", "w", accuracy_m=300, **kwargs)


def test_wifi_home_presence_applies_for_low_accuracy_wifi_near_home() -> None:
    settings = _settings(wifi_home_geofence_id="house")
    geofences = [_house_geofence()]
    kwargs = {
        "geofences": geofences,
        "lat": 41.194085,
        "lon": -73.888365,
        "accuracy_m": 300,
        "min_accuracy_m": _MIN_ACCURACY_M,
    }
    assert wifi_home_presence_applies(settings, "house", "w", **kwargs)
    assert not wifi_home_presence_applies(settings, "house", "m", **kwargs)


def test_wifi_home_presence_does_not_apply_for_good_accuracy_wifi() -> None:
    settings = _settings(wifi_home_geofence_id="house")
    geofences = [_house_geofence()]
    assert not wifi_home_presence_applies(
        settings,
        "house",
        "w",
        geofences=geofences,
        lat=41.194085,
        lon=-73.888365,
        accuracy_m=20,
        min_accuracy_m=_MIN_ACCURACY_M,
    )


def test_wifi_home_presence_applies_in_radius_slack_zone() -> None:
    settings = _settings(wifi_home_geofence_id="house")
    geofences = [_house_geofence()]
    assert wifi_home_presence_applies(
        settings,
        "house",
        "w",
        geofences=geofences,
        lat=_SLACK_ZONE_LAT,
        lon=_SLACK_ZONE_LON,
        accuracy_m=300,
        min_accuracy_m=_MIN_ACCURACY_M,
    )


def test_wifi_home_presence_requires_coordinates_within_slack_radius() -> None:
    settings = _settings(wifi_home_geofence_id="house")
    geofences = [_house_geofence()]
    assert not wifi_home_presence_applies(
        settings,
        "house",
        "w",
        geofences=geofences,
        lat=41.2000,
        lon=-73.9000,
        accuracy_m=300,
        min_accuracy_m=_MIN_ACCURACY_M,
    )


def test_effective_geofence_ids_includes_wifi_home() -> None:
    settings = _settings(wifi_home_geofence_id="house")
    geofences = [_house_geofence()]
    location = UserLocationRecord(
        user_id="kristen",
        lat=41.1941344,
        lon=-73.8882358,
        accuracy_m=97,
        connection_type="w",
        fix_at=1_700_000_000.0,
        reported_at=1_700_000_000.0,
        source="test",
    )
    assert effective_geofence_ids_containing_location(
        location,
        geofences,
        settings=settings,
        min_accuracy_m=_MIN_ACCURACY_M,
    ) == ["house"]


def test_history_row_geofence_inside_credits_wifi() -> None:
    settings = _settings(wifi_home_geofence_id="house")
    geofences = [_house_geofence()]
    row = UserLocationRecord(
        user_id="kristen",
        lat=41.1941344,
        lon=-73.8882358,
        accuracy_m=97,
        connection_type="w",
        fix_at=1_700_000_000.0,
        reported_at=1_700_000_000.0,
        source="test",
    )
    assert (
        history_row_geofence_inside(
            row,
            geofences[0],
            geofences,
            settings=settings,
            min_accuracy_m=_MIN_ACCURACY_M,
        )
        is True
    )


def test_reconstruct_geofence_seed_credits_wifi_dwell_streak() -> None:
    settings = _settings(wifi_home_geofence_id="house")
    geofences = [_house_geofence()]
    inside_since_at = 1_700_000_000.0
    history = [
        UserLocationRecord(
            user_id="kristen",
            lat=41.1941344,
            lon=-73.8882358,
            accuracy_m=97,
            connection_type="w",
            fix_at=inside_since_at,
            reported_at=inside_since_at,
            source="test",
        ),
        UserLocationRecord(
            user_id="kristen",
            lat=41.1941344,
            lon=-73.8882358,
            accuracy_m=97,
            connection_type="w",
            fix_at=inside_since_at + 600.0,
            reported_at=inside_since_at + 600.0,
            source="test",
        ),
    ]
    was_inside, outside_since, inside_since = _reconstruct_geofence_seed_from_history(
        geofences[0],
        history,
        dwell_accuracy_limit_m=_MIN_ACCURACY_M,
        edge_accuracy_limit_m=_MIN_ACCURACY_M,
        geofences=geofences,
        settings=settings,
        user_id="kristen",
    )
    assert was_inside is True
    assert outside_since is None
    assert inside_since == inside_since_at
