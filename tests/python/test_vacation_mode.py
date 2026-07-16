"""Hermetic tests for the vacation-mode latch."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from app.api.schemas import VacationModeSettingsOut
from app.db.engine import dispose_engine
from app.db.schema import clear_bootstrap_cache
from app.device_enums import VacationEmailSource
from app.vacation_mode import (
    VACATION_SETTINGS_TEST_PREAMBLE,
    VACATION_SETTINGS_TEST_TRANSITION_DISCLAIMER,
    build_vacation_mode_transition_bodies,
    evaluate_vacation_mode_tick,
    send_vacation_mode_transition_email,
    tick_vacation_mode,
)
from app.vacation_mode_store import (
    VacationModeStateRecord,
    load_vacation_mode_state,
    save_vacation_mode_state,
)


def test_evaluate_arms_only_after_hysteresis() -> None:
    disarmed = VacationModeStateRecord(armed=False, far_since=None, near_since=None)
    start = evaluate_vacation_mode_tick(
        all_far=True,
        anyone_home=False,
        hysteresis_s=1800.0,
        now=1000.0,
        state=disarmed,
    )
    assert start.armed is False
    assert start.far_since == 1000.0
    assert start.transitioned_to is None

    still_waiting = evaluate_vacation_mode_tick(
        all_far=True,
        anyone_home=False,
        hysteresis_s=1800.0,
        now=2799.0,
        state=VacationModeStateRecord(
            armed=False,
            far_since=1000.0,
            near_since=None,
        ),
    )
    assert still_waiting.armed is False
    assert still_waiting.transitioned_to is None

    armed = evaluate_vacation_mode_tick(
        all_far=True,
        anyone_home=False,
        hysteresis_s=1800.0,
        now=2800.0,
        state=VacationModeStateRecord(
            armed=False,
            far_since=1000.0,
            near_since=None,
        ),
    )
    assert armed.armed is True
    assert armed.transitioned_to is True


def test_evaluate_disarms_immediately_when_anyone_home() -> None:
    armed = VacationModeStateRecord(armed=True, far_since=100.0, near_since=None)
    disarmed = evaluate_vacation_mode_tick(
        all_far=False,
        anyone_home=True,
        hysteresis_s=60.0,
        now=200.0,
        state=armed,
    )
    assert disarmed.armed is False
    assert disarmed.far_since is None
    assert disarmed.near_since is None
    assert disarmed.transitioned_to is False


def test_evaluate_stays_armed_when_closer_but_not_home() -> None:
    armed = VacationModeStateRecord(armed=True, far_since=100.0, near_since=None)
    still_armed = evaluate_vacation_mode_tick(
        all_far=False,
        anyone_home=False,
        hysteresis_s=60.0,
        now=500.0,
        state=armed,
    )
    assert still_armed.armed is True
    assert still_armed.transitioned_to is None
    assert still_armed.near_since is None


def test_evaluate_resets_arm_dwell_when_not_all_far() -> None:
    waiting_to_arm = VacationModeStateRecord(
        armed=False,
        far_since=1000.0,
        near_since=None,
    )
    flipped = evaluate_vacation_mode_tick(
        all_far=False,
        anyone_home=False,
        hysteresis_s=1800.0,
        now=1500.0,
        state=waiting_to_arm,
    )
    assert flipped.far_since is None
    assert flipped.near_since is None
    assert flipped.armed is False


def test_evaluate_anyone_home_blocks_arm_dwell() -> None:
    waiting = VacationModeStateRecord(armed=False, far_since=1000.0, near_since=None)
    blocked = evaluate_vacation_mode_tick(
        all_far=True,
        anyone_home=True,
        hysteresis_s=1800.0,
        now=2800.0,
        state=waiting,
    )
    assert blocked.armed is False
    assert blocked.far_since is None
    assert blocked.transitioned_to is None


def test_vacation_mode_state_survives_round_trip(tmp_path: Path) -> None:
    db = tmp_path / "discovery.sqlite"
    clear_bootstrap_cache()
    save_vacation_mode_state(
        db,
        armed=True,
        far_since=1234.5,
        near_since=None,
    )
    loaded = load_vacation_mode_state(db)
    assert loaded.armed is True
    assert loaded.far_since == 1234.5
    assert loaded.near_since is None
    dispose_engine(db)


def test_tick_vacation_mode_disabled_leaves_state(tmp_path: Path) -> None:
    db = tmp_path / "discovery.sqlite"
    clear_bootstrap_cache()
    save_vacation_mode_state(db, armed=True, far_since=10.0, near_since=None)
    settings = VacationModeSettingsOut(
        enabled=False,
        hysteresis_s=30.0,
        min_distance_m=80_000.0,
        notification_emails=["operator@example.com"],
        user_ids=["henrique"],
    )
    result = tick_vacation_mode(
        db,
        ctx=MagicMock(),
        now=999.0,
        settings=settings,
    )
    assert result is None
    loaded = load_vacation_mode_state(db)
    assert loaded.armed is True
    assert loaded.far_since == 10.0
    dispose_engine(db)


def test_tick_vacation_mode_arms_and_emails(tmp_path: Path) -> None:
    db = tmp_path / "discovery.sqlite"
    clear_bootstrap_cache()
    settings = VacationModeSettingsOut(
        enabled=True,
        hysteresis_s=30.0,
        min_distance_m=80_000.0,
        min_location_accuracy_m=50,
        notification_emails=["operator@example.com"],
        user_ids=["henrique", "kristen"],
    )
    save_vacation_mode_state(db, armed=False, far_since=100.0, near_since=None)
    with (
        patch("app.vacation_mode.users_min_distance_from_home_met", return_value=True),
        patch("app.vacation_mode.home_geofence_ids", return_value=frozenset({"house"})),
        patch("app.vacation_mode.load_settings_location", return_value=MagicMock()),
        patch("app.vacation_mode.users_any_inside_home_geofence", return_value=False),
        patch(
            "app.vacation_mode.send_vacation_mode_transition_email",
            return_value=True,
        ) as send_email,
    ):
        result = tick_vacation_mode(
            db,
            ctx=MagicMock(),
            now=130.0,
            settings=settings,
        )
    assert result is not None
    assert result.armed is True
    assert result.transitioned_to is True
    send_email.assert_called_once()
    assert send_email.call_args.kwargs["armed"] is True
    assert send_email.call_args.kwargs["source"] is VacationEmailSource.LATCH
    assert load_vacation_mode_state(db).armed is True
    dispose_engine(db)


def test_tick_vacation_mode_disarms_and_emails(tmp_path: Path) -> None:
    db = tmp_path / "discovery.sqlite"
    clear_bootstrap_cache()
    settings = VacationModeSettingsOut(
        enabled=True,
        hysteresis_s=10.0,
        min_distance_m=80_000.0,
        notification_emails=["ops@example.com"],
        user_ids=["henrique"],
    )
    save_vacation_mode_state(db, armed=True, far_since=None, near_since=50.0)
    with (
        patch("app.vacation_mode.users_min_distance_from_home_met", return_value=False),
        patch("app.vacation_mode.home_geofence_ids", return_value=frozenset({"house"})),
        patch("app.vacation_mode.load_settings_location", return_value=MagicMock()),
        patch("app.vacation_mode.users_any_inside_home_geofence", return_value=True),
        patch(
            "app.vacation_mode.send_vacation_mode_transition_email",
            return_value=True,
        ) as send_email,
    ):
        result = tick_vacation_mode(
            db,
            ctx=MagicMock(),
            now=60.0,
            settings=settings,
        )
    assert result is not None
    assert result.armed is False
    assert result.transitioned_to is False
    assert send_email.call_args.kwargs["armed"] is False
    assert send_email.call_args.kwargs["source"] is VacationEmailSource.LATCH
    dispose_engine(db)


def test_tick_vacation_mode_closer_but_not_home_stays_armed(tmp_path: Path) -> None:
    db = tmp_path / "discovery.sqlite"
    clear_bootstrap_cache()
    settings = VacationModeSettingsOut(
        enabled=True,
        hysteresis_s=10.0,
        min_distance_m=80_000.0,
        notification_emails=["ops@example.com"],
        user_ids=["henrique"],
    )
    save_vacation_mode_state(db, armed=True, far_since=None, near_since=None)
    with (
        patch("app.vacation_mode.users_min_distance_from_home_met", return_value=False),
        patch("app.vacation_mode.home_geofence_ids", return_value=frozenset({"house"})),
        patch("app.vacation_mode.load_settings_location", return_value=MagicMock()),
        patch("app.vacation_mode.users_any_inside_home_geofence", return_value=False),
        patch(
            "app.vacation_mode.send_vacation_mode_transition_email",
            return_value=True,
        ) as send_email,
    ):
        result = tick_vacation_mode(
            db,
            ctx=MagicMock(),
            now=60.0,
            settings=settings,
        )
    assert result is not None
    assert result.armed is True
    assert result.transitioned_to is None
    send_email.assert_not_called()
    assert load_vacation_mode_state(db).armed is True
    dispose_engine(db)


def test_tick_notify_on_transition_false_skips_email(tmp_path: Path) -> None:
    db = tmp_path / "discovery.sqlite"
    clear_bootstrap_cache()
    settings = VacationModeSettingsOut(
        enabled=True,
        hysteresis_s=30.0,
        min_distance_m=80_000.0,
        notification_emails=["operator@example.com"],
        notify_on_transition=False,
        user_ids=["henrique"],
    )
    save_vacation_mode_state(db, armed=False, far_since=100.0, near_since=None)
    with (
        patch("app.vacation_mode.users_min_distance_from_home_met", return_value=True),
        patch("app.vacation_mode.home_geofence_ids", return_value=frozenset({"house"})),
        patch("app.vacation_mode.load_settings_location", return_value=MagicMock()),
        patch("app.vacation_mode.users_any_inside_home_geofence", return_value=False),
        patch(
            "app.vacation_mode.send_vacation_mode_transition_email",
            return_value=True,
        ) as send_email,
    ):
        result = tick_vacation_mode(
            db,
            ctx=MagicMock(),
            now=130.0,
            settings=settings,
        )
    assert result is not None
    assert result.armed is True
    assert result.transitioned_to is True
    send_email.assert_not_called()
    assert load_vacation_mode_state(db).armed is True
    dispose_engine(db)


def test_tick_no_home_geofence_fail_safe_disarms(tmp_path: Path) -> None:
    db = tmp_path / "discovery.sqlite"
    clear_bootstrap_cache()
    settings = VacationModeSettingsOut(
        enabled=True,
        hysteresis_s=10.0,
        min_distance_m=80_000.0,
        notification_emails=["ops@example.com"],
        user_ids=["henrique"],
    )
    save_vacation_mode_state(db, armed=True, far_since=None, near_since=None)
    ctx = MagicMock()
    ctx.geofences = ()
    with (
        patch("app.vacation_mode.users_min_distance_from_home_met", return_value=True),
        patch("app.vacation_mode.home_geofence_ids", return_value=frozenset()),
        patch("app.vacation_mode.load_settings_location", return_value=MagicMock()),
        patch(
            "app.vacation_mode.send_vacation_mode_transition_email",
            return_value=True,
        ) as send_email,
    ):
        result = tick_vacation_mode(
            db,
            ctx=ctx,
            now=60.0,
            settings=settings,
        )
    assert result is not None
    assert result.armed is False
    assert result.transitioned_to is False
    send_email.assert_not_called()
    assert load_vacation_mode_state(db).armed is False
    dispose_engine(db)


def test_tick_no_home_geofence_does_not_arm(tmp_path: Path) -> None:
    db = tmp_path / "discovery.sqlite"
    clear_bootstrap_cache()
    settings = VacationModeSettingsOut(
        enabled=True,
        hysteresis_s=30.0,
        min_distance_m=80_000.0,
        notification_emails=["ops@example.com"],
        user_ids=["henrique"],
    )
    save_vacation_mode_state(db, armed=False, far_since=100.0, near_since=None)
    ctx = MagicMock()
    ctx.geofences = ()
    with (
        patch("app.vacation_mode.users_min_distance_from_home_met", return_value=True),
        patch("app.vacation_mode.home_geofence_ids", return_value=frozenset()),
        patch("app.vacation_mode.load_settings_location", return_value=MagicMock()),
        patch(
            "app.vacation_mode.send_vacation_mode_transition_email",
            return_value=True,
        ) as send_email,
    ):
        result = tick_vacation_mode(
            db,
            ctx=ctx,
            now=130.0,
            settings=settings,
        )
    assert result is not None
    assert result.armed is False
    assert result.transitioned_to is None
    send_email.assert_not_called()
    dispose_engine(db)


def test_tick_mid_dwell_survives_reload(tmp_path: Path) -> None:
    db = tmp_path / "discovery.sqlite"
    clear_bootstrap_cache()
    settings = VacationModeSettingsOut(
        enabled=True,
        hysteresis_s=100.0,
        min_distance_m=80_000.0,
        notification_emails=["operator@example.com"],
        user_ids=["henrique"],
    )
    with (
        patch("app.vacation_mode.users_min_distance_from_home_met", return_value=True),
        patch("app.vacation_mode.home_geofence_ids", return_value=frozenset({"house"})),
        patch("app.vacation_mode.load_settings_location", return_value=MagicMock()),
        patch("app.vacation_mode.users_any_inside_home_geofence", return_value=False),
    ):
        first = tick_vacation_mode(
            db,
            ctx=MagicMock(),
            now=1000.0,
            settings=settings,
        )
    assert first is not None
    assert first.armed is False
    assert first.far_since == 1000.0

    # Simulate process restart: new tick with persisted far_since continues dwell.
    with (
        patch("app.vacation_mode.users_min_distance_from_home_met", return_value=True),
        patch("app.vacation_mode.home_geofence_ids", return_value=frozenset({"house"})),
        patch("app.vacation_mode.load_settings_location", return_value=MagicMock()),
        patch("app.vacation_mode.users_any_inside_home_geofence", return_value=False),
        patch(
            "app.vacation_mode.send_vacation_mode_transition_email",
            return_value=True,
        ) as send_email,
    ):
        second = tick_vacation_mode(
            db,
            ctx=MagicMock(),
            now=1100.0,
            settings=settings,
        )
    assert second is not None
    assert second.armed is True
    assert second.transitioned_to is True
    send_email.assert_called_once()
    dispose_engine(db)


def test_send_vacation_mode_transition_email_uses_smtp_stack(tmp_path: Path) -> None:
    db = tmp_path / "discovery.sqlite"
    clear_bootstrap_cache()
    settings = VacationModeSettingsOut(
        enabled=True,
        hysteresis_s=1800.0,
        min_distance_m=80_000.0,
        notification_emails=["operator@example.com", " operator@example.com "],
        user_ids=["user-a", "user-b"],
    )
    smtp_config = MagicMock()
    smtp_config.from_address = "noreply@example.com"
    smtp_config.host = "smtp.example.com"
    smtp_config.mail_domain = "example.com"
    smtp_config.port = 587
    smtp_config.username = "user"
    delivery = MagicMock()
    delivery.format_for_log.return_value = "recipient_count=1"
    home = MagicMock()
    home.home_label = "House"
    home.lat = 37.7749
    home.lon = -122.4194
    with (
        patch("app.vacation_mode.load_smtp_config", return_value=smtp_config),
        patch("app.vacation_mode.smtp_send_ready", return_value=True),
        patch("app.vacation_mode.resolve_password_for_send", return_value="secret"),
        patch(
            "app.vacation_mode.deliver_email_message",
            return_value=delivery,
        ) as deliver,
        patch("app.vacation_mode.operator_alert_store") as alerts,
        patch("app.vacation_mode.try_resolve_home_location", return_value=home),
        patch("app.vacation_mode.load_settings_location", return_value=MagicMock()),
    ):
        sent = send_vacation_mode_transition_email(db, armed=True, settings=settings)
    assert sent is True
    message = deliver.call_args.args[1]
    assert message["Subject"] == "domesti-bot vacation mode on"
    assert message["To"] == "operator@example.com"
    body = message.get_body(preferencelist=("plain",)).get_content()
    assert "Vacation mode is now on." in body
    assert "user-a and user-b" in body
    assert "80 km (≈ 50 mi)" in body
    assert "30 minutes" in body
    assert "House (37.774900, -122.419400)" in body
    assert "Open Automations → Vacation in domesti-bot" in body
    assert "Sent by: domesti-bot · Vacation mode (automatic)" in body
    alerts.clear_smtp_notification_failure.assert_called_once()
    dispose_engine(db)


def test_build_vacation_mode_transition_bodies_includes_deep_link(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DOMESTI_PUBLIC_BASE_URL", "https://domesti.example.com")
    settings = VacationModeSettingsOut(
        enabled=True,
        hysteresis_s=1800.0,
        min_distance_m=80_000.0,
        notification_emails=["ops@example.com"],
        user_ids=["a"],
    )
    home = MagicMock()
    home.home_label = "Home"
    home.lat = 1.0
    home.lon = 2.0
    with (
        patch("app.vacation_mode.try_resolve_home_location", return_value=home),
        patch("app.vacation_mode.load_settings_location", return_value=MagicMock()),
    ):
        plain, html = build_vacation_mode_transition_bodies(
            armed=True,
            settings=settings,
            source=VacationEmailSource.LATCH,
            cache_path=tmp_path / "cache.sqlite",
        )
    assert "Open Automations → Vacation: https://domesti.example.com/#/automations/vacation" in plain
    assert "Instance: https://domesti.example.com" in plain
    assert 'href="https://domesti.example.com/#/automations/vacation"' in html


def test_send_vacation_mode_test_email_marks_subject_and_body(tmp_path: Path) -> None:
    db = tmp_path / "discovery.sqlite"
    clear_bootstrap_cache()
    settings = VacationModeSettingsOut(
        enabled=True,
        hysteresis_s=90.0,
        min_distance_m=500.0,
        notification_emails=["ops@example.com"],
        user_ids=["henrique"],
    )
    smtp_config = MagicMock()
    smtp_config.from_address = "noreply@example.com"
    smtp_config.host = "smtp.example.com"
    smtp_config.mail_domain = "example.com"
    smtp_config.port = 587
    smtp_config.username = "user"
    delivery = MagicMock()
    delivery.format_for_log.return_value = "recipient_count=1"
    with (
        patch("app.vacation_mode.load_smtp_config", return_value=smtp_config),
        patch("app.vacation_mode.smtp_send_ready", return_value=True),
        patch("app.vacation_mode.resolve_password_for_send", return_value="secret"),
        patch(
            "app.vacation_mode.deliver_email_message",
            return_value=delivery,
        ) as deliver,
        patch("app.vacation_mode.operator_alert_store"),
        patch("app.vacation_mode.try_resolve_home_location", return_value=None),
        patch("app.vacation_mode.load_settings_location", return_value=MagicMock()),
    ):
        sent = send_vacation_mode_transition_email(
            db,
            armed=False,
            settings=settings,
            source=VacationEmailSource.SETTINGS_TEST,
        )
    assert sent is True
    message = deliver.call_args.args[1]
    assert message["Subject"] == "domesti-bot [test] vacation mode off"
    body = message.get_body(preferencelist=("plain",)).get_content()
    assert VACATION_SETTINGS_TEST_PREAMBLE in body
    assert VACATION_SETTINGS_TEST_TRANSITION_DISCLAIMER in body
    assert "entered the home geofence" in body
    assert "Sent by: domesti-bot · Automations → Vacation (test email)" in body
    dispose_engine(db)


def test_build_vacation_mode_transition_bodies_humanizes_duration() -> None:
    settings = VacationModeSettingsOut(
        enabled=True,
        hysteresis_s=1800.0,
        min_distance_m=80_000.0,
        notification_emails=["ops@example.com"],
        user_ids=["a", "b", "c"],
    )
    home = MagicMock()
    home.home_label = "Home"
    home.lat = 1.0
    home.lon = 2.0
    with (
        patch("app.vacation_mode.try_resolve_home_location", return_value=home),
        patch("app.vacation_mode.load_settings_location", return_value=MagicMock()),
    ):
        plain, html = build_vacation_mode_transition_bodies(
            armed=True,
            settings=settings,
            source=VacationEmailSource.LATCH,
        )
    assert "a, b, and c" in plain
    assert "30 minutes" in plain
    assert "Wait before turning on: 30 minutes" in plain
    assert "Home (1.000000, 2.000000)" in plain
    assert "Sent by: domesti-bot · Vacation mode (automatic)" in plain
    assert "<ul>" in html
    assert "Open Automations → Vacation in domesti-bot" in plain


def test_format_vacation_home_label_survives_settings_load_error() -> None:
    settings = VacationModeSettingsOut(
        enabled=True,
        hysteresis_s=60.0,
        min_distance_m=1000.0,
        notification_emails=["ops@example.com"],
        user_ids=["henrique"],
    )
    with patch(
        "app.vacation_mode.load_settings_location",
        side_effect=RuntimeError("boom"),
    ):
        plain, _html = build_vacation_mode_transition_bodies(
            armed=True,
            settings=settings,
            source=VacationEmailSource.LATCH,
        )
    assert "home (unavailable — could not load settings location)" in plain


def test_tick_email_failure_records_operator_alert(tmp_path: Path) -> None:
    db = tmp_path / "discovery.sqlite"
    clear_bootstrap_cache()
    settings = VacationModeSettingsOut(
        enabled=True,
        hysteresis_s=30.0,
        min_distance_m=80_000.0,
        notification_emails=["operator@example.com"],
        user_ids=["henrique"],
    )
    save_vacation_mode_state(db, armed=False, far_since=100.0, near_since=None)
    with (
        patch("app.vacation_mode.users_min_distance_from_home_met", return_value=True),
        patch("app.vacation_mode.home_geofence_ids", return_value=frozenset({"house"})),
        patch("app.vacation_mode.load_settings_location", return_value=MagicMock()),
        patch("app.vacation_mode.users_any_inside_home_geofence", return_value=False),
        patch(
            "app.vacation_mode.send_vacation_mode_transition_email",
            side_effect=RuntimeError("smtp down"),
        ),
        patch("app.vacation_mode.operator_alert_store") as alerts,
    ):
        result = tick_vacation_mode(
            db,
            ctx=MagicMock(),
            now=130.0,
            settings=settings,
        )
    assert result is not None
    assert result.armed is True
    alerts.record_smtp_notification_failure.assert_called_once()
    assert load_vacation_mode_state(db).armed is True
    dispose_engine(db)
