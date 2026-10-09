"""Validation messages say what was expected, never the submitted value (the 422 handler returns ``msg``)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.api.schemas import LocalTimeWindowCondition, LocationUpdateWebhookIn

# Built rather than written as literals so secret scanners do not flag them.
_SENTINEL = "-".join(["SENTINEL", "echo", "probe", "4242"])


_PAYLOAD = {
    "user_id": "henrique",
    "lat": 41.194085,
    "lon": -73.888365,
    "timestamp": "2026-06-09T23:14:58+00:00",
    "source": "my-tracks",
}


def _messages(exc: ValidationError) -> list[str]:
    return [str(error["msg"]) for error in exc.errors()]


def test_a_bad_connection_code_is_rejected_without_echoing_it() -> None:
    with pytest.raises(ValidationError) as excinfo:
        LocationUpdateWebhookIn.model_validate({**_PAYLOAD, "connection_type": _SENTINEL})
    messages = _messages(excinfo.value)
    assert any("conn code w, m, or o" in m for m in messages)
    assert all(_SENTINEL not in m for m in messages)


@pytest.mark.parametrize(
    "start",
    [_SENTINEL, f"{_SENTINEL}:30", "25:00", "12:99", "ab:cd"],
)
def test_a_bad_time_is_rejected_without_echoing_it(start: str) -> None:
    with pytest.raises(ValidationError) as excinfo:
        LocalTimeWindowCondition(type="local_time_window", start_hhmm=start, end_hhmm="08:00")
    messages = _messages(excinfo.value)
    assert messages and all("HH:MM" in m for m in messages)
    assert all(start not in m for m in messages)


def test_an_empty_window_is_rejected_without_echoing_the_times() -> None:
    with pytest.raises(ValidationError) as excinfo:
        LocalTimeWindowCondition(type="local_time_window", start_hhmm="07:15", end_hhmm="07:15")
    messages = _messages(excinfo.value)
    assert any("start_hhmm != end_hhmm" in m for m in messages)
    assert all("07:15" not in m for m in messages)


def test_valid_values_still_normalize() -> None:
    window = LocalTimeWindowCondition(type="local_time_window", start_hhmm=" 7:05 ", end_hhmm="23:59")
    assert (window.start_hhmm, window.end_hhmm) == ("07:05", "23:59")
    assert LocationUpdateWebhookIn.model_validate({**_PAYLOAD, "connection_type": "w"}).connection_type == "w"


@pytest.mark.parametrize(
    "expression",
    [_SENTINEL, f"{_SENTINEL} * * * *", "61 * * * *", f"* * * * {_SENTINEL}", "* * * *"],
)
def test_a_bad_cron_expression_is_rejected_without_echoing_it_or_croniters_message(expression: str) -> None:
    from app.cron_schedule import validate_schedule_cron_expression

    with pytest.raises(ValueError) as excinfo:
        validate_schedule_cron_expression(expression)
    message = str(excinfo.value)
    assert message.startswith("Expected")
    assert "cron expression" in message
    assert _SENTINEL not in message
    assert expression not in message


def test_a_valid_cron_expression_is_still_accepted() -> None:
    from app.cron_schedule import validate_schedule_cron_expression

    validate_schedule_cron_expression("*/15 * * * *")
