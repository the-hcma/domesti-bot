"""Hermetic tests for stale device display_name digest emails."""

from __future__ import annotations

import threading
from pathlib import Path
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

import pytest

from app.api.schemas import (
    DevicesAnyInStateCondition,
    RuleConditionDeviceRefOut,
    RuleConditionsOut,
    RuleDeviceActionOut,
    RuleOut,
    VacationModeSettingsOut,
)
from app.db.schema import bootstrap_schema
from app.device_display import format_device_display
from app.device_enums import (
    DeviceConditionState,
    DeviceFamilyId,
    OperatorDigestId,
    RuleDeviceActionType,
    RuleTrigger,
    StaleDeviceDisplayNameEmailSource,
)
from app.operator_digest_store import (
    complete_operator_digest_send,
    load_operator_digest_last_sent_at,
    try_claim_operator_digest_for_local_day,
    upsert_operator_digest_last_sent_at,
)
from app.outbound_email import provenance_footer
from app.rule_validation import RuleValidationContext
from app.smtp_service import SmtpConnectionParams, SmtpDeliveryResult
from app.stale_device_display_name_email import (
    STALE_DISPLAY_NAME_DIGEST_FINDING_TEMPLATE,
    STALE_DISPLAY_NAME_DIGEST_INSTANCE_PREFIX,
    STALE_DISPLAY_NAME_DIGEST_INTRO,
    STALE_DISPLAY_NAME_DIGEST_STATUS_LINK_TEXT,
    STALE_DISPLAY_NAME_DIGEST_SUBJECT,
    STALE_DISPLAY_NAME_DIGEST_SUBSYSTEM,
    StaleDisplayNameFinding,
    build_stale_display_name_digest_bodies,
    collect_stale_display_name_findings,
    maybe_send_stale_display_name_digest,
    send_stale_display_name_digest,
)


def test_build_stale_display_name_digest_bodies_include_provenance_and_facts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DOMESTI_PUBLIC_BASE_URL", "https://home.example.com")
    finding = _finding()
    plain, html = build_stale_display_name_digest_bodies(
        (finding,),
        cache_path=None,
        source=StaleDeviceDisplayNameEmailSource.AUTOMATIC,
    )
    device_label = format_device_display(finding.device_id, finding.live_display_name)
    expected_detail = STALE_DISPLAY_NAME_DIGEST_FINDING_TEMPLATE.format(
        device_label=device_label,
        stored=finding.stored_display_name,
    )
    expected_provenance = provenance_footer(
        subsystem=STALE_DISPLAY_NAME_DIGEST_SUBSYSTEM,
        trigger=StaleDeviceDisplayNameEmailSource.AUTOMATIC.value,
    )
    assert STALE_DISPLAY_NAME_DIGEST_INTRO in plain
    assert "Stale label (stale-label)" in plain
    assert expected_detail in plain
    assert device_label == "HDHomeRun tuner (dc:62:79:6c:86:77)"
    assert f"{STALE_DISPLAY_NAME_DIGEST_INSTANCE_PREFIX} https://home.example.com" in plain
    assert expected_provenance in plain
    assert STALE_DISPLAY_NAME_DIGEST_INTRO in html
    assert STALE_DISPLAY_NAME_DIGEST_STATUS_LINK_TEXT in html
    assert "#/automations/status/stale-label" in html
    assert expected_provenance in html


def test_collect_stale_display_name_findings_filters_kind() -> None:
    rule = RuleOut(
        conditions=RuleConditionsOut(all=[]),
        cooldown_s=60,
        device_actions=[
            RuleDeviceActionOut(
                action=RuleDeviceActionType.TURN_OFF,
                device_id="dc:62:79:6c:86:77",
                display_name="Old HDHomeRun name",
                family_id=DeviceFamilyId.KASA,
            ),
        ],
        enabled=True,
        id="stale-label",
        label="Stale label",
        min_location_accuracy_m=50,
        notification_emails=[],
        notify_on_fire=False,
        triggers=[RuleTrigger.SCHEDULED],
        schedule_cron="0 4 * * *",
    )
    ctx = RuleValidationContext(
        device_state=MagicMock(),
        geofence_ids=frozenset(),
        roster_name_hint_lookup={},
        roster_user_id_lookup={},
        smtp_configured=True,
    )
    with (
        patch(
            "app.rule_validation.resolve_kasa_host_by_label",
            return_value="dc:62:79:6c:86:77",
        ),
        patch(
            "app.rule_validation.lookup_preferred_label",
            return_value="HDHomeRun tuner",
        ),
        patch(
            "app.stale_device_display_name_email.lookup_preferred_label",
            return_value="HDHomeRun tuner",
        ),
    ):
        findings = collect_stale_display_name_findings([rule], ctx)
    assert len(findings) == 1
    assert findings[0].rule_id == "stale-label"
    assert findings[0].device_id == "dc:62:79:6c:86:77"
    assert findings[0].live_display_name == "HDHomeRun tuner"
    assert findings[0].stored_display_name == "Old HDHomeRun name"


