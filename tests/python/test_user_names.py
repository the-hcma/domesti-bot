"""Tests for automation user name parsing."""

from __future__ import annotations

from app.user_names import default_display_name, parse_person_name


def test_parse_person_name_splits_first_and_last() -> None:
    assert parse_person_name("Kristen Andrade") == ("Kristen", "Andrade")


def test_parse_person_name_single_token() -> None:
    assert parse_person_name("henrique") == ("henrique", "")


def test_default_display_name_returns_first_name() -> None:
    assert default_display_name("Henrique") == "Henrique"
