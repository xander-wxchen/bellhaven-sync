"""Manual corrections, overrides, the issue log and data-quality checks."""
import copy
import json
from pathlib import Path

import pytest

from bellhaven_sync.matching import build_proposals
from bellhaven_sync.store import Store, approve
from bellhaven_sync.validate import check_all, check_location

from .fake_crm import FakeCRM

FX = Path(__file__).parent / "fixtures"
SNAP = json.loads((FX / "crm_snapshot.json").read_text(encoding="utf-8"))
SITES = json.loads((FX / "site_locations.json").read_text(encoding="utf-8"))


@pytest.fixture
def env(tmp_path, monkeypatch):
    from bellhaven_sync import config
    monkeypatch.setattr(config, "ISSUE_EXPORT", tmp_path / "issue_log.csv")
    crm = FakeCRM(SNAP["accounts"], SNAP["contacts"])
    store = Store(tmp_path / "q.db")
    return crm, store


def run(crm, store, sites=SITES):
    props, summary = build_proposals(sites, crm.list_accounts(), crm.list_contacts(),
                                     today="2026-11-10", overrides=store.overrides())
    store.sync(props, {})
    return props, summary


def find(store, group, kind):
    return next(r for r in store.list(["pending"]) if r["grp"] == group and r["kind"] == kind)


def test_reviewer_edit_is_written_and_sticks_on_rerun(env):
    crm, store = env
    run(crm, store)
    row = find(store, "Bellhaven of Chagrin Falls", "update")
    edits = {"0:name": {"include": True, "value": "Bellhaven of Chagrin Falls (River St)"}}
    res = approve(store, row["fingerprint"], crm, who="Avery", edits=edits)
    assert res["ok"] and res["corrections"] == 1
    acct = crm.get_account(row["payload"]["account_ids"][0])
    assert acct["name"] == "Bellhaven of Chagrin Falls (River St)"
    assert "Avery set Account name" in acct["note"]
    # next day: the website still says the original name, but the override wins
    props, _ = run(crm, store)
    assert not [p for p in props if p.group == "Bellhaven of Chagrin Falls"]
    issue = store.list_issues()[0]
    assert issue["category"] == "Reviewer correction" and "River St" in issue["detail"]


def test_override_stops_applying_when_website_changes(env):
    crm, store = env
    run(crm, store)
    row = find(store, "Bellhaven of Chagrin Falls", "update")
    approve(store, row["fingerprint"], crm, edits={"0:name": {"include": True, "value": "Custom Name"}})
    sites = copy.deepcopy(SITES)
    for s in sites:
        if s["name"] == "Bellhaven of Chagrin Falls":
            s["name"] = "Bellhaven Riverside of Chagrin Falls"   # operator renamed it
    props, _ = run(crm, store, sites)
    (p,) = [p for p in props if p.group == "Bellhaven Riverside of Chagrin Falls"]
    assert p.actions[0]["fields"]["name"] == "Bellhaven Riverside of Chagrin Falls"


def test_unticking_a_field_keeps_crm_value_and_is_remembered(env):
    crm, store = env
    run(crm, store)
    row = find(store, "Bellhaven of Zanesville", "reparent")     # re-parent + rename
    aid = row["payload"]["account_ids"][0]
    res = approve(store, row["fingerprint"], crm, edits={"0:name": {"include": False, "value": ""}})
    assert res["ok"]
    acct = crm.get_account(aid)
    assert acct["name"] == "Cedar Trail of Zanesville"           # name kept
    assert acct["parent_name"].startswith("Bellhaven")           # parent still moved
    assert "Renamed" not in acct["note"]                         # note line for the dropped field is gone
    props, _ = run(crm, store)
    assert not [p for p in props if p.group == "Bellhaven of Zanesville"]


def test_required_fields_cannot_be_dropped_on_create(env):
    crm, store = env
    run(crm, store)
    row = find(store, "Bellhaven of Batavia", "create")
    res = approve(store, row["fingerprint"], crm, edits={"0:name": {"include": False, "value": ""}})
    assert not res["ok"] and "required" in res["error"]
    assert store.get(row["fingerprint"])["status"] == "pending"   # nothing claimed, nothing written
    assert crm.calls == []


def test_unticking_everything_asks_for_reject(env):
    crm, store = env
    run(crm, store)
    row = find(store, "Bellhaven of Portsmouth", "update")
    res = approve(store, row["fingerprint"], crm, edits={"0:billing_zip": {"include": False, "value": ""}})
    assert not res["ok"] and "Reject" in res["error"]


def test_rejection_reason_files_an_issue(env):
    crm, store = env
    run(crm, store)
    row = find(store, "Bellhaven of Batavia", "create")
    store.reject(row["fingerprint"], "Avery", "Website data is wrong", "street number looks off")
    (issue,) = store.list_issues()
    assert issue["category"] == "Scraper: wrong or missing value"
    assert "street number looks off" in issue["detail"]
    assert (store.list_issues() and store.export_issues().read_text(encoding="utf-8-sig").count("\n") == 2)


def test_business_rejection_files_no_issue(env):
    crm, store = env
    run(crm, store)
    row = find(store, "Bellhaven of Batavia", "create")
    store.reject(row["fingerprint"], "me", "Business decision: don't change this")
    assert store.list_issues() == []


def test_data_quality_flags():
    bad = {"slug": "x", "name": "X", "street": "Main St", "city": "Akron", "state": "OH", "zip": "15213",
           "care_offerings": ["Hospice"], "phone": "555-1234"}
    codes = {f["code"] for f in check_location(bad)}
    assert codes == {"zip_state_mismatch", "street_no_number", "unmapped_care_label", "phone_format"}
    assert {f["code"] for f in check_location(bad | {"state": "TX"})} >= {"state_outside_footprint"}
    two = [bad | {"slug": "a", "street": "1 Main St"}, bad | {"slug": "b", "street": "1 Main Street"}]
    assert all(any(f["code"] == "shared_address" for f in v) for v in check_all(two).values())


def test_data_quality_flag_lowers_confidence_and_warns(env):
    crm, store = env
    sites = copy.deepcopy(SITES)
    for s in sites:
        if s["name"] == "Bellhaven of Batavia":
            s["zip"] = "15103"          # a PA ZIP on an Ohio listing
    props, summary = build_proposals(sites, crm.list_accounts(), crm.list_contacts(), today="2026-11-10")
    (p,) = [p for p in props if p.group == "Bellhaven of Batavia"]
    assert p.confidence == "medium"
    assert any("does not belong to OH" in w for w in p.warnings)
    assert "bellhaven-of-batavia" in summary["data_quality"]
