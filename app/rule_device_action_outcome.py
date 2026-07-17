"""Shared types for rule device action dispatch outcomes."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.device_enums import DeviceFamilyId, RuleDeviceActionType


@dataclass(frozen=True)
class RuleDeviceActionOutcome:
    """Observed device state before and after one dispatched rule action."""

    action: RuleDeviceActionType
    after_state: str | None
    before_state: str | None
    completed_at: float
    device_id: str
    error: str | None
    family_id: DeviceFamilyId
    probable: bool
    succeeded: bool

    @classmethod
    def from_json_dict(cls, raw: dict[str, Any]) -> RuleDeviceActionOutcome:
        """Rebuild an outcome from :meth:`to_json_dict`."""

        return cls(
            action=RuleDeviceActionType(str(raw["action"])),
            after_state=_optional_str(raw.get("after_state")),
            before_state=_optional_str(raw.get("before_state")),
            completed_at=float(raw["completed_at"]),
            device_id=str(raw["device_id"]),
            error=_optional_str(raw.get("error")),
            family_id=DeviceFamilyId(str(raw["family_id"])),
            probable=bool(raw["probable"]),
            succeeded=bool(raw["succeeded"]),
        )

    def to_json_dict(self) -> dict[str, Any]:
        """JSON-serializable dict for SQLite pending-notification rows."""

        return {
            "action": self.action.value,
            "after_state": self.after_state,
            "before_state": self.before_state,
            "completed_at": self.completed_at,
            "device_id": self.device_id,
            "error": self.error,
            "family_id": self.family_id.value,
            "probable": self.probable,
            "succeeded": self.succeeded,
        }


def _optional_str(value: object) -> str | None:
    if value is None:
        return None
    return str(value)
