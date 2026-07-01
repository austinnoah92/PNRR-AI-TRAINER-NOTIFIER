from __future__ import annotations

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

DEFAULT_USER_AGENT = "PNRROpportunityMonitor/1.0 (+monitoring funded-school albo notices)"


class HttpClient:
    """A thin, reusable wrapper around ``requests.Session``.

    Centralizes the things every adapter needs to get right: connection
    pooling (one keep-alive session), polite identification, sane timeouts,
    and automatic backoff/retry on transient failures and rate limits. Adapters
    share one instance so we never hammer a host the way an ad-hoc script would.
    """

    def __init__(
        self,
        timeout: int = 20,
        retries: int = 3,
        backoff_factor: float = 0.6,
        user_agent: str = DEFAULT_USER_AGENT,
        pool_maxsize: int = 32,
    ) -> None:
        self.timeout = timeout
        self._session = requests.Session()
        self._session.headers.update({"User-Agent": user_agent})
        retry = Retry(
            total=retries,
            connect=retries,
            read=retries,
            status=retries,
            backoff_factor=backoff_factor,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=frozenset({"GET", "POST"}),
            respect_retry_after_header=True,
            raise_on_status=False,
        )
        adapter = HTTPAdapter(max_retries=retry, pool_connections=pool_maxsize, pool_maxsize=pool_maxsize)
        self._session.mount("https://", adapter)
        self._session.mount("http://", adapter)

    def get(self, url: str, **kwargs) -> requests.Response:
        kwargs.setdefault("timeout", self.timeout)
        return self._session.get(url, **kwargs)

    def post(self, url: str, **kwargs) -> requests.Response:
        kwargs.setdefault("timeout", self.timeout)
        return self._session.post(url, **kwargs)

    def close(self) -> None:
        self._session.close()

    def __enter__(self) -> "HttpClient":
        return self

    def __exit__(self, *exc) -> None:
        self.close()
