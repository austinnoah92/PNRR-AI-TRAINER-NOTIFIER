from __future__ import annotations

import html
import json
import re

from ..models import AlboItem, ProjectRecord
from .base import AlboAdapter

_DATA_PAGE = re.compile(r'data-page="([^"]+)"')


class SpaggiariAdapter(AlboAdapter):
    """Spaggiari "Albo Pretorio Online" — an Inertia.js app that ships the full
    published list inside the page's ``data-page`` attribute (HTML-escaped JSON).
    No separate API call needed for the list; each item carries a direct file
    download URL, so the base-class ``hydrate`` handles the PDF.

    Keyed by Spaggiari's ``custcode`` (stored as ``project.albo_id``), e.g.
    ``NOIT0005`` — not derivable from the MIUR code.
    """

    vendor = "Spaggiari"
    BASE = "https://web.spaggiari.eu/sdg2/AlboOnline"

    def fetch_items(self, project: ProjectRecord) -> list[AlboItem]:
        code = project.albo_id.strip()
        if not code:
            return []
        resp = self.http.get(f"{self.BASE}/{code}")
        resp.raise_for_status()
        match = _DATA_PAGE.search(resp.text)
        if not match:
            return []
        try:
            payload = json.loads(html.unescape(match.group(1)))
        except (ValueError, json.JSONDecodeError):
            return []
        records = (((payload.get("props") or {}).get("documenti") or {}).get("data")) or []
        items: list[AlboItem] = []
        for record in records:
            document = record.get("documento") or {}
            doc_id = record.get("id") or document.get("id")
            if doc_id is None:
                continue
            category = (record.get("categoria") or {}).get("descrizione_class") or ""
            download = document.get("url_download") or document.get("url") or ""
            items.append(
                AlboItem(
                    school_code=project.school_code,
                    vendor=self.vendor,
                    item_id=str(doc_id),
                    title=document.get("nome_file_origine") or f"Atto {doc_id}",
                    url=document.get("url") or f"{self.BASE}/{code}",
                    published=str(record.get("data_pubblicazione") or ""),
                    expires=str(record.get("data_scadenza") or ""),
                    category=category,
                    pdf_url=download,
                )
            )
        return items
