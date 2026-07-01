from __future__ import annotations

import io
import re
import zipfile
from dataclasses import replace
from xml.etree import ElementTree as ET

from ..models import AlboItem, ProjectRecord
from .base import AlboAdapter

_ATTO_ID = re.compile(r"[?&]id=(\d+)")


class ArgoAdapter(AlboAdapter):
    """Argo "Albo Pretorio Online" — the cleanest source: a public per-school
    RSS feed plus a public JSON detail/attachment API. Keyed by Argo's
    ``customerCode`` (stored as ``project.albo_id``), not the MIUR code.

    Endpoints (no auth):
      RSS list   GET {API}/public/atti/rss/{customerCode}
      detail     GET {API}/public/atti/online/{attoId}          -> JSON incl. allegati
      download   GET {API}/public/atti/{attoId}/allegati        -> JSON string: a
                 temporary signed ZIP URL bundling every attachment for that atto.
                 (The per-file endpoint, /public/atti/allegati/{allegatoId}, always
                 404s "Allegato non trovato" — confirmed by direct testing. This
                 atto-level route is the one that actually works.)
    """

    vendor = "Argo"
    API = "https://generale.portaleargo.it/albopretorio/api"
    _REFERER = "https://www.portaleargo.it/albopretorio/online/"

    def __init__(self, http, reader) -> None:
        super().__init__(http, reader)
        self._detail_cache: dict[str, dict] = {}

    def fetch_items(self, project: ProjectRecord) -> list[AlboItem]:
        code = project.albo_id.strip()
        if not code:
            return []
        resp = self.http.get(f"{self.API}/public/atti/rss/{code}", headers={"Referer": self._REFERER})
        resp.raise_for_status()
        try:
            root = ET.fromstring(resp.content)
        except ET.ParseError:
            return []
        items: list[AlboItem] = []
        for node in root.iter("item"):
            link = (node.findtext("link") or "").strip()
            guid = (node.findtext("guid") or "").strip()
            title = " ".join((node.findtext("title") or "").split())
            published = (node.findtext("pubDate") or "").strip()
            match = _ATTO_ID.search(guid) or _ATTO_ID.search(link)
            atto_id = match.group(1) if match else (guid or link)
            if not atto_id:
                continue
            items.append(
                AlboItem(
                    school_code=project.school_code,
                    vendor=self.vendor,
                    item_id=atto_id,
                    title=title,
                    url=link or guid,
                    published=published,
                    detail_ref=atto_id,
                )
            )
        return items

    def enrich_item(self, item: AlboItem) -> AlboItem:
        data = self._detail(item)
        expires = str(data.get("dataArchiviazioneEffettiva") or data.get("dataArchiviazioneOriginale") or item.expires or "")
        category = str(data.get("tipologiaAttoDenominazione") or item.category or "")
        return replace(item, expires=expires, category=category)

    def _detail(self, item: AlboItem) -> dict:
        atto_id = item.detail_ref or item.item_id
        if atto_id not in self._detail_cache:
            detail = self.http.get(
                f"{self.API}/public/atti/online/{atto_id}",
                headers={"Referer": self._REFERER, "Accept": "application/json"},
            )
            detail.raise_for_status()
            self._detail_cache[atto_id] = detail.json()
        return self._detail_cache[atto_id]

    def hydrate(self, item: AlboItem) -> str:
        """Resolve the notice's attachments and read every one we can. Filenames
        are folded in regardless (diagnostic even when a body can't be read, e.g.
        "Trattativa Diretta ... GOODWILL.docx" alone reveals a closed direct award),
        then attachment bodies are appended via the ZIP bundle download."""
        data = self._detail(item)
        parts: list[str] = []
        for key in ("descrizione", "tipologiaAttoDenominazione"):
            value = data.get(key)
            if value:
                parts.append(str(value))
        allegati = data.get("allegati") or []
        for allegato in allegati:
            name = allegato.get("nome") or ""
            if name:
                parts.append(name)
        if allegati:
            parts.extend(self._download_attachment_bodies(item))
        return "\n".join(parts)

    def _download_attachment_bodies(self, item: AlboItem) -> list[str]:
        """Download the atto's attachments and return the readable text of each.

        Argo bundles every attachment for one atto into a single temporary,
        signed ZIP file rather than serving files individually.
        """
        atto_id = item.detail_ref or item.item_id
        try:
            resp = self.http.get(
                f"{self.API}/public/atti/{atto_id}/allegati",
                headers={"Referer": self._REFERER, "Accept": "application/json"},
            )
            resp.raise_for_status()
            zip_url = resp.json()
            if not isinstance(zip_url, str) or not zip_url:
                return []
            blob = self.http.get(zip_url)
            blob.raise_for_status()
        except Exception:
            return []
        texts: list[str] = []
        try:
            with zipfile.ZipFile(io.BytesIO(blob.content)) as bundle:
                for name in bundle.namelist():
                    try:
                        text = self.reader.read(bundle.read(name), "", name).text
                    except Exception:
                        continue
                    if text:
                        texts.append(text)
        except zipfile.BadZipFile:
            return []
        return texts
