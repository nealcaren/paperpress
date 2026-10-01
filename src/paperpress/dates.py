"""Find an issue date in the messy strings sources give us.

Returns (YYYY-MM-DD, precision) where precision is "day" or "month". A
month-only date (common for monthly magazines) is stored as the 1st with
precision "month". A bare year is not enough to place an issue, so it
returns None and the issue goes to _undated/.
"""
from __future__ import annotations

import re
from datetime import date

MONTHS = {m: i for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july", "august",
     "september", "october", "november", "december"], start=1)}
MONTHS.update({m[:3]: i for m, i in list(MONTHS.items())})
MONTHS["sept"] = 9

_MON = r"(?P<mon>[A-Za-z]{3,9})\.?"
PATTERNS = [
    re.compile(r"(?P<y>1[5-9]\d\d|20\d\d)-(?P<m>\d{1,2})-(?P<d>\d{1,2})"),   # 1870-04-07
    re.compile(_MON + r"\s+(?P<d>\d{1,2})(?:st|nd|rd|th)?,?\s+(?P<y>1[5-9]\d\d|20\d\d)"),  # April 7, 1870
    re.compile(r"(?P<d>\d{1,2})(?:st|nd|rd|th)?\s+" + _MON + r",?\s+(?P<y>1[5-9]\d\d|20\d\d)"),  # 7 April 1870
    re.compile(r"(?P<y>1[5-9]\d\d|20\d\d)-(?P<m>\d{1,2})(?![\d-])"),          # 1915-05
    re.compile(_MON + r",?\s+(?P<y>1[5-9]\d\d|20\d\d)"),                      # May 1915
]


def parse_date(text: str | None) -> tuple[str, str] | None:
    if not text:
        return None
    for pat in PATTERNS:
        for m in pat.finditer(text):
            g = m.groupdict()
            month = int(g["m"]) if g.get("m") else MONTHS.get((g.get("mon") or "").lower())
            if not month:
                continue
            day = int(g["d"]) if g.get("d") else None
            try:
                d = date(int(g["y"]), month, day or 1)
            except ValueError:
                continue
            return d.isoformat(), ("day" if day else "month")
    return None
