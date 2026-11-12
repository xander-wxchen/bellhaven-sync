"""Match website locations to CRM accounts and turn differences into proposals.

Nothing here talks to the network or writes anything. Input: scraped
locations + CRM accounts + contacts (+ reviewer overrides). Output: a list of
Proposals, each a self-contained, reviewable unit with the exact API actions
it would run.

A proposal carries what a reviewer needs, in the shape big CRMs use for
"compare and update" screens (Salesforce merge / Data.com Clean, HubSpot merge):
  rows       field-by-field: in CRM now | on website | value to save (editable)
  decisions  structural changes in plain words (parent, status, duplicate, CHOW)
  record     the CRM account's context (parent, status, billing)
  signals    why we believe the website listing and the CRM record are the same

Re-run safety:
1. State-based diffing: if the CRM already looks right, no proposal is made.
2. Fingerprints: the store skips anything already decided.
3. Overrides: a reviewer's manual correction wins over the website value until
   the website value itself changes.
"""
from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import date

from . import config
from .normalize import (
    is_po_box, name_similarity, norm_city, norm_phone, norm_street,
    street_name, street_number, zip5,
)
from .validate import check_all

NEW = "$new"   # placeholder for the id of an account created earlier in the same proposal
KIND_ORDER = ["create", "chow", "reparent", "update", "duplicate", "stale"]
LABEL = config.FIELD_LABELS


@dataclass
class Proposal:
    kind: str                      # create | chow | reparent | update | duplicate | stale
    group: str                     # location name the reviewer sees it grouped under
    title: str                     # one plain-English sentence
    actions: list[dict]            # ordered API operations (see apply.py)
    before: dict                   # {account_id: {field: value}} for every field we change
    rows: list[dict] = field(default_factory=list)
    decisions: list[dict] = field(default_factory=list)
    record: dict | None = None
    records: dict | None = None    # duplicates: {"keep": ctx, "retire": ctx}
    signals: list[dict] = field(default_factory=list)
    evidence: list[str] = field(default_factory=list)
    confidence: str = "high"       # high | medium
    warnings: list[str] = field(default_factory=list)
    site: dict | None = None
    account_ids: list[str] = field(default_factory=list)
    override_key: str = ""         # key reviewer overrides are stored under (site slug)

    @property
    def fingerprint(self) -> str:
        """Identity of the *decision*: kind + target accounts + target values.
        Notes are excluded because they carry today's date."""
        core = [{k: v for k, v in a.items() if k not in ("fields", "note_lines", "marker")} |
                {"fields": {f: v for f, v in (a.get("fields") or {}).items() if f != "note"}}
                for a in self.actions]
        raw = json.dumps({"kind": self.kind, "group": self.group, "actions": core}, sort_keys=True)
        return hashlib.sha1(raw.encode()).hexdigest()[:16]

    def to_dict(self) -> dict:
        return asdict(self) | {"fingerprint": self.fingerprint}


# ------------------------------------------------------------------ the SOP

def billing_requires_chow(account: dict) -> bool:
    """Billing SOP: an account with revenue history AND open AR must keep its
    old parent; the facility moves via a new account linked by
    chow_current_account. Both conditions are required."""
    return _money(account.get("lifetime_revenue")) > 0 and _money(account.get("outstanding_ar")) > 0


def _money(v) -> float:
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


# ------------------------------------------------------------------ scoring

@dataclass
class Match:
    account: dict
    score: int
    signals: list[dict]
    phone_match: bool
    admin_match: bool

    @property
    def evidence(self) -> list[str]:
        return [s["label"] + (f" ({s['detail']})" if s.get("detail") else "") for s in self.signals if s["ok"]]


