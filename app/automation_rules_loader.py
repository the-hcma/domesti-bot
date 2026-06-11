"""Load the file-backed automation rule bundle from ``automation-rules.json``."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, ValidationError

from app.api.schemas import RuleOut, SettingsLocationOut
from app.db.secrets_key import secrets_json_path

_AUTOMATION_RULES_FILENAME = "automation-rules.json"
_AUTOMATION_RULES_EXAMPLE_FILENAME = "automation-rules.json.example"
AutomationRulesSource = Literal["operator", "example"]


class AutomationRulesBundle(BaseModel):
    """On-disk rule bundle (``automation-rules.json`` or the committed example)."""

    model_config = ConfigDict(extra="ignore")

    device_id_resolution: str = "preferred_label"
    rules: list[RuleOut]
    settings_location: SettingsLocationOut
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


def automation_rules_source() -> AutomationRulesSource:
    """Whether the active bundle path is the operator copy or the example template."""
    override = (os.environ.get("DOMESTI_AUTOMATION_RULES_FILE") or "").strip()
    if override:
        return "operator"
    root = secrets_json_path().parent
    if (root / _AUTOMATION_RULES_FILENAME).is_file():
        return "operator"
    return "example"


def load_automation_rules_bundle(
    *,
    path: Path | None = None,
) -> AutomationRulesBundle:
    """Parse and validate the automation rule bundle."""
    resolved = (path or automation_rules_json_path()).expanduser().resolve()
    if not resolved.is_file():
        raise AutomationRulesLoadError(
            f"Expected automation rules file at {resolved}, got missing path"
        )
    try:
        text = resolved.read_text(encoding="utf-8")
        raw = json.loads(text)
    except json.JSONDecodeError as exc:
        raise AutomationRulesLoadError(
            f"Expected {resolved} to contain JSON, got invalid JSON: {exc}"
        ) from exc
    if not isinstance(raw, dict):
        raise AutomationRulesLoadError(
            f"Expected {resolved} to contain a JSON object, got {type(raw).__name__}"
        )
    try:
        return AutomationRulesBundle.model_validate(raw)
    except ValidationError as exc:
        raise AutomationRulesLoadError(
            f"Expected {resolved} to match the automation rules schema, got: {exc}"
        ) from exc


def list_automation_rules(*, path: Path | None = None) -> list[RuleOut]:
    """Return enabled and disabled rules from the bundle."""
    return load_automation_rules_bundle(path=path).rules


def load_settings_location(*, path: Path | None = None) -> SettingsLocationOut:
    """Return sunset/home coordinates from the bundle."""
    return load_automation_rules_bundle(path=path).settings_location
