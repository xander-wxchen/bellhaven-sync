"""Review app: see each proposed CRM change with its evidence, correct it if
needed, approve or reject. Problems go to an issue log for later fixes.

Run:  python -m streamlit run app.py
Approve is the only code path that writes to the CRM (bellhaven_sync.store.approve).

Card layout follows the "compare and update" pattern of big CRMs (Salesforce
merge / Data.com Clean, HubSpot merge): one row per field, CRM value next to
the source value, a tick per field, and the value to save editable.
"""
from __future__ import annotations

import html
import os
from collections import OrderedDict

import pandas as pd
import streamlit as st

from bellhaven_sync import config
from bellhaven_sync.crm import CRMClient
from bellhaven_sync.store import Store, approve, revert

st.set_page_config(page_title="Bellhaven CRM Sync", page_icon=":clipboard:", layout="wide")

KIND_LABEL = {
    "create": "New account", "chow": "Change of ownership", "reparent": "Move to Bellhaven",
    "update": "Field update", "duplicate": "Duplicate", "stale": "Not on website",
}
KIND_STYLE = {   # muted tags; brand crimson is reserved for actions
    "create": ("#e7efe9", "#1e6e46"), "chow": ("#fdf6d8", "#5c4a10"),
    "reparent": ("#e8eef6", "#2b4a73"), "update": ("#f0eeea", "#4a4040"),
    "duplicate": ("#efe9f2", "#5a3d6b"), "stale": ("#fbeee6", "#8a4a1c"),
}
STATUS_STYLE = {
    "pending": ("#f0eeea", "#4a4040"), "applied": ("#e7efe9", "#1e6e46"),
    "rejected": ("#f0eeea", "#7d6a63"), "failed": ("#fbe7ea", "#a52441"),
    "applying": ("#fdf6d8", "#5c4a10"), "superseded": ("#f0eeea", "#7d6a63"),
}

st.markdown("""
<style>
  .block-container { padding-top: 1.6rem; max-width: 1280px; }
  .app-head { border-top: 3px solid #ca3051; padding-top: .7rem; margin-bottom: .2rem; }
  .app-head h1 { font-size: 1.45rem; font-weight: 650; margin: 0; color: #37171a; }
  .app-head .sub { color: #7d6a63; font-size: .88rem; margin-top: .15rem; }
  .tag { display:inline-block; padding: 1px 8px; border-radius: 999px; font-size: .74rem;
         font-weight: 600; margin-right: 6px; vertical-align: 1px; }
  .ptitle { font-weight: 600; font-size: 1rem; }
  .grp { font-size: 1.02rem; font-weight: 650; margin: 1.3rem 0 .35rem; color: #37171a; }
  .grp .where { color: #7d6a63; font-weight: 400; font-size: .82rem; margin-left: 8px; }
  .grp a { color: #ca3051; font-weight: 400; font-size: .82rem; margin-left: 8px; text-decoration: none; }
  .rec { background:#f7f5f0; border:1px solid #ece6dc; border-radius:6px; padding:7px 11px;
         font-size:.84rem; color:#4a3a3c; margin:.35rem 0 .6rem; }
  .rec b { color:#37171a; }
  .rec .sep { color:#c9bdb8; margin: 0 6px; }
  .rec a { color:#ca3051; text-decoration:none; }
  .sec { font-size:.78rem; font-weight:650; text-transform:uppercase; letter-spacing:.04em;
         color:#7d6a63; margin:.5rem 0 .2rem; }
  .hint { font-size:.8rem; color:#7d6a63; margin-bottom:.25rem; }
  .dec { font-size:.86rem; margin:2px 0; }
  .dec .arrow { color:#7d6a63; margin:0 5px; }
  .dec .why { color:#7d6a63; font-size:.8rem; }
  ul.sig { list-style:none; padding-left:0; margin:0; font-size:.85rem; }
  ul.sig li { margin-bottom:3px; }
  ul.sig .ok { color:#1e6e46; font-weight:700; margin-right:6px; }
  ul.sig .no { color:#b0a39e; font-weight:700; margin-right:6px; }
  ul.sig .d { color:#7d6a63; font-size:.8rem; }
  ul.ev { margin: 4px 0 0; padding-left: 1.05rem; font-size: .8rem; color:#6d4a4e; }
  table.cmp { width:100%; border-collapse:collapse; font-size:.84rem; }
  table.cmp th { text-align:left; color:#7d6a63; font-weight:600; border-bottom:1px solid #e4dcc9; padding:5px 7px; }
  table.cmp td { border-bottom:1px solid #f0ebe0; padding:5px 7px; vertical-align:top; }
  table.cmp td.lbl { color:#7d6a63; width:22%; }
  table.cmp a { color:#ca3051; text-decoration:none; }
  table.cmp tr.diff td.val { background:#fbf6ea; }
  .warn { background:#fdf6d8; color:#5c4a10; border-radius:6px; padding:6px 10px; font-size:.84rem; margin-top:6px; }
  .err  { background:#fbe7ea; color:#a52441; border-radius:6px; padding:6px 10px; font-size:.84rem; margin-top:6px; }
  .muted { color:#7d6a63; font-size:.82rem; }
  .muted a { color:#ca3051; }
  div[data-testid="stMetricValue"] { font-size: 1.5rem; }
  [data-testid="stMultiSelectTagsContainer"] > span > span {
      background-color: #ece6dc !important; color: #37171a !important; }
  [data-testid="stMultiSelectTagsContainer"] > span > span * { color: #37171a !important; }
  [data-testid="stMultiSelectTagsContainer"] svg { fill: #7d6a63 !important; color: #7d6a63 !important; }
</style>
""", unsafe_allow_html=True)


