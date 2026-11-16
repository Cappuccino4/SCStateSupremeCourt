"""Polite HTTP client for fetching public pages and PDFs.

Every request carries an identifying User-Agent with a way to reach the operator
(see config.user_agent), requests to the same host are spaced at least
``min_interval`` seconds apart (retries included), transient failures (429/5xx,
connection errors) are retried with exponential backoff that honors ``Retry-After``,
and every request has a timeout. A server that asks to wait longer than
MAX_RETRY_AFTER_SECONDS is not retried: the run fails fast with that status instead of
sleeping past the job's time limit. Each retry is logged. One run makes a few dozen
requests at most.

(Named ``net`` rather than ``http`` so it never shadows the standard-library module.)
"""

from __future__ import annotations

import logging
import time
from typing import Any
from urllib.parse import urlsplit

import requests
from requests.adapters import HTTPAdapter
from urllib3.exceptions import InvalidHeader, MaxRetryError, ResponseError
from urllib3.util.retry import Retry

log = logging.getLogger(__name__)

DEFAULT_MAX_BYTES = 40 * 1024 * 1024  # largest response we will read (a very long PDF is ~5 MB)
MAX_RETRY_AFTER_SECONDS = 60.0  # a longer Retry-After is not waited out (CourtListener's hourly/daily limits)
ERROR_BODY_BYTES = 4096  # how much of an error response is kept on FetchError.body


class FetchError(RuntimeError):
    """A request failed after retries. ``status`` is the HTTP status, or None for network errors.

    ``body`` holds the start of an error response's body (for an API's error detail), or None.
    """

    def __init__(self, message: str, url: str, status: int | None = None, body: bytes | None = None):
        super().__init__(message)
        self.url = url
        self.status = status
        self.body = body


class _PoliteRetry(Retry):
    """urllib3's Retry, made polite and visible.

    - Never retries sooner than ``min_backoff`` seconds (urllib3 sends its first retry at once).
    - Does not retry when the server asks to wait more than ``max_retry_after`` seconds;
      the response is returned as it is, and PoliteSession raises FetchError with its status.
    - Logs every retry it schedules (urllib3's own logging is kept quiet).
    """

    def __init__(
        self, *args: Any, min_backoff: float = 0.0, max_retry_after: float = MAX_RETRY_AFTER_SECONDS, **kwargs: Any
    ) -> None:
        super().__init__(*args, **kwargs)
        self.min_backoff = max(0.0, float(min_backoff))
        self.max_retry_after = max(0.0, float(max_retry_after))

    def new(self, **kw: Any) -> _PoliteRetry:
        kw.setdefault("min_backoff", self.min_backoff)
        kw.setdefault("max_retry_after", self.max_retry_after)
        return super().new(**kw)

    def get_backoff_time(self) -> float:
        return max(self.min_backoff, super().get_backoff_time())

    def sleep_for_retry(self, response: Any) -> bool:
        retry_after = self.get_retry_after(response)
        if retry_after:
            time.sleep(max(retry_after, self.min_backoff))
            return True
        return False

    def sleep(self, response: Any = None) -> None:
        if self.respect_retry_after_header and response and self.sleep_for_retry(response):
            return
        backoff = self.get_backoff_time()
        if backoff > 0:
            time.sleep(backoff)

    def _retry_after_of(self, response: Any) -> float | None:
        value = response.headers.get("Retry-After") if response is not None else None
        if value is None:
            return None
        try:
            return self.parse_retry_after(value)
        except InvalidHeader:
            return None

    def increment(
        self,
        method: str | None = None,
        url: str | None = None,
        response: Any = None,
        error: Exception | None = None,
        _pool: Any = None,
        _stacktrace: Any = None,
    ) -> _PoliteRetry:
        target = _target(_pool, url)
        status = getattr(response, "status", None)
        wait = self._retry_after_of(response) if self.respect_retry_after_header else None
        if wait is not None and wait > self.max_retry_after:
            log.warning(
                "%s %s: HTTP %s asks to wait %.0f s before trying again, more than %.0f s, so it is not retried.",
                method or "GET", target, status, wait, self.max_retry_after,
            )  # fmt: skip
            raise MaxRetryError(_pool, url, ResponseError(f"Retry-After of {wait:.0f} s is too long"))
        new_retry = super().increment(method, url, response, error, _pool, _stacktrace)
        done = len(new_retry.history)
        limit = done + new_retry.total if isinstance(new_retry.total, int) else done
        reason = f"{type(error).__name__}" if error is not None else f"HTTP {status}"
        planned = max(wait, new_retry.min_backoff) if wait else new_retry.get_backoff_time()
        log.warning(
            "%s %s: %s; retrying in %.0f s (retry %d of %d).", method or "GET", target, reason, planned, done, limit
        )
        return new_retry


