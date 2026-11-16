"""Tests for src/net.py: throttling, retries, the size cap, and FetchError."""

from __future__ import annotations

import logging

import pytest
import requests
import responses
from urllib3.response import HTTPResponse
from urllib3.util.retry import Retry

from src import net
from src.net import DEFAULT_MAX_BYTES, FetchError, PoliteSession

UA = "SC-Advance-Sheet-Digest/test (pytest)"
URL = "https://www.courtlistener.com/feed/court/sc/"
OTHER_HOST_URL = "https://storage.courtlistener.com/pdf/x.pdf"


class FakeTime:
    """Stands in for the ``time`` module inside src.net: a clock that only moves when told to."""

    def __init__(self, start: float = 1000.0):
        self.now = start
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clock(monkeypatch) -> FakeTime:
    fake = FakeTime()
    monkeypatch.setattr(net, "time", fake)
    return fake


@pytest.fixture
def web():
    with responses.RequestsMock(assert_all_requests_are_fired=False) as rsps:
        yield rsps


# --------------------------------------------------------------------------- throttling


def test_requests_to_the_same_host_are_spaced_out(clock, web):
    web.get(URL, body="ok")
    session = PoliteSession(UA, min_interval=2.0, max_retries=0)
    session.get(URL)
    assert clock.sleeps == []  # the first request never waits
    clock.advance(0.5)
    session.get(URL)
    assert clock.sleeps == [pytest.approx(1.5)]
    clock.advance(5.0)
    session.get(URL)
    assert clock.sleeps == [pytest.approx(1.5)]  # enough time had passed


def test_throttling_is_per_host(clock, web):
    web.get(URL, body="ok")
    web.get(OTHER_HOST_URL, body=b"%PDF")
    session = PoliteSession(UA, min_interval=2.0, max_retries=0)
    session.get(URL)
    session.get(OTHER_HOST_URL)
    assert clock.sleeps == []
    session.get(OTHER_HOST_URL)
    assert len(clock.sleeps) == 1


def test_host_matching_ignores_case(clock, web):
    web.get(URL, body="ok")
    session = PoliteSession(UA, min_interval=2.0, max_retries=0)
    session.get(URL)
    session.get(URL.replace("www.courtlistener.com", "WWW.CourtListener.com"))
    assert len(clock.sleeps) == 1


def test_zero_interval_never_sleeps(clock, web):
    web.get(URL, body="ok")
    session = PoliteSession(UA, min_interval=0, max_retries=0)
    for _ in range(3):
        session.get(URL)
    assert clock.sleeps == []


def test_negative_interval_is_treated_as_zero():
    assert PoliteSession(UA, min_interval=-5).min_interval == 0.0


# --------------------------------------------------------------------------- requests and responses


def test_every_request_identifies_the_tool_and_has_a_timeout(web):
    web.get(URL, body="ok")
    PoliteSession(UA, min_interval=0, max_retries=0).get(URL, headers={"Accept": "application/atom+xml"})
    request = web.calls[0].request
    assert request.headers["User-Agent"] == UA
    assert request.headers["Accept"] == "application/atom+xml"
    assert request.headers["Accept-Language"].startswith("en-US")
    assert request.req_kwargs["timeout"] == (10.0, 60.0)
    assert request.req_kwargs["stream"] is True


def test_params_are_sent(web):
    web.get(URL, body="ok")
    PoliteSession(UA, min_interval=0, max_retries=0).get(URL, params={"court": "sc scctapp", "type": "o"})
    assert web.calls[0].request.url == URL + "?court=sc+scctapp&type=o"


def test_get_bytes_and_the_counters(web):
    web.get(OTHER_HOST_URL, body=b"%PDF-1.4 data")
    session = PoliteSession(UA, min_interval=0, max_retries=0)
    assert session.get_bytes(OTHER_HOST_URL) == b"%PDF-1.4 data"
    assert session.request_count == 1 and session.bytes_downloaded == len(b"%PDF-1.4 data")


def test_get_text_uses_the_declared_charset(web):
    web.get(URL, body="Section § and en dash –".encode("utf-8"), content_type="text/html; charset=utf-8")
    assert PoliteSession(UA, min_interval=0, max_retries=0).get_text(URL) == "Section § and en dash –"


