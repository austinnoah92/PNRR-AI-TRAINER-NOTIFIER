from __future__ import annotations

import csv
import html as _html
from datetime import datetime
import random
import re
import sys
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import replace
from pathlib import Path

from .http_client import HttpClient
from .models import ProjectRecord
from .repository import ProjectRepository

# Official, server-rendered registry page -> reliable website lookup by MIUR code.
SCHEDA = "https://cercalatuascuola.istruzione.it/cercalatuascuola/istituti/{code}/scheda"
_WEBSITE = re.compile(r'[Ss]ito\s*web[^<]*</[^>]+>\s*<[^>]+>\s*<a[^>]+href="([^"]+)"')
_EDU = re.compile(r'https?://[^\s"\'<>]*\.edu\.it[^\s"\'<>]*')

# Vendor fingerprints + how to extract the albo id from the homepage HTML.
_SPAGGIARI_ID = re.compile(
    r"web\.spaggiari\.eu/sdg2/(?:AlboOnline|Trasparenza)/([A-Za-z0-9]+)"
    r"|albo_pretorio\.php\?sede_codice=([A-Za-z0-9]+)"
    # White-labelled Spaggiari app (served on the school's own domain) embeds the
    # code as custcode in its JSON. Requiring custcode avoids matching the plain
    # Spaggiari *registro* login link that many unrelated schools also carry.
    r'|custcode"\s*:\s*"([A-Za-z0-9]+)"'
)
_ARGO_ID = re.compile(r"albipretorionline\.com/([A-Za-z0-9]+)|customerCode=([A-Za-z0-9]+)", re.IGNORECASE)
_ALBO_LINK = re.compile(
    r'href="([^"]*(?:albo[-/]?(?:online|pretorio)|amministrazione[-/]?trasparente'
    r'|tipologia-documento/albo)[^"]*)"',
    re.IGNORECASE,
)

CSV_FIELDS = [
    "progressive", "area", "school_code", "school_name", "region", "school_type",
    "cup", "clp", "amount", "website", "funded_date", "vendor", "albo_id", "albo_url",
]


