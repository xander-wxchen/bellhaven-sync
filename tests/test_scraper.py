from pathlib import Path

import pytest

from bellhaven_sync import config
from bellhaven_sync.scraper import (
    ScrapeError, internal_links, is_detail_url, parse_detail, stated_counts,
)

FX = Path(__file__).parent / "fixtures"
BASE = config.BASE_URL


def html(name: str) -> str:
    return (FX / name).read_text(encoding="utf-8")


def test_parse_basic_detail_page():
    loc = parse_detail(html("detail_basic.html"), f"{BASE}/communities/bellhaven-of-maplewood")
    assert loc.slug == "bellhaven-of-maplewood"
    assert loc.name == "Bellhaven of Maplewood"
    assert (loc.street, loc.city, loc.state, loc.zip) == ("210 Orchard Lane", "Maplewood", "OH", "44280")
    assert loc.care_offerings == ["Assisted Living"]
    assert loc.administrator == "Nadia Duval"
    assert loc.phone == "(614) 250-9447"


def test_parse_keeps_every_care_offering():
    loc = parse_detail(html("detail_multi_care.html"), f"{BASE}/communities/bellhaven-shores-of-erie")
    assert loc.care_offerings == ["Assisted Living", "Memory Support"]


def test_homepage_links_reach_communities_missing_from_directory():
    # Findlay is announced on the homepage but is not in the directory listing.
    detail = {u for u in internal_links(html("home.html"), BASE + "/") if is_detail_url(u)}
    assert f"{BASE}/communities/bellhaven-meadows-of-findlay" in detail


def test_directory_pagination_links_are_not_detail_pages():
    links = internal_links(html("directory_p1.html"), BASE + "/communities")
    assert f"{BASE}/communities?page=2" in links
    assert not is_detail_url(f"{BASE}/communities?page=2")
    assert sum(is_detail_url(u) for u in links) == 16


def test_stated_counts_read_from_site_text():
    assert 35 in stated_counts(html("home.html"))
    assert 34 in stated_counts(html("directory_p1.html"))


def test_multi_line_street_is_kept():
    page = """<h1>Bellhaven Test</h1><dl class="detail">
      <dt>Address</dt><dd>100 Main St<br>Suite 200<br>Akron, OH 44313-1234</dd>
      <dt>Care Offerings</dt><dd><span class="badge">Assisted Living</span></dd></dl>"""
    loc = parse_detail(page, f"{BASE}/communities/test")
    assert loc.street == "100 Main St, Suite 200"
    assert loc.zip == "44313"


@pytest.mark.parametrize("page", [
    "<p>no heading</p>",
    '<h1>X</h1><dl class="detail"><dt>Address</dt><dd>only one line</dd></dl>',
    '<h1>X</h1><dl class="detail"><dt>Address</dt><dd>1 A St<br>Nowhere Ohio</dd></dl>',
])
def test_bad_pages_fail_loudly(page):
    with pytest.raises(ScrapeError):
        parse_detail(page, f"{BASE}/communities/bad")


def test_guardrail_stops_run_when_far_fewer_communities_than_claimed(monkeypatch):
    from bellhaven_sync import scraper
    pages = {
        BASE + "/": "<p>Today we proudly serve 35 communities.</p>"
                    '<a href="/communities/only-one">x</a>',
        BASE + "/communities": "<p>nothing listed</p>",
        BASE + "/communities/only-one": html("detail_basic.html"),
    }
    monkeypatch.setattr(scraper, "fetch", lambda session, url: pages[url])
    with pytest.raises(ScrapeError, match="claims 35"):
        scraper.scrape(BASE)