def test_get_text_guesses_the_encoding_when_none_is_declared(web):
    body = ("<p>§ 17-27-20 – " + "the court of appeals " * 20 + "</p>").encode("utf-8")
    web.get(URL, body=body, content_type="text/html")
    text = PoliteSession(UA, min_interval=0, max_retries=0).get_text(URL)
    assert "§ 17-27-20 –" in text  # not mojibake from the ISO-8859-1 default


def test_get_json(web):
    web.get(URL, json={"count": 1, "results": []})
    assert PoliteSession(UA, min_interval=0, max_retries=0).get_json(URL) == {"count": 1, "results": []}


def test_get_json_on_a_non_json_body_raises_fetch_error(web):
    web.get(URL, body="<html>maintenance</html>", content_type="text/html")
    with pytest.raises(FetchError, match="did not return JSON") as exc:
        PoliteSession(UA, min_interval=0, max_retries=0).get_json(URL)
    assert exc.value.status == 200 and exc.value.url == URL


# --------------------------------------------------------------------------- errors


def test_404_raises_fetch_error_with_the_status(web):
    web.get(URL, status=404)
    with pytest.raises(FetchError) as exc:
        PoliteSession(UA, min_interval=0, max_retries=0).get(URL)
    assert exc.value.status == 404
    assert exc.value.url == URL
    assert "HTTP 404" in str(exc.value)


def test_network_error_raises_fetch_error_without_a_status(web):
    web.get(URL, body=requests.ConnectionError("connection refused"))
    with pytest.raises(FetchError) as exc:
        PoliteSession(UA, min_interval=0, max_retries=0).get(URL)
    assert exc.value.status is None
    assert "ConnectionError" in str(exc.value)


def test_fetch_error_is_a_runtime_error():
    err = FetchError("GET x failed", "https://example.com/x", 503)
    assert isinstance(err, RuntimeError) and err.status == 503 and err.url == "https://example.com/x"
    assert FetchError("m", "u").status is None


def test_a_response_over_the_size_cap_is_refused(web):
    web.get(OTHER_HOST_URL, body=b"x" * 2048)
    session = PoliteSession(UA, min_interval=0, max_retries=0)
    with pytest.raises(FetchError, match="larger than") as exc:
        session.get(OTHER_HOST_URL, max_bytes=1024)
    assert exc.value.status is None
    assert session.get_bytes(OTHER_HOST_URL, max_bytes=4096) == b"x" * 2048  # under the cap is fine


def test_default_size_cap():
    assert DEFAULT_MAX_BYTES == 40 * 1024 * 1024


# --------------------------------------------------------------------------- retries


def _retry_of(session: PoliteSession) -> Retry:
    return session.session.get_adapter("https://www.courtlistener.com/").max_retries


def test_retry_configuration():
    session = PoliteSession(UA)
    retry = _retry_of(session)
    assert session.session.get_adapter("http://example.com/").max_retries is retry  # one policy for both schemes
    assert retry.total == 4 and retry.connect == 4 and retry.read == 4 and retry.status == 4
    assert retry.backoff_factor == 2.0
    assert set(retry.status_forcelist) == {429, 500, 502, 503, 504}
    assert retry.allowed_methods == frozenset({"GET", "HEAD"})  # never retries a POST
    assert retry.respect_retry_after_header is True
    assert retry.raise_on_status is False  # the final 5xx comes back as a response, then FetchError


def test_retry_count_is_configurable():
    assert _retry_of(PoliteSession(UA, max_retries=0)).total == 0
    assert _retry_of(PoliteSession(UA, max_retries=2, backoff_factor=0.5)).backoff_factor == 0.5


def test_a_transient_server_error_is_retried(web):
    web.get(URL, status=503)
    web.get(URL, body="ok")  # responses serves registered replies in order
    session = PoliteSession(UA, min_interval=0, max_retries=1, backoff_factor=0)
    assert session.get_text(URL) == "ok"
    assert len(web.calls) == 2


def test_persistent_server_errors_end_in_fetch_error(web):
    web.get(URL, status=503)
    session = PoliteSession(UA, min_interval=0, max_retries=2, backoff_factor=0)
    with pytest.raises(FetchError) as exc:
        session.get(URL)
    assert exc.value.status == 503
    assert len(web.calls) == 3  # the first request plus two retries


