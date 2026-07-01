from __future__ import annotations

import re
import urllib.parse as up
from threading import Lock
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import date

from .http_client import HttpClient


@dataclass(frozen=True, slots=True)
class SearchResult:
    url: str
    title: str
    published: str = ""  # ISO date if the provider supplies one


class SearchProvider(ABC):
    """Strategy for web search. Swap DuckDuckGo for Google Custom Search (true
    absolute date ranges, higher reliability) by adding another implementation —
    nothing else changes."""

    @abstractmethod
    def search(self, query: str, since: date | None = None,
               until: date | None = None, limit: int = 8) -> list[SearchResult]:
        ...


class DuckDuckGoSearchProvider(SearchProvider):
    """Free, no-key, best-effort. Suitable for the unresolved minority on a
    watchlist — NOT for high-volume runs (it rate-limits aggressively), hence the
    per-run ``budget``. Date filtering is best-effort: DDG's HTML endpoint doesn't
    expose result dates, so the [since, until] window is enforced downstream when
    each found notice's own date is read; we only bias the query toward recency."""

    ENDPOINT = "https://html.duckduckgo.com/html/"

    def __init__(self, http: HttpClient, budget: int = 30) -> None:
        self._http = http
        self._remaining = budget
        self._lock = Lock()

    @property
    def budget_left(self) -> int:
        return self._remaining

    def search(self, query: str, since: date | None = None,
               until: date | None = None, limit: int = 8) -> list[SearchResult]:
        with self._lock:
            if self._remaining <= 0:
                return []
            self._remaining -= 1
        data = {"q": query}
        if since and until:
            data["df"] = "w"  # DuckDuckGo HTML supports coarse recency; w = last week.
        try:
            resp = self._http.post(self.ENDPOINT, data=data)
            resp.raise_for_status()
        except Exception:
            return []
        return self._parse(resp.text, limit)

    @staticmethod
    def _parse(html: str, limit: int) -> list[SearchResult]:
        out: list[SearchResult] = []
        seen: set[str] = set()
        for href, label in re.findall(r'class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', html, re.S):
            if "uddg=" in href:
                m = re.search(r"uddg=([^&]+)", href)
                if m:
                    href = up.unquote(m.group(1))
            if not href.startswith("http") or "duckduckgo.com" in href or href in seen:
                continue
            seen.add(href)
            title = re.sub(r"<[^>]+>", "", label)
            out.append(SearchResult(url=href, title=" ".join(title.split())))
            if len(out) >= limit:
                break
        return out


def build_search_provider(http: HttpClient, budget: int = 30) -> SearchProvider:
    """Factory. Today: DuckDuckGo. Future: pick Google CSE when keys are set."""
    return DuckDuckGoSearchProvider(http, budget=budget)
