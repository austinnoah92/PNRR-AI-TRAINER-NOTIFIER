from __future__ import annotations

from dataclasses import replace
from datetime import date, timedelta

from ..deadline_parser import extract_published_date
from ..document_reader import DocumentReader
from ..http_client import HttpClient
from ..models import AlboItem, ProjectRecord
from ..search import SearchProvider
from .base import AlboAdapter

# Terms that bias the query toward an AI-trainer call (Italian).
_QUERY_TERMS = "albo pretorio DM 219 M4C1I2.1 snodi formativi intelligenza artificiale avviso selezione esperti tutor operatori economici"

# DuckDuckGo's HTML endpoint exposes no per-result date at all (confirmed:
# its result snippets never include one) — the `df=w` recency hint passed to
# the query is a soft bias on DDG's side, not something we can verify. The
# "since/until" window is only actually enforced once we have the document's
# own text (see enrich_item below), against whatever the CURRENT run's date
# is — so the window is always "the last N days as of today", not a fixed
# range left over from an earlier day.
SEARCH_WINDOW_DAYS = 7


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
        self._content_cache: dict[str, str] = {}

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

    def enrich_item(self, item: AlboItem) -> AlboItem:
        # DDG gives us no real date, so the only place one can come from is
        # the document's own text (an explicit "pubblicato il..." statement,
        # or a protocol citation) — extracted once here and cached so
        # hydrate() doesn't re-fetch the same URL a second time.
        text = self._fetch_text(item)
        published = extract_published_date(text)
        if published is not None:
            return replace(item, published=published.strftime("%d/%m/%Y"))
        return item

    def hydrate(self, item: AlboItem) -> str:
        return self._fetch_text(item)

    def _fetch_text(self, item: AlboItem) -> str:
        if item.url not in self._content_cache:
            target = item.pdf_url or item.url
            try:
                response = self.http.get(target)
                response.raise_for_status()
                content_type = response.headers.get("content-type", "")
                self._content_cache[item.url] = self.reader.read(response.content, content_type, response.url).text
            except Exception:
                self._content_cache[item.url] = ""
        return self._content_cache[item.url]