@st.cache_resource
def get_store() -> Store:
    return Store()


@st.cache_resource
def get_client() -> CRMClient:
    return CRMClient()


esc = lambda v: html.escape(str(v if v is not None else ""))


def tag(text: str, style: tuple[str, str]) -> str:
    return f'<span class="tag" style="background:{style[0]};color:{style[1]}">{esc(text)}</span>'


def crm_url(account_id: str) -> str:
    return f"{config.CRM_BROWSER}/{account_id}"


def money(v) -> str:
    return f"${float(v or 0):,.0f}"


def record_strip(rec: dict | None, kind: str) -> str:
    if kind == "create":
        return '<div class="rec"><b>New account</b><span class="sep">|</span>will be created under ' \
               '<b>Bellhaven Senior Living</b></div>'
    if not rec:
        return ""
    sep = '<span class="sep">|</span>'
    parts = [f'<b>{esc(rec["name"])}</b> <span class="muted">…{esc(rec["account_id"][-6:])}</span>',
             f'Parent: <b>{esc(rec["parent_name"].replace(" (Parent Account)", ""))}</b>',
             f'Status: {esc(rec["status"])}',
             f'Revenue {money(rec["lifetime_revenue"])}',
             f'Open AR <b>{money(rec["outstanding_ar"])}</b>' if rec["outstanding_ar"] else "Open AR $0",
             f'{rec["contacts"]} contact{"s" if rec["contacts"] != 1 else ""}',
             f'<a href="{crm_url(rec["account_id"])}" target="_blank">Open in CRM ↗</a>']
    return f'<div class="rec">CRM record{sep}' + sep.join(parts) + "</div>"


def decisions_html(decs: list[dict]) -> str:
    out = []
    for d in decs:
        frm = f'{esc(d["from"])}<span class="arrow">→</span>' if d.get("from") else ""
        out.append(f'<div class="dec"><b>{esc(d["label"])}:</b> {frm}<b>{esc(d["to"])}</b>'
                   f'<br><span class="why">{esc(d.get("why", ""))}</span></div>')
    return "".join(out)


def signals_html(sigs: list[dict]) -> str:
    items = []
    for s in sigs:
        mark = '<span class="ok">✓</span>' if s["ok"] else '<span class="no">✕</span>'
        d = f' <span class="d">· {esc(s["detail"])}</span>' if s.get("detail") else ""
        items.append(f"<li>{mark}{esc(s['label'])}{d}</li>")
    return '<ul class="sig">' + "".join(items) + "</ul>"


