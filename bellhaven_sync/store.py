"""SQLite review queue + audit log.

Statuses:
  pending    -> waiting for a reviewer
  applying   -> claimed by an approve click (guards against double clicks)
  applied    -> written to the CRM and verified
  failed     -> approve tried, write failed (error in `result`); can be retried
  rejected   -> reviewer said no; never re-proposed unless the facts change
  superseded -> was pending, but the latest run no longer produces it
                (the CRM changed by other means); hidden from the queue

Re-run rule: a fingerprint already in the table is never re-inserted. Only
`pending`/`superseded` rows are refreshed by a new run.
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS proposals (
  fingerprint TEXT PRIMARY KEY,
  kind TEXT NOT NULL,
  grp TEXT NOT NULL,
  title TEXT NOT NULL,
  confidence TEXT,
  payload TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending',
  first_seen TEXT NOT NULL,
  last_seen TEXT NOT NULL,
  decided_at TEXT,
  decided_by TEXT,
  reason TEXT,
  result TEXT
);
CREATE TABLE IF NOT EXISTS runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  at TEXT NOT NULL,
  summary TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS audit (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  at TEXT NOT NULL,
  fingerprint TEXT,
  op TEXT NOT NULL,
  target TEXT,
  request TEXT,
  response TEXT,
  ok INTEGER NOT NULL
);
-- A reviewer's manual correction. The pipeline uses `value` instead of the
-- website's value for this field, as long as the website still says
-- `site_value`. If the website changes, the override stops applying.
CREATE TABLE IF NOT EXISTS overrides (
  okey TEXT NOT NULL,
  field TEXT NOT NULL,
  site_value TEXT,
  value TEXT,
  kind TEXT,
  created_by TEXT,
  created_at TEXT,
  fingerprint TEXT,
  PRIMARY KEY (okey, field)
);
-- Anything that should make the system better later: reviewer reports,
-- rejections that point at a scraper/matching problem, manual corrections,
-- and automatic data-quality flags. Mirrored to data/issue_log.csv.
CREATE TABLE IF NOT EXISTS issues (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  created_at TEXT NOT NULL,
  source TEXT NOT NULL,
  category TEXT NOT NULL,
  grp TEXT,
  field TEXT,
  detail TEXT NOT NULL,
  fingerprint TEXT,
  reporter TEXT,
  context TEXT,
  status TEXT NOT NULL DEFAULT 'open',
  resolution TEXT,
  resolved_at TEXT,
  dedupe_key TEXT UNIQUE
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Store:
    def __init__(self, path: Path | str | None = None):
        self.path = Path(path or config.DB_PATH)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as c:
            c.executescript(SCHEMA)
            cols = {r["name"] for r in c.execute("PRAGMA table_info(proposals)")}
            if "edits" not in cols:   # migrate DBs created before edits existed
                c.execute("ALTER TABLE proposals ADD COLUMN edits TEXT")

    @contextmanager
    def _conn(self):
        c = sqlite3.connect(self.path)
        c.row_factory = sqlite3.Row
        try:
            yield c
            c.commit()
        finally:
            c.close()

    # ---------------------------------------------------------------- runs
    def sync(self, proposals, run_summary: dict) -> dict:
        now = _now()
        counts = {"new": 0, "refreshed": 0, "already_decided": 0, "superseded": 0}
        current = set()
        with self._conn() as c:
            for p in proposals:
                d = p.to_dict() if hasattr(p, "to_dict") else p
                fp = d["fingerprint"]
                current.add(fp)
                row = c.execute("SELECT status FROM proposals WHERE fingerprint=?", (fp,)).fetchone()
                if row is None:
                    c.execute("INSERT INTO proposals (fingerprint, kind, grp, title, confidence, payload, "
                              "status, first_seen, last_seen) VALUES (?,?,?,?,?,?,?,?,?)",
                              (fp, d["kind"], d["group"], d["title"], d["confidence"],
                               json.dumps(d, ensure_ascii=False), "pending", now, now))
                    counts["new"] += 1
                elif row["status"] in ("pending", "superseded"):
                    # refresh evidence/snapshot from the latest data, keep it in the queue
                    c.execute("UPDATE proposals SET payload=?, title=?, confidence=?, status='pending', "
                              "last_seen=? WHERE fingerprint=?",
                              (json.dumps(d, ensure_ascii=False), d["title"], d["confidence"], now, fp))
                    counts["refreshed"] += 1
                else:
                    c.execute("UPDATE proposals SET last_seen=? WHERE fingerprint=?", (now, fp))
                    counts["already_decided"] += 1
            stale = [r["fingerprint"] for r in c.execute(
                "SELECT fingerprint FROM proposals WHERE status='pending'") if r["fingerprint"] not in current]
            for fp in stale:
                c.execute("UPDATE proposals SET status='superseded', last_seen=? WHERE fingerprint=?", (now, fp))
            counts["superseded"] = len(stale)
            c.execute("INSERT INTO runs (at, summary) VALUES (?,?)",
                      (now, json.dumps(run_summary | {"queue": counts}, ensure_ascii=False, default=str)))
        return counts

    def last_run(self) -> dict | None:
        with self._conn() as c:
            r = c.execute("SELECT at, summary FROM runs ORDER BY id DESC LIMIT 1").fetchone()
        return {"at": r["at"], **json.loads(r["summary"])} if r else None

    # ---------------------------------------------------------------- queue
    def list(self, statuses: list[str] | None = None) -> list[dict]:
        q, args = "SELECT * FROM proposals", []
        if statuses:
            q += f" WHERE status IN ({','.join('?' * len(statuses))})"
            args = statuses
        q += " ORDER BY grp COLLATE NOCASE, kind"
        with self._conn() as c:
            rows = c.execute(q, args).fetchall()
        return [self._row(r) for r in rows]

    def get(self, fp: str) -> dict | None:
        with self._conn() as c:
            r = c.execute("SELECT * FROM proposals WHERE fingerprint=?", (fp,)).fetchone()
        return self._row(r) if r else None

    def counts(self) -> dict:
        with self._conn() as c:
            return {r["status"]: r["n"] for r in
                    c.execute("SELECT status, COUNT(*) n FROM proposals GROUP BY status")}

    @staticmethod
    def _row(r) -> dict:
        d = dict(r)
        d["payload"] = json.loads(d["payload"])
        d["result"] = json.loads(d["result"]) if d.get("result") else None
        d["edits"] = json.loads(d["edits"]) if d.get("edits") else None
        return d

    # ---------------------------------------------------------------- decisions
    def claim(self, fp: str, who: str) -> bool:
        """Atomically move pending/failed -> applying. False if someone else got it."""
        with self._conn() as c:
            cur = c.execute("UPDATE proposals SET status='applying', decided_by=?, decided_at=? "
                            "WHERE fingerprint=? AND status IN ('pending','failed')", (who, _now(), fp))
            return cur.rowcount == 1

    def finish(self, fp: str, ok: bool, result: dict, edits: dict | None = None) -> None:
        with self._conn() as c:
            c.execute("UPDATE proposals SET status=?, result=?, decided_at=?, edits=? WHERE fingerprint=?",
                      ("applied" if ok else "failed", json.dumps(result, ensure_ascii=False, default=str),
                       _now(), json.dumps(edits, ensure_ascii=False) if edits else None, fp))

    def reject(self, fp: str, who: str, reason: str, comment: str = "") -> bool:
        """Reject. If the reason points at a scraper/matching problem, file an issue too."""
        text = reason + (f": {comment}" if comment else "")
        with self._conn() as c:
            cur = c.execute("UPDATE proposals SET status='rejected', decided_by=?, decided_at=?, reason=? "
                            "WHERE fingerprint=? AND status IN ('pending','failed')", (who, _now(), text, fp))
            ok = cur.rowcount == 1
        category = config.REJECT_REASONS.get(reason, "Other") if reason in config.REJECT_REASONS else None
        if ok and category:
            p = self.get(fp)
            self.add_issue("rejection", category, f"Rejected: {text}", grp=p["grp"], fingerprint=fp,
                           reporter=who, context={"title": p["title"], "site": p["payload"].get("site")})
        return ok

    def reopen(self, fp: str) -> bool:
        """Undo a rejection (reviewer changed their mind). Applied changes can't be reopened."""
        with self._conn() as c:
            cur = c.execute("UPDATE proposals SET status='pending', decided_by=NULL, decided_at=NULL, reason=NULL "
                            "WHERE fingerprint=? AND status='rejected'", (fp,))
            return cur.rowcount == 1

    # ---------------------------------------------------------------- audit
    def audit(self, fp: str, op: str, target: str, request, response, ok: bool) -> None:
        with self._conn() as c:
            c.execute("INSERT INTO audit (at, fingerprint, op, target, request, response, ok) VALUES (?,?,?,?,?,?,?)",
                      (_now(), fp, op, target, json.dumps(request, ensure_ascii=False, default=str),
                       json.dumps(response, ensure_ascii=False, default=str), int(ok)))

    def audit_for(self, fp: str) -> list[dict]:
        with self._conn() as c:
            return [dict(r) for r in c.execute("SELECT * FROM audit WHERE fingerprint=? ORDER BY id", (fp,))]

    # ---------------------------------------------------------------- overrides
    def overrides(self) -> dict:
        """{(okey, field): {...}} as the matcher expects."""
        with self._conn() as c:
            return {(r["okey"], r["field"]): dict(r) for r in c.execute("SELECT * FROM overrides")}

    def list_overrides(self) -> list[dict]:
        with self._conn() as c:
            return [dict(r) for r in c.execute("SELECT * FROM overrides ORDER BY created_at DESC")]

    def set_override(self, okey: str, field: str, site_value: str, value: str, kind: str, who: str, fp: str):
        with self._conn() as c:
            c.execute("INSERT OR REPLACE INTO overrides VALUES (?,?,?,?,?,?,?,?)",
                      (okey, field, site_value, value, kind, who, _now(), fp))

    def remove_override(self, okey: str, field: str) -> None:
        with self._conn() as c:
            c.execute("DELETE FROM overrides WHERE okey=? AND field=?", (okey, field))

    def remove_overrides_from(self, fp: str) -> None:
        with self._conn() as c:
            c.execute("DELETE FROM overrides WHERE fingerprint=?", (fp,))

    # ---------------------------------------------------------------- issues
    def add_issue(self, source: str, category: str, detail: str, *, grp: str = "", field: str = "",
                  fingerprint: str = "", reporter: str = "", context: dict | None = None,
                  dedupe_key: str | None = None) -> bool:
        """File an issue. Auto-generated issues pass a dedupe_key so daily runs don't repeat them."""
        with self._conn() as c:
            cur = c.execute(
                "INSERT OR IGNORE INTO issues (created_at, source, category, grp, field, detail, fingerprint, "
                "reporter, context, dedupe_key) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (_now(), source, category, grp, field, detail, fingerprint, reporter,
                 json.dumps(context or {}, ensure_ascii=False, default=str), dedupe_key))
            added = cur.rowcount == 1
        if added:
            self.export_issues()
        return added

    def list_issues(self, status: str | None = None) -> list[dict]:
        q, args = "SELECT * FROM issues", []
        if status:
            q, args = q + " WHERE status=?", [status]
        with self._conn() as c:
            return [dict(r) for r in c.execute(q + " ORDER BY id DESC", args)]

    def resolve_issue(self, issue_id: int, resolution: str) -> None:
        with self._conn() as c:
            c.execute("UPDATE issues SET status='resolved', resolution=?, resolved_at=? WHERE id=?",
                      (resolution, _now(), issue_id))
        self.export_issues()

    def export_issues(self, path: Path | None = None) -> Path:
        import csv
        path = Path(path or config.ISSUE_EXPORT)
        path.parent.mkdir(parents=True, exist_ok=True)
        cols = ["id", "created_at", "status", "category", "source", "grp", "field", "detail",
                "reporter", "resolution", "resolved_at", "fingerprint"]
        with open(path, "w", newline="", encoding="utf-8-sig") as f:   # BOM so Excel reads UTF-8
            w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
            w.writeheader()
            w.writerows(self.list_issues())
        return path


