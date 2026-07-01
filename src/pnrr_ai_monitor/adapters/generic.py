from __future__ import annotations

from dataclasses import replace
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from ..crawler import SchoolCrawler
from ..document_reader import DocumentReader
from ..http_client import HttpClient
from ..models import AlboItem, CandidateDocument, ProjectRecord
from .base import AlboAdapter


class GenericCrawlerAdapter(AlboAdapter):
    """Fallback for schools whose albo lives on their own site (WordPress/AgID
    theme) rather than a known SaaS platform — ~21% of funded schools, including
    the "Axios" bucket (Axios is only their registro; the albo is self-hosted).

    Reuses the existing :class:`SchoolCrawler`: it walks the albo/trasparenza
    pages (same-host) and surfaces candidate notices, which we normalize to
    :class:`AlboItem`. Crawl starts at ``project.albo_url`` when the mapping pass
    found a specific albo page, otherwise the homepage.
    """

    vendor = "Generic"

    def __init__(self, http: HttpClient, reader: DocumentReader, max_pages: int = 12) -> None:
        super().__init__(http, reader)
        self._crawler = SchoolCrawler(reader, timeout=http.timeout)
        self._max_pages = max_pages

    def _start_url(self, project: ProjectRecord) -> str:
        return project.albo_url or project.website

    def fetch_items(self, project: ProjectRecord) -> list[AlboItem]:
        start = self._start_url(project)
        if not start:
            return []
        # The crawler keys off project.website, so point it at the albo entry URL.
        crawl_target = project if project.website == start else replace(project, website=start)
        candidates = self._crawler.discover(crawl_target, self._max_pages)
        items: list[AlboItem] = []
        for candidate in candidates:
            is_pdf = candidate.url.lower().endswith(".pdf") or "pdf" in candidate.content_type.lower()
            items.append(
                AlboItem(
                    school_code=project.school_code,
                    vendor=self.vendor,
                    item_id=candidate.url,  # stable per-school; full URL is unique
                    title=candidate.title or candidate.url.rsplit("/", 1)[-1],
                    url=candidate.url,
                    pdf_url=candidate.url if is_pdf else "",
                )
            )
        return items

    # How many attachment PDFs to pull from a single notice detail page.
    MAX_ATTACHMENTS = 3

    def hydrate(self, item: AlboItem) -> str:
        """Read the full notice text.

        Own-site albos usually expose a *detail page* (HTML) that links to the
        actual signed PDF(s). Reading only that page gives the AI thin, chrome-
        heavy text and causes false negatives. So: if the target is HTML, we read
        it AND follow its attachment PDFs, concatenating everything. If it's
        already a PDF, we just read it.
        """
        target = item.pdf_url or item.url
        if not target:
            return ""
        try:
            response = self.http.get(target)
            response.raise_for_status()
        except Exception:
            return ""
        content_type = response.headers.get("content-type", "")
        page_text = self.reader.read(response.content, content_type, response.url).text
        if "html" not in content_type.lower() and not response.url.lower().endswith((".htm", ".html", "/")):
            return page_text  # already a document (PDF, etc.)

        # Attachment PDFs carry the real notice; put them FIRST because the
        # downstream verifier truncates the text and the detail page is mostly
        # site chrome / the full albo listing.
        pdf_parts: list[str] = []
        for pdf_url in self._attachment_pdfs(response.content, response.url)[: self.MAX_ATTACHMENTS]:
            try:
                blob = self.http.get(pdf_url)
                blob.raise_for_status()
            except Exception:
                continue
            text = self.reader.read(blob.content, blob.headers.get("content-type", ""), pdf_url).text
            if text:
                pdf_parts.append(text)
        # Keep only a slice of the page text so a huge listing can't drown the PDF.
        parts = pdf_parts + ([page_text[:4000]] if page_text else [])
        return "\n".join(parts)

    @staticmethod
    def _attachment_pdfs(html: bytes, base_url: str) -> list[str]:
        """Same-host links that look like the notice's downloadable document."""
        soup = BeautifulSoup(html, "html.parser")
        host = urlparse(base_url).netloc.lower()
        found: list[str] = []
        seen: set[str] = set()
        for anchor in soup.find_all("a"):
            href = anchor.get("href")
            if not href:
                continue
            full = urljoin(base_url, href)
            low = full.lower()
            looks_like_doc = low.endswith(".pdf") or "download" in low or "allegat" in low or "/documenti/" in low
            if not looks_like_doc:
                continue
            if urlparse(full).netloc.lower() not in ("", host):
                continue
            if full not in seen:
                seen.add(full)
                found.append(full)
        return found
