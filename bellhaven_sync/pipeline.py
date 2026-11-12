"""Daily job: scrape -> read CRM -> match -> queue proposals.

This never writes to the CRM. Writes happen only from the review app, one
approved proposal at a time (see apply.py).

Safe to run on a schedule:
- a lock file stops two runs overlapping (cron + the app's "Run pipeline now")
- decided items are never re-queued (fingerprints, see store.py)
- a guard refuses to queue an implausible number of "no longer on website"
  proposals, which is what a broken scraper or a site redesign would produce
- exits non-zero on failure, so cron mail / GitHub Actions alerts fire
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from contextlib import contextmanager
from dataclasses import asdict
from datetime import datetime, timezone

from . import config
from .crm import CRMClient
from .matching import build_proposals
from .scraper import scrape


class RunInProgress(RuntimeError):
    pass


class SuspiciousRun(RuntimeError):
    pass


@contextmanager
def run_lock(path=None, stale_after: int = config.LOCK_STALE_SECONDS):
    """Exclusive lock via O_EXCL file create. A lock older than `stale_after`
    is assumed to be from a crashed run and is taken over."""
    path = path or config.DATA_DIR / "pipeline.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        age = time.time() - path.stat().st_mtime
        if age < stale_after:
            raise RunInProgress(f"Another pipeline run started {int(age)}s ago ({path.read_text().strip()}).")
        path.unlink(missing_ok=True)
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    os.write(fd, f"pid {os.getpid()} at {datetime.now(timezone.utc).isoformat(timespec='seconds')}".encode())
    os.close(fd)
    try:
        yield
    finally:
        path.unlink(missing_ok=True)


def collect(use_cached_site: bool = False):
    if use_cached_site and (config.DATA_DIR / "site_locations.json").exists():
        payload = json.loads((config.DATA_DIR / "site_locations.json").read_text(encoding="utf-8"))
        sites, meta = payload["locations"], payload["meta"]
    else:
        locs, meta = scrape()
        sites = [asdict(l) for l in locs]
        config.DATA_DIR.mkdir(exist_ok=True)
        (config.DATA_DIR / "site_locations.json").write_text(
            json.dumps({"meta": meta, "locations": sites}, indent=2, ensure_ascii=False), encoding="utf-8")
    crm = CRMClient()
    return sites, meta, crm.list_accounts(), crm.list_contacts()


def run(dry_run: bool = False, use_cached_site: bool = False) -> dict:
    if dry_run:
        return _run(dry_run, use_cached_site)
    with run_lock():
        return _run(dry_run, use_cached_site)


def _check_plausible(proposals, accounts) -> None:
    """A real week rarely removes more than a few communities. Many 'stale'
    proposals at once means the scraper missed pages, not that the operator
    sold a quarter of its portfolio overnight."""
    parent = next((a for a in accounts if a.get("name") == config.PARENT_ACCOUNT_NAME), {})
    under = sum(1 for a in accounts if parent and a.get("parent_id") == parent.get("account_id"))
    stale = sum(1 for p in proposals if p.kind == "stale")
    if under and stale >= config.STALE_GUARD_MIN and stale > config.STALE_GUARD_SHARE * under:
        raise SuspiciousRun(f"{stale} of {under} Bellhaven accounts would be flagged as no longer on the website "
                            f"(limit {config.STALE_GUARD_SHARE:.0%}). Nothing was queued; check the scraper.")


def _run(dry_run: bool, use_cached_site: bool) -> dict:
    from .store import Store
    store = Store()
    sites, meta, accounts, contacts = collect(use_cached_site)
    proposals, summary = build_proposals(sites, accounts, contacts, overrides=store.overrides())
    dq = summary.pop("data_quality", {})
    result = {"site": meta, "crm_accounts": len(accounts), "proposals": len(proposals),
              "summary": {k: len(v) for k, v in summary.items()},
              "data_quality_flags": sum(len(v) for v in dq.values())}
    if dry_run:
        result["details"] = summary
        result["data_quality"] = dq
        result["preview"] = [p.to_dict() for p in proposals]
        return result
    try:
        _check_plausible(proposals, accounts)
    except SuspiciousRun as e:
        store.add_issue("pipeline guard", "Scraper: wrong or missing value", str(e),
                        dedupe_key=f"guard:{datetime.now(timezone.utc).date()}")
        raise

    # file data-quality flags once each (dedupe_key), so the scraper can be improved later
    by_slug = {s["slug"]: s for s in sites}
    for slug, flags in dq.items():
        for f in flags:
            store.add_issue("data-quality check", "Data-quality check", f["message"],
                            grp=by_slug[slug]["name"], field=f["field"],
                            context={"site": by_slug[slug], "code": f["code"]},
                            dedupe_key=f"dq:{slug}:{f['code']}:{f['message']}")
    if not meta.get("count_matches_claim", True):
        store.add_issue("data-quality check", "Scraper: wrong or missing value",
                        f"Scraped {meta['locations']} communities but the site claims {meta['site_claims']}.",
                        dedupe_key=f"count:{meta['locations']}:{meta['site_claims']}")
    result["queue"] = store.sync(proposals, result)
    result["pending_total"] = store.counts().get("pending", 0)
    _write_step_summary(result)
    return result


def _write_step_summary(result: dict) -> None:
    """On GitHub Actions, show the run's outcome on the workflow page."""
    path = os.getenv("GITHUB_STEP_SUMMARY")
    if not path:
        return
    q, s = result.get("queue", {}), result["summary"]
    lines = [
        "## Bellhaven CRM sync",
        f"- Website: **{result['site']['locations']}** communities (site claims {result['site'].get('site_claims')})",
        f"- CRM accounts read: **{result['crm_accounts']}**",
        f"- Proposals this run: **{result['proposals']}** · new to the queue: **{q.get('new', 0)}** · "
        f"already decided (skipped): **{q.get('already_decided', 0)}** · superseded: **{q.get('superseded', 0)}**",
        f"- Waiting for review: **{result.get('pending_total', 0)}**",
        f"- Data-quality flags: **{result['data_quality_flags']}**",
        "", "| Type | Count |", "|---|---|", *[f"| {k} | {v} |" for k, v in sorted(s.items())],
    ]
    with open(path, "a", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def main(argv=None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(prog="bellhaven_sync")
    ap.add_argument("command", choices=["run", "scrape"])
    ap.add_argument("--dry-run", action="store_true", help="print proposals, don't queue them")
    ap.add_argument("--cached-site", action="store_true", help="reuse data/site_locations.json")
    ap.add_argument("--json", action="store_true", help="print full JSON")
    args = ap.parse_args(argv)

    if args.command == "scrape":
        from .scraper import main as scrape_main
        return scrape_main()

    try:
        result = run(dry_run=args.dry_run, use_cached_site=args.cached_site)
    except RunInProgress as e:
        print(f"Skipped: {e}", file=sys.stderr)
        return 0                      # not a failure: the other run will do the work
    except SuspiciousRun as e:
        print(f"STOPPED: {e}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0
    print(f"Website: {result['site']['locations']} communities | CRM: {result['crm_accounts']} accounts")
    print("Proposals:", result["proposals"], result["summary"])
    if "queue" in result:
        print("Queue:", result["queue"])
    if args.dry_run:
        for p in result["preview"]:
            print(f"\n[{p['kind']:9}] {p['title']}  ({p['confidence']}, {p['fingerprint']})")
            for r in p["rows"]:
                if r["changed"]:
                    print(f"     {r['label']:14} CRM {str(r['crm'])[:32]!r:36} website {str(r['site'])[:32]!r:36}"
                          f" -> save {str(r['value'])[:40]!r}")
            for d in p["decisions"]:
                print(f"     * {d['label']}: {d['from'] or '-'} -> {d['to']}  ({d['why']})")
            for s in p["signals"]:
                print(f"     {'+' if s['ok'] else '-'} {s['label']}" + (f" ({s['detail']})" if s.get("detail") else ""))
            for e in p["evidence"]:
                print("     ·", e)
            for w in p["warnings"]:
                print("     !", w)
    return 0


if __name__ == "__main__":
    sys.exit(main())