def score(site: dict, acct: dict, contact_names: set[str]) -> Match:
    if (acct.get("billing_state") or "").upper() != site["state"].upper():
        return Match(acct, 0, [], False, False)
    same_city = norm_city(acct.get("billing_city")) == norm_city(site["city"])
    same_zip = zip5(acct.get("billing_zip")) == zip5(site["zip"])
    if not (same_city or same_zip):
        return Match(acct, 0, [], False, False)

    pts, sig = 0, []
    crm_street = acct.get("billing_street") or ""
    same_num = bool(street_number(crm_street)) and street_number(crm_street) == street_number(site["street"])
    if same_num and street_name(crm_street) == street_name(site["street"]):
        pts += config.SCORE["address_exact"] if (same_city and same_zip) else config.SCORE["address_one_off"]
        sig.append({"label": "Same street address", "ok": True,
                    "detail": "" if norm_street(crm_street) == norm_street(site["street"]) else f"CRM writes it '{crm_street}'"})
    elif same_num and name_similarity(street_name(crm_street), street_name(site["street"])) >= 0.85:
        pts += config.SCORE["address_fuzzy"]
        sig.append({"label": "Street nearly identical", "ok": True, "detail": f"CRM: '{crm_street}'"})
    elif is_po_box(crm_street) and same_city:
        pts += config.SCORE["po_box_same_city"]
        sig.append({"label": "Same street address", "ok": False, "detail": f"CRM has a PO Box: '{crm_street}'"})
    else:
        sig.append({"label": "Same street address", "ok": False, "detail": f"CRM: '{crm_street}'"})

    sig.append({"label": "Same city and ZIP", "ok": same_city and same_zip,
                "detail": "" if same_city and same_zip else
                ("ZIP differs: CRM " + str(acct.get("billing_zip")) if same_city else "city differs")})

    sim = name_similarity(acct.get("name", ""), site["name"])
    if sim >= 0.5:
        pts += round(config.SCORE["name_max"] * sim)
    sig.append({"label": "Same name", "ok": sim >= 0.95,
                "detail": "" if sim >= 0.95 else f"CRM name '{acct.get('name', '')}', {sim:.0%} similar"})

    phone_match = bool(site.get("phone")) and norm_phone(acct.get("phone")) == norm_phone(site["phone"])
    if phone_match:
        pts += config.SCORE["phone"]
    sig.append({"label": "Same phone", "ok": phone_match,
                "detail": site.get("phone", "") if phone_match else f"CRM {acct.get('phone') or 'blank'}"})

    admin_match = bool(site.get("administrator")) and site["administrator"].lower() in contact_names
    if admin_match:
        pts += config.SCORE["administrator"]
    sig.append({"label": "Website administrator is a CRM contact", "ok": admin_match,
                "detail": site.get("administrator", "")})
    return Match(acct, pts, sig, phone_match, admin_match)


# ------------------------------------------------------------------ helpers

def _line(text: str, today: str, field_name: str | None = None) -> dict:
    """A note line tied to a field: if the reviewer drops that field, the line goes too."""
    return {"field": field_name, "text": f"[{today} {config.JOB_TAG}] {text}"}


def _site_value(site: dict, f: str) -> str:
    if f == "care_type":
        mapped = [config.CARE_TYPE_MAP.get(o, o) for o in site.get("care_offerings", [])]
        return mapped[0] if mapped else ""
    return {"name": site["name"], "billing_street": site["street"], "billing_city": site["city"],
            "billing_state": site["state"], "billing_zip": site["zip"], "phone": site.get("phone", "")}[f]


def _site_display(site: dict, f: str) -> str:
    """What the website literally says (care labels unmapped), for the 'On website' column."""
    if f == "care_type":
        return ", ".join(site.get("care_offerings", []))
    return _site_value(site, f)


def _target(site: dict, f: str, overrides: dict) -> tuple[str, bool]:
    """Value to write: the website's, unless a reviewer overrode it and the
    website still says what it said at the time."""
    want = _site_value(site, f)
    o = overrides.get((site["slug"], f))
    if o and o["site_value"] == want:
        return o["value"], True
    return want, False


def _differs(f: str, crm_v, target: str, site: dict, overridden: bool) -> bool:
    crm_v = str(crm_v or "")
    if f == "name":
        return crm_v != target
    if f == "billing_street":
        return is_po_box(crm_v) or norm_street(crm_v) != norm_street(target)
    if f == "billing_city":
        return norm_city(crm_v) != norm_city(target)
    if f == "billing_state":
        return crm_v.upper() != target.upper()
    if f == "billing_zip":
        return zip5(crm_v) != zip5(target)
    if f == "care_type":
        if overridden:
            return crm_v != target
        offered = [config.CARE_TYPE_MAP.get(o, o) for o in site.get("care_offerings", [])]
        return bool(offered) and crm_v not in offered
    if f == "phone":
        return norm_phone(crm_v) != norm_phone(target)
    return crm_v != target


