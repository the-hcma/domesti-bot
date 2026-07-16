"""Hermetic tests for shared outbound email helpers."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.outbound_email import (
    automations_mail_url,
    automations_vacation_url,
    build_outbound_message,
    provenance_footer,
    rule_fire_provenance_footer,
    with_instance_hash,
)


def test_build_outbound_message_sets_headers_and_bodies() -> None:
    message = build_outbound_message(
        from_address="noreply@example.com",
        html_body="<p>Hello there</p>",
        plain_body="Hello there\n",
        subject="domesti-bot test subject",
        to_addresses=["ops@example.com", " alerts@example.com ", ""],
    )
    assert message["Subject"] == "domesti-bot test subject"
    assert message["From"] == "noreply@example.com"
    assert message["To"] == "ops@example.com, alerts@example.com"
    plain_part = message.get_body(preferencelist=("plain",))
    assert plain_part is not None
    assert plain_part.get_content() == "Hello there\n"
    html_part = message.get_body(preferencelist=("html",))
    assert html_part is not None
    assert "<p>Hello there</p>" in html_part.get_content()


def test_build_outbound_message_rejects_empty_recipients() -> None:
    with pytest.raises(ValueError, match="at least one recipient"):
        build_outbound_message(
            from_address="noreply@example.com",
            html_body="<p>body</p>",
            plain_body="body",
            subject="subject",
            to_addresses=["", "   "],
        )


def test_provenance_footer_formats_subsystem_and_trigger() -> None:
    assert (
        provenance_footer(subsystem="Vacation mode", trigger="automatic")
        == "Sent by: domesti-bot · Vacation mode (automatic)"
    )


def test_provenance_footer_rejects_blank_parts() -> None:
    with pytest.raises(ValueError, match="subsystem"):
        provenance_footer(subsystem=" ", trigger="automatic")
    with pytest.raises(ValueError, match="trigger"):
        provenance_footer(subsystem="Vacation mode", trigger="")


def test_rule_fire_provenance_footer_includes_rule_id() -> None:
    assert rule_fire_provenance_footer("away-shutdown") == ("Sent by: domesti-bot · Rule away-shutdown (automation)")


def test_with_instance_hash_joins_origin_and_hash_path() -> None:
    assert (
        with_instance_hash("https://home.example.com/", "#/automations/vacation")
        == "https://home.example.com/#/automations/vacation"
    )


def test_automations_tab_urls_use_public_base(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DOMESTI_PUBLIC_BASE_URL", "https://domesti.example.com")
    cache = tmp_path / "cache.sqlite"
    assert automations_vacation_url(cache) == "https://domesti.example.com/#/automations/vacation"
    assert automations_mail_url(cache) == "https://domesti.example.com/#/automations/mail"


def test_automations_tab_urls_none_without_public_base(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("DOMESTI_PUBLIC_BASE_URL", raising=False)
    assert automations_vacation_url(tmp_path / "cache.sqlite") is None
    assert automations_mail_url(tmp_path / "cache.sqlite") is None