def approve(store: Store, fp: str, client, who: str = "reviewer", edits: dict | None = None) -> dict:
    """Approve = (apply edits) -> claim -> write -> verify -> record.
    The only path that writes to the CRM."""
    from .apply import InvalidEdit, apply_proposal, with_edits
    row = store.get(fp)
    if not row:
        return {"ok": False, "error": "Unknown proposal."}
    try:
        payload, corrections = with_edits(row["payload"], edits, who)
    except InvalidEdit as e:
        return {"ok": False, "error": str(e)}
    if not store.claim(fp, who):
        return {"ok": False, "error": "Already being applied or already decided."}
    try:
        res = apply_proposal(payload, client,
                             audit=lambda op, tgt, req, resp, ok: store.audit(fp, op, tgt, req, resp, ok))
    except Exception as e:  # noqa: BLE001 - surface any failure to the reviewer
        store.finish(fp, False, {"error": f"{type(e).__name__}: {e}"}, edits)
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    store.finish(fp, True, res, edits)
    okey = row["payload"].get("override_key") or ""
    for c in corrections:
        r = c["row"]
        if okey:
            store.set_override(okey, r["field"], r["site_value"], str(c["value"]), c["kind"], who, fp)
        what = (f"kept CRM value '{r['crm']}' instead of website '{r['site']}'" if c["kind"] == "excluded"
                else f"saved '{c['value']}' instead of website '{r['site']}'")
        store.add_issue("reviewer edit", "Reviewer correction", f"{r['label']}: {what}", grp=row["grp"],
                        field=r["field"], fingerprint=fp, reporter=who,
                        context={"site": row["payload"].get("site"), "crm_before": r["crm"]})
    return {"ok": True, **res, "corrections": len(corrections)}