def _compare_rows(acct: dict | None, site: dict, overrides: dict, action: int,
                  fields: list[str]) -> list[dict]:
    rows = []
    for f in fields:
        target, overridden = _target(site, f, overrides)
        crm_v = (acct or {}).get(f, "")
        rows.append({
            "key": f"{action}:{f}", "action": action, "field": f, "label": LABEL.get(f, f),
            "crm": crm_v if acct else "", "site": _site_display(site, f), "site_value": _site_value(site, f),
            "value": target, "overridden": overridden,
            "changed": True if acct is None else _differs(f, crm_v, target, site, overridden),
            "required": acct is None and f in config.REQUIRED_FOR_CREATE,
        })
    return rows


def _ctx(a: dict, contacts_by_acct: dict) -> dict:
    return {"account_id": a["account_id"], "name": a.get("name", ""),
            "parent_name": a.get("parent_name") or "(no parent)", "status": a.get("status", ""),
            "street": a.get("billing_street", ""), "city": a.get("billing_city", ""),
            "state": a.get("billing_state", ""), "zip": a.get("billing_zip", ""), "phone": a.get("phone", ""),
            "care_type": a.get("care_type", ""),
            "lifetime_revenue": _money(a.get("lifetime_revenue")), "outstanding_ar": _money(a.get("outstanding_ar")),
            "contacts": len(contacts_by_acct.get(a["account_id"], []))}


def _money_str(a: dict) -> str:
    return (f"lifetime revenue ${_money(a.get('lifetime_revenue')):,.0f}, "
            f"open AR ${_money(a.get('outstanding_ar')):,.0f}")


def _short(parent_name: str) -> str:
    return (parent_name or "no parent").replace(" (Parent Account)", "")


# ------------------------------------------------------------------ main entry

def build_proposals(sites: list[dict], accounts: list[dict], contacts: list[dict],
                    today: str | None = None, overrides: dict | None = None) -> tuple[list[Proposal], dict]:
    today = today or date.today().isoformat()
    overrides = overrides or {}
    by_id = {a["account_id"]: a for a in accounts}

    parent = next((a for a in accounts if a.get("name") == config.PARENT_ACCOUNT_NAME), None)
    if not parent:
        raise RuntimeError(f"Parent account '{config.PARENT_ACCOUNT_NAME}' not found in CRM")
    bh = parent["account_id"]

    parent_ids = {a.get("parent_id") for a in accounts if a.get("parent_id")}
    is_parent = lambda a: a["account_id"] in parent_ids or "(Parent Account)" in (a.get("name") or "")
    # Accounts already resolved by an earlier decision are out of the pool.
    pool = [a for a in accounts if not is_parent(a)
            and not a.get("duplicate_of_account") and not a.get("chow_current_account")]

    contacts_by_acct: dict[str, list[dict]] = defaultdict(list)
    for c in contacts:
        if c.get("is_active", True):
            contacts_by_acct[c["account_id"]].append(c)

    dq = check_all(sites)   # data-quality flags per site slug

    # 1) score every (site, account) pair; keep strong matches
    matches: dict[str, list[Match]] = {}
    for s in sites:
        ms = []
        for a in pool:
            names = {c["name"].lower() for c in contacts_by_acct.get(a["account_id"], [])}
            m = score(s, a, names)
            if m.score >= config.MATCH_THRESHOLD:
                ms.append(m)
        matches[s["slug"]] = ms

    owner: dict[str, tuple[int, str]] = {}   # an account belongs to one site: its best
    for slug, ms in matches.items():
        for m in ms:
            aid = m.account["account_id"]
            if aid not in owner or m.score > owner[aid][0]:
                owner[aid] = (m.score, slug)
    for slug in matches:
        matches[slug] = [m for m in matches[slug] if owner[m.account["account_id"]][1] == slug]

    proposals: list[Proposal] = []
    summary = defaultdict(list)
    matched_ids: set[str] = set()
    ctx = lambda a: _ctx(a, contacts_by_acct)

    for s in sites:
        ms = matches[s["slug"]]
        flags = dq.get(s["slug"], [])
        if not ms:
            p = _create(s, pool, contacts_by_acct, bh, today, overrides)
            _attach_flags(p, flags)
            proposals.append(p)
            summary["create"].append(s["name"])
            continue

        def rank(m: Match):
            a = m.account
            return (a.get("parent_id") != bh,
                    not (_money(a.get("lifetime_revenue")) or _money(a.get("outstanding_ar"))),
                    not contacts_by_acct.get(a["account_id"]),
                    not m.phone_match, -m.score, a["account_id"])
        ms.sort(key=rank)
        keep, dups = ms[0], ms[1:]
        matched_ids.update(m.account["account_id"] for m in ms)
        tie_break = bool(dups) and rank(keep)[:4] == rank(dups[0])[:4]

        p = _survivor(s, keep, bh, by_id, today, tie_break, overrides, ctx)
        if p:
            _attach_flags(p, flags)
            proposals.append(p)
            summary[p.kind].append(s["name"])
        elif not dups:
            summary["no_change"].append(s["name"])

        for d in dups:
            proposals.append(_duplicate(s, d, keep, contacts_by_acct, today, ctx))
            summary["duplicate"].append(f"{s['name']} ({d.account['name']})")

    # 3) accounts under Bellhaven that the website no longer lists
    for a in pool:
        if a.get("parent_id") == bh and a["account_id"] not in matched_ids \
                and a.get("status") not in ("Needs Review", "Inactive"):
            proposals.append(_stale(a, len(sites), today, ctx))
            summary["stale"].append(a["name"])

    proposals.sort(key=lambda p: (p.group.lower(), KIND_ORDER.index(p.kind)))
    out = {k: v for k, v in summary.items()}
    out["data_quality"] = dq
    return proposals, out


