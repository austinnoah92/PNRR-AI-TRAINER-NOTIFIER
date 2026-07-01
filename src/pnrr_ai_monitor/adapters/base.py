from __future__ import annotations

from abc import ABC, abstractmethod

from ..document_reader import DocumentReader
from ..http_client import HttpClient
from ..models import AlboItem, ProjectRecord


class AlboAdapter(ABC):
    """Strategy for reading one albo platform.

    Each adapter knows how to (1) list a school's published notices as
    normalized :class:`AlboItem` objects and (2) fetch the full text of a single
    notice for verification. Everything platform-specific lives behind this
    interface; the monitor stays vendor-agnostic.
    """

    #: vendor label this adapter handles — must match ``ProjectRecord.vendor``.
    vendor: str = ""

    def __init__(self, http: HttpClient, reader: DocumentReader) -> None:
        self.http = http
        self.reader = reader

    @abstractmethod
    def fetch_items(self, project: ProjectRecord) -> list[AlboItem]:
        """Return currently-published notices for ``project`` (newest first)."""

    def enrich_item(self, item: AlboItem) -> AlboItem:
        """Return item with any extra structured metadata the listing lacks.

        Platforms like Argo expose expiry/archive dates only on a detail API.
        The monitor calls this after cheap prefiltering and before expensive
        document hydration, so shared open/expired checks stay vendor-agnostic.
        """
        return item

    def hydrate(self, item: AlboItem) -> str:
        """Return the full document text for one notice.

        Default behaviour downloads ``pdf_url`` (falling back to ``url``) and
        runs it through the shared reader. Adapters whose notice text lives
        behind an extra API call (e.g. Argo attachments) override this.
        """
        target = item.pdf_url or item.url
        if not target:
            return ""
        response = self.http.get(target)
        response.raise_for_status()
        content_type = response.headers.get("content-type", "")
        return self.reader.read(response.content, content_type, response.url).text


class AdapterRegistry:
    """O(1) vendor -> adapter lookup."""

    def __init__(self) -> None:
        self._by_vendor: dict[str, AlboAdapter] = {}

    def register(self, adapter: AlboAdapter) -> "AdapterRegistry":
        if not adapter.vendor:
            raise ValueError(f"{type(adapter).__name__} has no vendor set")
        self._by_vendor[adapter.vendor.lower()] = adapter
        return self

    def get(self, vendor: str) -> AlboAdapter | None:
        return self._by_vendor.get(vendor.strip().lower())

    def supported(self) -> frozenset[str]:
        return frozenset(self._by_vendor)
