"""Execute one approved proposal against the CRM, safely.

Safety rules:
1. Stale check first: re-read every account the proposal touches. If any field
   we are about to change no longer has the value the reviewer saw, stop.
2. Notes are appended to the *live* note, never overwritten.
3. Creates are idempotent: each created account carries a marker in its note;
   before creating, look for it. A retry after a partial failure reuses it.
4. Verify after writing: re-read and compare. Report a mismatch as a failure.

Reviewer edits (with_edits) are applied to a copy of the proposal before any
of this runs, so the same safety rules cover edited values.
"""
from __future__ import annotations

import copy
from datetime import date
from typing import Callable

from . import config
from .matching import NEW


class StaleProposal(RuntimeError):
    pass


class VerifyFailed(RuntimeError):
    pass


class InvalidEdit(ValueError):
    pass


def _same(a, b) -> bool:
    return str(a or "").strip() == str(b or "").strip()


# ------------------------------------------------------------------ edits

def with_edits(p: dict, edits: dict | None, who: str, today: str | None = None) -> tuple[dict, list[dict]]:
    """Apply a reviewer's per-field choices to a copy of the proposal.

    edits: {row_key: {"include": bool, "value": str}} for the changed rows.
    Returns (edited proposal, list of corrections) where a correction records
    each field the reviewer excluded or changed (used for overrides + issue log).
    """
    if not edits:
        return p, []
    today = today or date.today().isoformat()
    p = copy.deepcopy(p)
    rows = {r["key"]: r for r in p.get("rows", [])}
    corrections = []

    for key, e in edits.items():
        r = rows.get(key)
        if not r or not r["changed"]:
            continue
        a = p["actions"][r["action"]]
        value = str(e.get("value", "")).strip()
        if not e.get("include", True):
            if r["required"]:
                raise InvalidEdit(f"{r['label']} is required for a new account; edit it instead of unticking it.")
            a["fields"].pop(r["field"], None)
            corrections.append({"row": r, "kind": "excluded", "value": r["crm"]})
            continue
        if r["required"] and not value:
            raise InvalidEdit(f"{r['label']} can't be blank.")
        if r["field"] == "billing_state" and len(value) != 2:
            raise InvalidEdit("State must be a 2-letter code.")
        if value != str(r["value"]):
            a["fields"][r["field"]] = value
            corrections.append({"row": r, "kind": "edited", "value": value})
            a.setdefault("note_lines", []).append({
                "field": r["field"],
                "text": f"[{today} {config.JOB_TAG}] {who} set {r['label']} to '{value}' "
                        f"(website shows '{r['site']}')."})

    # drop note lines whose field is no longer written; drop empty updates
    kept = []
    for a in p["actions"]:
        a["note_lines"] = [ln for ln in a.get("note_lines", [])
                           if ln.get("field") is None or ln["field"] in a.get("fields", {})]
        if a["op"] == "update" and not a.get("fields"):
            continue
        kept.append(a)
    if not kept:
        raise InvalidEdit("Nothing left to change. Use Reject instead.")
    p["actions"] = kept

    written = {}
    for a in kept:
        if a["op"] == "update":
            written.setdefault(a["account_id"], set()).update(a["fields"])
    p["before"] = {aid: {f: v for f, v in fields.items() if f in written.get(aid, set())}
                   for aid, fields in (p.get("before") or {}).items() if aid in written}
    return p, corrections


# ------------------------------------------------------------------ apply

