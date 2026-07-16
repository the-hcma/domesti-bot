"""Hermetic tests that ``GotailwindDevice.close`` / ``.open`` are idempotent.

``gotailwind`` raises :class:`TailwindDoorAlreadyInStateError` when the
controller is asked to send a door to a position it already reports.
That used to crash the global ``Turn off / pause / close everything`` flow whenever any
Tailwind door happened to already be closed. The wrapper now swallows
that exception and reports success so the bulk operation cannot be
aborted by a no-op call.
"""

from __future__ import annotations

import pytest
from gotailwind.const import TailwindDoorState
from gotailwind.exceptions import TailwindDoorAlreadyInStateError

from app.gotailwind_device_manager import GotailwindDevice


class _FakeTailwind:
    """Minimal stand-in for ``gotailwind.Tailwind`` used by GotailwindDevice."""

    def __init__(
        self,
        *,
        raise_already_in_state: bool = False,
        response_state: TailwindDoorState = TailwindDoorState.CLOSED,
    ) -> None:
        self._raise = raise_already_in_state
        self._response_state = response_state
        self.calls: list[tuple[int | str, str]] = []

    async def operate(
        self,
        *,
        door: int | str,
        operation: object,
    ) -> object:
        self.calls.append((door, str(operation)))
        if self._raise:
            raise TailwindDoorAlreadyInStateError(f"Door {door} is already in the requested state")

        class _Door:
            state = self._response_state

        return _Door()


def _make_device(tailwind: _FakeTailwind, *, reported: TailwindDoorState) -> GotailwindDevice:
    return GotailwindDevice(
        identifier="garage:0",
        tailwind=tailwind,  # type: ignore[arg-type]
        door=0,
        reported_state=reported,
        door_index=0,
        display_name="Garage",
    )


@pytest.mark.asyncio
async def test_close_swallows_already_in_state_and_pins_cache_to_closed() -> None:
    tw = _FakeTailwind(raise_already_in_state=True)
    gd = _make_device(tw, reported=TailwindDoorState.OPEN)

    await gd.close()

    assert gd.is_closed is True
    assert gd.is_open is False
    assert tw.calls, "Wrapper should still issue the operate() call"


@pytest.mark.asyncio
async def test_open_swallows_already_in_state_and_pins_cache_to_open() -> None:
    tw = _FakeTailwind(raise_already_in_state=True)
    gd = _make_device(tw, reported=TailwindDoorState.CLOSED)

    await gd.open()

    assert gd.is_open is True
    assert gd.is_closed is False
    assert tw.calls, "Wrapper should still issue the operate() call"


@pytest.mark.asyncio
async def test_close_updates_reported_state_from_response_on_success() -> None:
    tw = _FakeTailwind(
        raise_already_in_state=False,
        response_state=TailwindDoorState.CLOSED,
    )
    gd = _make_device(tw, reported=TailwindDoorState.OPEN)

    await gd.close()

    assert gd.is_closed is True
    assert gd.is_open is False


@pytest.mark.asyncio
async def test_open_updates_reported_state_from_response_on_success() -> None:
    tw = _FakeTailwind(
        raise_already_in_state=False,
        response_state=TailwindDoorState.OPEN,
    )
    gd = _make_device(tw, reported=TailwindDoorState.CLOSED)

    await gd.open()

    assert gd.is_open is True
    assert gd.is_closed is False
