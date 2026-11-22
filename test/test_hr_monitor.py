"""The heart-rate monitor rule, pinned by the canonical export's fixture."""

import json
from pathlib import Path

import pytest

from garmin_mcp.hr_monitor import fnv1a32, hr_monitor

CASES = json.loads((Path(__file__).parent / "fixtures" / "hr-monitor-v1-fixture.json").read_text())["cases"]


def test_fnv1a32_vector():
    assert fnv1a32("0a141e28323c") == "36a366b0"


@pytest.mark.parametrize("case", CASES, ids=[c["name"] for c in CASES])
def test_fixture_case(case):
    assert hr_monitor(case["rows"], case["sensors"]) == case["expected"]
