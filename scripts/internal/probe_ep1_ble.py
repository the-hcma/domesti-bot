"""Probe an EP1's ESPHome native API for BLE advertisements without Home Assistant.

Run ``uv run scripts/internal/probe-ep1-ble --duration 30`` from the repository
root. Addresses are redacted by default; pass ``--show-address`` only when
diagnosing a trusted device.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import math
import sys
from collections.abc import Iterable

from aioesphomeapi.client import APIClient


def main() -> int:
    """Connect, list entities, and print redacted raw BLE advertisement samples."""
    args = _parse_args()
    return asyncio.run(_probe(args))


_DEFAULT_DURATION_S = 20.0
_DEFAULT_HOST = "192.168.86.214"
_DEFAULT_PORT = 6053
_DEFAULT_SAMPLE_LIMIT = 5


def _address_display(address: object, *, show_address: bool) -> str:
    if isinstance(address, int):
        raw = address.to_bytes(6, "big", signed=False).hex(":") if 0 <= address < 1 << 48 else str(address)
    else:
        raw = str(address)
    if show_address:
        return raw
    return f"redacted:{hashlib.sha256(raw.encode()).hexdigest()[:12]}"


def _advertisements_from_response(response: object) -> Iterable[object]:
    advertisements = getattr(response, "advertisements", None)
    if isinstance(advertisements, Iterable) and not isinstance(advertisements, (bytes, str)):
        return advertisements
    if hasattr(response, "address"):
        return (response,)
    return ()


def _ble_entity_matches(entities: Iterable[object]) -> list[str]:
    matches: list[str] = []
    for entity in entities:
        object_id = getattr(entity, "object_id", "")
        name = getattr(entity, "name", "")
        text = f"{object_id} {name}".lower()
        if "ble_presence" in text or "ble_rssi" in text or "phone" in text:
            matches.append(f"{type(entity).__name__}: object_id={object_id!r}, name={name!r}")
    return matches


async def _disconnect_client(client: APIClient) -> None:
    try:
        await client.disconnect(force=True)
    except Exception:
        pass


def _on_advertisements(
    response: object,
    *,
    samples: list[str],
    show_address: bool,
    sample_limit: int,
) -> None:
    for advertisement in _advertisements_from_response(response):
        if len(samples) >= sample_limit:
            return
        address = _address_display(
            getattr(advertisement, "address", "unknown"),
            show_address=show_address,
        )
        address_type = getattr(advertisement, "address_type", "unknown")
        data = getattr(advertisement, "data", b"")
        data_length = len(data) if isinstance(data, bytes | bytearray) else "unknown"
        rssi = getattr(advertisement, "rssi", "unknown")
        sample = (
            f"address={address}, rssi={rssi}, address_type={address_type}, "
            f"data_hex_length={data_length * 2 if isinstance(data_length, int) else data_length}"
        )
        samples.append(sample)
        print(f"  {sample}")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--duration",
        default=_DEFAULT_DURATION_S,
        type=float,
        help=f"Seconds to subscribe for (default: {_DEFAULT_DURATION_S:g})",
    )
    parser.add_argument(
        "--host",
        default=_DEFAULT_HOST,
        help=f"EP1 host (default: {_DEFAULT_HOST})",
    )
    parser.add_argument(
        "--port",
        default=_DEFAULT_PORT,
        type=int,
        help=f"ESPHome native API port (default: {_DEFAULT_PORT})",
    )
    parser.add_argument(
        "--sample-limit",
        default=_DEFAULT_SAMPLE_LIMIT,
        type=int,
        help=f"Maximum advertisement records to print (default: {_DEFAULT_SAMPLE_LIMIT})",
    )
    parser.add_argument(
        "--show-address",
        action="store_true",
        help="Print BLE addresses instead of stable redacted hashes",
    )
    args = parser.parse_args()
    if not math.isfinite(args.duration) or args.duration <= 0:
        parser.error(f"Expected a finite positive duration, got {args.duration}")
    if args.sample_limit <= 0:
        parser.error(f"Expected a positive sample limit, got {args.sample_limit}")
    return args


async def _probe(args: argparse.Namespace) -> int:
    client = APIClient(
        args.host,
        args.port,
        password=None,
        noise_psk=None,
        client_info="domesti-bot-ep1-ble-probe",
    )
    samples: list[str] = []
    try:
        await client.connect(login=True)
        entities, _services = await client.list_entities_services()
        matches = _ble_entity_matches(entities)
        print(f"Connected to {args.host}:{args.port}; {len(entities)} entities.")
        if matches:
            print("BLE/presence-looking entities:")
            for match in matches:
                print(f"  {match}")
        else:
            print("No BLE/presence-looking entities found.")

        unsubscribe = client.subscribe_bluetooth_le_raw_advertisements(
            lambda response: _on_advertisements(
                response,
                samples=samples,
                show_address=args.show_address,
                sample_limit=args.sample_limit,
            )
        )
        print(f"Subscribed to raw BLE advertisements for {args.duration:g}s.")
        await asyncio.sleep(args.duration)
        unsubscribe()
    except Exception as exc:
        print(f"BLE probe failed: {exc!r}", file=sys.stderr)
        return 1
    finally:
        await _disconnect_client(client)

    print(f"Received {len(samples)} sampled advertisement records.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
