# Bellhaven CRM Sync

Keeps the CRM's parent-company links for **Bellhaven Senior Living** in line with Bellhaven's own website.

Senior-care facilities get bought, renamed and merged all the time, so CRM records go stale: a facility
sits under its old owner, the same building shows up three times, or a new location is missing. This
project scrapes Bellhaven's website every day, compares it with the CRM, and puts each suggested fix in a
review queue. **Nothing is written to the CRM until a person approves it.**

```
Bellhaven website ──scrape──▶ 35 locations ─┐
                                            ├─▶ match + compare ─▶ proposals ─▶ review queue (SQLite)
CRM API ───────────read─────▶ accounts,  ───┘                                       │
                              contacts                                     reviewer approves
                                                                                    │
CRM API ◀──────────────── write, then re-read to verify ◀────────────────────────────┘
```

Short writeup (approach, AI use, next steps): [WRITEUP.md](WRITEUP.md)

## Result on the assessment CRM

35 website locations, about 120 CRM accounts, 29 fixes:

| Outcome | Count | Examples |
|---|---:|---|
| Already correct | 12 locations | Wooster, Akron, Toledo |
| Field update (renamed or wrong address) | 9 | Riverbend Manor → Bellhaven of Chagrin Falls; Portsmouth ZIP typo; Ashtabula PO Box → street |
| Move to Bellhaven (no open billing) | 4 | Lima (from Harborview), Findlay (had no parent) |
| Change of ownership (billing SOP) | 2 | Tiffin, Marietta |
| New account | 4 | Batavia OH, Carlisle PA (not New Carlisle OH) |
| Duplicate retired | 7 accounts at 5 locations | Kettering ×2, Monroe ×2, Port Clinton, Erie, Owosso |
| Not on website → Needs Review | 3 | Alliance, Coldwater, Sandusky |

All 29 fixes were approved through the app with 0 failed writes. Afterwards, every one of the 35 locations
has exactly one Active account under Bellhaven whose name, address and care type match the website. Look-alikes
owned by other operators (Amberly Manor in Colorado, Union Square Senior Living) were left untouched. A fresh
run now finds **0 changes**.

## Quick start

```bash
python -m pip install -r requirements.txt
cp .env.example .env                       # then put your CRM_TOKEN in .env
python -m pytest -q                        # 63 offline tests, never touch the real CRM
python -m bellhaven_sync run --dry-run     # scrape + match, print proposals, queue nothing
python -m bellhaven_sync run               # queue proposals for review (still no CRM writes)
python -m streamlit run app.py             # review app at http://localhost:8501
```

Requires Python 3.10+.

## How it works

### 1. Scrape (`bellhaven_sync/scraper.py`)
- Crawls every internal page and collects every `/communities/<slug>` link, instead of trusting the
  directory alone. The directory says "34 communities listed" but the homepage says 35; the 35th (Findlay)
  is only linked from a homepage announcement.
- **Stops if it scrapes fewer than 80% of the count the site claims.** A broken scrape must never look like
  "half the locations closed."
- Fails loudly on a page it can't parse instead of guessing.

### 2. Match (`bellhaven_sync/matching.py`, `normalize.py`)
**Address first, name second.** A building doesn't move when it's sold, but its name usually changes.

| Signal | Points |
|---|---:|
| Same street number + street, same city and ZIP | 60 |
| Same street, city *or* ZIP differs (typo) | 50 |
| Same number, street name nearly equal | 40 |
| CRM has a PO Box in the same city | 25 |
| Name similarity (brand word handled separately) | up to 20 |
| Same phone | 15 |
| Website administrator is a contact on the account | 15 |

50 or more means the same facility. Addresses are normalized first (`West Main Street` = `W Main St`). The
brand word alone never counts as a name match ("Bellhaven of X" vs "Bellhaven of Y"). Near misses are shown
on the card as "checked and rejected," so the reviewer can see why a new account is proposed.

**Duplicates:** when several accounts match one location, one is kept. The order is: already under
Bellhaven, then has billing history, then has contacts, then phone match, then highest score, then lowest
id. The others get `duplicate_of_account` and status Inactive, and their contacts move to the kept account.
The API has no delete or merge.

**No longer on the website:** status becomes **Needs Review**, not Inactive. A missing web page isn't proof
that a facility closed, and the parent and billing fields stay as they are.

