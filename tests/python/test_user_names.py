"""Tests for automation user name parsing."""

from __future__ import annotations

from app.rules_store import user_record_from_export
from app.user_names import (
    default_display_name,
    format_person_display_name,
    parse_person_name,
)


def test_parse_person_name_splits_first_and_last() -> None:
    assert parse_person_name("Kristen Andrade") == ("Kristen", "Andrade")


def test_parse_person_name_single_token() -> None:
    assert parse_person_name("henrique") == ("henrique", "")


def test_default_display_name_returns_first_name() -> None:
    assert default_display_name("Henrique") == "Henrique"


def test_format_person_display_name_title_cases_lowercase() -> None:
    assert format_person_display_name("henrique") == "Henrique"
    assert format_person_display_name("kristen andrade") == "Kristen Andrade"


def test_user_record_from_export_stores_display_name_on_roster_row() -> None:
    row = user_record_from_export(
        user_id="henrique",
        export_display_name="henrique",
        tracking_device_label="Henrique iPhone",
        enabled=True,
    )
    assert row.first_name == "Henrique"
    assert row.display_name == "Henrique"
