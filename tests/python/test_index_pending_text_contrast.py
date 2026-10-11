"""The amber text color on the landing page stays readable on both themes."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_INDEX_HTML = Path(__file__).resolve().parents[2] / "app" / "api" / "static" / "index.html"
_MIN_TEXT_CONTRAST = 4.5  # WCAG AA for small text
_WHITE = (255, 255, 255)


def _contrast(foreground: tuple[int, int, int], background: tuple[int, int, int]) -> float:
    def luminance(rgb: tuple[int, int, int]) -> float:
        channels = [c / 255 for c in rgb]
        linear = [c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
        return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]

    lighter, darker = sorted((luminance(foreground), luminance(background)), reverse=True)
    return (lighter + 0.05) / (darker + 0.05)


def _rgb(hex_color: str) -> tuple[int, int, int]:
    return int(hex_color[1:3], 16), int(hex_color[3:5], 16), int(hex_color[5:7], 16)


def test_pending_text_is_readable_on_a_white_card() -> None:
    html = _INDEX_HTML.read_text(encoding="utf-8")
    light_values = re.findall(r"--pending-text:\s*(#[0-9a-fA-F]{6});\s*/\*", html)
    assert light_values, "Expected a light-theme --pending-text declaration, got none"
    for value in light_values:
        assert _contrast(_rgb(value), _WHITE) >= _MIN_TEXT_CONTRAST, value


@pytest.mark.parametrize(
    "selector",
    [
        ".tile-state-unknown",
        ".rules-card-warn",
        ".settings-dialog-help.mytracks-transport-warning",
        '.ep1-header-status-metric[data-responding="false"]',
    ],
)
def test_amber_text_rules_use_the_text_variable(selector: str) -> None:
    html = _INDEX_HTML.read_text(encoding="utf-8")
    rule = re.search(re.escape(selector) + r"\s*\{([^}]*)\}", html)
    assert rule is not None, f"Expected a rule for {selector}, got none"
    assert "color: var(--pending-text)" in rule.group(1)