### 3. Billing SOP (`billing_requires_chow` in `matching.py`)
Before re-parenting, check two fields:
- `lifetime_revenue > 0` **and** `outstanding_ar > 0`: don't touch the old account. Create a new account
  under Bellhaven and set the old account's `chow_current_account` to it.
- Otherwise: change `parent_id` directly.

The rule is one small function with its own tests, so a policy change is a one-line edit.

### 4. Review app (`app.py`)
Each card follows the "compare and update" screen of big CRMs (Salesforce merge, HubSpot merge). There is one
row per field: **In CRM now | On website | Save as**. The card also shows the account's parent, status,
revenue and open AR, plus a ✓/✕ list of why we think it's the same facility.

- **Approve** is the only code path that writes to the CRM.
- **Fix before approving:** if the website is wrong, untick a field or edit *Save as*. The correction is
  remembered (overrides) until the website value itself changes, and it is logged.
- **Reject with a reason:** reasons that point at a bug ("website data is wrong", "wrong match") file an
  entry in the **Issue log** (also exported to `data/issue_log.csv`).
- **Roll back** restores the overwritten values of an approved update. Creates can't be rolled back because
  the API has no delete, so the app warns before approving one.
- Views: To review / Applied / Rejected / All.

### 5. Safe writes (`bellhaven_sync/apply.py`, `store.py`)
1. **Stale check:** re-read each account first. If a field changed since the proposal was made, stop.
2. **Notes are appended** to the live note with a dated `[bellhaven-sync]` line, never overwritten.
3. **Creates are idempotent:** each new account carries a marker in its note. A retry after a partial failure
   reuses the account instead of creating a second one.
4. **Verify after writing:** re-read and compare. A mismatch is reported as a failure.
5. **No double-clicks:** a proposal is claimed before it runs, and every request and response goes to an audit table.

### 6. Daily schedule (`.github/workflows/daily-sync.yml`, `deploy/crontab`)
Runs at 6 am ET, before the sales day. The daily job **only reads** the CRM and queues proposals.
- **Safe to re-run:** each proposal has a fingerprint (type + accounts + target values). Anything already
  approved or rejected is never queued again, and a proposal whose CRM record already looks right isn't made
  at all. A test runs the pipeline twice and asserts nothing new is queued and nothing is written.
- **No overlapping runs:** a lock file, taken over if older than 1 hour (crashed run). Actions also has a
  concurrency group.
- **Guard against a bad scrape:** if more than 25% of Bellhaven accounts would be flagged "not on website,"
  the run stops and files an issue instead.
- Exit codes: 0 ok (or another run in progress), 1 error, 2 stopped by the guard.
- The Actions job keeps the queue in the Actions cache and skips the sync if the `CRM_TOKEN` secret isn't
  set. In production the queue would live in a shared database used by both the job and the app.

## Configuration

Everything a sales-ops analyst might want to tune is in `bellhaven_sync/config.py`:
- the match points and threshold
- the care-type mapping (website label → CRM picklist)
- street abbreviations
- which fields are synced
- reject reasons and issue categories
- the stale-guard limits

Environment variables: `CRM_TOKEN` (required), `BASE_URL`, `REVIEWER`, `REVIEW_DB`.

## Tests

```bash
python -m pytest -q
```

63 tests, all offline: a fake CRM plus saved copies of the website pages. They cover:
- scraper parsing and the 80% guard
- address and name normalization
- the billing SOP
- end-to-end classification of the full CRM snapshot (the counts in the table above)
- approve-all then re-run gives 0 proposals
- rejected items never come back
- no writes without approval
- stale proposals are refused
- notes are appended
- a retried change of ownership creates one account
- double-click protection
- reviewer edits and overrides
- rollback
- the run lock and the stale-run guard

## Project layout

```
app.py                      review app (Streamlit)
bellhaven_sync/
  scraper.py                crawl the website, parse each community page
  normalize.py              addresses, names, phones
  validate.py               data-quality checks on scraped values
  matching.py               scoring, duplicates, billing SOP, proposals
  apply.py                  safe writes, reviewer edits, rollback
  store.py                  SQLite queue, overrides, issue log, audit
  crm.py                    CRM API client
  pipeline.py               daily job: python -m bellhaven_sync run [--dry-run]
  config.py                 every tunable setting
tests/                      offline tests and fixtures
.github/workflows/          tests on every push, daily sync
deploy/crontab              cron / Windows Task Scheduler equivalent
```
