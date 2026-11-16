"""Create the digest in Buttondown as a draft, and optionally send it.

Buttondown API v1 (``https://api.buttondown.com/v1``, header ``Authorization: Token <key>``):

- ``POST /emails`` with ``"status": "draft"`` creates a draft. The status is ALWAYS sent
  explicitly: Buttondown's documented default has changed over time, and leaving it out
  must never be able to email subscribers.
- ``PATCH /emails/{id}`` with ``{"status": "about_to_send"}`` sends a draft (AUTO_SEND).
- ``POST /emails/{id}/send-draft`` with ``{"recipients": [...]}`` emails a test copy.

Retries: HTTP 429 is retried after ``Retry-After`` on every call. Server errors and network
failures are retried only when sending and test-sending. Creating a draft is never retried
after a server error or a timeout, because Buttondown may have created it anyway; the
error then says to check the dashboard before re-running.
"""

from __future__ import annotations

import json
import logging
import math
import re
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import quote

import requests
from requests.auth import AuthBase

log = logging.getLogger(__name__)

API_BASE = "https://api.buttondown.com/v1"
API_VERSION = "2026-04-01"  # Buttondown's current API version (checked 2026-11-16); sent as X-API-Version
DASHBOARD_URL = "https://buttondown.com/emails"

MAX_RETRIES = 2  # retries per call after the first request (shared by 429s and failures)
DEFAULT_RETRY_AFTER = 10.0  # seconds to wait after a 429 that has no usable Retry-After header
MAX_RETRY_AFTER = 60.0  # never wait longer than this before one retry
FAILURE_BACKOFF = (2.0, 4.0)  # waits before the 1st and 2nd retry after a 5xx or network error

# Network failures worth retrying (on calls that may be retried). Other RequestExceptions
# (an invalid URL, for example) would fail the same way again.
_NETWORK_ERRORS = (requests.ConnectionError, requests.Timeout, requests.exceptions.ChunkedEncodingError)
_KEY_CHARS = re.compile(r"[\x21-\x7e]+")  # printable ASCII, no spaces: all a header value may safely hold


class ButtondownError(RuntimeError):
    """A Buttondown API call failed. The message says what to check; it never contains the API key.

    ``status`` is the HTTP status (None for network errors); ``code`` and ``detail`` come
    from Buttondown's error body ``{"code": ..., "detail": ...}`` when there is one.
    """

    def __init__(
        self,
        message: str,
        status: int | None = None,
        code: str | None = None,
        detail: str | None = None,
    ):
        super().__init__(message)
        self.status = status
        self.code = code
        self.detail = detail


class DuplicateEmailError(ButtondownError):
    """Buttondown already has an email with this subject (error code ``email_duplicate``)."""


@dataclass
class CreatedEmail:
    id: str
    status: str
    absolute_url: str | None


class _TokenAuth(AuthBase):
    """Sets ``Authorization: Token <key>``. Passing an auth object (rather than a bare header)
    also stops requests from replacing the header with credentials from a ~/.netrc file."""

    def __init__(self, key: str):
        self._key = key

    def __call__(self, request: requests.PreparedRequest) -> requests.PreparedRequest:
        request.headers["Authorization"] = f"Token {self._key}"
        return request

    def __repr__(self) -> str:
        return "_TokenAuth([redacted])"


