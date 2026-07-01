from __future__ import annotations

from urllib.parse import urljoin

from bs4 import BeautifulSoup

from ..models import AlboItem, ProjectRecord
from .generic import GenericCrawlerAdapter

# Nuvola renders the full AgID "Amministrazione Trasparente" category taxonomy
# as a nav block on EVERY page, so a plain fetch of the top "bacheca" page only
# ever shows these ~28 folder names — never actual notices. Real documents live
# one level deeper, inside a specific category. We exclude this fixed label set
# to tell "a document link" (any title not in here) apart from "a nav link".
_CATEGORY_LABELS = {
    "Disposizioni generali", "Organizzazione", "Consulenti e collaboratori", "Personale",
    "Bandi di concorso", "Performance", "Enti Controllati", "Attività e procedimenti",
    "Provvedimenti", "Bandi di gara e contratti",
    "Sovvenzioni, contributi, sussidi, vantaggi economici", "Bilanci",
    "Beni immobili e gestione patrimonio", "Controlli e rilievi sull'amministrazione",
    "Servizi erogati", "Pagamenti dell'amministrazione", "Opere Pubbliche",
    "Pianificazione e governo del territorio", "Informazioni ambientali",
    "Interventi straordinari e di emergenza", "Altri contenuti",
    "Piano triennale per la prevenzione della corruzione e della trasparenza",
    "Atti generali", "Oneri informativi per cittadini e imprese",
    "Riferimenti normativi su organizzazione e attività", "Atti amministrativi generali",
    "Documenti di programmazione strategico-gestionale", "Codice disciplinare e di condotta",
}

# The category that actually holds tender/call notices (confirmed by inspection:
# it lists real "Avviso ..." / "Bando ..." documents, unlike "Bandi di concorso",
# which is teacher/staff competition notices, a different thing).
_TARGET_CATEGORY = "Bandi di gara e contratti"


class NuvolaAdapter(GenericCrawlerAdapter):
    """Nuvola / Madisoft "Bacheca Digitale" (~13% of funded schools).

    The bacheca is server-rendered (no JS needed) and keyed directly by the MIUR
    mechanographic code (no opaque id to discover). It has no clean public JSON
    feed, so ``fetch_items`` does a two-step fetch: load the top page, follow the
    "Bandi di gara e contratti" category link, and read the real document links
    from that page. ``hydrate`` is inherited from :class:`GenericCrawlerAdapter`,
    which follows a document's detail page to its actual signed PDF.
    """

    vendor = "Nuvola"
    BACHECA = "https://nuvola.madisoft.it/bacheca-digitale/bacheca/{code}/5/IN_PUBBLICAZIONE/0/show"

    def fetch_items(self, project: ProjectRecord) -> list[AlboItem]:
        code = project.albo_id.strip() or project.school_code.strip()
        if not code:
            return []
        top_url = self.BACHECA.format(code=code)
        category_url = self._find_category_url(top_url)
        if not category_url:
            return []
        return self._list_documents(project, category_url)

    def _find_category_url(self, top_url: str) -> str:
        try:
            resp = self.http.get(top_url)
            resp.raise_for_status()
        except Exception:
            return ""
        soup = BeautifulSoup(resp.content, "html.parser")
        for anchor in soup.find_all("a"):
            if anchor.get_text(" ", strip=True) == _TARGET_CATEGORY:
                href = anchor.get("href")
                if href:
                    return urljoin(resp.url, href)
        return ""

    def _list_documents(self, project: ProjectRecord, category_url: str) -> list[AlboItem]:
        try:
            resp = self.http.get(category_url)
            resp.raise_for_status()
        except Exception:
            return []
        soup = BeautifulSoup(resp.content, "html.parser")
        items: list[AlboItem] = []
        seen: set[str] = set()
        for anchor in soup.find_all("a"):
            label = anchor.get_text(" ", strip=True)
            href = anchor.get("href")
            if not href or not label or label in _CATEGORY_LABELS:
                continue
            full = urljoin(resp.url, href)
            if full in seen:
                continue
            seen.add(full)
            items.append(
                AlboItem(
                    school_code=project.school_code,
                    vendor=self.vendor,
                    item_id=full,
                    title=label,
                    url=full,
                )
            )
        return items
