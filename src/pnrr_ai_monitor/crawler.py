from __future__ import annotations

from collections import deque
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from .document_reader import DocumentReader
from .models import CandidateDocument, ProjectRecord


HIGH_VALUE_SECTIONS = (
    "albo", "amministrazione-trasparente", "bandi", "gare", "contratti",
    "pnrr", "avvisi", "determine", "determina", "esperti", "mepa",
)

CANDIDATE_TERMS = (
    "cup", "clp", "dm 219", "dm 219_2025", "d.m. 219", "219/2025", "219_2025",
    "73226/2026", "m4c1i2.1", "m4c1i2.1-2026-1745", "pnrr",
    "intelligenza artificiale", "snodi formativi", "snodo formativo",
    "educare all'i.a.", "educare all’ia", "procedura di selezione",
    "procedura per la selezione", "avviso di selezione", "esperto", "esperti",
    "formatore", "formatori", "tutor", "operatori economici", "operatore economico",
    "manifestazione di interesse", "indagine di mercato", "rdo", "mepa",
    "istanza di partecipazione", "domanda di partecipazione", "richiesta disponibilita",
    "richiesta disponibilità", "percorsi formativi", "laboratori formativi",
    "percorsi e laboratori formativi",
)

NOISE = ("privacy", "cookie", "accessibilita", "mailto:", "tel:", "facebook.com", "instagram.com")


class SchoolCrawler:
    def __init__(self, reader: DocumentReader, timeout: int = 25):
        self.reader = reader
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "Mozilla/5.0 PNRROpportunityMonitor/0.1"})

    def discover(self, project: ProjectRecord, max_pages: int) -> list[CandidateDocument]:
        if not project.has_website:
            return []
        host = urlparse(project.website).netloc.lower()
        queue = deque([project.website])
        seen: set[str] = set()
        candidates: dict[str, CandidateDocument] = {}

        while queue and len(seen) < max_pages:
            url = queue.popleft()
            if url in seen:
                continue
            seen.add(url)
            try:
                response = self.session.get(url, timeout=self.timeout)
                response.raise_for_status()
            except Exception:
                continue

            content_type = response.headers.get("content-type", "")
            read = self.reader.read(response.content, content_type, response.url)
            title = self._title_from_html(response.content) if "html" in content_type.lower() else response.url.rsplit("/", 1)[-1]

            if self._is_candidate_text(project, response.url, title, read.text):
                candidates[response.url] = CandidateDocument(project, response.url, title, url, content_type, read.text)

            if "html" not in content_type.lower():
                continue

            soup = BeautifulSoup(response.content, "html.parser")
            for anchor in soup.find_all("a"):
                href = anchor.get("href")
                if not href:
                    continue
                child_url = self._clean_url(urljoin(response.url, href))
                if not child_url:
                    continue
                parsed = urlparse(child_url)
                if parsed.netloc.lower() != host:
                    continue
                label = " ".join(anchor.get_text(" ", strip=True).split())
                haystack = f"{child_url} {label}".lower()
                if any(noise in haystack for noise in NOISE):
                    continue
                if any(term in haystack for term in CANDIDATE_TERMS):
                    candidates.setdefault(child_url, CandidateDocument(project, child_url, label or child_url, response.url))
                if any(section in haystack for section in HIGH_VALUE_SECTIONS) and child_url not in seen:
                    queue.append(child_url)
        return list(candidates.values())

    def hydrate(self, candidate: CandidateDocument) -> CandidateDocument:
        if candidate.text:
            return candidate
        response = self.session.get(candidate.url, timeout=self.timeout)
        response.raise_for_status()
        content_type = response.headers.get("content-type", "")
        read = self.reader.read(response.content, content_type, response.url)
        return CandidateDocument(candidate.project, response.url, candidate.title, candidate.source_url, content_type, read.text)

    def _is_candidate_text(self, project: ProjectRecord, url: str, title: str, text: str) -> bool:
        haystack = f" {url} {title} {text[:3000]} ".lower()
        exact_terms = [project.cup.lower(), project.clp.lower()]
        return any(term and term in haystack for term in exact_terms) or any(term in haystack for term in CANDIDATE_TERMS)

    def _title_from_html(self, content: bytes) -> str:
        soup = BeautifulSoup(content, "html.parser")
        if soup.title and soup.title.string:
            return " ".join(soup.title.string.split())
        heading = soup.find(["h1", "h2"])
        return " ".join(heading.get_text(" ", strip=True).split()) if heading else "Untitled page"

    def _clean_url(self, url: str) -> str | None:
        if url.startswith(("mailto:", "tel:", "javascript:")):
            return None
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"}:
            return None
        return url.split("#", 1)[0]
