from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, time as dt_time
from zoneinfo import ZoneInfo

from .verifier import normalize_text

ROME_TZ = ZoneInfo("Europe/Rome")

_MONTHS = {
    "gennaio": 1, "febbraio": 2, "marzo": 3, "aprile": 4, "maggio": 5, "giugno": 6,
    "luglio": 7, "agosto": 8, "settembre": 9, "ottobre": 10, "novembre": 11, "dicembre": 12,
}
_MONTH_ALT = "|".join(_MONTHS)
_WEEKDAY_ALT = "|".join(("lunedi", "martedi", "mercoledi", "giovedi", "venerdi", "sabato", "domenica"))

# Real documents collapse whitespace entirely in places ("entroenonoltreleore
# 12.00 del 29giugno 2026") — every separator below is \s* (zero-or-more), not
# \s+, so a run of words with no spaces at all still matches.
_TIME = r"(?P<hour>\d{1,2})\s*(?:[:.,]\s*(?P<minute>\d{2}))?"
# Both separators independently tolerate "/" or ".", plus a stray extra "/"
# in either slot — confirmed real typos: "04/07//2026" (extra slash before
# year) as well as the day/month slot.
_NUM_DATE = r"(?P<day>\d{1,2})\s*[/.]\s*/?\s*(?P<month>\d{1,2})\s*[/.]\s*/?\s*(?P<year>\d{2,4})"
_WORD_DATE = rf"(?P<day2>\d{{1,2}})\s*(?P<monthname>{_MONTH_ALT})\s*(?P<year2>\d{{4}})"
# "del", "del giorno", or "di", optionally followed by a weekday name in any
# case — confirmed real variants: "del 04/07/2026", "del giorno 03/07/2026",
# "di venerdì 19/6/2026", "del giorno sabato 4 luglio 2026". Text is
# accent-folded before matching (see extract_application_deadline), so the
# weekday list here is unaccented ("mercoledi", not "mercoledì").
_CONNECTOR = rf"(?:del|di)\s*(?:giorno\s*)?(?:(?:{_WEEKDAY_ALT})\S*\s*)?"

# Ordered by specificity: try the most fully-formed shapes first so a partial
# match (e.g. the swapped-order case) doesn't get short-circuited by a looser
# pattern matching only the date portion.
_PATTERNS = [
    re.compile(rf"entro\s*(?:e\s*non\s*oltre\s*)?le\s*ore\s*{_TIME}\s*{_CONNECTOR}\s*{_NUM_DATE}", re.IGNORECASE),
    re.compile(rf"entro\s*(?:e\s*non\s*oltre\s*)?le\s*ore\s*{_TIME}\s*{_CONNECTOR}\s*{_WORD_DATE}", re.IGNORECASE),
    # Swapped date/time — a confirmed recurring authoring error across
    # multiple real schools: "entro le ore 06.07.2026 ore 8.00".
    re.compile(rf"entro\s*le\s*ore\s*{_NUM_DATE}\s*ore\s*{_TIME}", re.IGNORECASE),
    re.compile(rf"entro\s*le\s*ore\s*{_WORD_DATE}\s*ore\s*{_TIME}", re.IGNORECASE),
    # No time given at all — date follows "ore" directly (confirmed real:
    # "entro le ore 30/06/2026, tramite..."). Negative lookahead so this
    # doesn't swallow the date half of the swapped-order case above; try
    # this only after the more specific patterns have failed.
    re.compile(rf"entro\s*le\s*ore\s*{_NUM_DATE}(?!\s*ore)", re.IGNORECASE),
    re.compile(rf"entro\s*le\s*ore\s*{_WORD_DATE}(?!\s*ore)", re.IGNORECASE),
]

# Deliberately NOT handled: relative deadlines ("del settimo giorno dalla
# data di pubblicazione") — seen once across ~150 real documents sampled.
# Building an Italian-ordinal-word parser for a pattern this rare isn't
# worth the added surface area; these fall through to the platform date.

_PLAUSIBLE_YEARS = range(2020, 2036)


@dataclass(frozen=True)
class ApplicationDeadline:
    date: date
    time: dt_time | None
    raw: str


def _to_date(day: str, month: str | int, year: str) -> date | None:
    try:
        day_i, year_i = int(day), int(year)
        month_i = _MONTHS[month.lower()] if isinstance(month, str) and not month.isdigit() else int(month)
        if year_i < 100:
            year_i += 2000
        if year_i not in _PLAUSIBLE_YEARS:
            return None
        return date(year_i, month_i, day_i)
    except (ValueError, KeyError):
        return None


def _to_time(hour: str | None, minute: str | None) -> dt_time | None:
    if hour is None:
        return None
    try:
        h, m = int(hour), int(minute) if minute else 0
        return dt_time(h, m)
    except ValueError:
        return None


def extract_application_deadline(text: str) -> ApplicationDeadline | None:
    """Deterministic extraction of the real application deadline from an
    Italian school notice's body text — deliberately conservative: an
    extracted date that doesn't strictly validate (real calendar date,
    plausible year) is discarded entirely rather than guessed, so a
    malformed source document falls back to the platform-reported date
    instead of producing a wrong one."""
    # normalize_text folds accents (mercoledì -> mercoledi) and replaces
    # apostrophes/hyphens/mojibake with spaces — the same normalization
    # already proven against these documents for the rule verifier.
    normalized = normalize_text(text)
    for pattern in _PATTERNS:
        match = pattern.search(normalized)
        if not match:
            continue
        groups = match.groupdict()
        if groups.get("monthname"):
            found_date = _to_date(groups["day2"], groups["monthname"], groups["year2"])
        else:
            found_date = _to_date(groups["day"], groups["month"], groups["year"])
        if found_date is None:
            continue
        found_time = _to_time(groups.get("hour"), groups.get("minute"))
        return ApplicationDeadline(date=found_date, time=found_time, raw=match.group(0))
    return None


def is_deadline_passed(deadline: ApplicationDeadline) -> bool:
    """Europe/Rome-aware check: a same-day deadline with a time is compared
    against the current time, not just the date; a date-only deadline (no
    time stated) stays open through the end of that day."""
    now = datetime.now(ROME_TZ)
    if deadline.time is not None:
        deadline_dt = datetime.combine(deadline.date, deadline.time, tzinfo=ROME_TZ)
        return now > deadline_dt
    return deadline.date < now.date()
