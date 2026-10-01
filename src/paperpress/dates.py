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


# --- dates in file names ---------------------------------------------------
# Filenames usually carry the date as a run of digits, and the order of those
# digits (20080114 vs 01142008 vs 14012008) is a property of the whole batch,
# not of one file. So we work out the format from every name at once, and only
# ask the user when the batch genuinely can't tell MM/DD from DD/MM.

FILENAME_FORMATS = ["YYYYMMDD", "MMDDYYYY", "DDMMYYYY", "YYYY-MM-DD", "MM-DD-YYYY",
                    "DD-MM-YYYY"]
_TOKEN = {"YYYY": r"(?P<y>\d{4})", "MM": r"(?P<m>\d{2})", "DD": r"(?P<d>\d{2})"}


class DateFormatError(ValueError):
    pass


def _format_regex(fmt: str) -> re.Pattern:
    out, i = "", 0
    while i < len(fmt):
        for tok, rx in _TOKEN.items():
            if fmt.startswith(tok, i):
                out += rx
                i += len(tok)
                break
        else:
            out += "[-_. ]" if fmt[i] in "-_. " else re.escape(fmt[i])
            i += 1
    if not all(f"?P<{g}>" in out for g in "ymd"):
        raise DateFormatError(f"date format {fmt!r} needs YYYY, MM and DD")
    return re.compile(r"(?<!\d)" + out + r"(?!\d)")


def date_from_filename(name: str, fmt: str) -> str | None:
    """The date in `name` written in `fmt` (e.g. MMDDYYYY), as YYYY-MM-DD."""
    for m in _format_regex(fmt).finditer(name):
        try:
            d = date(int(m["y"]), int(m["m"]), int(m["d"]))
        except ValueError:
            continue
        if 1600 <= d.year <= 2100:
            return d.isoformat()
    return None


def infer_filename_format(names: list[str]) -> str:
    """The one format that reads a valid date from every name.

    Raises DateFormatError when no format fits them all, or when more than one
    does in a way that gives different dates (all days <= 12, so MM/DD vs DD/MM
    is undecidable).
    """
    if not names:
        raise DateFormatError("no file names to infer a date format from")
    fits = {}
    for fmt in FILENAME_FORMATS:
        dates = [date_from_filename(n, fmt) for n in names]
        if all(dates):
            fits[fmt] = dates
    if not fits:
        bad = next((n for n in names
                    if not any(date_from_filename(n, f) for f in FILENAME_FORMATS)), None)
        if bad is None:
            raise DateFormatError("file names use different date formats; add them in "
                                  "separate batches with --date-format")
        raise DateFormatError(f"can't find a date in file names like {bad!r}; "
                              f"pass --date-format (e.g. MMDDYYYY)")
    distinct = {tuple(v) for v in fits.values()}
    if len(distinct) > 1:
        raise DateFormatError("file names fit more than one date order ("
                              + ", ".join(fits) + "); pass --date-format")
    return next(iter(fits))