def test_client_errors_are_not_retried(web):
    web.get(URL, status=404)
    with pytest.raises(FetchError):
        PoliteSession(UA, min_interval=0, max_retries=3, backoff_factor=0).get(URL)
    assert len(web.calls) == 1


def test_an_injected_session_is_used_and_configured():
    inner = requests.Session()
    session = PoliteSession(UA, session=inner)
    assert session.session is inner
    assert inner.headers["User-Agent"] == UA


# --------------------------------------------------------------------------- polite retries


def _http_response(status: int, headers: dict | None = None) -> HTTPResponse:
    return HTTPResponse(body=b"", status=status, headers=headers or {}, preload_content=True)


def test_retry_policy_has_a_floor_and_a_retry_after_ceiling():
    retry = _retry_of(PoliteSession(UA, min_interval=2.0))
    assert retry.min_backoff == 2.0
    assert retry.max_retry_after == net.MAX_RETRY_AFTER_SECONDS == 60.0
    assert _retry_of(PoliteSession(UA, min_interval=0)).min_backoff == 0.0


def test_a_long_retry_after_is_not_waited_out_and_fails_fast(web, caplog):
    # CourtListener's hourly/daily limits answer 429 with a Retry-After of up to hours; urllib3 would
    # sleep it out (up to 6 hours per retry) and the job would hit its time limit with no message.
    web.get(URL, status=429, headers={"Retry-After": "3600"})
    session = PoliteSession(UA, min_interval=0, max_retries=4)  # the production retry count
    with caplog.at_level(logging.WARNING, logger="src.net"):
        with pytest.raises(FetchError) as exc:
            session.get(URL)
    assert exc.value.status == 429
    assert len(web.calls) == 1  # no retries
    assert any("asks to wait 3600 s" in r.getMessage() and "not retried" in r.getMessage() for r in caplog.records)


def test_a_short_retry_after_is_retried_and_logged(web, caplog):
    web.get(URL, status=503, headers={"Retry-After": "5"})
    web.get(URL, body="ok")
    session = PoliteSession(UA, min_interval=0, max_retries=4)
    with caplog.at_level(logging.WARNING, logger="src.net"):
        assert session.get_text(URL) == "ok"
    assert len(web.calls) == 2
    assert any("HTTP 503; retrying in 5 s (retry 1 of 4)" in r.getMessage() for r in caplog.records)


def test_the_first_retry_waits_at_least_the_per_host_spacing(clock):
    # urllib3's exponential backoff is 0 before the first retry; a retry is a request to the same host.
    retry = _retry_of(PoliteSession(UA, min_interval=2.0, max_retries=4))
    after_first = retry.increment(method="GET", url="/x", response=_http_response(503))
    assert after_first.get_backoff_time() == 2.0
    after_first.sleep(_http_response(503))
    assert clock.sleeps == [2.0]
    after_second = after_first.increment(method="GET", url="/x", response=_http_response(503))
    assert after_second.get_backoff_time() == 4.0  # backoff_factor 2: 2 * 2**1, already over the floor


def test_a_tiny_retry_after_is_raised_to_the_per_host_spacing(clock):
    retry = _retry_of(PoliteSession(UA, min_interval=2.0, max_retries=4))
    response = _http_response(503, {"Retry-After": "1"})
    retry.increment(method="GET", url="/x", response=response).sleep(response)
    assert clock.sleeps == [2.0]


def test_the_next_request_is_spaced_from_the_end_of_the_previous_one(clock, web):
    def slow(request):
        clock.advance(5.0)  # the transfer (or its retries) took 5 seconds
        return 200, {}, "ok"

    web.add_callback(responses.GET, URL, callback=slow)
    session = PoliteSession(UA, min_interval=2.0, max_retries=0)
    session.get(URL)
    session.get(URL)
    assert clock.sleeps == [pytest.approx(2.0)]  # measured from when the first one finished


def test_fetch_error_keeps_the_start_of_the_error_body(web):
    web.get(URL, status=400, json={"detail": "Invalid parameter: filed_after"})
    with pytest.raises(FetchError) as exc:
        PoliteSession(UA, min_interval=0, max_retries=0).get(URL)
    assert b"Invalid parameter" in exc.value.body
    assert FetchError("m", "u").body is None