def apply_proposal(p: dict, client, audit: Callable[[str, str, dict, object, bool], None] | None = None) -> dict:
    audit = audit or (lambda *a: None)

    # 1) stale check
    for acct_id, fields in (p.get("before") or {}).items():
        live = client.get_account(acct_id)
        for f, seen in fields.items():
            if not _same(live.get(f), seen):
                raise StaleProposal(
                    f"{config.FIELD_LABELS.get(f, f)} on …{acct_id[-6:]} changed since this proposal was made "
                    f"(was {seen!r}, now {live.get(f)!r}). Run the pipeline again for a fresh proposal.")

    created: str | None = None
    written: list[tuple[str, dict]] = []
    undo: list[dict] = []   # how to put every touched record back (see revert_proposal)
    resolve = lambda v: created if v == NEW else v

    for a in p["actions"]:
        op = a["op"]
        lines = "\n".join(ln["text"] for ln in a.get("note_lines", []))
        if op == "create":
            existing = _find_marker(client, a.get("marker"))
            if existing:
                created = existing["account_id"]
                audit("create (reused after retry)", created, a["fields"], existing, True)
            else:
                fields = dict(a["fields"])
                if lines:
                    fields["note"] = f"{fields.get('note', '')}\n{lines}".strip()
                try:
                    res = client.create_account(fields)
                except Exception as e:
                    audit("create", "", fields, str(e), False)
                    raise
                created = (res or {}).get("account_id")
                audit("create", created or "", fields, res, bool(created))
                if not created:
                    raise VerifyFailed(f"Create returned no account_id: {res}")
            written.append((created, {k: v for k, v in a["fields"].items() if k != "note"}))

        elif op == "update":
            acct_id = a["account_id"]
            fields = {k: resolve(v) for k, v in a["fields"].items()}
            live = client.get_account(acct_id)           # snapshot before writing
            if lines:
                live_note = live.get("note") or ""
                if lines not in live_note:          # don't double-append on retry
                    fields["note"] = f"{live_note.rstrip()}\n{lines}" if live_note.strip() else lines
            try:
                res = client.update_account(acct_id, fields)
            except Exception as e:
                audit("update", acct_id, fields, str(e), False)
                raise
            audit("update", acct_id, fields, res, True)
            undo.append({"op": "update", "id": acct_id, "expect": fields,
                         "restore": {f: live.get(f, "") for f in fields}})
            written.append((acct_id, {k: v for k, v in fields.items() if k != "note"}))

        elif op == "update_contact":
            before_c = client.get_contact(a["contact_id"])
            try:
                res = client.update_contact(a["contact_id"], a["fields"])
            except Exception as e:
                audit("update_contact", a["contact_id"], a["fields"], str(e), False)
                raise
            audit("update_contact", a["contact_id"], a["fields"], res, True)
            undo.append({"op": "update_contact", "id": a["contact_id"], "expect": dict(a["fields"]),
                         "restore": {f: before_c.get(f, "") for f in a["fields"]}})
        else:
            raise ValueError(f"Unknown op {op}")

    # verify
    for acct_id, fields in written:
        live = client.get_account(acct_id)
        bad = {f: (v, live.get(f)) for f, v in fields.items() if not _same(live.get(f), v)}
        if bad:
            raise VerifyFailed(f"…{acct_id[-6:]} did not take the change: {bad}")

    # A created account can't be rolled back: the API has no delete.
    return {"created_account": created, "accounts_written": sorted({w[0] for w in written}),
            "undo": undo, "reversible": created is None}


# ------------------------------------------------------------------ revert

def revert_proposal(result: dict, client, audit: Callable[[str, str, dict, object, bool], None] | None = None) -> None:
    """Put back the values an applied proposal overwrote.

    Refuses if the proposal created an account (no delete in this API), or if
    anyone changed a field we wrote since then (we'd overwrite their edit).
    """
    audit = audit or (lambda *a: None)
    if not result or not result.get("reversible") or not result.get("undo"):
        raise StaleProposal("This change created a new account, so it can't be rolled back (the API has no delete).")
    get = lambda s: client.get_account(s["id"]) if s["op"] == "update" else client.get_contact(s["id"])

    for s in result["undo"]:                          # 1) still exactly as we left it?
        live = get(s)
        for f, v in s["expect"].items():
            if not _same(live.get(f), v):
                raise StaleProposal(f"{config.FIELD_LABELS.get(f, f)} on …{s['id'][-6:]} was changed after approval. "
                                    "Roll back by hand so that edit isn't lost.")
    for s in reversed(result["undo"]):                # 2) restore, newest write first
        write = client.update_account if s["op"] == "update" else client.update_contact
        res = write(s["id"], s["restore"])
        audit(f"revert {s['op']}", s["id"], s["restore"], res, True)
    for s in result["undo"]:                          # 3) verify
        live = get(s)
        bad = {f: (v, live.get(f)) for f, v in s["restore"].items() if not _same(live.get(f), v)}
        if bad:
            raise VerifyFailed(f"…{s['id'][-6:]} did not roll back cleanly: {bad}")


def _find_marker(client, marker: str | None) -> dict | None:
    if not marker:
        return None
    for acct in client.list_accounts():
        if marker in (acct.get("note") or ""):
            return acct
    return None