def test_collect_uses_stale_condition_snapshot_when_action_is_current() -> None:
    rule = RuleOut(
        conditions=RuleConditionsOut(
            all=[
                DevicesAnyInStateCondition(
                    devices=[
                        RuleConditionDeviceRefOut(
                            device_id="dc:62:79:6c:86:77",
                            display_name="Old HDHomeRun name",
                            family_id=DeviceFamilyId.KASA,
                        ),
                    ],
                    state=DeviceConditionState.ON,
                    type="devices_any_in_state",
                ),
            ],
        ),
        cooldown_s=60,
        device_actions=[
            RuleDeviceActionOut(
                action=RuleDeviceActionType.TURN_OFF,
                device_id="dc:62:79:6c:86:77",
                display_name="HDHomeRun tuner",
                family_id=DeviceFamilyId.KASA,
            ),
        ],
        enabled=True,
        id="mixed-label",
        label="Mixed label",
        min_location_accuracy_m=50,
        notification_emails=[],
        notify_on_fire=False,
        triggers=[RuleTrigger.SCHEDULED],
        schedule_cron="0 4 * * *",
    )
    ctx = RuleValidationContext(
        device_state=MagicMock(),
        geofence_ids=frozenset(),
        roster_name_hint_lookup={},
        roster_user_id_lookup={},
        smtp_configured=True,
    )
    with (
        patch(
            "app.rule_validation.resolve_kasa_host_by_label",
            return_value="dc:62:79:6c:86:77",
        ),
        patch(
            "app.rule_validation.lookup_preferred_label",
            return_value="HDHomeRun tuner",
        ),
        patch(
            "app.stale_device_display_name_email.lookup_preferred_label",
            return_value="HDHomeRun tuner",
        ),
    ):
        findings = collect_stale_display_name_findings([rule], ctx)
    assert len(findings) == 1
    assert findings[0].stored_display_name == "Old HDHomeRun name"
    assert findings[0].live_display_name == "HDHomeRun tuner"


def test_maybe_send_delivers_when_findings_and_recipients(
    tmp_path: Path,
) -> None:
    cache = tmp_path / "cache.sqlite"
    bootstrap_schema(cache)
    vacation = VacationModeSettingsOut(
        enabled=True,
        hysteresis_s=1800.0,
        min_distance_m=80_000.0,
        min_location_accuracy_m=50,
        notification_emails=["ops@example.com"],
        notify_on_transition=True,
        user_ids=["hcma"],
    )
    finding = _finding()
    with (
        patch(
            "app.stale_device_display_name_email.load_settings_location",
            return_value=MagicMock(timezone="America/New_York"),
        ),
        patch(
            "app.stale_device_display_name_email.load_smtp_config",
            return_value=MagicMock(),
        ),
        patch(
            "app.stale_device_display_name_email.smtp_send_ready",
            return_value=True,
        ),
        patch(
            "app.stale_device_display_name_email.load_vacation_mode_settings",
            return_value=vacation,
        ),
        patch(
            "app.stale_device_display_name_email.list_automation_rules",
            return_value=[],
        ),
        patch(
            "app.stale_device_display_name_email.collect_stale_display_name_findings",
            return_value=(finding,),
        ),
        patch(
            "app.stale_device_display_name_email.load_outbound_smtp_params",
            return_value=_smtp_params(),
        ),
        patch(
            "app.stale_device_display_name_email.deliver_outbound_email",
            return_value=SmtpDeliveryResult(
                host="smtp.example.com",
                port=587,
                recipients=("ops@example.com",),
                smtp_code=250,
                smtp_response="ok",
            ),
        ) as deliver_mock,
        patch(
            "app.stale_device_display_name_email.domesti_public_base_url",
            return_value=None,
        ),
        patch(
            "app.stale_device_display_name_email.time.time",
            return_value=_NOW + 5.0,
        ),
    ):
        sent = maybe_send_stale_display_name_digest(
            cache_path=cache,
            device_state=MagicMock(),
            now_epoch=_NOW,
            validation_ctx=MagicMock(),
        )
    assert sent is True
    deliver_mock.assert_called_once()
    assert (
        load_operator_digest_last_sent_at(
            cache,
            OperatorDigestId.STALE_DEVICE_DISPLAY_NAME,
        )
        == _NOW + 5.0
    )