def duplicate_table(keep: dict, retire: dict) -> str:
    rows = [("Account name", "name"), ("Street", "street"), ("City / ZIP", None), ("Parent", "parent_name"),
            ("Status", "status"), ("Phone", "phone"), ("Care type", "care_type"),
            ("Revenue", "lifetime_revenue"), ("Open AR", "outstanding_ar"), ("Contacts", "contacts")]

    def val(r, k):
        if k is None:
            return f'{r["city"]}, {r["state"]} {r["zip"]}'
        if k in ("lifetime_revenue", "outstanding_ar"):
            return money(r[k])
        return str(r[k]).replace(" (Parent Account)", "")

    out = ['<table class="cmp"><tr><th></th>'
           f'<th>Keep · <a href="{crm_url(keep["account_id"])}" target="_blank">…{esc(keep["account_id"][-6:])} ↗</a></th>'
           f'<th>Retire · <a href="{crm_url(retire["account_id"])}" target="_blank">…{esc(retire["account_id"][-6:])} ↗</a></th></tr>']
    from bellhaven_sync.normalize import norm_street
    for label, k in rows:
        a, b = val(keep, k), val(retire, k)
        same = norm_street(a) == norm_street(b) if k == "street" else a == b   # 'W Lake Rd' == 'West Lake Road'
        cls = ' class="diff"' if not same else ""
        out.append(f'<tr{cls}><td class="lbl">{label}</td><td class="val">{esc(a)}</td><td class="val">{esc(b)}</td></tr>')
    return "".join(out) + "</table>"


def field_editor(p: dict, fp: str, disabled: bool) -> dict | None:
    """Editable compare table. Returns edits {row_key: {include, value}} or None if untouched."""
    rows = [r for r in p.get("rows", []) if r["changed"]]
    same = [r for r in p.get("rows", []) if not r["changed"]]
    if not rows:
        return None
    is_new = p["kind"] in ("create", "chow")
    title = {"create": "Values for the new account",
             "chow": "Step 1 · Create a new account under Bellhaven Senior Living"}.get(p["kind"], "Field changes")
    untick = "Untick an optional field to leave it blank on the new account." if is_new else \
        "Untick a field to keep the CRM value."
    st.markdown(f'<div class="sec">{title}</div>'
                f'<div class="hint">{untick} Edit <b>Save as</b> if the website is '
                'wrong; the correction is remembered on future runs and logged.</div>', unsafe_allow_html=True)
    df = pd.DataFrame([{
        "Update": True, "Field": r["label"] + (" *" if r["required"] else ""),
        "In CRM now": str(r["crm"] or ""), "On website": str(r["site"] or ""),
        "Save as": str(r["value"] or "")} for r in rows])
    cols = ["Update", "Field", "On website", "Save as"] if is_new else \
        ["Update", "Field", "In CRM now", "On website", "Save as"]
    edited = st.data_editor(
        df, key=f"ed-{fp}", hide_index=True, width="stretch", disabled=disabled,
        column_order=cols,
        column_config={
            "Update": st.column_config.CheckboxColumn(
                "Include" if is_new else "Update", width=70,
                help="Untick to leave this field blank" if is_new else "Untick to leave this field as it is in the CRM"),
            "Field": st.column_config.TextColumn("Field", disabled=True, width=130),
            "In CRM now": st.column_config.TextColumn("In CRM now", disabled=True, width="medium"),
            "On website": st.column_config.TextColumn("On website", disabled=True, width="medium"),
            "Save as": st.column_config.TextColumn("Save as ✎", help="Value written to the CRM", width="medium"),
        })
    if same:
        with st.expander(f"{len(same)} field{'s' if len(same) != 1 else ''} already match the website"):
            st.markdown("".join(f'<div class="muted">{esc(r["label"])}: {esc(r["crm"])}</div>' for r in same),
                        unsafe_allow_html=True)
    edits, touched = {}, False
    for r, (_, e) in zip(rows, edited.iterrows()):
        include, value = bool(e["Update"]), str(e["Save as"] if e["Save as"] is not None else "")
        edits[r["key"]] = {"include": include, "value": value}
        touched |= (not include) or value.strip() != str(r["value"])
    return edits if touched else None


