"""Smoke import for the EP1 ESPHome native API client dependency."""

from __future__ import annotations

import aioesphomeapi


def test_aioesphomeapi_is_importable() -> None:
    assert aioesphomeapi.__name__ == "aioesphomeapi"