class ButtondownClient:
    """The three Buttondown calls this project makes. ``sleep`` is injectable so tests never wait."""

    def __init__(
        self,
        api_key: str | None,
        user_agent: str,
        session: requests.Session | None = None,
        timeout: float = 30.0,
        sleep: Callable[[float], None] = time.sleep,
    ):
        key = (api_key or "").strip()
        if key[:6].lower() == "token ":  # tolerate a secret saved with the header's "Token " prefix
            key = key[6:].strip()
        if not key:
            raise ButtondownError("No Buttondown API key was provided. Set the BUTTONDOWN_API_KEY secret.")
        if not _KEY_CHARS.fullmatch(key):
            # Checked here because the HTTP library's error for a bad header value quotes the value.
            raise ButtondownError(
                "The Buttondown API key contains spaces, line breaks, or other characters an API key cannot "
                "have. Copy the key again from your Buttondown settings into the BUTTONDOWN_API_KEY secret."
            )
        self._key = key
        self.user_agent = user_agent
        self.session = session if session is not None else requests.Session()
        self.timeout = timeout
        self.sleep = sleep

    def __repr__(self) -> str:
        return f"ButtondownClient(api_base={API_BASE!r})"  # never shows the key

    def create_draft(self, subject: str, body: str, metadata: Mapping[str, Any] | None = None) -> CreatedEmail:
        """Create the email as a DRAFT (``"status": "draft"`` is always sent). Expects HTTP 201/200."""
        what = "create the draft"
        payload: dict[str, Any] = {"subject": subject, "body": body, "status": "draft"}
        meta = _metadata(metadata)
        if meta:  # metadata is paid-only for Buttondown accounts created since 2026-04-20; never send an empty one
            payload["metadata"] = meta
        resp = self._call("POST", "/emails", payload, what, retry_failures=False)
        if not _is_2xx(resp):
            raise self._error(resp, what, subject=subject, may_exist=True)
        data = _json_or_none(resp)
        email_id = data.get("id") if isinstance(data, dict) else None
        if email_id is None or not str(email_id).strip():
            raise ButtondownError(
                f"Buttondown answered HTTP {resp.status_code} but its reply had no email id, so it is unclear "
                f"whether the draft was created. Check the Buttondown dashboard ({DASHBOARD_URL}) for a draft "
                "before re-running.",
                status=resp.status_code,
            )
        status = str(data.get("status") or "draft")
        url = data.get("absolute_url")
        created = CreatedEmail(id=str(email_id), status=status, absolute_url=str(url) if url else None)
        if status != "draft":
            log.warning("Buttondown reports the new email's status as %r rather than 'draft'.", status)
        log.info("Created Buttondown draft %s (status: %s).", created.id, created.status)
        return created

    def send_now(self, email_id: str) -> None:
        """Send a draft to subscribers: PATCH its status to ``about_to_send``."""
        what = f"send email {email_id} to subscribers"
        hint = f"The email should still be a draft in Buttondown; you can send it from the dashboard ({DASHBOARD_URL})."
        payload = {"status": "about_to_send"}
        resp = self._call("PATCH", f"/emails/{_quote(email_id)}", payload, what, retry_failures=True, hint=hint)
        if not _is_2xx(resp):
            raise self._error(resp, what, hint=hint)
        log.info("Buttondown accepted email %s for sending to subscribers.", email_id)

    def send_test(self, email_id: str, recipients: Iterable[str]) -> None:
        """Email a test copy of a draft to ``recipients`` only (never to subscribers).

        Does nothing when there are no recipients.
        """
        if isinstance(recipients, str):
            recipients = [recipients]
        addresses = [r.strip() for r in recipients if r and r.strip()]
        if not addresses:
            log.info("No test recipients were given, so no test email was sent.")
            return
        what = f"send a test copy of email {email_id}"
        hint = "The draft itself is in Buttondown; only the test copy failed."
        payload = {"recipients": addresses}
        path = f"/emails/{_quote(email_id)}/send-draft"
        resp = self._call("POST", path, payload, what, retry_failures=True, hint=hint)
        if not _is_2xx(resp):
            raise self._error(resp, what, hint=hint)
        log.info("Buttondown sent a test copy of email %s to %d address(es).", email_id, len(addresses))

    def _call(
        self,
        method: str,
        path: str,
        payload: dict[str, Any],
        what: str,
        *,
        retry_failures: bool,
        hint: str = "",
    ) -> requests.Response:
        """Make one API request with retries (see the module docstring). Returns the final response.

        Raises ButtondownError only when no response arrived; the caller judges the status code.
        """
        url = API_BASE + path
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": self.user_agent,
            # Pin the API version so a future Buttondown default can't change how drafts behave.
            "X-API-Version": API_VERSION,
        }
        auth = _TokenAuth(self._key)  # adds "Authorization: Token <key>"
        retries = 0
        while True:
            started = time.monotonic()
            try:
                # No redirects: requests would turn a redirected PATCH into a GET that "succeeds".
                resp = self.session.request(
                    method,
                    url,
                    json=payload,
                    headers=headers,
                    auth=auth,
                    timeout=self.timeout,
                    allow_redirects=False,
                )
            except requests.RequestException as exc:
                if isinstance(exc, _NETWORK_ERRORS) and retry_failures and retries < MAX_RETRIES:
                    wait = FAILURE_BACKOFF[retries]
                    retries += 1
                    log.warning(
                        "Buttondown %s %s failed (%s); retrying in %g s (retry %d of %d).",
                        method, path, self._exc_text(exc), wait, retries, MAX_RETRIES,
                    )
                    self.sleep(wait)
                    continue
                # Creating a draft is never retried: the request may have reached Buttondown.
                raise self._network_error(
                    exc, what, attempts=retries + 1, may_exist=not retry_failures, hint=hint
                ) from exc
            log.info("Buttondown %s %s -> HTTP %d (%.1f s)", method, path, resp.status_code, time.monotonic() - started)
            if retries >= MAX_RETRIES:
                return resp
            if resp.status_code == 429:
                wait = retry_after_seconds(resp.headers.get("Retry-After"))
                reason = "rate limited (HTTP 429)"
            elif resp.status_code >= 500 and retry_failures:
                wait = FAILURE_BACKOFF[retries]
                reason = f"server error (HTTP {resp.status_code})"
            else:
                return resp
            retries += 1
            log.warning(
                "Buttondown %s %s: %s; retrying in %g s (retry %d of %d).",
                method, path, reason, wait, retries, MAX_RETRIES,
            )
            self.sleep(wait)

    def _error(
        self,
        resp: requests.Response,
        what: str,
        *,
        subject: str | None = None,
        may_exist: bool = False,
        hint: str = "",
    ) -> ButtondownError:
        """A ButtondownError with a message that says what went wrong and what to do."""
        status = resp.status_code
        code, detail = _error_fields(resp)
        if 300 <= status < 400 and not detail and resp.headers.get("Location"):
            detail = f"redirected to {resp.headers['Location']}"
        detail = self._scrub(detail) if detail else detail
        error_class = ButtondownError
        if code == "email_duplicate":
            error_class = DuplicateEmailError
            message = (
                "Buttondown already has an email with this subject. If a previous run created it, check your "
                f"Buttondown drafts ({DASHBOARD_URL}): delete that email or change its subject, then re-run this "
                "workflow so these opinions are recorded as processed. If that email was already sent, turn "
                "AUTO_SEND off for the re-run so subscribers do not get the digest twice."
            )
            if subject:
                message += f" (Subject: {subject})"
        elif status == 401:
            message = "Buttondown rejected the API key (HTTP 401). Check the BUTTONDOWN_API_KEY secret."
        elif code == "email_not_confirmed":
            message = (
                f"Buttondown will not {what} until you confirm the email address of your Buttondown account "
                "(code email_not_confirmed). Open the confirmation email Buttondown sent you (or resend it from "
                "your Buttondown settings), confirm the address, then re-run."
            )
        elif code == "sending_requires_confirmation":
            message = (
                f"Buttondown wants an extra confirmation before it will {what} ({_describe(status, code, detail)}). "
                f"Send it from the Buttondown dashboard ({DASHBOARD_URL}) instead."
            )
        elif code == "body_contains_frontmatter":
            message = (
                "Buttondown refused the email body because it starts with '---', which Buttondown reads as front "
                "matter (code body_contains_frontmatter). The digest body must start with the editor-mode "
                "comment; this is a bug in src/digest.py."
            )
        elif status == 429:
            message = (
                f"Buttondown kept rate-limiting requests (HTTP 429), so it could not {what} after "
                f"{MAX_RETRIES} retries. Wait a few minutes, then re-run."
            )
        elif status >= 500 and may_exist:
            message = (
                f"Buttondown had a server error while trying to {what} ({_describe(status, code, detail)}). "
                f"The draft may have been created anyway: check the Buttondown dashboard ({DASHBOARD_URL}) for "
                "a draft before re-running, so the digest is not created twice."
            )
        else:
            message = f"Buttondown could not {what} ({_describe(status, code, detail)})."
        if hint:
            message += " " + hint
        return error_class(self._scrub(message), status=status, code=code, detail=detail)

    def _network_error(
        self, exc: Exception, what: str, *, attempts: int, may_exist: bool, hint: str = ""
    ) -> ButtondownError:
        text = self._exc_text(exc)
        if may_exist:
            message = (
                f"The request to Buttondown to {what} failed ({text}). The draft may have been created anyway: "
                f"check the Buttondown dashboard ({DASHBOARD_URL}) for a draft before re-running, so the digest "
                "is not created twice."
            )
        else:
            plural = "s" if attempts != 1 else ""
            message = f"The request to Buttondown to {what} failed after {attempts} attempt{plural} ({text})."
        if hint:
            message += " " + hint
        return ButtondownError(self._scrub(message))

    def _exc_text(self, exc: Exception) -> str:
        return self._scrub(f"{type(exc).__name__}: {_shorten(str(exc), 200) or 'no details'}")

    def _scrub(self, text: str) -> str:
        """Belt and braces: the key must never appear in a message or log line (plain or repr-escaped)."""
        if len(self._key) < 6:
            return text
        for form in (self._key, repr(self._key)[1:-1]):
            text = text.replace(form, "[redacted]")
        return text


