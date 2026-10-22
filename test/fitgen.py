"""A minimal FIT encoder for test fixtures (no real data, no serial numbers).

Writes a valid FIT file with the few message types the decoder tests need. Field
definitions follow the FIT profile (global message numbers and field numbers).
"""

from __future__ import annotations

import struct
from datetime import UTC, datetime

FIT_EPOCH = datetime(1989, 12, 31, tzinfo=UTC)

# base types: (type id byte, struct format, size)
ENUM = (0x00, "B", 1)
UINT8 = (0x02, "B", 1)
SINT16 = (0x83, "h", 2)
UINT16 = (0x84, "H", 2)
SINT32 = (0x85, "i", 4)
UINT32 = (0x86, "I", 4)
UINT32Z = (0x8C, "I", 4)


def fit_ts(dt: datetime) -> int:
    return int((dt - FIT_EPOCH).total_seconds())


def crc16(data: bytes) -> int:
    table = [0x0000, 0xCC01, 0xD801, 0x1400, 0xF001, 0x3C00, 0x2800, 0xE401, 0xA001, 0x6C00, 0x7800, 0xB401, 0x5000, 0x9C01, 0x8801, 0x4400]
    crc = 0
    for b in data:
        tmp = table[crc & 0xF]
        crc = (crc >> 4) & 0x0FFF
        crc = crc ^ tmp ^ table[b & 0xF]
        tmp = table[crc & 0xF]
        crc = (crc >> 4) & 0x0FFF
        crc = crc ^ tmp ^ table[(b >> 4) & 0xF]
    return crc


class FitWriter:
    def __init__(self) -> None:
        self.body = bytearray()
        self.local = 0
        self.defs: dict[tuple, int] = {}

    def message(self, global_num: int, fields: list[tuple[int, tuple, int | list[int]]]) -> None:
        """fields: [(field_num, base_type, value)]; a list value is an array field."""
        sig = (global_num, tuple((f, t[0], (len(v) if isinstance(v, list) else 1)) for f, t, v in fields))
        if sig not in self.defs:
            local = self.local % 16
            self.local += 1
            self.defs[sig] = local
            d = bytearray([0x40 | local, 0, 0, *struct.pack("<H", global_num), len(fields)])
            for f, t, v in fields:
                n = len(v) if isinstance(v, list) else 1
                d += bytes([f, t[2] * n, t[0]])
            self.body += d
        local = self.defs[sig]
        rec = bytearray([local])
        for f, t, v in fields:
            vals = v if isinstance(v, list) else [v]
            for x in vals:
                rec += struct.pack("<" + t[1], x)
        self.body += rec

    def tobytes(self) -> bytes:
        header = bytearray(struct.pack("<BBHI4s", 14, 0x20, 2195, len(self.body), b".FIT"))
        header += struct.pack("<H", crc16(bytes(header)))
        out = bytes(header) + bytes(self.body)
        return out + struct.pack("<H", crc16(out))


def activity_fit(activity_start: datetime, seconds: int = 30) -> bytes:
    w = FitWriter()
    t0 = fit_ts(activity_start)
    w.message(0, [(0, ENUM, 4), (1, UINT16, 1), (2, UINT16, 3121), (3, UINT32Z, 12345), (4, UINT32, t0)])  # file_id: activity, garmin
    w.message(21, [(253, UINT32, t0), (0, ENUM, 0), (1, ENUM, 0), (3, UINT32, 0)])  # event timer start
    deg = 2**31 / 180.0
    for i in range(seconds):
        w.message(
            20,
            [
                (253, UINT32, t0 + i),
                (0, SINT32, int(45.0 * deg) + i * 10),
                (1, SINT32, int(-120.0 * deg) + i * 10),
                (5, UINT32, i * 300),  # distance cm
                (3, UINT8, 120 + (i % 5)),
                (4, UINT8, 85),
                (73, UINT32, 3000),  # enhanced_speed mm/s
                (78, UINT32, 500 * 1000),  # enhanced_altitude (m+500)*5 ... scaled: value/5 - 500
                (7, UINT16, 200 + i),  # power
                (250, UINT8, 7),  # unknown field -> extra
            ],
        )
    w.message(78, [(0, UINT16, [850, 860, 870, 0xFFFF, 0xFFFF])])  # hrv
    w.message(19, [(253, UINT32, t0 + seconds), (2, UINT32, t0), (254, UINT16, 0), (5, ENUM, 1), (7, UINT32, seconds * 1000), (9, UINT32, seconds * 300), (16, UINT8, 122)])
    w.message(18, [(253, UINT32, t0 + seconds), (2, UINT32, t0), (254, UINT16, 0), (5, ENUM, 1), (7, UINT32, seconds * 1000), (9, UINT32, seconds * 300), (16, UINT8, 122), (25, UINT16, 0), (26, UINT16, 1)])
    w.message(21, [(253, UINT32, t0 + seconds), (0, ENUM, 0), (1, ENUM, 4), (3, UINT32, 0)])  # stop_all
    w.message(34, [(253, UINT32, t0 + seconds), (0, UINT32, seconds * 1000), (1, UINT16, 1), (2, ENUM, 26), (3, ENUM, 1), (4, ENUM, 0)])  # activity
    w.message(300, [(0, UINT8, 1), (253, UINT32, t0)])  # unknown message -> fit_message
    return w.tobytes()


def wellness_fit(day_start: datetime, minutes: int = 10) -> bytes:
    w = FitWriter()
    t0 = fit_ts(day_start)
    w.message(0, [(0, ENUM, 32), (1, UINT16, 1), (2, UINT16, 3121), (3, UINT32Z, 12345), (4, UINT32, t0)])  # monitoring_b
    w.message(103, [(253, UINT32, t0), (0, UINT32, t0)])  # monitoring_info
    for i in range(minutes):
        w.message(55, [(253, UINT32, t0 + i * 60), (3, UINT32, 200 * i), (27, UINT8, 60 + i), (5, ENUM, 6)])  # monitoring: cycles (steps when walking), hr, walking
        w.message(227, [(0, SINT16, 20 + i), (1, UINT32, t0 + i * 60)])  # stress_level
        w.message(297, [(253, UINT32, t0 + i * 60), (0, UINT16, 1500)])  # respiration_rate (breaths/min *100)
    return w.tobytes()