def revert(store: Store, fp: str, client, who: str = "reviewer") -> dict:
    """Roll back an applied proposal from its saved snapshot, then put it back
    in the queue so it can be decided again. Corrections made on that approval
    are dropped too, since the change they belonged to is gone."""
    from .apply import revert_proposal
    with store._conn() as c:   # claim: applied -> reverting (guards double clicks)
        claimed = c.execute("UPDATE proposals SET status='reverting' WHERE fingerprint=? AND status='applied'",
                            (fp,)).rowcount == 1
    if not claimed:
        return {"ok": False, "error": "Only applied changes can be rolled back."}
    row = store.get(fp)
    try:
        revert_proposal(row["result"], client,
                        audit=lambda op, tgt, req, resp, ok: store.audit(fp, op, tgt, req, resp, ok))
    except Exception as e:  # noqa: BLE001
        with store._conn() as c:
            c.execute("UPDATE proposals SET status='applied' WHERE fingerprint=?", (fp,))
        return {"ok": False, "error": str(e)}
    note = {"rolled_back_by": who, "rolled_back_at": _now(), "previous_result": row["result"]}
    with store._conn() as c:
        c.execute("UPDATE proposals SET status='pending', decided_by=NULL, decided_at=NULL, edits=NULL, "
                  "result=? WHERE fingerprint=?", (json.dumps(note, ensure_ascii=False, default=str), fp))
    store.remove_overrides_from(fp)
    return {"ok": True}
