from __future__ import annotations

from datetime import date, timedelta

from ..document_reader import DocumentReader
from ..http_client import HttpClient
from ..models import AlboItem, ProjectRecord
from ..search import SearchProvider
from .base import AlboAdapter

# Terms that bias the query toward an AI-trainer call (Italian).
_QUERY_TERMS = "albo pretorio DM 219 M4C1I2.1 snodi formativi intelligenza artificiale avviso selezione esperti tutor operatori economici"


class SearchFallbackAdapter(AlboAdapter):
    """Last-resort source for schools the mapping pass could NOT place on a known
    albo platform (no resolvable website / undetectable albo). Instead of polling
    a feed, it asks a search engine for the school's recent call notices, currently
    constrained to DuckDuckGo's last-week recency bucket, then lets the normal
    verify/AI pipeline judge whatever it finds.

    Budget-capped via the injected SearchProvider, because web search does not
    scale to thousands of schools per run."""

    vendor = "Search"

    def __init__(self, http: HttpClient, reader: DocumentReader, provider: SearchProvider | None) -> None:
        super().__init__(http, reader)
        self._provider = provider

    def fetch_items(self, project: ProjectRecord) -> list[AlboItem]:
        if self._provider is None or not project.school_name:
            return []
        query = f'"{project.school_name}" {_QUERY_TERMS}'
        # DDG only gives coarse date filtering; keep fallback search focused on newly published notices.
        results = self._provider.search(query, since=date.today() - timedelta(days=7), until=date.today(), limit=8)
        return [
            AlboItem(
                school_code=project.school_code,
                vendor=self.vendor,
                item_id=r.url,           # full URL is a stable per-school key
                title=r.title or r.url,
                url=r.url,
                published=r.published,
                pdf_url=r.url if r.url.lower().endswith(".pdf") else "",
            )
            for r in results
        ]
