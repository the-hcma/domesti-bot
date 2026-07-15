"""Hermetic tests for the vacation-mode latch."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from app.api.schemas import VacationModeSettingsOut
from app.db.engine import dispose_engine
from app.db.schema import clear_bootstrap_cache
from app.vacation_mode import (
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
        hysteresis_s=1800.0,
        now=1000.0,
        state=disarmed,
    )
    assert start.armed is False
    assert start.far_since == 1000.0
    assert start.transitioned_to is None

    still_waiting = evaluate_vacation_mode_tick(
        all_far=True,
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


def test_evaluate_disarms_only_after_hysteresis() -> None:
    armed = VacationModeStateRecord(armed=True, far_since=100.0, near_since=None)
    first_near = evaluate_vacation_mode_tick(
        all_far=False,
        hysteresis_s=60.0,
        now=200.0,
        state=armed,
    )
    assert first_near.armed is True
    assert first_near.far_since is None
    assert first_near.near_since == 200.0
    assert first_near.transitioned_to is None

    disarmed = evaluate_vacation_mode_tick(
        all_far=False,
        hysteresis_s=60.0,
        now=260.0,
        state=VacationModeStateRecord(
            armed=True,
            far_since=None,
            near_since=200.0,
        ),
    )
    assert disarmed.armed is False
    assert disarmed.transitioned_to is False


def test_evaluate_resets_dwell_when_predicate_flips() -> None:
    waiting_to_arm = VacationModeStateRecord(
        armed=False,
        far_since=1000.0,
        near_since=None,
    )
    flipped = evaluate_vacation_mode_tick(
        all_far=False,
        hysteresis_s=1800.0,
        now=1500.0,
        state=waiting_to_arm,
    )
    assert flipped.far_since is None
    assert flipped.near_since == 1500.0
    assert flipped.armed is False


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
    with patch("app.vacation_mode.users_min_distance_from_home_met", return_value=True):
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
        notification_emails=["operator@example.com", " operator@example.com "],
        user_ids=["user-a"],
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
        patch("app.vacation_mode.operator_alert_store") as alerts,
    ):
        sent = send_vacation_mode_transition_email(db, armed=True, settings=settings)
    assert sent is True
    message = deliver.call_args.args[1]
    assert message["Subject"] == "domesti-bot vacation mode on"
    assert message["To"] == "operator@example.com"
    alerts.clear_smtp_notification_failure.assert_called_once()
    dispose_engine(db)
