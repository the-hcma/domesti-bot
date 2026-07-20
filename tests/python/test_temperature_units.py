"""Hermetic tests for Celsius / Fahrenheit helpers."""

from __future__ import annotations

from app.temperature_units import (
    celsius_to_fahrenheit,
    fahrenheit_to_celsius,
    format_temperature_c_and_f,
)


def test_celsius_to_fahrenheit_freezing_and_body() -> None:
    assert celsius_to_fahrenheit(0.0) == 32.0
    assert celsius_to_fahrenheit(100.0) == 212.0
    assert abs(celsius_to_fahrenheit(21.25) - 70.25) < 1e-9


def test_fahrenheit_to_celsius_round_trip() -> None:
    celsius = 21.25
    assert abs(fahrenheit_to_celsius(celsius_to_fahrenheit(celsius)) - celsius) < 1e-9


def test_format_temperature_c_and_f_from_celsius_only() -> None:
    assert format_temperature_c_and_f(20.0) == "20.0 °C / 68.0 °F"


def test_format_temperature_c_and_f_from_fahrenheit_only() -> None:
    assert format_temperature_c_and_f(None, 68.0) == "20.0 °C / 68.0 °F"


def test_format_temperature_c_and_f_none_when_both_missing() -> None:
    assert format_temperature_c_and_f(None) is None
    assert format_temperature_c_and_f(None, None) is None
