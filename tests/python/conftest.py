from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _set_default_automation_rules_bundle(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default tests to the committed example rules bundle."""
    repo_root = Path(__file__).resolve().parents[2]
    example = repo_root / "automation-rules.json.example"
    monkeypatch.setenv("DOMESTI_AUTOMATION_RULES_FILE", str(example))
