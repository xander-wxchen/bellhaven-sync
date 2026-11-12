"""Roll back an applied proposal from its pre-write snapshot."""
import json
from pathlib import Path

import pytest

from bellhaven_sync.matching import build_proposals
from bellhaven_sync.store import Store, approve, revert

from .fake_crm import FakeCRM

FX = Path(__file__).parent / "fixtures"
SNAP = json.loads((FX / "crm_snapshot.json").read_text(encoding="utf-8"))
SITES = json.loads((FX / "site_locations.json").read_text(encoding="utf-8"))


@pytest.fixture
def env(tmp_path, monkeypatch):
    from bellhaven_sync import config
    monkeypatch.setattr(config, "ISSUE_EXPORT", tmp_path / "issue_log.csv")
    crm, store = FakeCRM(SNAP["accounts"], SNAP["contacts"]), Store(tmp_path / "q.db")
    store.sync(build(crm, store), {})
    return crm, store


def build(crm, store):
    return build_proposals(SITES, crm.list_accounts(), crm.list_contacts(), today="2026-11-11",
                           overrides=store.overrides())[0]


def pending(store, group, kind):
    return next(r for r in store.list(["pending"]) if r["grp"] == group and r["kind"] == kind)


def test_rollback_restores_exact_previous_record(env):
    crm, store = env
    row = pending(store, "Bellhaven of Chagrin Falls", "update")
    aid = row["payload"]["account_ids"][0]
    crm.accounts[aid]["note"] = "Rep: call back in Jan"
    original = crm.get_account(aid)
    assert approve(store, row["fingerprint"], crm)["ok"]
    assert crm.get_account(aid) != original
    assert revert(store, row["fingerprint"], crm, who="Avery")["ok"]
    assert crm.get_account(aid) == original                       # name and note exactly as before
    back = store.get(row["fingerprint"])
    assert back["status"] == "pending" and back["result"]["rolled_back_by"] == "Avery"
    # it can be decided again, and the pipeline still proposes it
    assert row["fingerprint"] in {p.fingerprint for p in build(crm, store)}


def test_rollback_moves_contacts_back_on_duplicates(env):
    crm, store = env
    row = pending(store, "Bellhaven of Owosso", "duplicate")
    before_accounts = {k: dict(v) for k, v in crm.accounts.items()}
    before_contacts = {k: dict(v) for k, v in crm.contacts.items()}
    assert approve(store, row["fingerprint"], crm)["ok"]
    assert revert(store, row["fingerprint"], crm)["ok"]
    assert crm.accounts == before_accounts and crm.contacts == before_contacts


def test_rollback_refused_if_someone_edited_after_approval(env):
    crm, store = env
    row = pending(store, "Bellhaven of Portsmouth", "update")
    aid = row["payload"]["account_ids"][0]
    assert approve(store, row["fingerprint"], crm)["ok"]
    crm.accounts[aid]["billing_zip"] = "45663"                    # a rep fixed it again by hand
    out = revert(store, row["fingerprint"], crm)
    assert not out["ok"] and "changed after approval" in out["error"]
    assert crm.get_account(aid)["billing_zip"] == "45663"         # their edit is untouched
    assert store.get(row["fingerprint"])["status"] == "applied"


def test_creates_cannot_be_rolled_back(env):
    crm, store = env
    row = pending(store, "Bellhaven of Batavia", "create")
    res = approve(store, row["fingerprint"], crm)
    assert res["ok"] and res["reversible"] is False
    out = revert(store, row["fingerprint"], crm)
    assert not out["ok"] and "can't be rolled back" in out["error"]
    assert store.get(row["fingerprint"])["status"] == "applied"


def test_rollback_drops_the_corrections_from_that_approval(env):
    crm, store = env
    row = pending(store, "Bellhaven of Chagrin Falls", "update")
    approve(store, row["fingerprint"], crm, edits={"0:name": {"include": True, "value": "Custom Name"}})
    assert store.list_overrides()
    assert revert(store, row["fingerprint"], crm)["ok"]
    assert store.list_overrides() == []


def test_rollback_twice_is_refused(env):
    crm, store = env
    row = pending(store, "Bellhaven of Portsmouth", "update")
    approve(store, row["fingerprint"], crm)
    assert revert(store, row["fingerprint"], crm)["ok"]
    assert not revert(store, row["fingerprint"], crm)["ok"]
