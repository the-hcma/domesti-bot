"""Hermetic tests for vacation-mode device anomaly emails (#464)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

from app.api.schemas import VacationModeSettingsOut
from app.db.engine import dispose_engine
from app.db.schema import clear_bootstrap_cache
from app.device_enums import DeviceFamilyId, VacationEmailSource
from app.expected_device_change import (
    expected_device_changes,
    mark_expected_device_change,
)
from app.smtp_service import SmtpConnectionParams
from app.vacation_mode import (
    VACATION_SETTINGS_TEST_ANOMALY_DISCLAIMER,
    VACATION_SETTINGS_TEST_PREAMBLE,
    _vacation_anomaly_debounce,
    build_vacation_mode_anomaly_bodies,
    format_vacation_bool_device_state,
    handle_vacation_device_anomaly,
    send_vacation_mode_anomaly_email,
)
from app.vacation_mode_store import save_vacation_mode_state


def setup_function() -> None:
    expected_device_changes.clear()
    _vacation_anomaly_debounce.clear()


def test_build_vacation_mode_anomaly_bodies_html_and_plain() -> None:
    plain, html = build_vacation_mode_anomaly_bodies(
        family_id=DeviceFamilyId.KASA,
        device_id="porch",
        previous=True,
        current=False,
        observed_at=datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC),
    )
    assert "porch went from on to off." in plain
    assert "Change: on → off" in plain
    assert "<ul>" in html
    assert "Vacation mode (device anomaly)" in plain


def test_format_vacation_bool_device_state_by_family() -> None:
    assert format_vacation_bool_device_state(DeviceFamilyId.KASA, True) == "on"
    assert format_vacation_bool_device_state(DeviceFamilyId.KASA, False) == "off"
    assert format_vacation_bool_device_state(DeviceFamilyId.SONOS, True) == "playing"
    assert format_vacation_bool_device_state(DeviceFamilyId.SONOS, False) == "paused"
    assert format_vacation_bool_device_state(DeviceFamilyId.TAILWIND, True) == "open"
    assert format_vacation_bool_device_state(DeviceFamilyId.TAILWIND, False) == "closed"
    assert format_vacation_bool_device_state(DeviceFamilyId.KASA, None) == "unknown"


def test_handle_vacation_device_anomaly_debounces_same_device(tmp_path: Path) -> None:
    db = tmp_path / "discovery.sqlite"
    clear_bootstrap_cache()
    save_vacation_mode_state(db, armed=True, far_since=1.0, near_since=None)
    with (
        patch("app.vacation_mode.load_vacation_mode_settings", return_value=_settings()),
        patch(
            "app.vacation_mode.send_vacation_mode_anomaly_email",
            return_value=True,
        ) as send,
    ):
        first = handle_vacation_device_anomaly(
            db,
            family_id=DeviceFamilyId.TAILWIND,
            device_id="Left",
            previous=False,
            current=True,
            now_monotonic=100.0,
        )
        second = handle_vacation_device_anomaly(
            db,
            family_id=DeviceFamilyId.TAILWIND,
            device_id="Left",
            previous=True,
            current=False,
            now_monotonic=110.0,
        )
        third = handle_vacation_device_anomaly(
            db,
            family_id=DeviceFamilyId.TAILWIND,
            device_id="Left",
            previous=False,
            current=True,
            now_monotonic=140.0,
        )
    assert first is True
    assert second is False
    assert third is True
    assert send.call_count == 2
    dispose_engine(db)


def test_handle_vacation_device_anomaly_quiet_when_disabled(tmp_path: Path) -> None:
    db = tmp_path / "discovery.sqlite"
    clear_bootstrap_cache()
    save_vacation_mode_state(db, armed=True, far_since=1.0, near_since=None)
    disabled = VacationModeSettingsOut(
        enabled=False,
        hysteresis_s=1800.0,
        min_distance_m=80_000.0,
        notification_emails=["ops@example.com"],
        user_ids=["henrique"],
    )
    with (
        patch("app.vacation_mode.load_vacation_mode_settings", return_value=disabled),
        patch(
            "app.vacation_mode.send_vacation_mode_anomaly_email",
            return_value=True,
        ) as send,
    ):
        sent = handle_vacation_device_anomaly(
            db,
            family_id=DeviceFamilyId.KASA,
            device_id="lamp.local",
            previous=False,
            current=True,
            now_monotonic=10.0,
        )
    assert sent is False
    send.assert_not_called()
    dispose_engine(db)


def test_handle_vacation_device_anomaly_quiet_when_disarmed(tmp_path: Path) -> None:
    db = tmp_path / "discovery.sqlite"
    clear_bootstrap_cache()
    save_vacation_mode_state(db, armed=False, far_since=None, near_since=None)
    with patch(
        "app.vacation_mode.send_vacation_mode_anomaly_email",
        return_value=True,
    ) as send:
        sent = handle_vacation_device_anomaly(
            db,
            family_id=DeviceFamilyId.TAILWIND,
            device_id="Left",
            previous=False,
            current=True,
            now_monotonic=100.0,
        )
    assert sent is False
    send.assert_not_called()
    dispose_engine(db)


def test_handle_vacation_device_anomaly_releases_debounce_when_send_fails(
    tmp_path: Path,
) -> None:
    db = tmp_path / "discovery.sqlite"
    clear_bootstrap_cache()
    save_vacation_mode_state(db, armed=True, far_since=1.0, near_since=None)
    with (
        patch("app.vacation_mode.load_vacation_mode_settings", return_value=_settings()),
        patch(
            "app.vacation_mode.send_vacation_mode_anomaly_email",
            side_effect=[False, True],
        ) as send,
    ):
        first = handle_vacation_device_anomaly(
            db,
            family_id=DeviceFamilyId.TAILWIND,
            device_id="Left",
            previous=False,
            current=True,
            now_monotonic=100.0,
        )
        second = handle_vacation_device_anomaly(
            db,
            family_id=DeviceFamilyId.TAILWIND,
            device_id="Left",
            previous=True,
            current=False,
            now_monotonic=105.0,
        )
    assert first is False
    assert second is True
    assert send.call_count == 2
    dispose_engine(db)


def test_handle_vacation_device_anomaly_sends_when_armed_unmarked(
    tmp_path: Path,
) -> None:
    db = tmp_path / "discovery.sqlite"
    clear_bootstrap_cache()
    save_vacation_mode_state(db, armed=True, far_since=1.0, near_since=None)
    observed = datetime(2026, 7, 15, 12, 0, tzinfo=UTC)
    with (
        patch("app.vacation_mode.load_vacation_mode_settings", return_value=_settings()),
        patch(
            "app.vacation_mode.send_vacation_mode_anomaly_email",
            return_value=True,
        ) as send,
    ):
        sent = handle_vacation_device_anomaly(
            db,
            family_id=DeviceFamilyId.SONOS,
            device_id="RINCON_1",
            previous=True,
            current=False,
            now_monotonic=10.0,
            observed_at=observed,
        )
    assert sent is True
    send.assert_called_once()
    assert send.call_args.kwargs["family_id"] is DeviceFamilyId.SONOS
    assert send.call_args.kwargs["device_id"] == "RINCON_1"
    assert send.call_args.kwargs["previous"] is True
    assert send.call_args.kwargs["current"] is False
    assert send.call_args.kwargs["observed_at"] == observed
    dispose_engine(db)


def test_handle_vacation_device_anomaly_skips_expected_mark(tmp_path: Path) -> None:
    db = tmp_path / "discovery.sqlite"
    clear_bootstrap_cache()
    save_vacation_mode_state(db, armed=True, far_since=1.0, near_since=None)
    mark_expected_device_change(
        DeviceFamilyId.KASA,
        "lamp.local",
        now=50.0,
        window_s=90.0,
    )
    with (
        patch("app.vacation_mode.load_vacation_mode_settings", return_value=_settings()),
        patch(
            "app.vacation_mode.send_vacation_mode_anomaly_email",
            return_value=True,
        ) as send,
    ):
        sent = handle_vacation_device_anomaly(
            db,
            family_id=DeviceFamilyId.KASA,
            device_id="lamp.local",
            previous=False,
            current=True,
            now_monotonic=55.0,
        )
    assert sent is False
    send.assert_not_called()
    dispose_engine(db)


def test_send_vacation_mode_anomaly_email_uses_smtp_stack(tmp_path: Path) -> None:
    db = tmp_path / "discovery.sqlite"
    clear_bootstrap_cache()
    delivery = MagicMock()
    delivery.format_for_log.return_value = "recipient_count=1"
    observed = datetime(2026, 7, 15, 18, 30, tzinfo=UTC)
    with (
        patch(
            "app.vacation_mode.load_outbound_smtp_params",
            return_value=_smtp_params(),
        ),
        patch(
            "app.vacation_mode.deliver_outbound_email",
            return_value=delivery,
        ) as deliver,
        patch("app.vacation_mode.clear_outbound_smtp_failure") as clear_failure,
    ):
        sent = send_vacation_mode_anomaly_email(
            db,
            settings=_settings(),
            family_id=DeviceFamilyId.TAILWIND,
            device_id="Left",
            previous=False,
            current=True,
            observed_at=observed,
        )
    assert sent is True
    message = deliver.call_args.args[1]
    assert message["To"] == "ops@example.com"
    assert "vacation anomaly" in message["Subject"]
    assert "Left" in message["Subject"]
    body = message.get_body(preferencelist=("plain",)).get_content()
    assert "Unexpected Tailwind change while vacation mode is on." in body
    assert "closed → open" in body
    assert "2026-07-15 18:30:00 UTC" in body
    assert "Sent by: domesti-bot · Vacation mode (device anomaly)" in body
    assert VacationEmailSource.ANOMALY.value == "anomaly"
    clear_failure.assert_called_once()
    dispose_engine(db)


def test_send_vacation_mode_anomaly_test_email_marks_subject_and_body(
    tmp_path: Path,
) -> None:
    db = tmp_path / "discovery.sqlite"
    clear_bootstrap_cache()
    delivery = MagicMock()
    delivery.format_for_log.return_value = "recipient_count=1"
    observed = datetime(2026, 7, 16, 12, 0, tzinfo=UTC)
    with (
        patch(
            "app.vacation_mode.load_outbound_smtp_params",
            return_value=_smtp_params(),
        ),
        patch(
            "app.vacation_mode.deliver_outbound_email",
            return_value=delivery,
        ) as deliver,
        patch("app.vacation_mode.clear_outbound_smtp_failure"),
    ):
        sent = send_vacation_mode_anomaly_email(
            db,
            settings=_settings(),
            family_id=DeviceFamilyId.KASA,
            device_id="sample-switch",
            previous=False,
            current=True,
            observed_at=observed,
            source=VacationEmailSource.SETTINGS_TEST,
        )
    assert sent is True
    message = deliver.call_args.args[1]
    assert message["Subject"].startswith("domesti-bot [test] vacation anomaly:")
    body = message.get_body(preferencelist=("plain",)).get_content()
    assert VACATION_SETTINGS_TEST_PREAMBLE in body
    assert VACATION_SETTINGS_TEST_ANOMALY_DISCLAIMER in body
    assert "Sent by: domesti-bot · Automations → Vacation (test email)" in body
    dispose_engine(db)


def _settings() -> VacationModeSettingsOut:
    return VacationModeSettingsOut(
        enabled=True,
        hysteresis_s=1800.0,
        min_distance_m=80_000.0,
        notification_emails=["ops@example.com"],
        user_ids=["henrique"],
    )


def _smtp_params() -> SmtpConnectionParams:
    return SmtpConnectionParams(
        from_address="noreply@example.com",
        host="smtp.example.com",
        mail_domain="example.com",
        password="secret",
        port=587,
        username="user",
    )
