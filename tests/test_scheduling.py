"""Scheduling safety: overlapping runs, implausible runs, re-runs."""
import json
import os
import time
from pathlib import Path

import pytest

from bellhaven_sync import pipeline
from bellhaven_sync.pipeline import RunInProgress, SuspiciousRun, _check_plausible, run_lock
from bellhaven_sync.matching import build_proposals

from .fake_crm import FakeCRM

FX = Path(__file__).parent / "fixtures"
SNAP = json.loads((FX / "crm_snapshot.json").read_text(encoding="utf-8"))
SITES = json.loads((FX / "site_locations.json").read_text(encoding="utf-8"))


def test_second_run_is_blocked_while_first_holds_the_lock(tmp_path):
    lock = tmp_path / "pipeline.lock"
    with run_lock(lock):
        with pytest.raises(RunInProgress):
            with run_lock(lock):
                pass
    assert not lock.exists()           # released afterwards


def test_lock_from_a_crashed_run_is_taken_over(tmp_path):
    lock = tmp_path / "pipeline.lock"
    lock.write_text("pid 1 at long ago")
    old = time.time() - 7200
    os.utime(lock, (old, old))
    with run_lock(lock, stale_after=3600):
        pass


def test_guard_stops_mass_removal_from_a_broken_scrape():
    accounts = SNAP["accounts"]
    half_site = SITES[: len(SITES) // 3]      # scraper "lost" two thirds of the pages
    props, _ = build_proposals(half_site, accounts, SNAP["contacts"], today="2026-11-11")
    with pytest.raises(SuspiciousRun):
        _check_plausible(props, accounts)


def test_guard_allows_a_normal_run():
    props, _ = build_proposals(SITES, SNAP["accounts"], SNAP["contacts"], today="2026-11-11")
    _check_plausible(props, SNAP["accounts"])   # 3 stale out of ~29: fine


def test_scheduled_run_twice_queues_nothing_new(tmp_path, monkeypatch):
    """Simulates two daily runs against an unchanged CRM and website."""
    from bellhaven_sync import config
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "review.db")
    monkeypatch.setattr(config, "ISSUE_EXPORT", tmp_path / "issue_log.csv")
    crm = FakeCRM(SNAP["accounts"], SNAP["contacts"])
    monkeypatch.setattr(pipeline, "collect",
                        lambda cached=False: (SITES, {"locations": len(SITES), "site_claims": [34, 35]},
                                              crm.list_accounts(), crm.list_contacts()))
    first = pipeline.run()
    second = pipeline.run()
    assert first["queue"]["new"] == 29
    assert second["queue"]["new"] == 0 and second["queue"]["superseded"] == 0
    assert crm.calls == []                        # the scheduled job never writes to the CRM