def chow_old_account(p: dict, created: str | None) -> str:
    """Step 2 of a change of ownership: the one field written on the old account, plus what stays."""
    rec = p.get("record") or {}
    old = p["account_ids"][0]
    before = (p.get("before", {}).get(old) or {}).get("chow_current_account") or "(empty)"
    after = (f'<a href="{crm_url(created)}" target="_blank">…{esc(created[-6:])} ↗</a> (the new account)'
             if created else "ID of the new account from step 1")
    parent = esc(str(rec.get("parent_name", "")).replace(" (Parent Account)", ""))
    return (f'<div class="sec">Step 2 · Update the old account …{esc(old[-6:])} (one field)</div>'
            '<table class="cmp"><tr><th>Field</th><th>In CRM now</th><th>After approval</th></tr>'
            f'<tr class="diff"><td class="lbl">CHOW: current account</td><td class="val">{esc(before)}</td>'
            f'<td class="val"><b>{after}</b></td></tr></table>'
            f'<div class="hint" style="margin-top:4px">Everything else on the old account stays for billing: '
            f'parent {parent}, status {esc(rec.get("status", ""))}, revenue {money(rec.get("lifetime_revenue"))}, '
            f'open AR {money(rec.get("outstanding_ar"))}.</div>')


def report_popover(store: Store, p: dict, fp: str, grp: str, reviewer: str, where: str) -> None:
    with st.popover("Report a problem", width="stretch"):
        st.markdown("**Log a problem to fix later**")
        cat = st.selectbox("What's wrong?", config.ISSUE_CATEGORIES, key=f"rc-{where}-{fp}")
        fields = ["(whole proposal)"] + [r["label"] for r in p.get("rows", [])]
        fld = st.selectbox("Which field?", fields, key=f"rf-{where}-{fp}")
        det = st.text_area("Details", key=f"rd-{where}-{fp}", placeholder="What did you expect to see?")
        if st.button("Log issue", key=f"rs-{where}-{fp}", type="primary"):
            if not det.strip():
                st.error("Add a short description.")
            else:
                store.add_issue("reviewer report", cat, det.strip(), grp=grp,
                                field="" if fld == "(whole proposal)" else fld, fingerprint=fp,
                                reporter=reviewer, context={"title": p["title"], "site": p.get("site")})
                st.toast("Issue logged ✓")
                st.rerun()


# ------------------------------------------------------------------ data
store = get_store()
counts = store.counts()
last = store.last_run()
open_issues = store.list_issues("open")
overrides = store.list_overrides()

# ------------------------------------------------------------------ sidebar
with st.sidebar:
    st.markdown("**Reviewer**")
    reviewer = st.text_input("Reviewer", value=st.session_state.get("reviewer", os.getenv("REVIEWER", "")),
                             placeholder="Your name (written into CRM notes)", label_visibility="collapsed")
    st.session_state["reviewer"] = reviewer
    st.markdown("**Queue filters**")
    kinds = st.multiselect("Type", list(KIND_LABEL), default=list(KIND_LABEL), format_func=KIND_LABEL.get)
    query = st.text_input("Search", placeholder="Name, city, account id")
    st.divider()
    st.markdown("**Pipeline**")
    st.caption("Scrapes the website, reads the CRM and refreshes the queue. Never writes to the CRM.")
    if st.button("Run pipeline now", width="stretch"):
        from bellhaven_sync.pipeline import run
        with st.spinner("Scraping website and reading CRM…"):
            try:
                res = run()
                q = res["queue"]
                st.success(f"{res['proposals']} proposals · {q['new']} new · {q['already_decided']} already "
                           f"decided · {res['data_quality_flags']} data-quality flags")
            except Exception as e:  # noqa: BLE001
                st.error(f"Run failed: {e}")
                store.add_issue("pipeline", "Scraper: wrong or missing value", f"Pipeline run failed: {e}",
                                dedupe_key=f"runfail:{e}")
        st.rerun()
    st.divider()
    st.markdown(f'<div class="muted"><a href="{config.BASE_URL}/communities" target="_blank">Bellhaven website ↗</a>'
                f'<br><a href="{config.CRM_BROWSER}" target="_blank">CRM browser ↗</a></div>', unsafe_allow_html=True)

