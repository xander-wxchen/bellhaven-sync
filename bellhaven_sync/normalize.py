"""Turn messy text into comparable keys. Pure functions, easy to test."""
from __future__ import annotations

import re
from difflib import SequenceMatcher

from . import config

_PUNCT = re.compile(r"[^\w\s&]")
_PO_BOX = re.compile(r"^\s*(p\.?\s*o\.?\s*box|post office box)\b", re.I)


def _tokens(s: str) -> list[str]:
    s = (s or "").lower().replace("&", " & ")
    return _PUNCT.sub(" ", s).split()


def norm_street(s: str) -> str:
    """'4850 Northwest Sylvania Ave.' -> '4850 nw sylvania ave'"""
    return " ".join(config.STREET_ABBREVIATIONS.get(t, t) for t in _tokens(s))


def street_number(s: str) -> str | None:
    m = re.match(r"\s*(\d+)", s or "")
    return m.group(1) if m else None


def street_name(s: str) -> str:
    """Street without the house number: '210 Orchard Ln' -> 'orchard ln'"""
    n = norm_street(s)
    return re.sub(r"^\d+\s*", "", n)


def is_po_box(s: str) -> bool:
    return bool(_PO_BOX.match(s or ""))


def norm_city(s: str) -> str:
    return " ".join(_tokens(s))


def zip5(s: str) -> str:
    m = re.match(r"\s*(\d{5})", s or "")
    return m.group(1) if m else ""


def norm_phone(s: str) -> str:
    d = re.sub(r"\D", "", s or "")
    return d[-10:] if len(d) >= 10 else d


def norm_name(s: str, drop_brand: bool = False) -> str:
    out = []
    for t in _tokens(s):
        t = config.NAME_SYNONYMS.get(t, t)
        for part in t.split():
            if part in config.STOP_WORDS:
                continue
            if drop_brand and part in config.BRAND_WORDS:
                continue
            out.append(part)
    return " ".join(out)


def name_similarity(a: str, b: str) -> float:
    """0..1. Order-insensitive, and compares both with and without the brand
    word, so 'Kettering Care Centre' vs 'Bellhaven of Kettering' isn't punished
    just for the brand, while 'Bellhaven of X' vs 'Bellhaven of Y' isn't
    rewarded just for sharing it."""
    def ratio(x: str, y: str) -> float:
        if not x or not y:
            return 0.0
        return SequenceMatcher(None, " ".join(sorted(x.split())), " ".join(sorted(y.split()))).ratio()

    return max(ratio(norm_name(a), norm_name(b)) if not _only_brand_shared(a, b) else 0.0,
               ratio(norm_name(a, True), norm_name(b, True)))


def _only_brand_shared(a: str, b: str) -> bool:
    ta, tb = set(norm_name(a).split()), set(norm_name(b).split())
    return bool(ta & tb) and (ta & tb) <= config.BRAND_WORDS
