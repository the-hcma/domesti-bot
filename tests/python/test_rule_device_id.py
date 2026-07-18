"""Hermetic tests for canonical rule device_id helpers and migration."""

from __future__ import annotations

from app.api.schemas import (
    RuleConditionsOut,
    RuleDeviceActionOut,
    RuleOut,
    SettingsLocationOut,
    VacationModeSettingsOut,
)
from app.automation_rules_loader import AutomationRulesBundle
from app.device_enums import DeviceFamilyId, DeviceIdResolution, RuleDeviceActionType, RuleTrigger
from app.rule_device_id import (
    RULE_DEVICE_ID_DISPLAY_NAME_WARNING,
    is_canonical_rule_device_id,
    is_tailwind_composite_device_id,
    non_canonical_device_id_detail,
)
from app.rule_device_id_migrate import (
    DEVICE_ID_RESOLUTION_MAC,
    migrate_bundle_device_ids,
)


def test_is_canonical_rule_device_id_accepts_normalized_mac() -> None:
    assert is_canonical_rule_device_id(DeviceFamilyId.KASA, "aa:bb:cc:dd:ee:ff")
    assert is_canonical_rule_device_id(DeviceFamilyId.SONOS, "aa:bb:cc:dd:ee:ff")
    assert is_canonical_rule_device_id(DeviceFamilyId.VIZIO, "aa:bb:cc:dd:ee:ff")


def test_is_canonical_rule_device_id_rejects_display_name() -> None:
    assert not is_canonical_rule_device_id(DeviceFamilyId.KASA, "Garage light")
    assert not is_canonical_rule_device_id(DeviceFamilyId.SONOS, "Living Room")


def test_is_tailwind_composite_device_id_requires_hub_mac_and_door() -> None:
    assert is_tailwind_composite_device_id("aa:bb:cc:dd:ee:ff:door-1")
    assert is_canonical_rule_device_id(
        DeviceFamilyId.TAILWIND,
        "aa:bb:cc:dd:ee:ff:door-1",
    )
    assert not is_tailwind_composite_device_id("aa:bb:cc:dd:ee:ff")
    assert not is_canonical_rule_device_id(DeviceFamilyId.TAILWIND, "Henrique's side")


def test_migrate_bundle_device_ids_records_unresolved_labels() -> None:
    bundle = AutomationRulesBundle(
        device_id_resolution=DeviceIdResolution.PREFERRED_LABEL,
        rules=[
            RuleOut(
                conditions=RuleConditionsOut(all=[]),
                cooldown_s=0,
                device_actions=[
                    RuleDeviceActionOut(
                        action=RuleDeviceActionType.TURN_OFF,
                        device_id="Missing lamp",
                        family_id=DeviceFamilyId.KASA,
                    ),
                ],
                enabled=True,
                id="away",
                label="Away",
                min_location_accuracy_m=50,
                notify_on_fire=False,
                triggers=[RuleTrigger.EDGE_TRUE],
            ),
        ],
        settings_location=SettingsLocationOut(
            home_label="Home",
            lat=1.0,
            lon=2.0,
            timezone="America/New_York",
        ),
        vacation_mode=VacationModeSettingsOut(),
        version=1,
    )
    _migrated, report = migrate_bundle_device_ids(bundle, label_to_canonical={})
    assert report.rewritten == ()
    assert report.unresolved == (("away", "kasa", "Missing lamp"),)


def test_migrate_bundle_device_ids_rewrites_labels_via_lookup() -> None:
    bundle = AutomationRulesBundle(
        device_id_resolution=DeviceIdResolution.PREFERRED_LABEL,
        rules=[
            RuleOut(
                conditions=RuleConditionsOut(all=[]),
                cooldown_s=0,
                device_actions=[
                    RuleDeviceActionOut(
                        action=RuleDeviceActionType.TURN_OFF,
                        device_id="Garage light",
                        family_id=DeviceFamilyId.KASA,
                    ),
                ],
                enabled=True,
                id="away",
                label="Away",
                min_location_accuracy_m=50,
                notify_on_fire=False,
                triggers=[RuleTrigger.EDGE_TRUE],
            ),
        ],
        settings_location=SettingsLocationOut(
            home_label="Home",
            lat=1.0,
            lon=2.0,
            timezone="America/New_York",
        ),
        vacation_mode=VacationModeSettingsOut(),
        version=1,
    )
    migrated, report = migrate_bundle_device_ids(
        bundle,
        label_to_canonical={("kasa", "garage light"): "98:25:4a:64:ac:90"},
    )
    assert migrated.device_id_resolution == DEVICE_ID_RESOLUTION_MAC
    assert migrated.rules[0].device_actions[0].device_id == "98:25:4a:64:ac:90"
    assert report.rewritten == (("away", "kasa", "Garage light", "98:25:4a:64:ac:90"),)
    assert report.unresolved == ()


def test_non_canonical_device_id_detail_uses_public_constant() -> None:
    detail = non_canonical_device_id_detail("Garage light")
    assert detail == RULE_DEVICE_ID_DISPLAY_NAME_WARNING.format(device_id="Garage light")
