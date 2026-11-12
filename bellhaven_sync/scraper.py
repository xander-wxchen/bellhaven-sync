"""Scrape every Bellhaven community from the public website.

Design choices:
- Crawl every internal page and collect every /communities/<slug> link,
  instead of trusting the directory alone. The homepage can link to a
  community the directory does not list yet (it happens: a new acquisition).
- Fail loudly. A partial scrape is worse than none, because the matcher would
  read missing locations as "no longer on the website".
- Parsing is split from fetching, so it can be unit-tested on saved HTML.
"""
from __future__ import annotations

import json
import re
import sys
import time
from collections import deque
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from . import config

DETAIL_RE = re.compile(r"^/communities/[^/?#]+/?$")
CITY_LINE_RE = re.compile(r"^(?P<city>.+?),\s*(?P<state>[A-Z]{2})\s+(?P<zip>\d{5})(?:-\d{4})?$")
STATED_COUNT_RES = [
    re.compile(r"serve\s+(\d+)\s+communities", re.I),
    re.compile(r"(\d+)\s+communities\s+listed", re.I),
]


class ScrapeError(RuntimeError):
    pass


@dataclass
class Location:
    slug: str
    url: str
    name: str
    street: str
    city: str
    state: str
    zip: str
    care_offerings: list[str] = field(default_factory=list)
    administrator: str = ""
    phone: str = ""


# ---------------------------------------------------------------- fetching

def _session() -> requests.Session:
    s = requests.Session()
    s.headers["User-Agent"] = config.USER_AGENT
    return s


def fetch(session: requests.Session, url: str) -> str:
    last_err = None
    for attempt in range(1, config.HTTP_RETRIES + 1):
        try:
            r = session.get(url, timeout=config.HTTP_TIMEOUT)
            if r.status_code == 404:
                raise ScrapeError(f"404 for {url}")
            r.raise_for_status()
            time.sleep(config.REQUEST_DELAY)
            return r.text
        except ScrapeError:
            raise
        except requests.RequestException as e:
            last_err = e
            time.sleep(attempt)  # simple backoff: 1s, 2s, ...
    raise ScrapeError(f"Failed to fetch {url} after {config.HTTP_RETRIES} tries: {last_err}")


# ---------------------------------------------------------------- parsing

def internal_links(html: str, page_url: str) -> set[str]:
    """Absolute same-site URLs linked from a page (fragments dropped)."""
    host = urlparse(config.BASE_URL).netloc
    out = set()
    for a in BeautifulSoup(html, "html.parser").find_all("a", href=True):
        u = urljoin(page_url, a["href"]).split("#")[0]
        if urlparse(u).netloc == host:
            out.add(u)
    return out


def is_detail_url(url: str) -> bool:
    return bool(DETAIL_RE.match(urlparse(url).path)) and not urlparse(url).query


def stated_counts(html: str) -> list[int]:
    """Numbers the site claims about itself, e.g. 'we serve 35 communities'."""
    text = BeautifulSoup(html, "html.parser").get_text(" ")
    return [int(m) for rx in STATED_COUNT_RES for m in rx.findall(text)]


def parse_detail(html: str, url: str) -> Location:
    soup = BeautifulSoup(html, "html.parser")
    h1 = soup.find("h1")
    if not h1 or not h1.get_text(strip=True):
        raise ScrapeError(f"No name (h1) on {url}")

    fields: dict[str, object] = {}
    for dt in soup.select("dl.detail dt"):
        dd = dt.find_next_sibling("dd")
        if dd is None:
            continue
        label = dt.get_text(strip=True).lower()
        if label == "care offerings":
            fields[label] = [b.get_text(strip=True) for b in dd.select(".badge")] or [dd.get_text(strip=True)]
        elif label == "address":
            fields[label] = [ln.strip() for ln in dd.get_text("\n").split("\n") if ln.strip()]
        else:
            fields[label] = dd.get_text(" ", strip=True)

    lines = fields.get("address") or []
    if len(lines) < 2:
        raise ScrapeError(f"Address not in 'street / City, ST 12345' form on {url}: {lines}")
    m = CITY_LINE_RE.match(lines[-1])
    if not m:
        raise ScrapeError(f"Cannot parse city/state/zip on {url}: {lines[-1]!r}")

    return Location(
        slug=urlparse(url).path.rstrip("/").rsplit("/", 1)[-1],
        url=url,
        name=h1.get_text(" ", strip=True),
        street=", ".join(lines[:-1]),   # keeps suite/unit lines if present
        city=m["city"].strip(),
        state=m["state"],
        zip=m["zip"],
        care_offerings=list(fields.get("care offerings") or []),
        administrator=str(fields.get("administrator", "")),
        phone=str(fields.get("phone", "")),
    )


# ---------------------------------------------------------------- crawl

def scrape(base_url: str | None = None) -> tuple[list[Location], dict]:
    base = (base_url or config.BASE_URL).rstrip("/") + "/"
    session = _session()

    queue = deque([base, urljoin(base, "communities")])
    seen: set[str] = set()
    detail_urls: set[str] = set()
    claims: list[int] = []

    # 1) crawl listing/info pages, collecting detail links and stated counts
    while queue:
        url = queue.popleft()
        if url in seen:
            continue
        if len(seen) >= config.MAX_PAGES:
            raise ScrapeError(f"Crawl exceeded MAX_PAGES={config.MAX_PAGES}; site structure changed?")
        seen.add(url)
        html = fetch(session, url)
        claims += stated_counts(html)
        for link in internal_links(html, url):
            if is_detail_url(link):
                detail_urls.add(link.rstrip("/"))
            elif link not in seen and not urlparse(link).path.startswith("/api") \
                    and not urlparse(link).path.startswith("/crm") \
                    and not urlparse(link).path.startswith("/welcome"):
                queue.append(link)

    # 2) fetch and parse each community page
    locations = [parse_detail(fetch(session, u), u) for u in sorted(detail_urls)]

    # 3) guardrails
    slugs = [l.slug for l in locations]
    if len(slugs) != len(set(slugs)):
        raise ScrapeError("Duplicate community slugs scraped")
    expected = max(claims) if claims else None
    if expected and len(locations) < config.MIN_SCRAPE_RATIO * expected:
        raise ScrapeError(
            f"Scraped {len(locations)} communities but the site claims {expected}. "
            "Refusing to continue: downstream would wrongly flag the rest as removed."
        )

    meta = {
        "scraped_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "pages_crawled": len(seen),
        "locations": len(locations),
        "site_claims": sorted(set(claims)),
        "count_matches_claim": expected is None or len(locations) == expected,
    }
    return locations, meta


def main() -> int:
    # Windows consoles default to cp1252; never crash on printing a path or name.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    locations, meta = scrape()
    config.DATA_DIR.mkdir(exist_ok=True)
    out = config.DATA_DIR / "site_locations.json"
    out.write_text(json.dumps({"meta": meta, "locations": [asdict(l) for l in locations]},
                              indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Scraped {meta['locations']} communities from {meta['pages_crawled']} pages "
          f"(site claims {meta['site_claims']}) -> {out}")
    if not meta["count_matches_claim"]:
        print("WARNING: scraped count differs from what the site claims; check before trusting.",
              file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
