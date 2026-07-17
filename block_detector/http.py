from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit, urlunsplit

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


class HttpResponseError(RuntimeError):
    pass


def _safe_response_url(value: object) -> str:
    """Remove user information, query parameters, and fragments from error URLs."""
    raw = str(value or "")
    try:
        parsed = urlsplit(raw)
        if not parsed.scheme or not parsed.netloc:
            return raw.split("?", 1)[0].split("#", 1)[0]
        netloc = parsed.netloc.rsplit("@", 1)[-1]
        return urlunsplit((parsed.scheme, netloc, parsed.path, "", ""))
    except ValueError:
        base = raw.split("?", 1)[0].split("#", 1)[0]
        if "://" in base:
            scheme, remainder = base.split("://", 1)
            return f"{scheme}://{remainder.rsplit('@', 1)[-1]}"
        return base.rsplit("@", 1)[-1]


def build_session(*, retries: int = 3) -> requests.Session:
    retry = Retry(
        total=retries,
        connect=retries,
        read=retries,
        status=retries,
        backoff_factor=0.4,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset({"GET", "POST"}),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry, pool_connections=8, pool_maxsize=8)
    session = requests.Session()
    session.headers.update({"User-Agent": "block-detector/0.3"})
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


class JsonHttpClient:
    def __init__(
        self,
        *,
        session: requests.Session | None = None,
        timeout: tuple[float, float] = (3.05, 10.0),
        retries: int = 3,
    ) -> None:
        self.session = session or build_session(retries=retries)
        self.timeout = timeout

    def get(self, url: str, **kwargs: Any) -> Any:
        try:
            response = self.session.get(url, timeout=self.timeout, **kwargs)
        except requests.RequestException as exc:
            raise HttpResponseError(
                f"{type(exc).__name__} while requesting {_safe_response_url(url)}"
            ) from exc
        return self._decode(response)

    def post(self, url: str, **kwargs: Any) -> Any:
        try:
            response = self.session.post(url, timeout=self.timeout, **kwargs)
        except requests.RequestException as exc:
            raise HttpResponseError(
                f"{type(exc).__name__} while requesting {_safe_response_url(url)}"
            ) from exc
        return self._decode(response)

    @staticmethod
    def _decode(response: requests.Response) -> Any:
        try:
            response.raise_for_status()
        except requests.HTTPError as exc:
            raise HttpResponseError(
                f"HTTP {response.status_code} from "
                f"{_safe_response_url(response.url)}"
            ) from exc
        try:
            return response.json()
        except ValueError as exc:
            raise HttpResponseError(
                f"Invalid JSON response from {_safe_response_url(response.url)}"
            ) from exc