def _attach_flags(p: Proposal, flags: list[dict]) -> None:
    if flags:
        p.warnings += [f"Website data check: {f['message']}" for f in flags]
        p.confidence = "medium"


# ------------------------------------------------------------------ builders

def _survivor(s, m: Match, bh, by_id, today, tie_break, overrides, ctx) -> Proposal | None:
    a = m.account
    rows = _compare_rows(a, s, overrides, 0, config.SYNCED_FIELDS)
    changed = [r for r in rows if r["changed"]]
    lines = []
    for r in changed:
        if r["field"] == "name":
            lines.append(_line(f"Renamed from '{a.get('name', '')}' to match the operator's website.", today, "name"))
        if r["field"] == "billing_street" and is_po_box(a.get("billing_street", "")):
            lines.append(_line(f"Replaced PO Box '{a.get('billing_street')}' with the street address.",
                               today, "billing_street"))
        if r["overridden"]:
            lines.append(_line(f"{r['label']} kept as reviewer-approved '{r['value']}'.", today, r["field"]))

    decisions, extra = [], {}
    if a.get("status") != "Active":
        extra["status"] = "Active"
        decisions.append({"label": "Status", "from": a.get("status"), "to": "Active",
                          "why": "Listed on the operator's website"})
        lines.append(_line("Reactivated: listed on the operator's website.", today))

    warnings, confidence = [], ("high" if m.score >= config.SCORE["address_exact"] else "medium")
    if tie_break:
        warnings.append("No record is under Bellhaven or has billing, contacts or a phone match; "
                        "kept the closest name match. Check the duplicates in this group.")
        confidence = "medium"

    old_parent = _short(by_id.get(a.get("parent_id") or "", {}).get("name") or "")
    if a.get("parent_id") != bh:
        if billing_requires_chow(a):
            return _chow(s, a, m, bh, today, overrides, ctx, old_parent, confidence, warnings)
        extra["parent_id"] = bh
        decisions.insert(0, {"label": "Parent account", "from": old_parent, "to": "Bellhaven Senior Living",
                             "why": f"{_money_str(a)}: no CHOW needed, re-parent directly"})
        lines.append(_line(f"Re-parented from {old_parent} to Bellhaven ({_money_str(a)}; "
                           "billing SOP allows a direct move).", today))

    if not changed and not extra:
        return None

    fields = {r["field"]: r["value"] for r in changed} | extra
    kind = "reparent" if "parent_id" in extra else "update"
    if kind == "reparent":
        title = f"Move '{a['name']}' from {old_parent} to Bellhaven"
        if changed:
            title += " and update " + ", ".join(r["label"].lower() for r in changed)
    elif len(changed) == 1 and changed[0]["field"] == "name":
        title = f"Rename '{a['name']}' to '{changed[0]['value']}'"
    else:
        title = "Update " + ", ".join(r["label"].lower() for r in changed) + f" on '{a['name']}'"
    return Proposal(kind=kind, group=s["name"], site=s, account_ids=[a["account_id"]], title=title,
                    actions=[{"op": "update", "account_id": a["account_id"], "fields": fields, "note_lines": lines}],
                    before={a["account_id"]: {f: a.get(f, "") for f in fields}},
                    rows=rows, decisions=decisions, record=ctx(a), signals=m.signals,
                    confidence=confidence, warnings=warnings, override_key=s["slug"])