def _target(pool: Any, url: str | None) -> str:
    """'https://host/path' for log lines (urllib3 passes only the path; the pool knows the host)."""
    host = getattr(pool, "host", None)
    scheme = getattr(pool, "scheme", None) or "https"
    if host and url and url.startswith("/"):
        return f"{scheme}://{host}{url}"
    return url or "(unknown URL)"


class PoliteSession:
    def __init__(
        self,
        user_agent: str,
        min_interval: float = 2.0,
        timeout: tuple[float, float] = (10.0, 60.0),
        max_retries: int = 4,
        backoff_factor: float = 2.0,
        session: requests.Session | None = None,
    ):
        self.min_interval = max(0.0, float(min_interval))
        self.timeout = timeout
        self.session = session or requests.Session()
        retry = _PoliteRetry(
            total=max_retries,
            connect=max_retries,
            read=max_retries,
            status=max_retries,
            backoff_factor=backoff_factor,
            backoff_max=60,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=frozenset({"GET", "HEAD"}),
            respect_retry_after_header=True,
            raise_on_status=False,
            min_backoff=self.min_interval,  # a retry is a request too: never sooner than the per-host spacing
            max_retry_after=MAX_RETRY_AFTER_SECONDS,
        )
        adapter = HTTPAdapter(max_retries=retry)
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)
        self.session.headers.update({"User-Agent": user_agent, "Accept-Language": "en-US,en;q=0.8"})
        self._last_request_at: dict[str, float] = {}
        self.request_count = 0
        self.bytes_downloaded = 0

    def _throttle(self, url: str) -> None:
        host = urlsplit(url).netloc.lower()
        last = self._last_request_at.get(host)
        if last is not None and self.min_interval > 0:
            wait = self.min_interval - (time.monotonic() - last)
            if wait > 0:
                time.sleep(wait)
        self._last_request_at[host] = time.monotonic()

    def get(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        max_bytes: int = DEFAULT_MAX_BYTES,
    ) -> requests.Response:
        """GET with throttling, retries, and a size cap. Raises FetchError unless the status is 2xx."""
        self._throttle(url)
        host = urlsplit(url).netloc.lower()
        started = time.monotonic()
        try:
            try:
                resp = self.session.get(url, params=params, headers=headers, timeout=self.timeout, stream=True)
            except requests.RequestException as exc:
                raise FetchError(f"GET {url} failed: {exc.__class__.__name__}: {exc}", url) from exc
            self.request_count += 1
            try:
                chunks: list[bytes] = []
                size = 0
                for chunk in resp.iter_content(chunk_size=64 * 1024):
                    size += len(chunk)
                    if size > max_bytes:
                        raise FetchError(f"GET {url}: response larger than {max_bytes // (1024 * 1024)} MB", url)
                    chunks.append(chunk)
                resp._content = b"".join(chunks)  # requests' documented way to pre-load a streamed body
            except requests.RequestException as exc:
                raise FetchError(f"GET {url} failed while reading: {exc}", url) from exc
            finally:
                resp.close()
        finally:
            # Space the next request from the end of this one (retries included), not its start.
            self._last_request_at[host] = time.monotonic()
        self.bytes_downloaded += size
        elapsed = time.monotonic() - started
        log.info("GET %s -> %s (%s, %.1fs)", resp.url, resp.status_code, _fmt_size(size), elapsed)
        if not 200 <= resp.status_code < 300:
            raise FetchError(
                f"GET {resp.url} returned HTTP {resp.status_code}",
                url,
                resp.status_code,
                body=resp.content[:ERROR_BODY_BYTES],
            )
        return resp

    def get_text(self, url: str, **kwargs: Any) -> str:
        resp = self.get(url, **kwargs)
        if not resp.encoding or resp.encoding.lower() == "iso-8859-1":
            resp.encoding = resp.apparent_encoding or "utf-8"
        return resp.text

    def get_json(self, url: str, **kwargs: Any) -> Any:
        resp = self.get(url, **kwargs)
        try:
            return resp.json()
        except ValueError as exc:
            raise FetchError(f"GET {url} did not return JSON", url, resp.status_code) from exc

    def get_bytes(self, url: str, **kwargs: Any) -> bytes:
        return self.get(url, **kwargs).content


def _fmt_size(n: int) -> str:
    return f"{n / 1024:.0f} KB" if n < 1024 * 1024 else f"{n / (1024 * 1024):.1f} MB"
