"""Migrate automation-rule ``device_id`` values from display names to MACs."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.api.schemas import RuleOut
from app.automation_rules_loader import (
    AutomationRulesBundle,
    automation_rules_json_path,
    load_automation_rules_bundle,
)
from app.device_discovery_store import (
    load_cached_configs,
    load_display_names,
    load_sonos_zones,
    load_vizio_tvs,
)
from app.device_enums import DeviceFamilyId, DeviceIdResolution
from app.device_mac import try_normalize_mac
from app.rule_device_id import is_canonical_rule_device_id

_LOGGER = logging.getLogger(__name__)

DEVICE_ID_RESOLUTION_MAC = DeviceIdResolution.MAC


@dataclass(frozen=True, slots=True)
class RuleDeviceIdMigrationReport:
    """Summary of a label → MAC rewrite pass."""

    rewritten: tuple[tuple[str, str, str, str], ...]
    """``(rule_id, family_id, old_device_id, new_device_id)``."""

    unresolved: tuple[tuple[str, str, str], ...]
    """``(rule_id, family_id, device_id)`` still non-canonical after the pass."""


def build_label_to_canonical_lookup(cache_path: Path) -> dict[tuple[str, str], str]:
    """Map ``(family_id, label_lower)`` → canonical device id from the discovery cache."""
    lookup: dict[tuple[str, str], str] = {}
    for _host, alias, _cfg, _klap, mac in load_cached_configs(cache_path):
        mac_s = try_normalize_mac(mac or "")
        if mac_s is None:
            continue
        alias_s = (alias or "").strip()
        if alias_s:
            lookup[("kasa", alias_s.lower())] = mac_s
        lookup[("kasa", mac_s)] = mac_s
    for _uuid, _host, zone_name, mac in load_sonos_zones(cache_path):
        mac_s = try_normalize_mac(mac or "")
        if mac_s is None:
            continue
        if zone_name:
            lookup[("sonos", zone_name.strip().lower())] = mac_s
        lookup[("sonos", mac_s)] = mac_s
    for _host, _port, name, _model, mac, _diid in load_vizio_tvs(cache_path):
        mac_s = try_normalize_mac(mac or "")
        if mac_s is None:
            continue
        if name:
            lookup[("vizio", name.strip().lower())] = mac_s
        lookup[("vizio", mac_s)] = mac_s
    for backend, canonical_key, display_name in load_display_names(cache_path):
        family = backend.strip().lower()
        key = (canonical_key or "").strip()
        label = (display_name or "").strip()
        if not family or not key or not label:
            continue
        if family == "tailwind":
            lookup[("tailwind", label.lower())] = key
            lookup[("tailwind", key.lower())] = key
        elif family in {"kasa", "sonos", "vizio", "androidtv"}:
            lookup[(family, label.lower())] = key
    return lookup


def migrate_automation_rules_file(
    *,
    rules_path: Path | None = None,
    cache_path: Path,
    dry_run: bool = False,
) -> RuleDeviceIdMigrationReport:
    """Rewrite operator/example rules so device_ids are MAC-canonical when resolvable."""
    path = rules_path if rules_path is not None else automation_rules_json_path()
    bundle = load_automation_rules_bundle(path=path)
    lookup = build_label_to_canonical_lookup(cache_path)
    migrated, report = migrate_bundle_device_ids(bundle, label_to_canonical=lookup)
    if dry_run:
        return report
    payload = migrated.model_dump(mode="json")
    # Preserve unknown top-level keys (e.g. ``_notes``) from the on-disk file.
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(
            f"Expected automation rules JSON object at top level, got {type(raw).__name__}",
        )
    raw["device_id_resolution"] = DEVICE_ID_RESOLUTION_MAC
    raw["rules"] = payload["rules"]
    text = json.dumps(raw, indent=2, ensure_ascii=False) + "\n"
    path.write_text(text, encoding="utf-8")
    _LOGGER.info(
        "Migrated rule device_ids path=%s rewritten=%d unresolved=%d",
        path,
        len(report.rewritten),
        len(report.unresolved),
    )
    return report


def migrate_bundle_device_ids(
    bundle: AutomationRulesBundle,
    *,
    label_to_canonical: dict[tuple[str, str], str],
) -> tuple[AutomationRulesBundle, RuleDeviceIdMigrationReport]:
    """Return a rewritten bundle plus a migration report."""
    rewritten: list[tuple[str, str, str, str]] = []
    unresolved: list[tuple[str, str, str]] = []
    new_rules: list[RuleOut] = []
    for rule in bundle.rules:
        raw = rule.model_dump(mode="python")
        _rewrite_rule_dict(
            raw,
            rule_id=rule.id,
            label_to_canonical=label_to_canonical,
            rewritten=rewritten,
            unresolved=unresolved,
        )
        new_rules.append(RuleOut.model_validate(raw))
    migrated = bundle.model_copy(
        update={
            "device_id_resolution": DEVICE_ID_RESOLUTION_MAC,
            "rules": new_rules,
        },
    )
    return migrated, RuleDeviceIdMigrationReport(
        rewritten=tuple(rewritten),
        unresolved=tuple(unresolved),
    )


def _rewrite_device_id(
    *,
    family_id: str,
    device_id: str,
    label_to_canonical: dict[tuple[str, str], str],
) -> str:
    family = family_id.strip().lower()
    trimmed = device_id.strip()
    try:
        family_enum = DeviceFamilyId(family)
    except ValueError:
        return trimmed
    if is_canonical_rule_device_id(family_enum, trimmed):
        return trimmed
    mapped = label_to_canonical.get((family, trimmed.lower()))
    if mapped is not None:
        return mapped
    return trimmed


def _rewrite_rule_dict(
    raw: dict[str, Any],
    *,
    rule_id: str,
    label_to_canonical: dict[tuple[str, str], str],
    rewritten: list[tuple[str, str, str, str]],
    unresolved: list[tuple[str, str, str]],
) -> None:
    actions = raw.get("device_actions")
    if isinstance(actions, list):
        for action in actions:
            if not isinstance(action, dict):
                continue
            family = str(action.get("family_id", ""))
            old = str(action.get("device_id", ""))
            new = _rewrite_device_id(
                family_id=family,
                device_id=old,
                label_to_canonical=label_to_canonical,
            )
            if new != old:
                action["device_id"] = new
                rewritten.append((rule_id, family, old, new))
            try:
                family_enum = DeviceFamilyId(family.strip().lower())
            except ValueError:
                unresolved.append((rule_id, family, str(action.get("device_id", ""))))
                continue
            if not is_canonical_rule_device_id(family_enum, str(action.get("device_id", ""))):
                unresolved.append((rule_id, family, str(action.get("device_id", ""))))
    conditions = raw.get("conditions")
    if isinstance(conditions, dict):
        _rewrite_conditions(
            conditions,
            rule_id=rule_id,
            label_to_canonical=label_to_canonical,
            rewritten=rewritten,
            unresolved=unresolved,
        )


def _rewrite_conditions(
    node: dict[str, Any],
    *,
    rule_id: str,
    label_to_canonical: dict[tuple[str, str], str],
    rewritten: list[tuple[str, str, str, str]],
    unresolved: list[tuple[str, str, str]],
) -> None:
    devices = node.get("devices")
    if isinstance(devices, list):
        for ref in devices:
            if not isinstance(ref, dict):
                continue
            family = str(ref.get("family_id", ""))
            old = str(ref.get("device_id", ""))
            new = _rewrite_device_id(
                family_id=family,
                device_id=old,
                label_to_canonical=label_to_canonical,
            )
            if new != old:
                ref["device_id"] = new
                rewritten.append((rule_id, family, old, new))
            try:
                family_enum = DeviceFamilyId(family.strip().lower())
            except ValueError:
                unresolved.append((rule_id, family, str(ref.get("device_id", ""))))
                continue
            if not is_canonical_rule_device_id(family_enum, str(ref.get("device_id", ""))):
                unresolved.append((rule_id, family, str(ref.get("device_id", ""))))
    for key in ("all", "any", "conditions"):
        children = node.get(key)
        if isinstance(children, list):
            for child in children:
                if isinstance(child, dict):
                    _rewrite_conditions(
                        child,
                        rule_id=rule_id,
                        label_to_canonical=label_to_canonical,
                        rewritten=rewritten,
                        unresolved=unresolved,
                    )