# ------------------------------------------------------------------ header
sub = "No pipeline run yet. Use “Run pipeline now” in the sidebar."
if last:
    site = last.get("site", {})
    sub = (f"Last run {last['at'].replace('T', ' ')[:16]} UTC · {site.get('locations', '?')} website communities · "
           f"{last.get('crm_accounts', '?')} CRM accounts")
st.markdown(f'<div class="app-head"><h1>Bellhaven CRM Sync</h1><div class="sub">{esc(sub)}</div></div>',
            unsafe_allow_html=True)

tab_q, tab_i, tab_o = st.tabs([f"Review queue ({counts.get('pending', 0)})",
                               f"Issue log ({len(open_issues)} open)",
                               f"Manual overrides ({len(overrides)})"])

# ================================================================== queue
with tab_q:
    m = st.columns(5)
    m[0].metric("Pending", counts.get("pending", 0))
    m[1].metric("Applied", counts.get("applied", 0))
    m[2].metric("Rejected", counts.get("rejected", 0))
    m[3].metric("Failed", counts.get("failed", 0))
    m[4].metric("Already correct", (last or {}).get("summary", {}).get("no_change", 0),
                help="Website locations whose CRM record needs no change")

    # Which decisions to show. Kept above the cards (not in the sidebar) so a reviewer can always find
    # what was already approved or rejected, like the open / done views of a CRM work queue.
    views = {"To review": ["pending", "failed"], "Applied": ["applied"], "Rejected": ["rejected"],
             "All": ["pending", "failed", "applied", "rejected"]}
    view = st.segmented_control(
        "Show", list(views), default="To review", key="queue-view", label_visibility="collapsed",
        format_func=lambda v: f"{v} ({sum(counts.get(s, 0) for s in views[v])})") or "To review"
    statuses = views[view]
    rows = [r for r in store.list(statuses) if r["kind"] in kinds]
    if query:
        ql = query.lower()
        rows = [r for r in rows if ql in (r["grp"] + r["title"] + str(r["payload"].get("account_ids"))
                                          + str(r["payload"].get("site") or "")).lower()]
    if not rows:
        st.info("Nothing to review with these filters.")

    groups: "OrderedDict[str, list[dict]]" = OrderedDict()
    for r in rows:
        groups.setdefault(r["grp"], []).append(r)

    for grp, items in groups.items():
        site = items[0]["payload"].get("site")
        where = f'{site["street"]}, {site["city"]}, {site["state"]} {site["zip"]}' if site else ""
        link = f'<a href="{site["url"]}" target="_blank">website listing ↗</a>' if site else ""
        st.markdown(f'<div class="grp">{esc(grp)}<span class="where">{esc(where)}</span>{link}</div>',
                    unsafe_allow_html=True)

        for r in items:
            p, fp, status = r["payload"], r["fingerprint"], r["status"]
            open_ = status in ("pending", "failed")
            with st.container(border=True):
                st.markdown(tag(KIND_LABEL.get(p["kind"], p["kind"]), KIND_STYLE.get(p["kind"], KIND_STYLE["update"]))
                            + (tag(status, STATUS_STYLE[status]) if status != "pending" else "")
                            + (tag("needs a closer look", ("#fdf6d8", "#5c4a10")) if p["confidence"] != "high" else "")
                            + f'<span class="ptitle">{esc(p["title"])}</span>', unsafe_allow_html=True)
                st.markdown(record_strip(p.get("record"), p["kind"]) if p["kind"] != "duplicate" else "",
                            unsafe_allow_html=True)
                for w in p.get("warnings", []):
                    st.markdown(f'<div class="warn">⚠ {esc(w)}</div>', unsafe_allow_html=True)

                # full-width comparison, like a CRM merge / clean screen
                edits = None
                if p["kind"] == "duplicate" and p.get("records"):
                    st.markdown('<div class="sec">Side by side</div>', unsafe_allow_html=True)
                    st.markdown(duplicate_table(p["records"]["keep"], p["records"]["retire"]),
                                unsafe_allow_html=True)
                else:
                    edits = field_editor(p, fp, disabled=not open_)
                if p["kind"] == "chow":
                    st.markdown(chow_old_account(p, (r.get("result") or {}).get("created_account")),
                                unsafe_allow_html=True)

                show_decisions = bool(p.get("decisions")) and p["kind"] != "chow"  # chow: the two steps say it
                left, right = st.columns([1, 1], gap="large")
                if show_decisions:
                    with left:
                        st.markdown('<div class="sec">What else happens</div>' + decisions_html(p["decisions"]),
                                    unsafe_allow_html=True)
                with (right if show_decisions else left):
                    st.markdown('<div class="sec">Why we think this is right</div>' + signals_html(p.get("signals", [])),
                                unsafe_allow_html=True)
                    if p.get("evidence"):
                        st.markdown('<ul class="ev">' + "".join(f"<li>{esc(e)}</li>" for e in p["evidence"]) + "</ul>",
                                    unsafe_allow_html=True)

                # ---------------- decision row
                if open_:
                    if status == "failed" and r.get("result"):
                        st.markdown(f'<div class="err">Last attempt failed: {esc(r["result"].get("error", ""))}</div>',
                                    unsafe_allow_html=True)
                    if (r.get("result") or {}).get("rolled_back_by"):
                        rb = r["result"]
                        st.markdown(f'<div class="muted">Approved earlier, then rolled back by {esc(rb["rolled_back_by"])} '
                                    f'· {rb["rolled_back_at"][:16].replace("T", " ")} UTC</div>', unsafe_allow_html=True)
                    if p["kind"] in ("create", "chow"):
                        st.markdown('<div class="warn">This creates a new CRM account and <b>can\'t be rolled back</b> '
                                    '(the API has no delete). Check the name and address before approving.</div>',
                                    unsafe_allow_html=True)
                    b1, b2, b3, _ = st.columns([1.3, 1, 1.3, 2.4])
                    label = ("Approve with edits" if edits else "Approve") if status == "pending" else "Retry"
                    if b1.button(label, key=f"ok-{fp}", type="primary", width="stretch"):
                        with st.spinner("Writing to CRM and verifying…"):
                            res = approve(store, fp, get_client(), who=reviewer or "reviewer", edits=edits)
                        if res["ok"]:
                            st.toast("Applied and verified ✓" + (f" · {res['corrections']} correction(s) remembered"
                                                                 if res.get("corrections") else "")
                                     + " · find it under Applied")
                            st.rerun()
                        else:
                            st.markdown(f'<div class="err">{esc(res["error"])}</div>', unsafe_allow_html=True)
                    with b2.popover("Reject", width="stretch"):
                        reason = st.radio("Why?", list(config.REJECT_REASONS), key=f"rr-{fp}")
                        comment = st.text_input("Comment (optional)", key=f"rw-{fp}")
                        if config.REJECT_REASONS.get(reason):
                            st.caption(f"This also files an issue: {config.REJECT_REASONS[reason]}")
                        if st.button("Confirm reject", key=f"no-{fp}"):
                            store.reject(fp, reviewer or "reviewer", reason, comment)
                            st.rerun()
                    with b3:
                        report_popover(store, p, fp, grp, reviewer, "q")
                elif status == "applied":
                    res = r.get("result") or {}
                    new = res.get("created_account")
                    parts = [f"Applied by {esc(r.get('decided_by') or '?')} · "
                             f"{(r.get('decided_at') or '')[:16].replace('T', ' ')} UTC · verified in CRM"]
                    if r.get("edits"):
                        parts.append("with reviewer corrections")
                    if new:
                        parts.append(f'<a href="{crm_url(new)}" target="_blank">Open new account ↗</a>')
                    for a in res.get("accounts_written", []):
                        if a != new:
                            parts.append(f'<a href="{crm_url(a)}" target="_blank">Open …{esc(a[-6:])} ↗</a>')
                    c1, c2 = st.columns([5, 1])
                    c1.markdown(f'<div class="muted">{" · ".join(parts)}</div>', unsafe_allow_html=True)
                    if res.get("reversible") and res.get("undo"):
                        with c2.popover("Roll back", width="stretch"):
                            st.markdown("Put back the values this approval overwrote"
                                        + (" and move the contacts back" if any(s["op"] == "update_contact"
                                                                                for s in res["undo"]) else "")
                                        + ". Refused if anyone edited these fields since. "
                                          "The proposal returns to the queue.")
                            if st.button("Confirm roll back", key=f"rb-{fp}"):
                                out = revert(store, fp, get_client(), who=reviewer or "reviewer")
                                if out["ok"]:
                                    st.toast("Rolled back ✓")
                                    st.rerun()
                                else:
                                    st.markdown(f'<div class="err">{esc(out["error"])}</div>', unsafe_allow_html=True)
                    elif new:
                        c2.markdown('<div class="muted">Can\'t be rolled back</div>', unsafe_allow_html=True)
                elif status == "rejected":
                    c1, c2 = st.columns([5, 1])
                    c1.markdown(f'<div class="muted">Rejected by {esc(r.get("decided_by") or "?")}'
                                f'{": " + esc(r["reason"]) if r.get("reason") else ""}</div>', unsafe_allow_html=True)
                    if c2.button("Reopen", key=f"re-{fp}", width="stretch"):
                        store.reopen(fp)
                        st.rerun()

