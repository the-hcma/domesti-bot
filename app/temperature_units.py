"""Celsius / Fahrenheit helpers for occupancy climate readings."""

from __future__ import annotations


def celsius_to_fahrenheit(celsius: float) -> float:
    """Convert degrees Celsius to Fahrenheit (``F = C × 9/5 + 32``)."""
    return celsius * 9.0 / 5.0 + 32.0


def fahrenheit_to_celsius(fahrenheit: float) -> float:
    """Convert degrees Fahrenheit to Celsius (``C = (F − 32) × 5/9``)."""
    return (fahrenheit - 32.0) * 5.0 / 9.0


def format_temperature_c_and_f(
    temperature_c: float | None,
    temperature_f: float | None = None,
    *,
    decimals: int = 1,
) -> str | None:
    """Human label with both units when either reading is known.

    Derives the missing unit from the other. Returns ``None`` when both are
    ``None``. Example: ``\"21.3 °C / 70.3 °F\"``.
    """
    celsius = temperature_c
    fahrenheit = temperature_f
    if celsius is None and fahrenheit is not None:
        celsius = fahrenheit_to_celsius(fahrenheit)
    if fahrenheit is None and celsius is not None:
        fahrenheit = celsius_to_fahrenheit(celsius)
    if celsius is None or fahrenheit is None:
        return None
    c_label = f"{celsius:.{decimals}f} °C"
    f_label = f"{fahrenheit:.{decimals}f} °F"
    return f"{c_label} / {f_label}"
