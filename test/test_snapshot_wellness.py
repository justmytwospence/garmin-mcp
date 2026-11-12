"""The snapshot is published soon after last night's sleep and HRV days land, at most once a day."""

from __future__ import annotations

import time

import pytest

from garmin_mcp import snapshot as snap
from garmin_mcp.snapshot import SnapshotPublisher


@pytest.fixture
def clock(monkeypatch):
    now = [1_000_000.0]
    monkeypatch.setattr(time, "time", lambda: now[0])
    return now


def make(db):
    pub = SnapshotPublisher(db, wellness_delay=1800.0)
    pub.last_published = time.time() - 2 * 3600  # an earlier publish, more than MIN_GAP ago
    pub.dirty = False
    return pub


def test_sleep_day_arms_a_publish_after_the_delay(db, clock):
    pub = make(db)
    pub.mark_dirty("day:sleep")
    assert pub.dirty and not pub.due()
    clock[0] += 1799
    assert not pub.due()
    clock[0] += 2
    assert pub.due()


def test_publish_resets_and_a_second_arm_waits_a_day(db, clock):
    pub = make(db)
    pub.mark_dirty("day:hrv")
    clock[0] += 1900
    pub.publish()
    assert pub.wellness_since is None and pub.last_wellness_publish == clock[0]
    clock[0] += 3600
    pub.mark_dirty("day:sleep")
    assert pub.wellness_since is None  # within 20 h of the last wellness publish
    clock[0] += snap.WELLNESS_GAP
    pub.mark_dirty("day:sleep")
    assert pub.wellness_since is not None


def test_other_kinds_do_not_arm_it(db, clock):
    pub = make(db)
    pub.mark_dirty("wellness_fit")
    clock[0] += 3600
    assert pub.wellness_since is None and not pub.due()
