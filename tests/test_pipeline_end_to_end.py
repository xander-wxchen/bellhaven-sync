"""End-to-end on a snapshot of the sandbox CRM, using an in-memory fake API.

These are regression tests for the matching rules on realistic data, plus the
safety properties the exercise asks for: approve-only writes, the billing SOP,
and re-runs that don't re-propose decided items.
"""
import json
from pathlib import Path

import pytest

from bellhaven_sync.apply import StaleProposal, apply_proposal
from bellhaven_sync.matching import build_proposals
from bellhaven_sync.store import Store, approve

from .fake_crm import FakeCRM

FX = Path(__file__).parent / "fixtures"
SNAP = json.loads((FX / "crm_snapshot.json").read_text(encoding="utf-8"))
SITES = json.loads((FX / "site_locations.json").read_text(encoding="utf-8"))
TODAY = "2026-11-10"


def build(crm):
    return build_proposals(SITES, crm.list_accounts(), crm.list_contacts(), today=TODAY)


def by(proposals, kind, group):
    return [p for p in proposals if p.kind == kind and p.group == group]


@pytest.fixture
def crm():
    return FakeCRM(SNAP["accounts"], SNAP["contacts"])


def test_snapshot_classification(crm):
    _, summary = build(crm)
    assert summary.pop("data_quality") == {}          # the live site has no suspicious values
    assert {k: len(v) for k, v in summary.items()} == {
        "create": 4, "update": 9, "reparent": 4, "chow": 2, "duplicate": 7, "stale": 3, "no_change": 12}


def test_same_name_in_other_state_is_not_matched(crm):
    props, _ = build(crm)
    (p,) = by(props, "create", "Amberly Manor")
    assert any("Colorado Springs" in e and "rejected" in e for e in p.evidence)


def test_chow_only_when_revenue_and_ar(crm):
    props, _ = build(crm)
    assert by(props, "chow", "Bellhaven of Tiffin") and by(props, "chow", "Bellhaven of Marietta")
    assert by(props, "reparent", "Bellhaven Crossings of Lima")       # revenue, AR 0
    assert by(props, "reparent", "Bellhaven Meadows of Findlay")      # no parent today


def test_chow_leaves_old_account_untouched_except_link(crm):
    props, _ = build(crm)
    (p,) = by(props, "chow", "Bellhaven of Tiffin")
    old_id = p.account_ids[0]
    before = crm.get_account(old_id)
    apply_proposal(p.to_dict(), crm)
    after = crm.get_account(old_id)
    changed = {k for k in after if after[k] != before[k]}
    assert changed == {"chow_current_account"}
    new = crm.get_account(after["chow_current_account"])
    assert new["parent_name"] == "Bellhaven Senior Living (Parent Account)"
    assert new["name"] == "Bellhaven of Tiffin"


def test_stale_keeps_parent_and_flags_open_ar(crm):
    props, _ = build(crm)
    (p,) = by(props, "stale", "Bellhaven of Sandusky")
    assert set(p.actions[0]["fields"]) == {"status"}
    assert p.actions[0]["fields"]["status"] == "Needs Review"
    assert any("Open AR" in w for w in p.warnings)


def test_duplicate_survivor_prefers_contacts_and_phone(crm):
    props, _ = build(crm)
    (p,) = by(props, "duplicate", "Bellhaven of Owosso")
    loser = crm.get_account(p.account_ids[0])
    keeper = crm.get_account(p.account_ids[1])
    assert keeper["phone"] == "(734) 503-1363"          # matches the website
    assert loser["account_id"] != keeper["account_id"]
    assert any(a["op"] == "update_contact" for a in p.actions)  # contact moves to the keeper


def test_approve_everything_then_rerun_proposes_nothing(crm, tmp_path):
    store = Store(tmp_path / "q.db")
    props, _ = build(crm)
    assert store.sync(props, {})["new"] == len(props)
    for row in store.list(["pending"]):
        res = approve(store, row["fingerprint"], crm)
        assert res["ok"], (row["title"], res)
    # second run against the corrected CRM: nothing left to do
    props2, summary2 = build(crm)
    assert props2 == []
    assert len(summary2["no_change"]) == 35
    assert store.sync(props2, {})["new"] == 0


def test_rejected_items_are_not_reproposed(crm, tmp_path):
    store = Store(tmp_path / "q.db")
    props, _ = build(crm)
    store.sync(props, {})
    fp = props[0].fingerprint
    assert store.reject(fp, "me", "not sure")
    counts = store.sync(build(crm)[0], {})       # next day's run, CRM unchanged
    assert counts["new"] == 0
    assert store.get(fp)["status"] == "rejected"


def test_nothing_is_written_without_approval(crm, tmp_path):
    store = Store(tmp_path / "q.db")
    store.sync(build(crm)[0], {})
    assert crm.calls == []


def test_stale_proposal_is_refused(crm):
    props, _ = build(crm)
    (p,) = by(props, "update", "Bellhaven of Chagrin Falls")
    crm.accounts[p.account_ids[0]]["name"] = "Edited by a sales rep"
    with pytest.raises(StaleProposal):
        apply_proposal(p.to_dict(), crm)
    assert not any(c[0] == "update" for c in crm.calls)


def test_note_is_appended_not_overwritten(crm):
    props, _ = build(crm)
    (p,) = by(props, "update", "Bellhaven of Chagrin Falls")
    aid = p.account_ids[0]
    crm.accounts[aid]["note"] = "Rep: call back in Jan"
    apply_proposal(p.to_dict(), crm)
    note = crm.get_account(aid)["note"]
    assert note.startswith("Rep: call back in Jan\n") and "bellhaven-sync" in note


def test_chow_retry_after_partial_failure_creates_one_account(crm, tmp_path):
    store = Store(tmp_path / "q.db")
    props, _ = build(crm)
    store.sync(props, {})
    (p,) = by(props, "chow", "Bellhaven of Marietta")
    crm.fail_next = "update"                      # create succeeds, link fails
    assert not approve(store, p.fingerprint, crm)["ok"]
    assert store.get(p.fingerprint)["status"] == "failed"
    assert approve(store, p.fingerprint, crm)["ok"]   # retry
    created = [c for c in crm.calls if c[0] == "create"]
    assert len(created) == 1
    assert sum(a["name"] == "Bellhaven of Marietta" for a in crm.accounts.values()) == 2  # old + new


def test_double_click_applies_once(crm, tmp_path):
    store = Store(tmp_path / "q.db")
    props, _ = build(crm)
    store.sync(props, {})
    fp = props[0].fingerprint
    assert store.claim(fp, "a") is True
    assert store.claim(fp, "b") is False
