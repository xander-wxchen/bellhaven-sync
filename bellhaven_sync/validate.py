"""Sanity checks on scraped website data.

The scraper already fails hard on pages it can't parse at all. These checks
catch values that parse fine but look wrong (a ZIP from another state, a care
label we've never seen). They never block the run: they lower the proposal's
confidence, show a warning on the card, and file an entry in the issue log so
the scraper can be improved later.
"""
from __future__ import annotations

from collections import defaultdict

from . import config
from .normalize import norm_phone, norm_street, street_number


def check_location(loc: dict) -> list[dict]:
    """Return [{code, field, message}] for one scraped location."""
    out = []

    def flag(code, field, msg):
        out.append({"code": code, "field": field, "message": msg})

    state, zip_ = loc.get("state", ""), loc.get("zip", "")
    if state not in config.OPERATING_STATES:
        flag("state_outside_footprint", "billing_state",
             f"State {state} is outside the operator's stated footprint "
             f"({', '.join(sorted(config.OPERATING_STATES))}).")
    elif not zip_.startswith(config.ZIP_PREFIXES.get(state, ("",))):
        flag("zip_state_mismatch", "billing_zip", f"ZIP {zip_} does not belong to {state}.")

    if not street_number(loc.get("street", "")):
        flag("street_no_number", "billing_street", f"Street '{loc.get('street', '')}' has no house number.")

    offerings = loc.get("care_offerings") or []
    if not offerings:
        flag("no_care_offering", "care_type", "No care offering listed on the website.")
    for o in offerings:
        if o not in config.CARE_TYPE_MAP:
            flag("unmapped_care_label", "care_type",
                 f"Care offering '{o}' is not in CARE_TYPE_MAP; it would be written as-is.")

    if loc.get("phone") and len(norm_phone(loc["phone"])) != 10:
        flag("phone_format", "phone", f"Phone '{loc['phone']}' is not a 10-digit US number.")

    if not loc.get("name", "").strip():
        flag("no_name", "name", "Listing has no name.")
    return out


def check_all(locations: list[dict]) -> dict[str, list[dict]]:
    """{slug: [issues]} including cross-listing checks."""
    result = {loc["slug"]: check_location(loc) for loc in locations}
    by_addr = defaultdict(list)
    for loc in locations:
        by_addr[(norm_street(loc.get("street", "")), loc.get("zip"))].append(loc)
    for same in by_addr.values():
        if len(same) > 1:
            names = ", ".join(l["name"] for l in same)
            for loc in same:
                result[loc["slug"]].append({"code": "shared_address", "field": "billing_street",
                                            "message": f"Several website listings share this address: {names}."})
    return {k: v for k, v in result.items() if v}
