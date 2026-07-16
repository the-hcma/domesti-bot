"""Load the file-backed automation rule bundle from ``automation-rules.json``."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.api.schemas import (
    RuleOut,
    SettingsLocationIn,
    SettingsLocationOut,
    VacationModeSettingsOut,
)
from app.db.secrets_key import secrets_json_path
from app.home_location import HomeLocationRef, resolve_home_location

_AUTOMATION_RULES_EXAMPLE_FILENAME = "automation-rules.json.example"
_AUTOMATION_RULES_FILENAME = "automation-rules.json"
AutomationRulesSource = Literal["operator", "example"]


class AutomationRulesBundle(BaseModel):
    """On-disk rule bundle (``automation-rules.json`` or the committed example)."""

    model_config = ConfigDict(extra="ignore")

    device_id_resolution: str = "preferred_label"
    rules: list[RuleOut]
    settings_location: SettingsLocationOut
    vacation_mode: VacationModeSettingsOut = Field(
        default_factory=VacationModeSettingsOut,
    )
    version: int


class AutomationRulesLoadError(ValueError):
    """Raised when the bundle file is missing or fails schema validation."""


def automation_rules_json_path() -> Path:
    """Resolve the bundle path (operator file, else committed example)."""
    override = (os.environ.get("DOMESTI_AUTOMATION_RULES_FILE") or "").strip()
    if override:
        return Path(override).expanduser().resolve()
    root = secrets_json_path().parent
    operator = root / _AUTOMATION_RULES_FILENAME
    if operator.is_file():
        return operator
    return root / _AUTOMATION_RULES_EXAMPLE_FILENAME


def automation_rules_operator_json_path() -> Path:
    """Path for mutable operator rules (never the committed ``*.example`` template)."""
    override = (os.environ.get("DOMESTI_AUTOMATION_RULES_FILE") or "").strip()
    if override:
        return Path(override).expanduser().resolve()
    return secrets_json_path().parent / _AUTOMATION_RULES_FILENAME


def automation_rules_source() -> AutomationRulesSource:
    """Whether the active bundle path is the operator copy or the example template."""
    override = (os.environ.get("DOMESTI_AUTOMATION_RULES_FILE") or "").strip()
    if override:
        return "operator"
    root = secrets_json_path().parent
    if (root / _AUTOMATION_RULES_FILENAME).is_file():
        return "operator"
    return "example"


def list_automation_rules(*, path: Path | None = None) -> list[RuleOut]:
    """Return enabled and disabled rules from the bundle."""
    return load_automation_rules_bundle(path=path).rules


def load_automation_rules_bundle(
    *,
    path: Path | None = None,
) -> AutomationRulesBundle:
    """Parse and validate the automation rule bundle."""
    resolved = (path or automation_rules_json_path()).expanduser().resolve()
    if not resolved.is_file():
        raise AutomationRulesLoadError(f"Expected automation rules file at {resolved}, got missing path")
    try:
        text = resolved.read_text(encoding="utf-8")
        raw = json.loads(text)
    except json.JSONDecodeError as exc:
        raise AutomationRulesLoadError(f"Expected {resolved} to contain JSON, got invalid JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise AutomationRulesLoadError(f"Expected {resolved} to contain a JSON object, got {type(raw).__name__}")
    try:
        return AutomationRulesBundle.model_validate(raw)
    except ValidationError as exc:
        raise AutomationRulesLoadError(f"Expected {resolved} to match the automation rules schema, got: {exc}") from exc


def load_home_location(*, path: Path | None = None) -> HomeLocationRef:
    """Return the configured home point, or raise if unconfigured.

    Prefer this (or :func:`app.home_location.resolve_home_location`) for distance
    features instead of reading geofence centers.
    """
    return resolve_home_location(load_settings_location(path=path))


def load_settings_location(*, path: Path | None = None) -> SettingsLocationOut:
    """Return home / timezone settings from the automation rules bundle."""
    return load_automation_rules_bundle(path=path).settings_location


def load_vacation_mode_settings(*, path: Path | None = None) -> VacationModeSettingsOut:
    """Return vacation-mode latch config from the automation rules bundle."""
    return load_automation_rules_bundle(path=path).vacation_mode


def save_settings_location(
    location: SettingsLocationIn | SettingsLocationOut,
    *,
    path: Path | None = None,
) -> SettingsLocationOut:
    """Persist ``settings_location`` into the operator automation rules file.

    Never writes the committed ``automation-rules.json.example`` template. When only
    the example is present, copies it to the operator path first.
    """
    validated = SettingsLocationOut.model_validate(
        location.model_dump(exclude={"home_configured"}),
    )
    source_path = (path or automation_rules_json_path()).expanduser().resolve()
    dest_path = (path or automation_rules_operator_json_path()).expanduser().resolve()
    if not source_path.is_file():
        raise AutomationRulesLoadError(f"Expected automation rules file at {source_path}, got missing path")
    try:
        text = source_path.read_text(encoding="utf-8")
        raw = json.loads(text)
    except json.JSONDecodeError as exc:
        raise AutomationRulesLoadError(f"Expected {source_path} to contain JSON, got invalid JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise AutomationRulesLoadError(f"Expected {source_path} to contain a JSON object, got {type(raw).__name__}")
    raw["settings_location"] = validated.model_dump(
        mode="json",
        exclude={"home_configured"},
    )
    try:
        AutomationRulesBundle.model_validate(raw)
    except ValidationError as exc:
        raise AutomationRulesLoadError(
            f"Expected updated bundle to match the automation rules schema, got: {exc}"
        ) from exc
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    dest_path.write_text(
        json.dumps(raw, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return validated


def save_vacation_mode_settings(
    settings: VacationModeSettingsOut,
    *,
    path: Path | None = None,
) -> VacationModeSettingsOut:
    """Persist ``vacation_mode`` into the operator automation rules file."""
    validated = VacationModeSettingsOut.model_validate(settings.model_dump())
    source_path = (path or automation_rules_json_path()).expanduser().resolve()
    dest_path = (path or automation_rules_operator_json_path()).expanduser().resolve()
    if not source_path.is_file():
        raise AutomationRulesLoadError(f"Expected automation rules file at {source_path}, got missing path")
    try:
        text = source_path.read_text(encoding="utf-8")
        raw = json.loads(text)
    except json.JSONDecodeError as exc:
        raise AutomationRulesLoadError(f"Expected {source_path} to contain JSON, got invalid JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise AutomationRulesLoadError(f"Expected {source_path} to contain a JSON object, got {type(raw).__name__}")
    raw["vacation_mode"] = validated.model_dump(mode="json")
    try:
        AutomationRulesBundle.model_validate(raw)
    except ValidationError as exc:
        raise AutomationRulesLoadError(
            f"Expected updated bundle to match the automation rules schema, got: {exc}"
        ) from exc
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    dest_path.write_text(
        json.dumps(raw, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return validated
