"""The heart-rate monitor of an activity: a stable key for its model or sensor, and a name when Garmin Connect knows it.

The rule is the canonical export's schema SPEC (Heart-rate monitor); `test/fixtures/hr-monitor-v1-fixture.json` pins it.
Rows are the activity's FIT device_info messages, in file order, as dicts with `device_index`, `manufacturer`, `product`,
`garmin_product`, `source_type`, `device_type`, `antplus_device_type`, `ble_device_type` and `ble_address` (6 ints or None).
"""

from __future__ import annotations

from typing import Any


def fnv1a32(s: str) -> str:
    """FNV-1a 32-bit over the UTF-8 bytes of `s`, as 8 lowercase hex digits."""
    h = 0x811C9DC5
    for b in s.encode("utf-8"):
        h = ((h ^ b) * 0x01000193) & 0xFFFFFFFF
    return f"{h:08x}"


def _is_candidate(r: dict[str, Any]) -> bool:
    return (
        r.get("antplus_device_type") == "heart_rate"
        or r.get("ble_device_type") == "heart_rate"
        or (r.get("source_type") == "bluetooth_low_energy" and r.get("device_type") in ("1", "120"))
        or (r.get("garmin_product") or "").lower().startswith("hrm")
    )


def _index_rank(index: str | None) -> tuple[int, int]:
    """Smaller numeric index first, a null (or non-numeric) one last."""
    try:
        return (0, int(index))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return (1, 0)


def _pick(rows: list[dict[str, Any]]) -> tuple[dict[str, Any], list[int] | None] | None:
    """The monitor's `{key, name: None}` and its Bluetooth address (if any), or None when there is no monitor."""
    groups: list[list[dict[str, Any]]] = []
    by_index: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        if not _is_candidate(r):
            continue
        index = r.get("device_index")
        if index is None:
            groups.append([r])
        elif index in by_index:
            by_index[index].append(r)
        else:
            by_index[index] = [r]
            groups.append(by_index[index])
    if not groups:
        return None

    def rank(item: tuple[int, list[dict[str, Any]]]) -> tuple:
        order, g = item
        ant = any(r.get("source_type") in ("antplus", "ant") for r in g)
        return (-len(g), 0 if ant else 1, _index_rank(g[0].get("device_index")), order)

    group = min(enumerate(groups), key=rank)[1]
    if any(r.get("source_type") == "bluetooth_low_energy" for r in group):
        address = next((r["ble_address"] for r in group if r.get("ble_address") is not None), None)
        if address is None:
            return {"key": "ble:unknown", "name": None}, None
        return {"key": "ble:" + fnv1a32("".join(f"{b:02x}" for b in address)), "name": None}, address
    manufacturer = next((r["manufacturer"] for r in group if r.get("manufacturer") is not None), None)
    product = next((r["product"] for r in group if r.get("garmin_product") is not None and not r["garmin_product"].isdigit()), None)
    if product is None:
        product = next((r["product"] for r in group if r.get("product") is not None), None)
    return {"key": f"ant:{manufacturer if manufacturer is not None else 'unknown'}:{product if product is not None else 'unknown'}", "name": None}, None


def pick(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The monitor's `{key, name: None}` from the device_info rows, or None when there is none."""
    found = _pick(rows)
    return None if found is None else found[0]


def ble_name(address: list[int], sensors: list[dict[str, Any]]) -> str | None:
    """The paired sensor's name: Garmin Connect's serial is the address's first five bytes and a counter near the sixth."""
    prefix = "".join(str(b) for b in address[:5])
    last = address[5]
    best: tuple[int, int, str | None] | None = None
    for n in range(last - 2, last + 3):
        if n < 0:
            continue
        serial = prefix + str(n)
        for s in sensors:
            if s.get("sensor_type") != "HEART_RATE" or str(s.get("serial_number")) != serial:
                continue
            cand = (abs(n - last), n, s.get("device_name"))
            if best is None or cand[:2] < best[:2]:
                best = cand
    return None if best is None else best[2]


def hr_monitor(rows: list[dict[str, Any]], sensors: list[dict[str, Any]]) -> dict[str, Any] | None:
    found = _pick(rows)
    if found is None:
        return None
    monitor, address = found
    if address is not None:
        monitor = {**monitor, "name": ble_name(address, sensors)}
    return monitor