class SchoolMapper:
    """Resolves each school to (vendor, albo_id, albo_url) so the monitor knows
    which adapter to use. Runs offline of the hourly loop (one-time / weekly)."""

    def __init__(self, http: HttpClient, timeout: int = 8) -> None:
        self.http = http
        # Shorter than the monitor's: most mapping cost is waiting on dead/slow
        # sites, and a down site doesn't get better by waiting ~25s for it.
        self.timeout = timeout

    def resolve_website(self, code: str) -> str:
        try:
            resp = self.http.get(SCHEDA.format(code=code), timeout=self.timeout)
            resp.raise_for_status()
        except Exception:
            return ""
        match = _WEBSITE.search(resp.text)
        if match:
            return match.group(1).strip().rstrip("/")
        edu = _EDU.search(resp.text)
        return edu.group(0).rstrip("/") if edu else ""

    @staticmethod
    def _scan_saas(raw_html: str, school_code: str) -> tuple[str, str]:
        """Detect a known SaaS albo platform + its id from a page's HTML.
        Returns ("", "") if none found."""
        html = _html.unescape(raw_html)  # custcode etc. are often HTML-escaped JSON
        spag = _SPAGGIARI_ID.search(html)
        if spag:
            return ("Spaggiari", spag.group(1) or spag.group(2) or spag.group(3))
        argo = _ARGO_ID.search(html)
        if argo:
            return ("Argo", argo.group(1) or argo.group(2))
        if "nuvola.madisoft" in html.lower() or "madisoft" in html.lower():
            return ("Nuvola", school_code)  # Nuvola is keyed by the MIUR code
        return ("", "")

    def detect(self, website: str, school_code: str) -> tuple[str, str, str]:
        """Return (vendor, albo_id, albo_url).

        Looks for a SaaS platform on the homepage; if none, follows the school's
        own albo page one level deeper (many AgID sites host an ``/albo-online``
        page that embeds the real Spaggiari/Argo/Nuvola albo) before falling back
        to the generic crawler.
        """
        if not website:
            return ("Search", "", "")  # no site to poll -> search fallback (by name)
        url = website if website.startswith("http") else f"http://{website}"
        try:
            html = self.http.get(url, timeout=self.timeout).text
        except Exception:
            return ("", "", "")

        vendor, albo_id = self._scan_saas(html, school_code)
        if vendor:
            return (vendor, albo_id, "")

        # Resolve the on-site albo page and re-check it for an embedded platform.
        albo = _ALBO_LINK.search(html)
        albo_url = albo.group(1) if albo else ""
        if albo_url.startswith("/"):
            albo_url = website.rstrip("/") + albo_url
        if albo_url:
            try:
                albo_html = self.http.get(albo_url, timeout=self.timeout).text
                vendor, albo_id = self._scan_saas(albo_html, school_code)
                if vendor:
                    return (vendor, albo_id, "")
            except Exception:
                pass

        if albo_url or "amministrazione trasparente" in html.lower() or "albo" in html.lower():
            return ("Generic", "", albo_url or website)
        return ("Search", "", "")  # no albo detected on-site -> search fallback

    def map_record(self, record: ProjectRecord, refresh: bool = False) -> ProjectRecord:
        if record.is_mapped and not refresh:
            return record
        # Small jitter so parallel workers don't hit the shared registry host in
        # synchronized bursts (politeness against rate-limiting).
        time.sleep(random.uniform(0, 0.4))
        website = record.website or self.resolve_website(record.school_code)
        vendor, albo_id, albo_url = self.detect(website, record.school_code)
        return replace(record, website=website or record.website,
                       vendor=vendor, albo_id=albo_id, albo_url=albo_url)

    def map_csv(self, csv_path: Path, log_file: Path, refresh: bool = False,
                workers: int = 10, checkpoint_every: int = 100) -> dict[str, int]:
        """Map every school in the CSV, in parallel, writing progress to disk every
        ``checkpoint_every`` completions so a long run is resumable if interrupted
        (a re-run skips already-mapped rows). I/O-bound, so threads parallelize well."""
        records = ProjectRepository(csv_path).load()
        by_code = {r.school_code: r for r in records}      # updated as results arrive
        order = [r.school_code for r in records]            # preserve CSV order on write
        counts: Counter[str] = Counter()

        pending = [r for r in records if refresh or not r.is_mapped]
        for r in records:                                   # already-mapped rows: keep, count
            if not (refresh or not r.is_mapped):
                counts[r.vendor or "UNRESOLVED"] += 1

        def write():
            self._write(csv_path, [by_code[c] for c in order])

        done = 0
        already_done = len(records) - len(pending)
        total = len(records)
        self._print_progress(counts, already_done, total, first=True)
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(self.map_record, r, refresh): r.school_code for r in pending}
            for future in as_completed(futures):
                code = futures[future]
                try:
                    result = future.result()
                except Exception:
                    result = by_code[code]                  # leave row unchanged on error
                by_code[code] = result
                counts[result.vendor or "UNRESOLVED"] += 1
                done += 1
                self._log_map_detail(log_file, done, len(pending), result)
                self._print_progress(counts, already_done + done, total)
                if done % checkpoint_every == 0:
                    write()
        write()
        print()
        return dict(counts)

    @staticmethod
    def _log_map_detail(log_file: Path, done: int, pending_total: int, result: ProjectRecord) -> None:
        line = (
            f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] "
            f"map [{done}/{pending_total}] {result.school_code} "
            f"{result.school_name[:28]:28} -> {result.vendor or 'UNRESOLVED':9} "
            f"{result.albo_id or result.albo_url}"
        )
        with log_file.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")

    @staticmethod
    def _progress_lines(counts: Counter[str], completed: int, total: int) -> list[str]:
        columns = ["Argo", "Spaggiari", "Nuvola", "Generic", "Search", "Unresolved", "Total"]
        values = [
            counts.get("Argo", 0),
            counts.get("Spaggiari", 0),
            counts.get("Nuvola", 0),
            counts.get("Generic", 0),
            counts.get("Search", 0),
            counts.get("UNRESOLVED", 0),
            f"{completed}/{total}",
        ]
        widths = [max(len(str(col)), len(str(val))) for col, val in zip(columns, values)]
        header = " | ".join(str(col).rjust(width) for col, width in zip(columns, widths))
        sep = "-+-".join("-" * width for width in widths)
        row = " | ".join(str(val).rjust(width) for val, width in zip(values, widths))
        return ["Mapping schools...", header, sep, row]

    @classmethod
    def _print_progress(cls, counts: Counter[str], completed: int, total: int, first: bool = False) -> None:
        lines = cls._progress_lines(counts, completed, total)
        if not first:
            sys.stdout.write(f"\x1b[{len(lines)}F")
        for line in lines:
            sys.stdout.write("\x1b[2K" + line + "\n")
        sys.stdout.flush()

    @staticmethod
    def _write(csv_path: Path, records: list[ProjectRecord]) -> None:
        with csv_path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS)
            writer.writeheader()
            for r in records:
                writer.writerow({
                    "progressive": r.progressive, "area": r.area, "school_code": r.school_code,
                    "school_name": r.school_name, "region": r.region, "school_type": r.school_type,
                    "cup": r.cup, "clp": r.clp, "amount": r.amount, "website": r.website,
                    "funded_date": r.funded_date,
                    "vendor": r.vendor, "albo_id": r.albo_id, "albo_url": r.albo_url,
                })
