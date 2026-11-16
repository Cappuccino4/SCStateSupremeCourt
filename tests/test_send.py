"""Tests for src/send.py (Buttondown). All HTTP is mocked with ``responses``; sleeps are recorded, not taken."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime

import pytest
import requests
import responses

from src.send import (
    API_BASE,
    DASHBOARD_URL,
    DEFAULT_RETRY_AFTER,
    MAX_RETRY_AFTER,
    ButtondownClient,
    ButtondownError,
    CreatedEmail,
    DuplicateEmailError,
    retry_after_seconds,
)

KEY = "bd-test-4f9c2a7e1b8d4c3a9e6f"  # fake; distinctive so leaks are easy to spot
UA = "SC-Advance-Sheet-Digest/1.0.0 (+https://github.com/example/sc-advance-sheet-digest)"
EMAILS_URL = f"{API_BASE}/emails"
EMAIL_ID = "6f2c1a3e-9b8d-4c7a-8e5f-0a1b2c3d4e5f"
EMAIL_URL = f"{API_BASE}/emails/{EMAIL_ID}"
SEND_DRAFT_URL = f"{EMAIL_URL}/send-draft"
SUBJECT = "SC Advance Sheet Digest — November 12, 2026 — 3 opinions + 1 order"
BODY = (
    "<!-- buttondown-editor-mode: plaintext -->\n"
    "**SC Advance Sheet Digest** · November 12, 2026 · Advance Sheet No. 41\n\n"
    "Text with § 1–2 and “quotes”."
)
CREATED = {
    "id": EMAIL_ID,
    "status": "draft",
    "subject": SUBJECT,
    "absolute_url": "https://buttondown.com/sc-digest/archive/sc-advance-sheet-digest-november-12-2026/",
}


class RecordingSleep:
    """Stands in for time.sleep: records the waits so tests run instantly."""

    def __init__(self):
        self.calls: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


@pytest.fixture
def mock():
    with responses.RequestsMock(assert_all_requests_are_fired=True) as rsps:
        yield rsps


@pytest.fixture
def sleeper():
    return RecordingSleep()


@pytest.fixture
def client(sleeper):
    return ButtondownClient(KEY, UA, sleep=sleeper)


def sent_json(mock, index: int = 0) -> dict:
    return json.loads(mock.calls[index].request.body)


def err(code: str | None, detail) -> dict:
    """A Buttondown error body."""
    return {"code": code, "detail": detail} if code else {"detail": detail}


def assert_no_key(*texts: str) -> None:
    for text in texts:
        assert KEY not in text


# ---------------------------------------------------------------- create_draft


def test_create_draft_sends_status_draft_auth_and_user_agent(mock, client, sleeper):
    mock.add(responses.POST, EMAILS_URL, json=CREATED, status=201)
    metadata = {"items": ["SC:28354", "SC:28355"], "prompt_version": "2026-11-16.1"}
    created = client.create_draft(SUBJECT, BODY, metadata)

    assert created == CreatedEmail(id=EMAIL_ID, status="draft", absolute_url=CREATED["absolute_url"])
    assert len(mock.calls) == 1
    request = mock.calls[0].request
    assert request.method == "POST"
    assert request.url == EMAILS_URL
    assert request.headers["Authorization"] == f"Token {KEY}"
    assert request.headers["User-Agent"] == UA
    assert request.headers["Content-Type"] == "application/json"
    assert request.headers["Accept"] == "application/json"
    assert sent_json(mock) == {
        "subject": SUBJECT,
        "body": BODY,
        "status": "draft",
        "metadata": {"items": "SC:28354, SC:28355", "prompt_version": "2026-11-16.1"},
    }
    assert sleeper.calls == []


def test_create_draft_always_sends_status_draft_without_metadata(mock, client):
    mock.add(responses.POST, EMAILS_URL, json=CREATED, status=200)  # 200 is accepted too
    created = client.create_draft(SUBJECT, BODY)
    assert created.id == EMAIL_ID
    body = sent_json(mock)
    assert body["status"] == "draft"
    # Metadata is paid-only for new Buttondown accounts, so no metadata key is sent at all.
    assert "metadata" not in body
    assert body == {"subject": SUBJECT, "body": BODY, "status": "draft"}


def test_every_call_pins_the_buttondown_api_version(mock, client):
    mock.add(responses.POST, EMAILS_URL, json=CREATED, status=201)
    mock.add(responses.POST, f"{EMAILS_URL}/{EMAIL_ID}/send-draft", status=200)
    mock.add(responses.PATCH, f"{EMAILS_URL}/{EMAIL_ID}", json=CREATED, status=200)
    client.create_draft(SUBJECT, BODY)
    client.send_test(EMAIL_ID, ["editor@example.com"])
    client.send_now(EMAIL_ID)
    assert [call.request.headers["X-API-Version"] for call in mock.calls] == ["2026-04-01"] * 3


def test_metadata_values_are_sent_as_strings(mock, client):
    mock.add(responses.POST, EMAILS_URL, json=CREATED, status=201)
    metadata = {
        "items": ("SC:28355", "COA:6170"),
        "count": 3,
        "mock": False,
        "skip": None,
        "costs": {"usd": 0.12},
        "tags": {"b", "a"},
    }
    client.create_draft(SUBJECT, BODY, metadata)
    assert sent_json(mock)["metadata"] == {
        "items": "SC:28355, COA:6170",
        "count": "3",
        "mock": "false",
        "costs": '{"usd": 0.12}',
        "tags": "a, b",
    }


def test_create_draft_tolerates_missing_optional_fields(mock, client):
    mock.add(responses.POST, EMAILS_URL, json={"id": 12345}, status=201)
    created = client.create_draft(SUBJECT, BODY)
    assert created == CreatedEmail(id="12345", status="draft", absolute_url=None)


def test_create_draft_warns_if_status_is_not_draft(mock, client, caplog):
    mock.add(responses.POST, EMAILS_URL, json={**CREATED, "status": "about_to_send"}, status=201)
    with caplog.at_level(logging.WARNING, logger="src.send"):
        created = client.create_draft(SUBJECT, BODY)
    assert created.status == "about_to_send"
    assert "rather than 'draft'" in caplog.text


@pytest.mark.parametrize("reply", [{"status": "draft"}, {"id": ""}, ["not", "an", "object"]])
def test_create_draft_without_an_id_is_an_error(mock, client, reply):
    mock.add(responses.POST, EMAILS_URL, json=reply, status=201)
    with pytest.raises(ButtondownError) as excinfo:
        client.create_draft(SUBJECT, BODY)
    assert "no email id" in str(excinfo.value)
    assert DASHBOARD_URL in str(excinfo.value)


def test_create_draft_retries_after_429(mock, client, sleeper):
    slow_down = err("rate_limited", "Slow down.")
    mock.add(responses.POST, EMAILS_URL, json=slow_down, status=429, headers={"Retry-After": "7"})
    mock.add(responses.POST, EMAILS_URL, json=CREATED, status=201)
    created = client.create_draft(SUBJECT, BODY)
    assert created.id == EMAIL_ID
    assert len(mock.calls) == 2
    assert sleeper.calls == [7.0]
    assert sent_json(mock, 1)["status"] == "draft"


def test_429_without_retry_after_waits_the_default(mock, client, sleeper):
    mock.add(responses.POST, EMAILS_URL, status=429)
    mock.add(responses.POST, EMAILS_URL, json=CREATED, status=201)
    client.create_draft(SUBJECT, BODY)
    assert sleeper.calls == [DEFAULT_RETRY_AFTER] == [10.0]


def test_long_retry_after_is_capped(mock, client, sleeper):
    mock.add(responses.POST, EMAILS_URL, status=429, headers={"Retry-After": "3600"})
    mock.add(responses.POST, EMAILS_URL, json=CREATED, status=201)
    client.create_draft(SUBJECT, BODY)
    assert sleeper.calls == [MAX_RETRY_AFTER] == [60.0]


def test_429_is_retried_at_most_twice(mock, client, sleeper):
    slow_down = err("rate_limited", "Slow down.")
    mock.add(responses.POST, EMAILS_URL, json=slow_down, status=429, headers={"Retry-After": "1"})
    with pytest.raises(ButtondownError) as excinfo:
        client.create_draft(SUBJECT, BODY)
    assert len(mock.calls) == 3
    assert sleeper.calls == [1.0, 1.0]
    assert excinfo.value.status == 429
    assert "rate-limiting" in str(excinfo.value)
    assert not isinstance(excinfo.value, DuplicateEmailError)


@pytest.mark.parametrize("status", [500, 502, 503, 504])
def test_server_error_on_create_is_not_retried(mock, client, sleeper, status):
    mock.add(responses.POST, EMAILS_URL, json=err("server_error", "Something broke."), status=status)
    with pytest.raises(ButtondownError) as excinfo:
        client.create_draft(SUBJECT, BODY)
    assert len(mock.calls) == 1  # it may have been created: never retry
    assert sleeper.calls == []
    error = excinfo.value
    assert error.status == status
    assert error.code == "server_error"
    assert error.detail == "Something broke."
    message = str(error)
    assert f"HTTP {status}" in message
    assert "may have been created" in message
    assert "check the Buttondown dashboard" in message
    assert DASHBOARD_URL in message
    assert "before re-running" in message


def test_server_error_with_html_body_on_create(mock, client):
    page = "<html><body><h1>502 Bad Gateway</h1></body></html>"
    mock.add(responses.POST, EMAILS_URL, body=page, status=502, content_type="text/html")
    with pytest.raises(ButtondownError) as excinfo:
        client.create_draft(SUBJECT, BODY)
    assert len(mock.calls) == 1
    assert excinfo.value.detail == "502 Bad Gateway"
    assert "before re-running" in str(excinfo.value)


@pytest.mark.parametrize(
    "exc",
    [
        requests.ConnectionError("Connection aborted."),
        requests.exceptions.ReadTimeout("Read timed out. (read timeout=30.0)"),
    ],
)
def test_network_failure_on_create_is_not_retried(mock, client, sleeper, exc):
    mock.add(responses.POST, EMAILS_URL, body=exc)
    with pytest.raises(ButtondownError) as excinfo:
        client.create_draft(SUBJECT, BODY)
    assert len(mock.calls) == 1
    assert sleeper.calls == []
    message = str(excinfo.value)
    assert type(exc).__name__ in message
    assert "check the Buttondown dashboard" in message
    assert "before re-running" in message
    assert excinfo.value.status is None


def test_duplicate_subject_raises_duplicate_email_error(mock, client, sleeper):
    mock.add(
        responses.POST,
        EMAILS_URL,
        json={"code": "email_duplicate", "detail": "An email with this subject already exists."},
        status=400,
    )
    with pytest.raises(DuplicateEmailError) as excinfo:
        client.create_draft(SUBJECT, BODY)
    error = excinfo.value
    assert isinstance(error, ButtondownError)
    assert error.status == 400
    assert error.code == "email_duplicate"
    assert error.detail == "An email with this subject already exists."
    message = str(error)
    assert message.startswith(
        "Buttondown already has an email with this subject. If a previous run created it, check your Buttondown drafts"
    )
    assert SUBJECT in message
    assert len(mock.calls) == 1
    assert sleeper.calls == []


def test_401_message(mock, client):
    mock.add(responses.POST, EMAILS_URL, json={"detail": "Invalid token."}, status=401)
    with pytest.raises(ButtondownError) as excinfo:
        client.create_draft(SUBJECT, BODY)
    assert str(excinfo.value) == "Buttondown rejected the API key (HTTP 401). Check the BUTTONDOWN_API_KEY secret."
    assert excinfo.value.status == 401
    assert len(mock.calls) == 1


def test_email_not_confirmed_tells_the_user_to_confirm(mock, client):
    mock.add(
        responses.POST,
        EMAILS_URL,
        json=err("email_not_confirmed", "You need to confirm your email address before sending emails."),
        status=400,
    )
    with pytest.raises(ButtondownError) as excinfo:
        client.create_draft(SUBJECT, BODY)
    message = str(excinfo.value)
    assert "confirm the email address of your Buttondown account" in message
    assert excinfo.value.code == "email_not_confirmed"


def test_frontmatter_error_is_explained(mock, client):
    mock.add(responses.POST, EMAILS_URL, json=err("body_contains_frontmatter", "Front matter."), status=400)
    with pytest.raises(ButtondownError, match="starts with '---'"):
        client.create_draft("Subject", "---\ntitle: x\n---\nbody")


def test_other_errors_include_status_code_and_detail(mock, client):
    mock.add(responses.POST, EMAILS_URL, json=err("subject_invalid", "Subjects cannot be blank."), status=400)
    with pytest.raises(ButtondownError) as excinfo:
        client.create_draft("", BODY)
    error = excinfo.value
    assert str(error) == (
        "Buttondown could not create the draft (HTTP 400, code subject_invalid: Subjects cannot be blank.)."
    )
    assert (error.status, error.code, error.detail) == (400, "subject_invalid", "Subjects cannot be blank.")
    assert not isinstance(error, DuplicateEmailError)


def test_validation_error_list_is_readable(mock, client):
    mock.add(
        responses.POST,
        EMAILS_URL,
        json={"detail": [{"type": "missing", "loc": ["body", "subject"], "msg": "Field required"}]},
        status=422,
    )
    with pytest.raises(ButtondownError) as excinfo:
        client.create_draft(SUBJECT, BODY)
    assert excinfo.value.detail == "body.subject: Field required"
    assert "HTTP 422" in str(excinfo.value)
    assert excinfo.value.code is None


def test_empty_error_body(mock, client):
    mock.add(responses.POST, EMAILS_URL, body="", status=403)
    with pytest.raises(ButtondownError) as excinfo:
        client.create_draft(SUBJECT, BODY)
    assert str(excinfo.value) == "Buttondown could not create the draft (HTTP 403)."
    assert excinfo.value.detail is None


def test_redirect_is_not_followed(mock, client):
    mock.add(responses.POST, EMAILS_URL, status=301, headers={"Location": f"{EMAILS_URL}/"})
    with pytest.raises(ButtondownError) as excinfo:
        client.create_draft(SUBJECT, BODY)
    assert len(mock.calls) == 1
    assert "HTTP 301" in str(excinfo.value)
    assert "redirected to" in str(excinfo.value)


# ---------------------------------------------------------------- send_now


def test_send_now_patches_status_about_to_send(mock, client, sleeper):
    mock.add(responses.PATCH, EMAIL_URL, json={**CREATED, "status": "about_to_send"}, status=200)
    assert client.send_now(EMAIL_ID) is None
    request = mock.calls[0].request
    assert request.method == "PATCH"
    assert request.url == EMAIL_URL
    assert json.loads(request.body) == {"status": "about_to_send"}
    assert request.headers["Authorization"] == f"Token {KEY}"
    assert request.headers["User-Agent"] == UA
    assert "X-Buttondown-Live-Dangerously" not in request.headers
    assert sleeper.calls == []


def test_send_now_retries_after_503(mock, client, sleeper):
    mock.add(responses.PATCH, EMAIL_URL, json={"detail": "Service unavailable"}, status=503)
    mock.add(responses.PATCH, EMAIL_URL, json={**CREATED, "status": "about_to_send"}, status=200)
    client.send_now(EMAIL_ID)
    assert len(mock.calls) == 2
    assert sleeper.calls == [2.0]
    assert json.loads(mock.calls[1].request.body) == {"status": "about_to_send"}


def test_send_now_gives_up_after_two_retries_with_backoff(mock, client, sleeper):
    mock.add(responses.PATCH, EMAIL_URL, json={"detail": "Bad gateway"}, status=502)
    with pytest.raises(ButtondownError) as excinfo:
        client.send_now(EMAIL_ID)
    assert len(mock.calls) == 3
    assert sleeper.calls == [2.0, 4.0]
    message = str(excinfo.value)
    assert f"could not send email {EMAIL_ID} to subscribers (HTTP 502: Bad gateway)" in message
    assert DASHBOARD_URL in message  # where to send it by hand
    assert excinfo.value.status == 502


def test_send_now_retries_network_failures(mock, client, sleeper):
    mock.add(responses.PATCH, EMAIL_URL, body=requests.ConnectionError("Connection reset by peer"))
    mock.add(responses.PATCH, EMAIL_URL, body=requests.exceptions.ConnectTimeout("Connect timed out"))
    mock.add(responses.PATCH, EMAIL_URL, json=CREATED, status=200)
    client.send_now(EMAIL_ID)
    assert len(mock.calls) == 3
    assert sleeper.calls == [2.0, 4.0]


def test_send_now_network_failure_after_retries(mock, client, sleeper):
    mock.add(responses.PATCH, EMAIL_URL, body=requests.ConnectionError("Name or service not known"))
    with pytest.raises(ButtondownError) as excinfo:
        client.send_now(EMAIL_ID)
    assert len(mock.calls) == 3
    assert sleeper.calls == [2.0, 4.0]
    message = str(excinfo.value)
    assert "failed after 3 attempts" in message
    assert "ConnectionError: Name or service not known" in message
    assert DASHBOARD_URL in message


def test_other_request_failures_are_not_retried(mock, client, sleeper):
    exc = requests.exceptions.ContentDecodingError("Received response with content-encoding: gzip, but failed to decode")
    mock.add(responses.PATCH, EMAIL_URL, body=exc)
    with pytest.raises(ButtondownError) as excinfo:
        client.send_now(EMAIL_ID)
    assert len(mock.calls) == 1
    assert sleeper.calls == []
    assert "failed after 1 attempt (ContentDecodingError" in str(excinfo.value)


def test_any_request_failure_on_create_points_to_the_dashboard(mock, client, sleeper):
    # The reply arrived but could not be decoded, so the draft may well exist.
    mock.add(responses.POST, EMAILS_URL, body=requests.exceptions.ContentDecodingError("failed to decode it"))
    with pytest.raises(ButtondownError) as excinfo:
        client.create_draft(SUBJECT, BODY)
    assert len(mock.calls) == 1
    assert sleeper.calls == []
    assert "may have been created anyway" in str(excinfo.value)
    assert "before re-running" in str(excinfo.value)


def test_send_now_honors_retry_after_on_429(mock, client, sleeper):
    mock.add(responses.PATCH, EMAIL_URL, status=429, headers={"Retry-After": "3"})
    mock.add(responses.PATCH, EMAIL_URL, json=CREATED, status=200)
    client.send_now(EMAIL_ID)
    assert sleeper.calls == [3.0]


def test_retries_are_shared_between_429_and_server_errors(mock, client, sleeper):
    mock.add(responses.PATCH, EMAIL_URL, status=429, headers={"Retry-After": "5"})
    mock.add(responses.PATCH, EMAIL_URL, status=500)
    mock.add(responses.PATCH, EMAIL_URL, status=500)
    with pytest.raises(ButtondownError) as excinfo:
        client.send_now(EMAIL_ID)
    assert len(mock.calls) == 3  # one request plus at most two retries in total
    assert sleeper.calls == [5.0, 4.0]
    assert excinfo.value.status == 500


def test_send_now_client_error_is_not_retried(mock, client, sleeper):
    mock.add(responses.PATCH, EMAIL_URL, json={"code": "email_not_found", "detail": "Not found."}, status=404)
    with pytest.raises(ButtondownError) as excinfo:
        client.send_now(EMAIL_ID)
    assert len(mock.calls) == 1
    assert sleeper.calls == []
    assert "HTTP 404, code email_not_found: Not found." in str(excinfo.value)


def test_send_now_redirect_is_an_error_not_a_silent_success(mock, client):
    mock.add(responses.PATCH, EMAIL_URL, status=302, headers={"Location": "https://buttondown.com/login"})
    with pytest.raises(ButtondownError, match="HTTP 302"):
        client.send_now(EMAIL_ID)
    assert len(mock.calls) == 1


def test_sending_requires_confirmation_is_explained(mock, client):
    mock.add(
        responses.PATCH,
        EMAIL_URL,
        json={"code": "sending_requires_confirmation", "detail": "Confirm before sending."},
        status=400,
    )
    with pytest.raises(ButtondownError) as excinfo:
        client.send_now(EMAIL_ID)
    assert "extra confirmation" in str(excinfo.value)
    assert DASHBOARD_URL in str(excinfo.value)


# ---------------------------------------------------------------- send_test


def test_send_test_posts_recipients(mock, client, sleeper):
    mock.add(responses.POST, SEND_DRAFT_URL, status=200, json={})
    assert client.send_test(EMAIL_ID, ["editor@example.com", "partner@example.com"]) is None
    request = mock.calls[0].request
    assert request.method == "POST"
    assert request.url == SEND_DRAFT_URL
    assert json.loads(request.body) == {"recipients": ["editor@example.com", "partner@example.com"]}
    assert request.headers["Authorization"] == f"Token {KEY}"
    assert sleeper.calls == []


def test_send_test_accepts_a_tuple_and_any_2xx(mock, client):
    mock.add(responses.POST, SEND_DRAFT_URL, status=204)
    client.send_test(EMAIL_ID, ("editor@example.com",))
    assert json.loads(mock.calls[0].request.body) == {"recipients": ["editor@example.com"]}


def test_send_test_with_a_single_string(mock, client):
    mock.add(responses.POST, SEND_DRAFT_URL, status=200)
    client.send_test(EMAIL_ID, "editor@example.com")
    assert json.loads(mock.calls[0].request.body) == {"recipients": ["editor@example.com"]}


def test_send_test_without_recipients_makes_no_request(mock, client):
    client.send_test(EMAIL_ID, [])
    client.send_test(EMAIL_ID, ["", "  "])
    assert len(mock.calls) == 0


def test_send_test_retries_server_errors(mock, client, sleeper):
    mock.add(responses.POST, SEND_DRAFT_URL, status=500)
    mock.add(responses.POST, SEND_DRAFT_URL, status=200)
    client.send_test(EMAIL_ID, ["editor@example.com"])
    assert len(mock.calls) == 2
    assert sleeper.calls == [2.0]


def test_send_test_failure_says_the_draft_is_fine(mock, client):
    mock.add(responses.POST, SEND_DRAFT_URL, json=err("invalid_recipients", "Bad address."), status=400)
    with pytest.raises(ButtondownError) as excinfo:
        client.send_test(EMAIL_ID, ["editor@example.com"])
    message = str(excinfo.value)
    assert "HTTP 400, code invalid_recipients: Bad address." in message
    assert "only the test copy failed" in message


def test_email_id_is_escaped_in_the_path(mock, client):
    mock.add(responses.PATCH, f"{API_BASE}/emails/a%2Fb", status=200)
    client.send_now("a/b")
    assert mock.calls[0].request.url == f"{API_BASE}/emails/a%2Fb"


# ---------------------------------------------------------------- the API key


def test_api_key_never_appears_in_errors_or_logs(mock, client, caplog):
    mock.add(responses.POST, EMAILS_URL, json={"detail": f"Token {KEY} is not valid"}, status=403)
    mock.add(responses.PATCH, EMAIL_URL, body=requests.ConnectionError(f"failed with key {KEY}"))
    mock.add(responses.POST, SEND_DRAFT_URL, json={"code": "x", "detail": KEY}, status=500)
    messages = []
    with caplog.at_level(logging.DEBUG):
        for call in (
            lambda: client.create_draft(SUBJECT, BODY),
            lambda: client.send_now(EMAIL_ID),
            lambda: client.send_test(EMAIL_ID, ["editor@example.com"]),
        ):
            with pytest.raises(ButtondownError) as excinfo:
                call()
            messages.append(str(excinfo.value))
            messages.append(str(excinfo.value.detail))
    assert_no_key(caplog.text, *messages, repr(client))
    assert "[redacted]" in messages[0]


def test_injected_session_is_used_without_storing_the_key(mock, sleeper):
    session = requests.Session()
    client = ButtondownClient(KEY, UA, session=session, sleep=sleeper)
    mock.add(responses.POST, EMAILS_URL, json=CREATED, status=201)
    client.create_draft(SUBJECT, BODY)
    assert client.session is session
    assert "Authorization" not in session.headers  # the key is added per request, never to a shared session
    assert mock.calls[0].request.headers["Authorization"] == f"Token {KEY}"


def test_netrc_credentials_cannot_replace_the_token(mock, client, monkeypatch):
    monkeypatch.setattr(requests.sessions, "get_netrc_auth", lambda url, raise_errors=False: ("someone", "secret"))
    mock.add(responses.POST, EMAILS_URL, json=CREATED, status=201)
    client.create_draft(SUBJECT, BODY)
    assert mock.calls[0].request.headers["Authorization"] == f"Token {KEY}"


def test_key_with_token_prefix_is_tolerated(mock, sleeper):
    client = ButtondownClient(f"  Token {KEY}\n", UA, sleep=sleeper)
    mock.add(responses.POST, EMAILS_URL, json=CREATED, status=201)
    client.create_draft(SUBJECT, BODY)
    assert mock.calls[0].request.headers["Authorization"] == f"Token {KEY}"


@pytest.mark.parametrize("key", ["", "   ", None])
def test_missing_key_is_rejected(key):
    with pytest.raises(ButtondownError, match="BUTTONDOWN_API_KEY"):
        ButtondownClient(key, UA)


@pytest.mark.parametrize(
    "bad_key",
    [f"{KEY[:10]}\n{KEY[10:]}", f"{KEY[:10]} {KEY[10:]}", f"{KEY}é", f"{KEY}\x00"],
    ids=["newline", "space", "non-ascii", "nul"],
)
def test_malformed_key_is_rejected_without_echoing_it(bad_key):
    # The HTTP library's own error for an invalid header value would quote the key.
    with pytest.raises(ButtondownError) as excinfo:
        ButtondownClient(bad_key, UA)
    message = str(excinfo.value)
    assert "BUTTONDOWN_API_KEY" in message
    assert KEY[:10] not in message and KEY[10:] not in message


def test_default_sleep_is_never_reached_in_tests(mock, monkeypatch):
    # The default client must use the injected sleep only; guard against real waits sneaking in.
    import src.send as send

    monkeypatch.setattr(send.time, "sleep", lambda s: pytest.fail("real sleep called"))
    sleeper = RecordingSleep()
    client = ButtondownClient(KEY, UA, sleep=sleeper)
    mock.add(responses.PATCH, EMAIL_URL, status=503)
    mock.add(responses.PATCH, EMAIL_URL, status=200)
    client.send_now(EMAIL_ID)
    assert sleeper.calls == [2.0]


# ---------------------------------------------------------------- Retry-After parsing


@pytest.mark.parametrize(
    "value, expected",
    [
        (None, 10.0),
        ("", 10.0),
        ("  ", 10.0),
        ("5", 5.0),
        (" 12 ", 12.0),
        ("0", 0.0),
        ("2.5", 2.5),
        ("60", 60.0),
        ("61", 60.0),
        ("86400", 60.0),
        ("-3", 0.0),
        ("nan", 10.0),
        ("inf", 60.0),
        ("soon", 10.0),
    ],
)
def test_retry_after_seconds(value, expected):
    assert retry_after_seconds(value) == expected


def test_retry_after_http_date():
    future = datetime.now(timezone.utc) + timedelta(hours=1)
    past = datetime.now(timezone.utc) - timedelta(hours=1)
    assert retry_after_seconds(format_datetime(future, usegmt=True)) == 60.0
    assert retry_after_seconds(format_datetime(past, usegmt=True)) == 0.0
    near = datetime.now(timezone.utc) + timedelta(seconds=30)
    assert 25.0 <= retry_after_seconds(format_datetime(near, usegmt=True)) <= 30.0
