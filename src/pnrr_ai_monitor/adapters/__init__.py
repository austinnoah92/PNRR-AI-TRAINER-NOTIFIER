from .base import AdapterRegistry, AlboAdapter
from .argo import ArgoAdapter
from .spaggiari import SpaggiariAdapter
from .generic import GenericCrawlerAdapter
from .nuvola import NuvolaAdapter
from .search import SearchFallbackAdapter

__all__ = [
    "AdapterRegistry", "AlboAdapter", "ArgoAdapter", "SpaggiariAdapter",
    "GenericCrawlerAdapter", "NuvolaAdapter", "SearchFallbackAdapter",
]