def retry_after_seconds(value: str | None) -> float:
    """Seconds to wait from a Retry-After header (seconds or an HTTP date): 0..60, 10 when absent."""
    if value is None or not value.strip():
        return DEFAULT_RETRY_AFTER
    value = value.strip()
    try:
        seconds = float(value)
    except ValueError:
        try:
            when = parsedate_to_datetime(value)
        except (TypeError, ValueError, IndexError):
            return DEFAULT_RETRY_AFTER
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        seconds = (when - datetime.now(timezone.utc)).total_seconds()
    if math.isnan(seconds):
        return DEFAULT_RETRY_AFTER
    return min(max(seconds, 0.0), MAX_RETRY_AFTER)


def _metadata(metadata: Mapping[str, Any] | None) -> dict[str, str]:
    """Metadata with string values (lists joined with ", "), a shape Buttondown always accepts."""
    out: dict[str, str] = {}
    for key, value in (metadata or {}).items():
        if value is None:
            continue
        if isinstance(value, str):
            text = value
        elif isinstance(value, bool):
            text = "true" if value else "false"
        elif isinstance(value, (int, float)):
            text = str(value)
        elif isinstance(value, (list, tuple)):
            text = ", ".join(str(v) for v in value)
        elif isinstance(value, (set, frozenset)):
            text = ", ".join(sorted(str(v) for v in value))
        else:
            text = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
        out[str(key)] = text
    return out