def _chow(s, a, m, bh, today, overrides, ctx, old_parent, confidence, warnings) -> Proposal:
    marker = f"[{config.JOB_TAG}:{s['slug']}]"
    rows = _compare_rows(None, s, overrides, 0, config.SYNCED_FIELDS + ["phone"])
    new_fields = {r["field"]: r["value"] for r in rows} | {
        "parent_id": bh, "status": "Active",
        "note": f"[{today} {config.JOB_TAG}] Change of ownership from {a['account_id']} ({old_parent}); "
                f"old account kept for billing. Source: {s['url']}\n{marker}"}
    return Proposal(
        kind="chow", group=s["name"], site=s, account_ids=[a["account_id"]], override_key=s["slug"],
        title=f"Change of ownership: open a new Bellhaven account for '{s['name']}' and link the old {old_parent} account",
        actions=[{"op": "create", "ref": NEW, "fields": new_fields, "marker": marker, "note_lines": []},
                 {"op": "update", "account_id": a["account_id"], "fields": {"chow_current_account": NEW},
                  "note_lines": []}],
        before={a["account_id"]: {"chow_current_account": a.get("chow_current_account", "")}},
        rows=rows,
        decisions=[{"label": "New account", "from": "", "to": f"Created under Bellhaven Senior Living",
                    "why": "Values below"},
                   {"label": "Old account …" + a["account_id"][-6:], "from": f"under {old_parent}",
                    "to": "unchanged, except CHOW link → new account",
                    "why": f"{_money_str(a)}: billing SOP requires keeping it as is"}],
        record=ctx(a), signals=m.signals, confidence=confidence, warnings=warnings)


def _duplicate(s, d: Match, keep: Match, contacts_by_acct, today, ctx) -> Proposal:
    a, k = d.account, keep.account
    fields = {"status": "Inactive", "duplicate_of_account": k["account_id"]}
    actions = [{"op": "update", "account_id": a["account_id"], "fields": fields,
                "note_lines": [_line(f"Duplicate of {k['account_id']} ({k['name']}): same facility as website "
                                     f"listing {s['url']}.", today)]}]
    moved = contacts_by_acct.get(a["account_id"], [])
    for c in moved:
        actions.append({"op": "update_contact", "contact_id": c["contact_id"], "contact_name": c["name"],
                        "fields": {"account_id": k["account_id"]}})
    decisions = [{"label": "Retired record", "from": a.get("status", ""), "to": "Inactive, marked duplicate of kept record",
                  "why": "Same facility; no merge or delete in this CRM"}]
    if moved:
        decisions.append({"label": "Contacts", "from": "on retired record",
                          "to": f"moved to kept record: {', '.join(c['name'] for c in moved)}",
                          "why": "So sales doesn't lose them"})
    warnings = []
    if _money(a.get("lifetime_revenue")) or _money(a.get("outstanding_ar")):
        warnings.append(f"The retired record has billing history ({_money_str(a)}). Confirm with billing first.")
    return Proposal(kind="duplicate", group=s["name"], site=s, account_ids=[a["account_id"], k["account_id"]],
                    title=f"Retire '{a['name']}' as a duplicate of '{k['name']}'",
                    actions=actions, before={a["account_id"]: {f: a.get(f, "") for f in fields}},
                    decisions=decisions, records={"keep": ctx(k), "retire": ctx(a)},
                    record=ctx(a), signals=d.signals,
                    evidence=[f"Kept …{k['account_id'][-6:]} because it " + _why_kept(keep, contacts_by_acct)],
                    confidence="high" if d.score >= config.SCORE["address_exact"] else "medium",
                    warnings=warnings)