# ================================================================== issues
with tab_i:
    st.markdown('<div class="hint">Everything that should make the system better later: problems you report, '
                'rejections that point at the scraper or matching, your manual corrections, and automatic '
                f'data-quality flags. Also saved to <code>data/issue_log.csv</code>.</div>', unsafe_allow_html=True)
    all_issues = store.list_issues()
    c1, c2, c3 = st.columns([1, 1, 2])
    show = c1.selectbox("Show", ["open", "resolved", "all"], key="iss-show")
    cats = c2.multiselect("Category", config.ISSUE_CATEGORIES, key="iss-cat")
    with c3:
        with st.popover("Log a general problem"):
            cat = st.selectbox("Category", config.ISSUE_CATEGORIES, key="g-cat")
            det = st.text_area("Details", key="g-det")
            if st.button("Log issue", key="g-go", type="primary") and det.strip():
                store.add_issue("reviewer report", cat, det.strip(), reporter=reviewer)
                st.rerun()
    view = [i for i in all_issues if (show == "all" or i["status"] == show) and (not cats or i["category"] in cats)]
    if view:
        st.dataframe(pd.DataFrame([{
            "#": i["id"], "Logged": i["created_at"][:16].replace("T", " "), "Status": i["status"],
            "Category": i["category"], "Location": i["grp"] or "", "Field": i["field"] or "",
            "Details": i["detail"], "From": i["source"], "By": i["reporter"] or "",
            "Resolution": i["resolution"] or ""} for i in view]),
            hide_index=True, width="stretch")
    else:
        st.info("No issues with these filters.")
    if open_issues:
        st.markdown('<div class="sec">Resolve an issue</div>', unsafe_allow_html=True)
        r1, r2, r3 = st.columns([1, 3, 1])
        pick = r1.selectbox("Issue #", [i["id"] for i in open_issues], key="res-id")
        note = r2.text_input("Resolution", key="res-note", placeholder="e.g. fixed ZIP parsing in scraper.py")
        if r3.button("Mark resolved", key="res-go", width="stretch"):
            store.resolve_issue(pick, note or "resolved")
            st.rerun()
    if all_issues:
        st.download_button("Download issue log (CSV)", store.export_issues().read_bytes(),
                           file_name="issue_log.csv", mime="text/csv")

# ================================================================== overrides
with tab_o:
    st.markdown('<div class="hint">Corrections you made while approving. The pipeline uses your value instead of the '
                "website's for that field, until the website itself changes. Remove one to go back to the website "
                "value on the next run.</div>", unsafe_allow_html=True)
    if overrides:
        st.dataframe(pd.DataFrame([{
            "Location": o["okey"], "Field": config.FIELD_LABELS.get(o["field"], o["field"]),
            "Website said": o["site_value"], "Use instead": o["value"] or "(keep CRM value)",
            "Type": o["kind"], "By": o["created_by"], "When": (o["created_at"] or "")[:16].replace("T", " ")}
            for o in overrides]), hide_index=True, width="stretch")
        o1, o2 = st.columns([3, 1])
        choice = o1.selectbox("Override", [f'{o["okey"]} · {o["field"]}' for o in overrides], key="ov-pick")
        if o2.button("Remove override", key="ov-go", width="stretch"):
            okey, field = choice.split(" · ")
            store.remove_override(okey, field)
            st.rerun()
    else:
        st.info("No manual overrides yet.")
