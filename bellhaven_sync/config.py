"""All tunable settings live here, so changes during review don't touch logic."""
import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

BASE_URL = os.getenv("BASE_URL", "https://analyst-assessment-production.up.railway.app").rstrip("/")
CRM_API = f"{BASE_URL}/api/v1"
CRM_TOKEN = os.getenv("CRM_TOKEN", "")

DATA_DIR = ROOT / "data"

# --- Scraper ---
USER_AGENT = "bellhaven-sync/0.1 (CRM hygiene job)"
HTTP_TIMEOUT = 20          # seconds per request
HTTP_RETRIES = 3           # attempts per URL before failing the run
REQUEST_DELAY = 0.2        # seconds between requests, to be polite to the site
MAX_PAGES = 300            # hard stop for the crawler

# Guardrail: if we scrape fewer than this share of the count the site itself
# claims ("35 communities"), stop instead of proposing mass "no longer listed".
MIN_SCRAPE_RATIO = 0.8

# --- CRM ---
CRM_PAGE_SIZE = 50
CRM_BROWSER = f"{BASE_URL}/crm/{CRM_TOKEN}/accounts"
PARENT_ACCOUNT_NAME = "Bellhaven Senior Living (Parent Account)"
# Review queue location. Scheduler and review app must point at the same store.
DB_PATH = Path(os.getenv("REVIEW_DB", str(DATA_DIR / "review.db")))

# --- Scheduling safety ---
LOCK_STALE_SECONDS = 3600      # a lock older than this is from a crashed run
STALE_GUARD_SHARE = 0.25       # refuse to queue if more than 25% of Bellhaven accounts look removed...
STALE_GUARD_MIN = 5            # ...and at least this many (small portfolios can legitimately lose 1-2)
JOB_TAG = "bellhaven-sync"   # prefix on every note line we write, so humans can tell

# Website care labels -> CRM care_type picklist values.
CARE_TYPE_MAP = {
    "Short-Term Rehabilitation & Nursing": "Skilled Nursing",
    "Memory Support": "Memory Care",
    "Assisted Living": "Assisted Living",
    "Independent Living": "Independent Living",
}

# Fields we keep in sync with the website (scope set by the exercise).
# Phone is used only as match evidence, not written to existing accounts.
SYNCED_FIELDS = ["name", "billing_street", "billing_city", "billing_state", "billing_zip", "care_type"]

# Human labels for the review screen (reviewers shouldn't need API field names).
FIELD_LABELS = {
    "name": "Account name", "billing_street": "Street", "billing_city": "City",
    "billing_state": "State", "billing_zip": "ZIP", "care_type": "Care type",
    "phone": "Phone", "parent_id": "Parent account", "status": "Status",
    "duplicate_of_account": "Duplicate of", "chow_current_account": "CHOW: current account",
    "account_id": "Contact's account",
}
# Fields a new account can't be created without.
REQUIRED_FOR_CREATE = {"name", "billing_street", "billing_city", "billing_state", "billing_zip"}

# --- Data-quality checks on scraped values (warn, don't block) ---
OPERATING_STATES = {"OH", "MI", "IN", "PA"}   # from the operator's About page
ZIP_PREFIXES = {                               # first 2 digits of ZIPs per state (USPS ranges)
    "OH": ("43", "44", "45"), "MI": ("48", "49"), "IN": ("46", "47"),
    "PA": ("15", "16", "17", "18", "19"),
}

# --- Issue log ---
ISSUE_CATEGORIES = [
    "Scraper: wrong or missing value",
    "Matching: wrong account",
    "Matching: missed an existing account",
    "Business rule question",
    "Reviewer correction",
    "Data-quality check",
    "Other",
]
REJECT_REASONS = {   # reason shown to reviewer -> issue category it files (None = no issue)
    "Not the same facility (wrong match)": "Matching: wrong account",
    "Website data is wrong": "Scraper: wrong or missing value",
    "Should match a different CRM account": "Matching: missed an existing account",
    "Business decision: don't change this": None,
    "Other": "Other",
}
ISSUE_EXPORT = DATA_DIR / "issue_log.csv"

# --- Matching ---
# Address is the primary key: names change on acquisition, buildings don't move.
SCORE = {
    "address_exact": 60,      # same number + street, same city and zip
    "address_one_off": 50,    # same number + street, city OR zip differs (typo)
    "address_fuzzy": 40,      # same number, street name nearly equal
    "po_box_same_city": 25,   # CRM has a PO Box; can't compare streets
    "name_max": 20,           # scaled by name similarity (0..1), only if >= 0.5
    "phone": 15,
    "administrator": 15,      # website administrator is a contact on the account
}
MATCH_THRESHOLD = 50          # at or above: same facility
NEAR_MISS_SCORE = 10          # same-city lookalikes listed as "checked and rejected" evidence
POSSIBLE_MATCH_SCORE = 35     # above this, a lookalike gets a warning on "create"
SAME_NAME_ANYWHERE = 0.9      # name similarity that is worth mentioning even across states

BRAND_WORDS = {"bellhaven"}
STOP_WORDS = {"the", "of", "at", "a", "an"}
NAME_SYNONYMS = {
    "&": "and", "centre": "center", "rehab": "rehabilitation",
    "healthcare": "health care",
}
STREET_ABBREVIATIONS = {
    "street": "st", "avenue": "ave", "av": "ave", "road": "rd", "drive": "dr",
    "lane": "ln", "boulevard": "blvd", "court": "ct", "place": "pl",
    "parkway": "pkwy", "highway": "hwy", "pike": "pike", "pk": "pike",
    "circle": "cir", "terrace": "ter", "trail": "trl", "square": "sq",
    "north": "n", "south": "s", "east": "e", "west": "w",
    "northwest": "nw", "northeast": "ne", "southwest": "sw", "southeast": "se",
    "suite": "ste", "apartment": "apt",
}
