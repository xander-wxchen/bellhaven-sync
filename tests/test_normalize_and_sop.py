import pytest

from bellhaven_sync.matching import billing_requires_chow
from bellhaven_sync.normalize import (
    is_po_box, name_similarity, norm_phone, norm_street, street_name, zip5,
)


@pytest.mark.parametrize("a,b", [
    ("4850 Northwest Sylvania Ave.", "4850 NW Sylvania Ave"),
    ("980 West Michigan Avenue", "980 W Michigan Ave"),
    ("3313 Wilmington Pk", "3313 Wilmington Pike"),
    ("1125 Logan Boulevard", "1125 Logan Blvd"),
    ("199 Barks Road West", "199 Barks Rd W"),
])
def test_street_spellings_normalize_equal(a, b):
    assert norm_street(a) == norm_street(b)


def test_different_streets_stay_different():
    assert street_name("875 Elm St") != street_name("640 Walnut Bottom Rd")


@pytest.mark.parametrize("s,expected", [
    ("PO Box 517", True), ("P.O. Box 9", True), ("Post Office Box 1", True),
    ("517 Box Elder St", False), ("1420 Harbor Point Dr", False),
])
def test_po_box(s, expected):
    assert is_po_box(s) is expected


def test_small_helpers():
    assert zip5("44313-1234") == "44313"
    assert norm_phone("(614) 250-9447") == norm_phone("+1 614.250.9447")


def test_name_similarity_ignores_formatting_and_brand():
    assert name_similarity("Bellhaven Rehab and Nursing of Grove City",
                           "Bellhaven Rehabilitation & Nursing of Grove City") > 0.95
    assert name_similarity("Bellhaven Health Care Center of Ashland",
                           "Bellhaven Healthcare Centre of Ashland") > 0.95


def test_sharing_only_the_brand_word_is_not_similar():
    # 'Bellhaven of Carlisle' must not look like 'Bellhaven of Marion'
    assert name_similarity("Bellhaven of Marion", "Bellhaven of Carlisle") < 0.5


# ---- billing SOP: revenue history AND outstanding AR > 0 -> CHOW, else re-parent
@pytest.mark.parametrize("rev,ar,chow", [
    (84000, 12400, True),    # Tiffin-like
    (47000, 0, False),       # revenue but no AR -> direct re-parent
    (0, 500, False),         # AR without revenue history -> direct re-parent
    (0, 0, False),
    (None, None, False),     # missing data is treated as zero, not as "has billing"
    ("51250", "3800", True), # numeric strings
])
def test_billing_requires_chow(rev, ar, chow):
    assert billing_requires_chow({"lifetime_revenue": rev, "outstanding_ar": ar}) is chow