def test_maybe_send_skips_when_already_sent_today(tmp_path: Path) -> None:
    cache = tmp_path / "cache.sqlite"
    bootstrap_schema(cache)
    upsert_operator_digest_last_sent_at(
        cache,
        digest_id=OperatorDigestId.STALE_DEVICE_DISPLAY_NAME,
        last_sent_at=_NOW - 60.0,
    )
    with (
        patch(
            "app.stale_device_display_name_email.load_settings_location",
            return_value=MagicMock(timezone="America/New_York"),
        ),
        patch(
            "app.stale_device_display_name_email.send_stale_display_name_digest",
        ) as send_mock,
    ):
        sent = maybe_send_stale_display_name_digest(
            cache_path=cache,
            device_state=MagicMock(),
            now_epoch=_NOW,
        )
    assert sent is False
    send_mock.assert_not_called()


def test_maybe_send_skips_when_smtp_not_ready(tmp_path: Path) -> None:
    cache = tmp_path / "cache.sqlite"
    bootstrap_schema(cache)
    with (
        patch(
            "app.stale_device_display_name_email.load_settings_location",
            return_value=MagicMock(timezone="America/New_York"),
        ),
        patch(
            "app.stale_device_display_name_email.load_smtp_config",
            return_value=None,
        ),
        patch(
            "app.stale_device_display_name_email.send_stale_display_name_digest",
        ) as send_mock,
    ):
        sent = maybe_send_stale_display_name_digest(
            cache_path=cache,
            device_state=MagicMock(),
            now_epoch=_NOW,
        )
    assert sent is False
    send_mock.assert_not_called()


def test_send_stale_display_name_digest_delivers_message(
    tmp_path: Path,
) -> None:
    cache = tmp_path / "cache.sqlite"
    bootstrap_schema(cache)
    with (
        patch(
            "app.stale_device_display_name_email.load_outbound_smtp_params",
            return_value=_smtp_params(),
        ),
        patch(
            "app.stale_device_display_name_email.deliver_outbound_email",
            return_value=SmtpDeliveryResult(
                host="smtp.example.com",
                port=587,
                recipients=("ops@example.com",),
                smtp_code=250,
                smtp_response="ok",
            ),
        ) as deliver_mock,
        patch(
            "app.stale_device_display_name_email.domesti_public_base_url",
            return_value="https://home.example.com",
        ),
    ):
        sent = send_stale_display_name_digest(
            (_finding(),),
            cache_path=cache,
            recipients=["ops@example.com"],
        )
    assert sent is True
    deliver_mock.assert_called_once()
    message = deliver_mock.call_args.args[1]
    assert message["Subject"] == STALE_DISPLAY_NAME_DIGEST_SUBJECT
    assert message["To"] == "ops@example.com"


def test_try_claim_operator_digest_is_atomic_across_threads(tmp_path: Path) -> None:
    cache = tmp_path / "cache.sqlite"
    bootstrap_schema(cache)
    barrier = threading.Barrier(2)
    results: list[float | None] = []
    lock = threading.Lock()

    def _worker() -> None:
        barrier.wait()
        claimed = try_claim_operator_digest_for_local_day(
            cache,
            digest_id=OperatorDigestId.STALE_DEVICE_DISPLAY_NAME,
            now_epoch=_NOW,
            timezone=_TZ,
        )
        with lock:
            results.append(claimed)

    threads = [threading.Thread(target=_worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    tokens = [token for token in results if token is not None]
    assert len(tokens) == 1
    assert results.count(None) == 1
    assert complete_operator_digest_send(
        cache,
        claim_token=tokens[0],
        digest_id=OperatorDigestId.STALE_DEVICE_DISPLAY_NAME,
        last_sent_at=_NOW,
        local_date="2023-11-14",
    )
    assert (
        try_claim_operator_digest_for_local_day(
            cache,
            digest_id=OperatorDigestId.STALE_DEVICE_DISPLAY_NAME,
            now_epoch=_NOW + 10.0,
            timezone=_TZ,
        )
        is None
    )


_NOW = 1_700_000_000.0  # 2023-11-14 ~15:33 UTC
_TZ = ZoneInfo("America/New_York")


def _finding() -> StaleDisplayNameFinding:
    return StaleDisplayNameFinding(
        device_id="dc:62:79:6c:86:77",
        live_display_name="HDHomeRun tuner",
        rule_id="stale-label",
        rule_label="Stale label",
        stored_display_name="Old HDHomeRun name",
    )


def _smtp_params() -> SmtpConnectionParams:
    return SmtpConnectionParams(
        from_address="noreply@example.com",
        host="smtp.example.com",
        mail_domain="example.com",
        password="secret",
        port=587,
        username="noreply@example.com",
    )
