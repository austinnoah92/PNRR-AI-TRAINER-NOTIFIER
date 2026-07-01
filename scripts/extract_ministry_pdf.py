"""Extract the full list of funded projects from the ministry PDF into a CSV.

The parser is deliberately strict about row shape so one bad PDF text gap cannot
swallow the next row into the school name. If data/projects_funded.csv already
exists, mapping fields (website/vendor/albo_id/albo_url) are preserved by school
code so the funded-list cleanup can be rerun after a mapping pass.
"""

from __future__ import annotations

import csv
import re
from pathlib import Path

import pdfplumber
from pypdf import PdfReader

ROOT = Path(__file__).resolve().parents[1]
PDF_NAME = "26-05-18 Allegato 1 - Elenco Progetti Finanziati DM 219-2025_signed.pdf"
PDF_PATH = next((p for p in (ROOT / PDF_NAME, ROOT.parent / PDF_NAME) if p.exists()), ROOT / PDF_NAME)
OUTPUT_CSV = ROOT / "data" / "projects_funded.csv"

REGIONS = [
    "TRENTINO-ALTO ADIGE", "FRIULI VENEZIA GIULIA", "EMILIA-ROMAGNA",
    "VALLE D'AOSTA", "ABRUZZO", "BASILICATA", "CALABRIA", "CAMPANIA",
    "LAZIO", "LIGURIA", "LOMBARDIA", "MARCHE", "MOLISE", "PIEMONTE",
    "PUGLIA", "SARDEGNA", "SICILIA", "TOSCANA", "UMBRIA", "VENETO",
]

REGION_ALT = "|".join(re.escape(r) for r in sorted(REGIONS, key=len, reverse=True))

ROW_RE = re.compile(
    r"(?P<progressive>\d+)\s+"
    r"(?P<area>Mezzogiorno|Centro\s+-\s+Nord)\s+"
    r"(?P<school_code>[A-Z]{2}[A-Z0-9]{8})\s+"
    r"(?P<school_name>.+?)\s+"
    rf"(?P<region>{REGION_ALT})\s+"
    r"(?P<school_type>Statale|Paritaria)\s+"
    r"(?P<date>\d{2}-\d{2}-\d{4}\s+\d{2}:\d{2}:\d{2})\s+"
    r"(?P<cup>[A-Z0-9]{15})\s+"
    r"(?P<clp>M4C1I2\.1-\d{4}-\d+-P-\d+)\s+"
    r"(?P<amount>[\d.\s]+,\d{2})\s*€"
)

FIELDNAMES = [
    "progressive", "area", "school_code", "school_name", "region", "school_type",
    "cup", "clp", "amount", "website", "funded_date", "vendor", "albo_id", "albo_url",
]


def to_iso_date(raw: str) -> str:
    m = re.match(r"(\d{2})-(\d{2})-(\d{4})", raw or "")
    return f"{m.group(3)}-{m.group(2)}-{m.group(1)}" if m else ""


def normalize_amount(raw: str) -> str:
    return raw.replace(" ", "").replace(".", "").replace(",", ".")


def page_texts() -> list[str]:
    """Prefer pypdf text order for this ministry table; fall back to pdfplumber."""
    texts: list[str] = []
    try:
        reader = PdfReader(PDF_PATH)
        texts = [" ".join((page.extract_text() or "").split()) for page in reader.pages]
    except Exception:
        texts = []
    if sum(bool(t) for t in texts) > 10:
        return texts

    out: list[str] = []
    with pdfplumber.open(PDF_PATH) as pdf:
        for page in pdf.pages:
            text = page.extract_text(x_tolerance=1, y_tolerance=3) or ""
            out.append(" ".join(text.split()))
    return out


def existing_mapping() -> dict[str, dict[str, str]]:
    if not OUTPUT_CSV.exists():
        return {}
    with OUTPUT_CSV.open("r", encoding="utf-8-sig", newline="") as handle:
        rows = csv.DictReader(handle)
        return {r.get("school_code", ""): r for r in rows if r.get("school_code")}


def main() -> None:
    mapped = existing_mapping()
    rows: dict[str, dict[str, str]] = {}
    for text in page_texts():
        for match in ROW_RE.finditer(text):
            data = match.groupdict()
            school_code = data["school_code"]
            old = mapped.get(school_code, {})
            rows[school_code] = {
                "progressive": data["progressive"],
                "area": " ".join(data["area"].split()),
                "school_code": school_code,
                "school_name": " ".join(data["school_name"].split()),
                "region": data["region"],
                "school_type": data["school_type"],
                "cup": data["cup"],
                "clp": data["clp"],
                "amount": normalize_amount(data["amount"]),
                "website": old.get("website", ""),
                "funded_date": to_iso_date(data["date"]),
                "vendor": old.get("vendor", ""),
                "albo_id": old.get("albo_id", ""),
                "albo_url": old.get("albo_url", ""),
            }

    malformed = [r for r in rows.values() if "M4C1I2" in r["school_name"] or "€" in r["school_name"]]
    if malformed:
        examples = "; ".join(f"{r['school_code']}: {r['school_name'][:80]}" for r in malformed[:5])
        raise RuntimeError(f"Malformed parsed rows detected: {examples}")

    OUTPUT_CSV.parent.mkdir(parents=True, exist_ok=True)
    with OUTPUT_CSV.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDNAMES)
        writer.writeheader()
        writer.writerows(sorted(rows.values(), key=lambda r: int(r["progressive"])))

    mapped_count = sum(1 for r in rows.values() if r["vendor"])
    print(f"Wrote {len(rows)} funded schools to {OUTPUT_CSV}")
    print(f"Preserved mapping for {mapped_count}; remaining to map: {len(rows) - mapped_count}")
    print("Next: python run_monitor.py --map --projects data/projects_funded.csv")


if __name__ == "__main__":
    main()