def _why_kept(m: Match, contacts_by_acct: dict) -> str:
    a, why = m.account, []
    if "Bellhaven" in (a.get("parent_name") or ""):
        why.append("is already under Bellhaven")
    if _money(a.get("lifetime_revenue")) or _money(a.get("outstanding_ar")):
        why.append("has billing history")
    if contacts_by_acct.get(a["account_id"]):
        why.append("has contacts")
    if m.phone_match:
        why.append("matches the website phone")
    return ", ".join(why) or "has the strongest match score (closest name)"


def _create(s, pool, contacts_by_acct, bh, today, overrides) -> Proposal:
    marker = f"[{config.JOB_TAG}:{s['slug']}]"
    rows = _compare_rows(None, s, overrides, 0, config.SYNCED_FIELDS + ["phone"])
    fields = {r["field"]: r["value"] for r in rows} | {
        "parent_id": bh, "status": "Active",
        "note": f"[{today} {config.JOB_TAG}] Created from the operator's website: {s['url']}\n{marker}"}
    evidence, warnings = [], []
    for a in pool:
        sim = name_similarity(a.get("name", ""), s["name"])
        names = {c["name"].lower() for c in contacts_by_acct.get(a["account_id"], [])}
        sc = score(s, a, names).score
        if sim >= config.SAME_NAME_ANYWHERE or sc >= config.NEAR_MISS_SCORE:
            where = f"{a.get('billing_street')}, {a.get('billing_city')}, {a.get('billing_state')}"
            evidence.append(f"Checked and rejected: '{a['name']}' at {where} "
                            f"({_short(a.get('parent_name') or '')}), different address")
            if sc >= config.POSSIBLE_MATCH_SCORE:
                warnings.append(f"Possible related account '{a['name']}' in the same city. "
                                "Reject if it is actually this facility.")
    return Proposal(kind="create", group=s["name"], site=s, account_ids=[], override_key=s["slug"],
                    title=f"Create a new account for '{s['name']}' under Bellhaven",
                    actions=[{"op": "create", "ref": NEW, "fields": fields, "marker": marker, "note_lines": []}],
                    before={}, rows=rows,
                    decisions=[{"label": "Parent account", "from": "", "to": "Bellhaven Senior Living",
                                "why": "Listed on the operator's website"}],
                    signals=[{"label": "No CRM account at this address in the same city/ZIP", "ok": True,
                              "detail": ""}],
                    evidence=evidence, confidence="medium" if warnings else "high", warnings=warnings)


def _stale(a, n_sites, today, ctx) -> Proposal:
    fields = {"status": "Needs Review"}
    warnings = []
    if _money(a.get("outstanding_ar")) > 0:
        warnings.append("Open AR: leave the parent and billing untouched; flag to billing.")
    return Proposal(kind="stale", group=a["name"], account_ids=[a["account_id"]], override_key=a["account_id"],
                    title=f"'{a['name']}' is no longer on the Bellhaven website: mark it Needs Review",
                    actions=[{"op": "update", "account_id": a["account_id"], "fields": fields,
                              "note_lines": [_line(f"Not listed on the Bellhaven website ({n_sites} communities "
                                                   "checked). Parent left unchanged until the new owner is "
                                                   "confirmed.", today)]}],
                    before={a["account_id"]: {"status": a.get("status", "")}},
                    decisions=[{"label": "Status", "from": a.get("status", ""), "to": "Needs Review",
                                "why": "We don't know the new owner, so the parent stays"}],
                    record=ctx(a),
                    signals=[{"label": "Under the Bellhaven parent in CRM", "ok": True, "detail": ""},
                             {"label": "Listed on the website", "ok": False,
                              "detail": "no listing at this address or with this name"}],
                    confidence="medium", warnings=warnings)