def _error_fields(resp: requests.Response) -> tuple[str | None, str | None]:
    """(code, detail) from an error reply such as ``{"code": "email_duplicate", "detail": "..."}``."""
    data = _json_or_none(resp)
    if data is None:
        text = re.sub(r"<[^>]+>", " ", resp.text or "")  # an HTML error page from a proxy, say
        return None, _shorten(text)
    if isinstance(data, dict):
        code = data.get("code")
        detail = data.get("detail")
        if detail is None:
            detail = data.get("message", data.get("error"))
        return (str(code) if code not in (None, "") else None), _detail_text(detail)
    return None, _detail_text(data)


def _detail_text(detail: Any) -> str | None:
    """Readable text for a ``detail`` that may be a string, a list of validation errors, or an object."""
    if detail is None:
        return None
    if isinstance(detail, str):
        return _shorten(detail)
    if isinstance(detail, list):
        parts = [_detail_text(d) for d in detail]
        return _shorten("; ".join(p for p in parts if p))
    if isinstance(detail, dict):
        message = detail.get("msg") or detail.get("message") or detail.get("detail")
        if message:
            loc = detail.get("loc")
            where = ".".join(str(x) for x in loc) if isinstance(loc, list) and loc else ""
            return _shorten(f"{where}: {message}" if where else str(message))
        return _shorten(json.dumps(detail, ensure_ascii=False, sort_keys=True, default=str))
    return _shorten(str(detail))


def _describe(status: int, code: str | None, detail: str | None) -> str:
    text = f"HTTP {status}"
    if code:
        text += f", code {code}"
    if detail:
        text += f": {detail}"
    return text


def _shorten(text: str, limit: int = 300) -> str | None:
    text = " ".join(text.split())
    if not text:
        return None
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _json_or_none(resp: requests.Response) -> Any:
    try:
        return resp.json()
    except ValueError:
        return None


def _is_2xx(resp: requests.Response) -> bool:
    return 200 <= resp.status_code < 300


def _quote(email_id: str) -> str:
    return quote(str(email_id), safe="")
