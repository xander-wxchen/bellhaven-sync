"""In-memory stand-in for the CRM API, so tests never touch the real sandbox."""
import copy
import itertools


class FakeCRM:
    def __init__(self, accounts, contacts):
        self.accounts = {a["account_id"]: copy.deepcopy(a) for a in accounts}
        self.contacts = {c["contact_id"]: copy.deepcopy(c) for c in contacts}
        self._ids = itertools.count(1)
        self.calls = []
        self.fail_next = None   # e.g. "update" to simulate one failing write

    def _maybe_fail(self, op):
        if self.fail_next == op:
            self.fail_next = None
            raise RuntimeError(f"simulated {op} failure")

    def list_accounts(self, **_):
        return [copy.deepcopy(a) for a in self.accounts.values()]

    def list_contacts(self, **_):
        return [copy.deepcopy(c) for c in self.contacts.values()]

    def get_account(self, account_id):
        return copy.deepcopy(self.accounts[account_id])

    def update_account(self, account_id, fields):
        self.calls.append(("update", account_id, dict(fields)))
        self._maybe_fail("update")
        a = self.accounts[account_id]
        a.update(fields)
        if "parent_id" in fields:
            a["parent_name"] = self.accounts.get(fields["parent_id"], {}).get("name", "")
        return copy.deepcopy(a)

    def create_account(self, fields):
        self.calls.append(("create", None, dict(fields)))
        self._maybe_fail("create")
        aid = f"001TEST{next(self._ids):011d}"
        a = {"account_id": aid, "name": "", "parent_id": "", "parent_name": "", "billing_street": "",
             "billing_city": "", "billing_state": "", "billing_zip": "", "care_type": "", "status": "Active",
             "phone": "", "lifetime_revenue": 0, "outstanding_ar": 0, "chow_current_account": "",
             "duplicate_of_account": "", "note": "", "created_by_candidate": True}
        a.update(fields)
        a["parent_name"] = self.accounts.get(a["parent_id"], {}).get("name", "")
        self.accounts[aid] = a
        return copy.deepcopy(a)

    def get_contact(self, contact_id):
        return copy.deepcopy(self.contacts[contact_id])

    def update_contact(self, contact_id, fields):
        self.calls.append(("update_contact", contact_id, dict(fields)))
        self.contacts[contact_id].update(fields)
        return copy.deepcopy(self.contacts[contact_id])
