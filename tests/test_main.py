"""End-to-end tests for src/main.py (SPEC section H).

Every HTTP request goes to ``responses`` (CourtListener feeds and API, PDFs generated with
tests.pdfgen, Buttondown), and Claude is a fake client behind the real ClaudeSummarizer,
so the retry, fatal-error, and cost paths run through the real code. No network, no key:
where a run needs ANTHROPIC_API_KEY to be "set", the tests set a dummy value and the
injected fake client guarantees the SDK never builds a real one. Each test runs in its
own temporary directory, so no .env file is read and nothing is written to the repo.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import date, datetime, timezone
from functools import lru_cache
from pathlib import Path
from types import SimpleNamespace

import anthropic
import httpx2
import pytest
import responses

from src import main as main_mod
from src.main import (
    EXIT_FAILED,
    EXIT_OK,
    EXIT_PARTIAL,
    MOCK_OUTPUT_TOKENS_PER_ITEM,
    NO_ANTHROPIC_KEY,
    NOTHING_NEW,
    Services,
    main,
    most_recent_week,
    pipeline_order,
)
from src.models import Court, Item, ItemKind
from src.net import PoliteSession
from src.send import API_BASE, ButtondownClient
from src.sources import courtlistener, sccourts
from src.state import State
from src.summarize import MOCK_MARKER, ClaudeSummarizer, MockSummarizer, SummaryCache
from tests.conftest import fixture_bytes, fixture_json, fixture_text
from tests.pdfgen import make_pdf, make_pdf_from_text

TODAY = "2026-11-16"
SONNET = "claude-sonnet-5-5"
FIXED_NOW = datetime(2026, 11, 18, 20, 31, 2, tzinfo=timezone.utc)
UA = "SC-Advance-Sheet-Digest/test (pytest)"

NETTLES_PDF = "https://storage.courtlistener.com/pdf/2026/11/12/in_the_matter_of_louis_david_nettles.pdf"
FLEMING_PDF = "https://storage.courtlistener.com/pdf/2026/11/12/reid_fleming_v._lg_chem_ltd..pdf"
MOORE_PDF = "https://storage.courtlistener.com/pdf/2026/11/12/gary_moore_v._state.pdf"
TAYLOR_PDF = "https://storage.courtlistener.com/pdf/2026/10/21/in_the_matter_of_jason_f._taylor.pdf"
SC_FEED = courtlistener.FEED_URL.format(court_id="sc")
COA_FEED = courtlistener.FEED_URL.format(court_id="scctapp")
EMAILS_URL = f"{API_BASE}/emails"
EMAIL_ID = "em_test_1"

NETTLES = "In the Matter of Louis David Nettles"
FLEMING = "Reid Fleming v. LG Chem Ltd."
MOORE = "Gary Moore v. State"

# Every setting main reads from the environment; cleared so the developer's shell cannot leak in.
SETTING_NAMES = (
    "SOURCE", "CLAUDE_MODEL", "SUMMARY_EFFORT", "SUMMARY_REFUSAL_FALLBACK", "MAX_OUTPUT_TOKENS",
    "MAX_INPUT_TOKENS_PER_OPINION", "MAX_OPINIONS_PER_RUN", "AUTO_SEND", "INCLUDE_SUPREME_COURT",
    "INCLUDE_COURT_OF_APPEALS", "INCLUDE_ORDERS", "LOOKBACK_DAYS", "MAX_ATTEMPTS_PER_ITEM", "CONTACT_EMAIL",
    "BUTTONDOWN_TEST_RECIPIENTS", "STATE_FILE", "OUTPUT_DIR", "SUMMARY_CACHE_DIR", "REQUEST_DELAY_SECONDS",
    "LOG_LEVEL", "GITHUB_REPOSITORY", "DRY_RUN", "MOCK_LLM",
)  # fmt: skip


# --------------------------------------------------------------------------- PDFs


def _short_text(name: str, lines: int = 45) -> str:
    """The opinion's first lines: its whole header plus enough body (> 500 chars) to summarize."""
    return "\n".join(fixture_text(f"opinion_texts/{name}.txt").splitlines()[:lines])


@lru_cache(maxsize=None)
def opinion_pdf(name: str) -> bytes:
    return make_pdf_from_text(_short_text(name))


@lru_cache(maxsize=None)
def taylor_pdf() -> bytes:
    body = " ".join(["Respondent admits misconduct and consents to a public reprimand."] * 20)
    text = (
        "THE STATE OF SOUTH CAROLINA\nIn The Supreme Court\nIn the Matter of Jason F. Taylor, Respondent.\n"
        "Appellate Case No. 2026-000301\nOpinion No. 28353\nSubmitted September 20, 2026 – Filed October 21, 2026\n"
        f"PUBLIC REPRIMAND\nPER CURIAM: {body}"
    )
    return make_pdf_from_text(text)


@lru_cache(maxsize=None)
def advance_sheet_41_pdf() -> bytes:
    return make_pdf([page.splitlines() or [""] for page in fixture_json("sccourts/advance_sheet_41_pages.json")])


# --------------------------------------------------------------------------- fake Claude


def summary_payload(case: str, change_type: str = "none") -> dict:
    what = {
        "none": "No change to existing law.",
        "clarifies": "Clarifies: a manufacturer that targets the state's market is subject to specific jurisdiction.",
        "changes": "Changes: the rule now requires written notice.",
    }[change_type]
    return {
        "case_name": case,
        "opinion_number": "unclear from the opinion",
        "court": "Supreme Court",
        "date": "November 12, 2026",
        "author": "unclear from the opinion",
        "one_line": f"Practical takeaway for {case}.",
        "holding": f"The court decided the question presented in {case}.",
        "what_changed": what,
        "change_type": change_type,
        "practice_areas": ["Civil Procedure"],
        "practice_tip": "Lawyers should note the procedural point.",
        "disposition": "Reversed",
    }


def api_response(case: str, *, text: str | None = None, change_type: str = "none", model: str = SONNET):
    body = text if text is not None else json.dumps(summary_payload(case, change_type))
    return SimpleNamespace(
        content=[SimpleNamespace(type="thinking", thinking="(hidden)"), SimpleNamespace(type="text", text=body)],
        stop_reason="end_turn",
        stop_details=None,
        model=model,
        usage=SimpleNamespace(
            input_tokens=12_000, output_tokens=900, cache_creation_input_tokens=None, cache_read_input_tokens=None
        ),
        _request_id=f"req_{len(case)}",
    )


def api_error(cls, status: int, message: str = "error"):
    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    return cls(message, response=httpx2.Response(status, request=request), body=None)


class FakeAnthropic:
    """A Messages API stand-in. ``policy(case_name, attempt)`` returns a reply or an exception to raise."""

    def __init__(self, policy=None):
        self.policy = policy or (lambda case, attempt: api_response(case, change_type=default_change(case)))
        self.calls: list[str] = []
        self.kwargs: list[dict] = []
        self.messages = SimpleNamespace(create=self._create)
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        message = kwargs["messages"][0]["content"]
        case = re.search(r"^- case_name: (.*)$", message, re.MULTILINE).group(1)
        attempt = self.calls.count(case) + 1
        self.calls.append(case)
        self.kwargs.append(kwargs)
        outcome = self.policy(case, attempt)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def default_change(case: str) -> str:
    return "clarifies" if case == FLEMING else "none"


def fails_for(target: str, times: int = 99):
    """A policy whose replies for ``target`` are invalid JSON on the first ``times`` attempts."""

    def policy(case, attempt):
        if case == target and attempt <= times:
            return api_response(case, text="this is not JSON")
        return api_response(case, change_type=default_change(case))

    return policy


# --------------------------------------------------------------------------- environment


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    """Run in an empty directory, with no settings from the shell, and with pytest's log capture intact."""
    for name in SETTING_NAMES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("REQUEST_DELAY_SECONDS", "0")
    monkeypatch.setenv("GITHUB_OUTPUT", str(tmp_path / "gh_output.txt"))
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(tmp_path / "gh_summary.md"))
    monkeypatch.setattr(main_mod, "setup_logging", lambda level="INFO": None)


@pytest.fixture
def keys(monkeypatch):
    """Dummy keys, so main's "is the key set?" checks pass. The fake client is what answers."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-not-a-real-key")
    monkeypatch.setenv("BUTTONDOWN_API_KEY", "bd-test-not-a-real-key")


@pytest.fixture
def web():
    """Every HTTP request of the run: the two CourtListener feeds, the opinion PDFs, and Buttondown."""
    with responses.RequestsMock(assert_all_requests_are_fired=False) as rsps:
        atom = "application/atom+xml; charset=utf-8"
        rsps.get(SC_FEED, body=fixture_bytes("courtlistener/feed_sc.xml"), content_type=atom)
        rsps.get(COA_FEED, body=fixture_bytes("courtlistener/feed_scctapp.xml"), content_type=atom)
        rsps.get(NETTLES_PDF, body=opinion_pdf("sc_28354"), content_type="application/pdf")
        rsps.get(FLEMING_PDF, body=opinion_pdf("sc_28355"), content_type="application/pdf")
        rsps.get(MOORE_PDF, body=opinion_pdf("coa_6170"), content_type="application/pdf")
        rsps.get(TAYLOR_PDF, body=taylor_pdf(), content_type="application/pdf")
        yield rsps


def buttondown(rsps, *, create_status: int = 201, create_body: dict | None = None, patch_status: int = 200,
               test_status: int = 200) -> None:  # fmt: skip
    body = create_body if create_body is not None else {
        "id": EMAIL_ID, "status": "draft", "absolute_url": "https://buttondown.com/archive/test/"
    }  # fmt: skip
    rsps.post(EMAILS_URL, json=body, status=create_status)
    rsps.patch(f"{EMAILS_URL}/{EMAIL_ID}", json={"id": EMAIL_ID, "status": "about_to_send"}, status=patch_status)
    rsps.post(f"{EMAILS_URL}/{EMAIL_ID}/send-draft", json={}, status=test_status)


def services(client: FakeAnthropic | None = None, *, cache_dir: str = ".cache/summaries") -> Services:
    """Real code everywhere, except: no throttling or retries, a fake Claude client, and no sleeping."""
    client = client or FakeAnthropic()

    def summarizer(settings, mock):
        if mock:
            return MockSummarizer(settings)
        cache = SummaryCache(cache_dir, settings.claude_model, settings.claude_effort)
        return ClaudeSummarizer(settings, client=client, cache=cache)

    return Services(
        make_session=lambda settings: PoliteSession(UA, min_interval=0, max_retries=0),
        make_summarizer=summarizer,
        make_buttondown=lambda settings: ButtondownClient(settings.buttondown_api_key, UA, sleep=lambda _: None),
        now=lambda: FIXED_NOW,
    )


def run(*args: str, svc: Services | None = None, today: str = TODAY) -> int:
    return main(["--today", today, *args], services=svc or services())


# --------------------------------------------------------------------------- inspection helpers


def calls_to(rsps, method: str, url_prefix: str) -> list:
    return [c for c in rsps.calls if c.request.method == method and c.request.url.startswith(url_prefix)]


def buttondown_calls(rsps) -> list:
    return [c for c in rsps.calls if c.request.url.startswith(API_BASE)]


def pdf_downloads(rsps) -> list[str]:
    return [c.request.url for c in rsps.calls if c.request.url.endswith(".pdf")]


def read_state() -> dict:
    return json.loads(Path("data/processed.json").read_text(encoding="utf-8"))


def read_json(path: str) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def outputs(tmp_path: Path) -> list[tuple[str, str]]:
    path = tmp_path / "gh_output.txt"
    if not path.exists():
        return []
    return [tuple(line.split("=", 1)) for line in path.read_text(encoding="utf-8").splitlines() if line]


def output_dict(tmp_path: Path) -> dict[str, str]:
    return dict(outputs(tmp_path))


def messages(caplog, level: int | None = None) -> list[str]:
    return [r.getMessage() for r in caplog.records if level is None or r.levelno == level]


@pytest.fixture(autouse=True)
def _info_logs(caplog):
    caplog.set_level(logging.INFO)


# --------------------------------------------------------------------------- command line


def test_help_lists_the_flags_and_exits_0(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["--help"])
    assert exc.value.code == 0
    text = capsys.readouterr().out
    for flag in ("--dry-run", "--mock-llm", "--today", "--lookback-days", "--max-opinions", "--source",
                 "--output-dir", "--state-file", "--verbose"):  # fmt: skip
        assert flag in text


@pytest.mark.parametrize(
    "argv",
    [["--today", "2026-13-01"], ["--lookback-days", "0"], ["--lookback-days", "61"], ["--max-opinions", "x"],
     ["--source", "westlaw"], ["--unknown"]],
)  # fmt: skip
def test_bad_arguments_exit_1_not_2(argv, capsys):
    # Exit code 2 means "partial success" for this program, so usage errors must not use it.
    with pytest.raises(SystemExit) as exc:
        main(argv)
    assert exc.value.code == EXIT_FAILED
    assert "error:" in capsys.readouterr().err


def test_invalid_setting_exits_1_with_the_variable_name(monkeypatch, caplog, web):
    monkeypatch.setenv("SOURCE", "westlaw")
    assert run("--dry-run", "--mock-llm") == EXIT_FAILED
    assert any("SOURCE must be one of" in m for m in messages(caplog, logging.ERROR))
    assert not web.calls


def test_invalid_dry_run_value_exits_1(monkeypatch, caplog, web):
    monkeypatch.setenv("DRY_RUN", "maybe")
    assert run("--mock-llm") == EXIT_FAILED
    assert any("DRY_RUN must be true or false" in m for m in messages(caplog, logging.ERROR))
    assert not web.calls


# --------------------------------------------------------------------------- keys


def test_missing_anthropic_key_without_mock_exits_1_with_the_message(caplog, web):
    assert run("--dry-run") == EXIT_FAILED
    errors = messages(caplog, logging.ERROR)
    assert NO_ANTHROPIC_KEY in errors
    assert errors[0] == (
        "ANTHROPIC_API_KEY is not set. Real summaries run in GitHub Actions, where the key is a repository "
        "secret. To test locally without the API, add --mock-llm."
    )
    assert not web.calls  # fails before fetching anything
    assert not Path("data/processed.json").exists()
    assert read_json("output/run_summary.json")["exit_code"] == EXIT_FAILED


def test_missing_buttondown_key_in_a_real_run_exits_1_before_any_work(monkeypatch, caplog, web):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-not-a-real-key")
    client = FakeAnthropic()
    assert run(svc=services(client)) == EXIT_FAILED
    assert any(m.startswith("BUTTONDOWN_API_KEY is not set") for m in messages(caplog, logging.ERROR))
    assert not web.calls and not client.calls  # no scraping, no API spend
    assert not Path("data/processed.json").exists()


def test_mock_dry_run_needs_no_keys_at_all(web):
    assert run("--dry-run", "--mock-llm") == EXIT_OK


# --------------------------------------------------------------------------- dry run


def test_mock_dry_run_writes_the_outputs_and_no_state(tmp_path, caplog, capsys, web):
    assert run("--dry-run", "--mock-llm") == EXIT_OK

    md = Path("output/digest.md").read_text(encoding="utf-8")
    assert md.startswith("<!-- buttondown-editor-mode: plaintext -->")
    assert "MOCK summaries" in md
    assert MOCK_MARKER.strip("[]") in md  # Markdown-escaped in the body: \[MOCK SUMMARY ...\]
    for case in (NETTLES, FLEMING, MOORE):
        assert case in md
    html = Path("output/digest.html").read_text(encoding="utf-8")
    assert html.startswith("<!DOCTYPE html>") and NETTLES in html

    digest = read_json("output/digest.json")
    assert digest["subject"] == "SC Advance Sheet Digest — November 12, 2026 — 3 opinions"
    assert digest["mock"] is True and digest["dry_run"] is True
    assert [i["key"] for i in digest["items"]] == ["SC:28354", "SC:28355", "COA:6170"]
    assert all(i["status"] == "summarized" and i["summary"] for i in digest["items"])
    assert {p.name for p in Path("output/texts").iterdir()} == {"SC-28354.txt", "SC-28355.txt", "COA-6170.txt"}
    assert "Opinion No. 28355" in Path("output/texts/SC-28355.txt").read_text(encoding="utf-8")

    assert not Path("data/processed.json").exists() and not Path("data").exists()
    assert not buttondown_calls(web)
    assert "Dry run: not emailing and not updating data/processed.json" in messages(caplog, logging.INFO)
    assert "<!-- buttondown-editor-mode: plaintext -->" in capsys.readouterr().out  # printed in a log group

    out = output_dict(tmp_path)
    assert out["has_new"] == "true" and out["email_created"] == "false"
    assert out["digest_subject"] == digest["subject"]
    assert "state_changed" not in out


def test_dry_run_summary_reports_counts_tokens_and_a_mock_cost_estimate(tmp_path, web):
    assert run("--dry-run", "--mock-llm") == EXIT_OK
    summary = read_json("output/run_summary.json")
    assert summary["exit_code"] == 0 and summary["dry_run"] and summary["mock"]
    assert summary["source"] == "courtlistener-feed"
    assert summary["counts"]["found"] == 3 and summary["counts"]["new"] == 3
    assert summary["counts"]["summarized"] == 3 and summary["counts"]["failed"] == 0
    assert summary["usage"]["requests"] == 0 and summary["estimated_cost_usd"] == 0.0
    estimate = summary["mock_estimate"]
    assert estimate["items"] == 3 and estimate["output_tokens"] == 3 * MOCK_OUTPUT_TOKENS_PER_ITEM
    expected = (estimate["input_tokens"] * 2.00 + estimate["output_tokens"] * 10.00) / 1_000_000
    assert estimate["input_tokens"] > 3 * 1000 and estimate["estimated_cost_usd"] == pytest.approx(expected)
    assert "verify against current Anthropic pricing" in summary["prices_note"]
    assert courtlistener.NO_TOKEN_WARNING in summary["warnings"]

    page = (tmp_path / "gh_summary.md").read_text(encoding="utf-8")
    assert "## SC Advance Sheet Digest run" in page
    assert "Estimated cost with the API" in page and "verify against current Anthropic pricing" in page
    assert "| Estimated cost | $0.0000 |" in page


def test_dry_run_and_mock_can_come_from_the_environment(monkeypatch, web):
    monkeypatch.setenv("DRY_RUN", "true")
    monkeypatch.setenv("MOCK_LLM", "true")
    monkeypatch.setenv("BUTTONDOWN_API_KEY", "bd-test-not-a-real-key")
    buttondown(web)
    assert run() == EXIT_OK
    assert Path("output/digest.md").exists()
    assert not buttondown_calls(web) and not Path("data/processed.json").exists()


def test_mock_llm_always_runs_as_a_dry_run(monkeypatch, caplog, web, keys):
    # Placeholder summaries must never reach subscribers or the state file, even with AUTO_SEND on.
    monkeypatch.setenv("AUTO_SEND", "true")
    buttondown(web)
    assert run("--mock-llm") == EXIT_OK
    assert not buttondown_calls(web)
    assert not Path("data/processed.json").exists()
    assert any("so this run is a dry run" in m for m in messages(caplog, logging.WARNING))


def test_dry_run_with_the_api_uses_the_cache_so_the_real_run_does_not_pay_twice(web, keys, tmp_path):
    client = FakeAnthropic()
    assert run("--dry-run", svc=services(client)) == EXIT_OK
    assert len(client.calls) == 3
    assert not Path("data/processed.json").exists()

    buttondown(web)
    assert run(svc=services(client)) == EXIT_OK
    assert len(client.calls) == 3  # every summary came from the cache
    summary = read_json("output/run_summary.json")
    assert summary["counts"]["from_cache"] == 3 and summary["usage"]["requests"] == 0
    assert len(calls_to(web, "POST", EMAILS_URL)) == 1


# --------------------------------------------------------------------------- real run


def test_real_run_creates_a_draft_and_records_the_state(tmp_path, caplog, web, keys):
    buttondown(web)
    client = FakeAnthropic()
    assert run(svc=services(client)) == EXIT_OK

    posts = calls_to(web, "POST", EMAILS_URL)
    assert len(posts) == 1 and not calls_to(web, "PATCH", API_BASE)
    sent = json.loads(posts[0].request.body)
    assert sent["status"] == "draft"
    assert sent["subject"] == "SC Advance Sheet Digest — November 12, 2026 — 3 opinions"
    assert sent["body"].startswith("<!-- buttondown-editor-mode: plaintext -->")
    assert sent["body"] == Path("output/digest.md").read_text(encoding="utf-8")
    # No metadata (paid-only for Buttondown accounts created since 2026-04-20); state records the items.
    assert "metadata" not in sent
    assert posts[0].request.headers["Authorization"] == "Token bd-test-not-a-real-key"
    assert posts[0].request.headers["X-API-Version"] == "2026-04-01"

    assert client.calls == [NETTLES, FLEMING, MOORE]  # one request each, Supreme Court first
    assert all("temperature" not in kw for kw in client.kwargs)
    state = read_state()
    assert set(state["items"]) == {"SC:28354", "SC:28355", "COA:6170"}
    assert all(r["status"] == "summarized" and r["attempts"] == 1 for r in state["items"].values())
    # The feed knew Fleming only by its CourtListener id; the PDF header supplied the number.
    assert state["aliases"]["CL:11060100"] == "SC:28355"
    assert state["items"]["SC:28355"]["email_id"] == EMAIL_ID
    assert state["digests"] == [
        {
            "subject": sent["subject"],
            "email_id": EMAIL_ID,
            "status": "draft",
            "created_at": "2026-11-18T20:31:02Z",
            "items": ["SC:28354", "SC:28355", "COA:6170"],
        }
    ]

    out = output_dict(tmp_path)
    assert out["state_changed"] == "true" and out["email_created"] == "true" and out["has_new"] == "true"
    assert any(m.startswith("Draft created in Buttondown") for m in messages(caplog, logging.INFO))
    md = Path("output/digest.md").read_text(encoding="utf-8")
    assert "## Rule changes this week" in md and "**Clarifies:**" in md  # Fleming's summary
    assert "Justice Hill" in md and "Judge Konduros" in md  # authors from the PDF headers


def test_real_run_logs_token_usage_per_request_and_the_cost(caplog, web, keys):
    buttondown(web)
    assert run(svc=services()) == EXIT_OK
    per_item = [m for m in messages(caplog, logging.INFO) if "12,000 in / 900 out tokens" in m]
    assert len(per_item) == 3
    summary = read_json("output/run_summary.json")
    assert summary["usage"]["input_tokens"] == 36_000 and summary["usage"]["output_tokens"] == 2_700
    assert summary["usage"]["requests"] == 3
    assert summary["estimated_cost_usd"] == pytest.approx(3 * (12_000 * 2 + 900 * 10) / 1_000_000)
    lines = messages(caplog, logging.INFO)
    assert any("Estimated cost:" in m and "$0.0990" in m for m in lines)
    assert any("Prices used:" in m and "verify against current Anthropic pricing" in m for m in lines)


def test_second_run_with_the_same_items_finds_nothing_new(tmp_path, caplog, web, keys):
    buttondown(web)
    client = FakeAnthropic()
    assert run(svc=services(client)) == EXIT_OK
    state_before = Path("data/processed.json").read_bytes()
    (tmp_path / "gh_output.txt").unlink()
    caplog.clear()

    assert run(svc=services(client)) == EXIT_OK
    assert "No new opinions or orders since the last run. Nothing to send." in messages(caplog, logging.INFO)
    assert len(calls_to(web, "POST", EMAILS_URL)) == 1  # only the first run's
    assert len(client.calls) == 3  # nothing summarized again
    assert Path("data/processed.json").read_bytes() == state_before
    out = output_dict(tmp_path)
    assert out["has_new"] == "false" and out["email_created"] == "false" and "state_changed" not in out


def test_outputs_are_set_once_each(tmp_path, web, keys):
    buttondown(web)
    assert run(svc=services()) == EXIT_OK
    names = [name for name, _ in outputs(tmp_path)]
    assert sorted(names) == sorted(set(names))


# --------------------------------------------------------------------------- failures and retries


def test_failed_item_is_listed_then_retried_on_the_next_run(web, keys):
    buttondown(web)
    client = FakeAnthropic(fails_for(MOORE, times=2))
    assert run(svc=services(client)) == EXIT_OK
    assert client.calls.count(MOORE) == 2  # one retry, then skipped
    md = Path("output/digest.md").read_text(encoding="utf-8")
    assert "The AI summary isn't available this week (the AI response was not valid)" in md
    record = read_state()["items"]["COA:6170"]
    assert record["status"] == "failed" and record["attempts"] == 1
    assert record["error"] == "the AI response was not valid"

    # Next run: the failed item is new again (1 attempt < MAX_ATTEMPTS_PER_ITEM=2) and now succeeds.
    web.replace(responses.POST, EMAILS_URL, json={"id": "em_test_2", "status": "draft", "absolute_url": None})
    assert run(svc=services(client)) == EXIT_OK
    assert client.calls.count(MOORE) == 3 and client.calls.count(NETTLES) == 1
    state = read_state()
    assert state["items"]["COA:6170"]["status"] == "summarized"
    assert state["items"]["COA:6170"]["attempts"] == 2
    first = "SC Advance Sheet Digest — November 12, 2026 — 3 opinions"
    assert state["digests"][-1]["subject"] == "SC Advance Sheet Digest — November 12, 2026 — 1 opinion (supplement)"
    assert state["digests"][-1]["items"] == ["COA:6170"]
    # The second email for the week is a supplement: it never says the week had no Supreme Court
    # opinions or no rule changes, which would contradict the first email (Fleming clarifies a rule).
    md = Path("output/digest.md").read_text(encoding="utf-8")
    assert f"Supplement to this week's earlier issue (“{first}”): 1 published Court of Appeals opinion." in md
    assert "**SC Advance Sheet Digest** · November 12, 2026 · Supplement" in md
    assert "no published Supreme Court opinions" not in md
    assert "No new published opinions this week" not in md
    assert "No opinion this week changes or clarifies a rule" not in md
    assert "this week's earlier issue covers the rest of the week" in md

    # Third run: nothing is new.
    assert run(svc=services(client)) == EXIT_OK
    assert client.calls.count(MOORE) == 3
    assert len(calls_to(web, "POST", EMAILS_URL)) == 2


def test_a_failed_item_is_not_retried_after_max_attempts(monkeypatch, caplog, web, keys):
    monkeypatch.setenv("MAX_ATTEMPTS_PER_ITEM", "1")
    buttondown(web)
    client = FakeAnthropic(fails_for(MOORE))
    assert run(svc=services(client)) == EXIT_OK
    assert read_state()["items"]["COA:6170"]["attempts"] == 1
    caplog.clear()
    assert run(svc=services(client)) == EXIT_OK
    assert "No new opinions or orders since the last run. Nothing to send." in messages(caplog, logging.INFO)
    assert client.calls.count(MOORE) == 2


def test_a_retry_that_fails_again_alone_records_the_attempt_and_sends_nothing(caplog, web, keys):
    buttondown(web)
    client = FakeAnthropic(fails_for(MOORE))  # Moore's summary always fails
    assert run(svc=services(client)) == EXIT_OK  # run 1: the digest lists Moore without a summary
    assert read_state()["items"]["COA:6170"]["attempts"] == 1

    caplog.clear()
    # Run 2: only Moore is due (a retry), and it fails again. Nothing new to email, but the attempt
    # is recorded so a document that keeps failing is not retried (and does not fail) every run.
    assert run(svc=services(client)) == EXIT_OK
    record = read_state()["items"]["COA:6170"]
    assert record["status"] == "failed" and record["attempts"] == 2
    assert any("giving up after 2 attempts" in m for m in messages(caplog, logging.WARNING))
    assert len(calls_to(web, "POST", EMAILS_URL)) == 1  # no second email
    assert client.calls.count(MOORE) == 4  # two requests per run, never more

    # Run 3: Moore has used its attempts; nothing is new.
    assert run(svc=services(client)) == EXIT_OK
    assert client.calls.count(MOORE) == 4


def test_a_retry_that_fails_again_in_a_dry_run_changes_nothing(web, keys):
    buttondown(web)
    client = FakeAnthropic(fails_for(MOORE))
    assert run(svc=services(client)) == EXIT_OK
    state_before = Path("data/processed.json").read_bytes()
    assert run("--dry-run", svc=services(client)) == EXIT_FAILED  # a dry run never writes state
    assert Path("data/processed.json").read_bytes() == state_before
    assert len(calls_to(web, "POST", EMAILS_URL)) == 1


def test_all_failed_exits_1_without_email_or_state(caplog, web, keys):
    buttondown(web)
    client = FakeAnthropic(lambda case, attempt: api_response(case, text="{not json"))
    assert run(svc=services(client)) == EXIT_FAILED
    assert "All 3 summaries failed; not creating an email." in messages(caplog, logging.ERROR)
    assert len(client.calls) == 6  # two requests per item, never more
    assert not buttondown_calls(web)
    assert not Path("data/processed.json").exists()


def test_fatal_api_error_stops_the_run_with_no_email_and_no_state(tmp_path, caplog, web, keys):
    buttondown(web)
    client = FakeAnthropic(lambda case, attempt: api_error(anthropic.AuthenticationError, 401, "invalid x-api-key"))
    assert run(svc=services(client)) == EXIT_FAILED
    assert client.calls == [NETTLES]  # stopped at the first request
    errors = messages(caplog, logging.ERROR)
    assert any("rejected the API key (HTTP 401)" in m and "Nothing was emailed" in m for m in errors)
    assert not buttondown_calls(web)
    assert not Path("data/processed.json").exists()
    assert not Path("output/digest.md").exists()
    summary = read_json("output/run_summary.json")
    assert summary["exit_code"] == EXIT_FAILED and summary["result"].startswith("FAILED:")
    assert output_dict(tmp_path)["email_created"] == "false"


def test_spend_limit_on_a_later_item_stops_the_run_and_counts_what_was_spent(caplog, web, keys):
    def policy(case, attempt):
        if case == FLEMING:
            return api_error(anthropic.EnforcedSpendLimitReachedError, 429, "spend limit reached")
        return api_response(case)

    buttondown(web)
    client = FakeAnthropic(policy)
    assert run(svc=services(client)) == EXIT_FAILED
    assert client.calls == [NETTLES, FLEMING]  # Moore is never attempted
    assert any("monthly spend limit has been reached" in m for m in messages(caplog, logging.ERROR))
    summary = read_json("output/run_summary.json")
    assert summary["usage"]["requests"] == 2 and summary["usage"]["input_tokens"] == 12_000
    assert not buttondown_calls(web) and not Path("data/processed.json").exists()


def test_unreadable_pdf_is_listed_without_an_api_call(caplog, web, keys):
    web.replace(responses.GET, MOORE_PDF, status=404)
    buttondown(web)
    client = FakeAnthropic()
    assert run(svc=services(client)) == EXIT_OK
    assert MOORE not in client.calls
    md = Path("output/digest.md").read_text(encoding="utf-8")
    assert "The AI summary isn't available this week (the opinion PDF could not be read)" in md
    assert read_state()["items"]["COA:6170"]["error"] == "the opinion PDF could not be read"
    assert any("could not read the opinion PDF" in m for m in messages(caplog, logging.WARNING))


def test_scanned_pdf_with_no_text_is_listed_without_an_api_call(web, keys):
    web.replace(responses.GET, MOORE_PDF, body=make_pdf([[""], [""]]), content_type="application/pdf")
    buttondown(web)
    client = FakeAnthropic()
    assert run(svc=services(client)) == EXIT_OK
    assert MOORE not in client.calls
    md = Path("output/digest.md").read_text(encoding="utf-8")
    assert "no readable text (scanned PDF?)" in md
    # The feed had Moore's number, so its key is known even without the text.
    assert read_state()["items"]["COA:6170"]["status"] == "failed"


def test_source_failure_exits_1(caplog, web, keys):
    web.replace(responses.GET, SC_FEED, status=503)
    client = FakeAnthropic()
    assert run(svc=services(client)) == EXIT_FAILED
    assert any("Could not read CourtListener's public Supreme Court feed" in m for m in messages(caplog, logging.ERROR))
    assert not client.calls and not buttondown_calls(web)


def test_corrupt_state_file_exits_1_and_is_left_alone(caplog, web, keys):
    Path("data").mkdir()
    Path("data/processed.json").write_text("{not json", encoding="utf-8")
    assert run(svc=services()) == EXIT_FAILED
    assert Path("data/processed.json").read_text(encoding="utf-8") == "{not json"
    assert any("is not valid JSON" in m for m in messages(caplog, logging.ERROR))
    assert not web.calls


def test_unexpected_error_exits_1_and_writes_the_run_summary(caplog, web, keys):
    class Broken:
        def summarize(self, item, prepared, header):
            raise RuntimeError("boom")

    svc = services()
    svc.make_summarizer = lambda settings, mock: Broken()
    buttondown(web)
    assert run(svc=svc) == EXIT_FAILED
    assert any("unexpected error" in m for m in messages(caplog, logging.ERROR))
    assert read_json("output/run_summary.json")["result"] == "FAILED: unexpected error: RuntimeError: boom"
    assert not buttondown_calls(web) and not Path("data/processed.json").exists()


# --------------------------------------------------------------------------- Buttondown


def test_auto_send_patches_the_draft_after_creating_it(monkeypatch, caplog, web, keys):
    monkeypatch.setenv("AUTO_SEND", "true")
    buttondown(web)
    assert run(svc=services()) == EXIT_OK
    bd = buttondown_calls(web)
    assert [c.request.method for c in bd] == ["POST", "PATCH"]
    assert json.loads(bd[0].request.body)["status"] == "draft"
    assert bd[1].request.url == f"{EMAILS_URL}/{EMAIL_ID}"
    assert json.loads(bd[1].request.body) == {"status": "about_to_send"}
    assert read_state()["digests"][-1]["status"] == "about_to_send"
    assert any("AUTO_SEND" in m and "sending the digest" in m for m in messages(caplog, logging.INFO))


def test_auto_send_failure_exits_2_with_the_state_saved(monkeypatch, tmp_path, web, keys):
    monkeypatch.setenv("AUTO_SEND", "true")
    buttondown(web, patch_status=400)
    assert run(svc=services()) == EXIT_PARTIAL
    state = read_state()
    assert len(state["items"]) == 3 and state["digests"][-1]["status"] == "draft"
    assert output_dict(tmp_path)["state_changed"] == "true"


def test_without_auto_send_nothing_is_sent(web, keys):
    buttondown(web)
    assert run(svc=services()) == EXIT_OK
    assert not calls_to(web, "PATCH", API_BASE)
    assert not [c for c in buttondown_calls(web) if c.request.url.endswith("/send-draft")]


def test_test_recipients_get_a_test_copy_of_the_draft(monkeypatch, web, keys):
    monkeypatch.setenv("BUTTONDOWN_TEST_RECIPIENTS", "me@example.com, you@example.com")
    monkeypatch.setenv("AUTO_SEND", "true")
    buttondown(web)
    assert run(svc=services()) == EXIT_OK
    bd = buttondown_calls(web)
    # The test copy goes out while the email is still a draft, then AUTO_SEND sends it.
    assert [(c.request.method, c.request.url.rsplit("/", 1)[-1]) for c in bd] == [
        ("POST", "emails"),
        ("POST", "send-draft"),
        ("PATCH", EMAIL_ID),
    ]
    assert json.loads(bd[1].request.body) == {"recipients": ["me@example.com", "you@example.com"]}


def test_test_send_failure_is_a_warning_and_exit_2(monkeypatch, caplog, web, keys):
    monkeypatch.setenv("BUTTONDOWN_TEST_RECIPIENTS", "me@example.com")
    buttondown(web, test_status=400)
    assert run(svc=services()) == EXIT_PARTIAL
    assert any("test send failed" in m for m in messages(caplog, logging.WARNING))
    assert len(read_state()["items"]) == 3


def test_duplicate_subject_exits_1_with_the_state_unchanged(caplog, web, keys):
    buttondown(web, create_status=400, create_body={"code": "email_duplicate", "detail": "Email already exists."})
    assert run(svc=services()) == EXIT_FAILED
    assert any("Buttondown already has an email with this subject" in m for m in messages(caplog, logging.ERROR))
    assert not Path("data/processed.json").exists()
    assert Path("output/digest.md").exists()  # the artifact is still there to inspect


def test_server_error_on_create_is_not_retried(caplog, web, keys):
    buttondown(web, create_status=500, create_body={"detail": "oops"})
    assert run(svc=services()) == EXIT_FAILED
    assert len(calls_to(web, "POST", EMAILS_URL)) == 1
    assert any("check the Buttondown dashboard" in m for m in messages(caplog, logging.ERROR))
    assert not Path("data/processed.json").exists()


def test_subject_already_used_gets_an_update_suffix(web, keys):
    Path("data").mkdir()
    previous = "SC Advance Sheet Digest — November 12, 2026 — 3 opinions"
    Path("data/processed.json").write_text(
        json.dumps(
            {
                "version": 1,
                "items": {"SC:1": {"kind": "opinion", "court": "SC", "case_name": "Old", "filed_date": "2026-01-07",
                                   "status": "summarized", "attempts": 1}},
                "aliases": {},
                "digests": [{"subject": previous, "email_id": "em_old", "status": "sent", "items": ["SC:1"]}],
            }
        ),
        encoding="utf-8",
    )  # fmt: skip
    buttondown(web)
    assert run(svc=services()) == EXIT_OK
    assert json.loads(calls_to(web, "POST", EMAILS_URL)[0].request.body)["subject"] == previous + " (update)"


# --------------------------------------------------------------------------- choosing items


def test_cap_summarizes_the_first_items_and_lists_the_rest(caplog, web, keys):
    buttondown(web)
    client = FakeAnthropic()
    assert run("--max-opinions", "2", svc=services(client)) == EXIT_OK
    assert client.calls == [NETTLES, FLEMING]
    assert MOORE_PDF not in pdf_downloads(web)  # an overflow item costs nothing
    md = Path("output/digest.md").read_text(encoding="utf-8")
    assert "## Also filed this week (not summarized)" in md
    assert "This week had more items than the 2-summary limit; these are listed without summaries." in md
    assert any("exceed MAX_OPINIONS_PER_RUN=2" in m and "Gary Moore" in m for m in messages(caplog, logging.WARNING))
    state = read_state()
    assert state["items"]["COA:6170"]["status"] == "listed_only"
    assert state["items"]["COA:6170"]["attempts"] == 0
    assert read_json("output/run_summary.json")["counts"]["listed_only"] == 1


def test_first_run_keeps_only_the_most_recent_week(caplog, web):
    assert run("--dry-run", "--mock-llm", "--lookback-days", "30") == EXIT_OK
    keys_ = [i["key"] for i in read_json("output/digest.json")["items"]]
    assert keys_ == ["SC:28354", "SC:28355", "COA:6170"]  # Taylor (October 21) is left out
    assert TAYLOR_PDF not in pdf_downloads(web)
    assert any(m.startswith("First run") and "1 older item(s) left out" in m for m in messages(caplog, logging.INFO))
    assert read_json("output/run_summary.json")["counts"]["first_run_left_out"] == 1


def test_later_runs_do_not_apply_the_first_run_filter(web):
    Path("data").mkdir()
    Path("data/processed.json").write_text(
        json.dumps(
            {
                "version": 1,
                "items": {"SC:28354": {"kind": "opinion", "court": "SC", "case_name": NETTLES,
                                       "filed_date": "2026-11-12", "status": "summarized", "attempts": 1}},
                "aliases": {},
                "digests": [],
            }
        ),
        encoding="utf-8",
    )  # fmt: skip
    assert run("--dry-run", "--mock-llm", "--lookback-days", "30") == EXIT_OK
    keys_ = [i["key"] for i in read_json("output/digest.json")["items"]]
    assert keys_ == ["SC:28353", "SC:28355", "COA:6170"]  # Taylor is in; Nettles was already processed


def test_item_recognized_by_its_pdf_header_is_not_summarized_again(caplog, web, keys):
    # Recorded as SC:28355 by an earlier run (say, from sccourts.org) with no CourtListener alias.
    Path("data").mkdir()
    Path("data/processed.json").write_text(
        json.dumps(
            {
                "version": 1,
                "items": {"SC:28355": {"kind": "opinion", "court": "SC", "case_name": FLEMING,
                                       "filed_date": "2026-11-12", "status": "summarized", "attempts": 1}},
                "aliases": {},
                "digests": [],
            }
        ),
        encoding="utf-8",
    )  # fmt: skip
    buttondown(web)
    client = FakeAnthropic()
    assert run(svc=services(client)) == EXIT_OK
    assert FLEMING not in client.calls  # the feed's CL:11060100 became SC:28355 once the PDF was read
    assert client.calls == [NETTLES, MOORE]
    subject = json.loads(calls_to(web, "POST", EMAILS_URL)[0].request.body)["subject"]
    assert subject.endswith("— 2 opinions")
    assert any("already recorded" in m and "SC:28355" in m for m in messages(caplog, logging.INFO))
    counts = read_json("output/run_summary.json")["counts"]
    assert counts["already_processed"] == 1 and counts["new"] == 2


def test_courtlistener_api_mode_end_to_end(monkeypatch, web, keys, caplog):
    monkeypatch.setenv("COURTLISTENER_API_TOKEN", "cl-test-token")
    web.get(courtlistener.API_SEARCH_URL, json=fixture_json("courtlistener/search_v4_sample.json"))
    buttondown(web)
    client = FakeAnthropic()
    caplog.set_level(logging.INFO)
    assert run(svc=services(client)) == EXIT_OK
    search = [c for c in web.calls if c.request.url.startswith(courtlistener.API_SEARCH_URL)]
    assert len(search) == 1 and search[0].request.headers["Authorization"] == "Token cl-test-token"
    assert "filed_after=2026-11-06" in search[0].request.url
    # The public feeds are checked too, for opinions the search has not indexed yet, and never get the token.
    feeds = [c for c in web.calls if "/feed/" in c.request.url]
    assert [c.request.url for c in feeds] == [SC_FEED, COA_FEED]
    assert not [c for c in feeds if "Authorization" in c.request.headers]
    # They list the same three opinions as the search, so nothing is added and nothing is summarized twice.
    assert not any("not yet in its search results" in m for m in messages(caplog, logging.INFO))
    assert sorted(client.calls) == sorted([NETTLES, FLEMING, MOORE])
    state = read_state()
    # Moore had no number in the API result; its PDF header supplied 6170.
    assert set(state["items"]) == {"SC:28354", "SC:28355", "COA:6170"}
    assert state["aliases"]["CL:11060103"] == "COA:6170"
    md = Path("output/digest.md").read_text(encoding="utf-8")
    assert "https://www.sccourts.org/media/opinions/HTMLFiles/COA/6170%20.pdf" in md  # readers get the court's PDF
    assert "CourtListener copy" in md
    assert not [u for u in pdf_downloads(web) if "sccourts.org" in u]  # but the job never downloads from it


# November 18, 2026: CourtListener's search index lagged behind its feeds, and it re-created last
# week's (already emailed) Fleming under a new cluster whose feed excerpt shows no opinion number.
RECREATED_FLEMING_PDF = "https://storage.courtlistener.com/pdf/2026/11/12/reid_fleming_v._lg_chem_ltd._1.pdf"


def _stale_search_and_recreated_fleming(web, monkeypatch, pdf_status: int) -> None:
    """The search returns only Nettles and Moore; the SC feed lists Fleming as cluster 11065477."""
    monkeypatch.setenv("COURTLISTENER_API_TOKEN", "cl-test-token")
    sample = fixture_json("courtlistener/search_v4_sample.json")
    sample["results"] = [r for r in sample["results"] if r["cluster_id"] in (11060101, 11060103)]
    web.get(courtlistener.API_SEARCH_URL, json=sample)
    feed = fixture_bytes("courtlistener/feed_sc.xml").replace(b"11060100", b"11065477")
    feed = feed.replace(FLEMING_PDF.encode(), RECREATED_FLEMING_PDF.encode())
    web.replace(responses.GET, SC_FEED, body=feed, content_type="application/atom+xml; charset=utf-8")
    web.get(RECREATED_FLEMING_PDF, status=pdf_status)


@pytest.mark.parametrize("pdf_status", [404, 503])
def test_feed_copy_of_an_emailed_opinion_with_an_unreadable_pdf_is_not_emailed_again(
    monkeypatch, web, keys, caplog, pdf_status
):
    seed_state({"SC:28355": summarized(FLEMING)}, aliases={"CL:11060100": "SC:28355"})
    _stale_search_and_recreated_fleming(web, monkeypatch, pdf_status)
    buttondown(web)
    client = FakeAnthropic()
    assert run(svc=services(client)) == EXIT_OK
    assert client.calls == [NETTLES, MOORE]
    assert RECREATED_FLEMING_PDF in pdf_downloads(web)  # the feed's copy was tried...
    subject = json.loads(calls_to(web, "POST", EMAILS_URL)[0].request.body)["subject"]
    assert subject.endswith("— 2 opinions")
    md = Path("output/digest.md").read_text(encoding="utf-8")
    assert FLEMING not in md  # ...but, unconfirmed, it is not listed again
    state = read_state()
    assert "CL:11065477" not in state["items"] and "CL:11065477" not in state["aliases"]
    assert set(state["items"]) == {"SC:28354", "SC:28355", "COA:6170"}
    assert any("left for a later run" in m and FLEMING in m for m in messages(caplog, logging.INFO))


def test_an_unconfirmed_feed_copy_alone_ends_the_run_quietly(monkeypatch, web, keys, caplog, tmp_path):
    # Everything the search returns is already recorded; the only "new" item is the feed's copy of
    # Fleming, and its PDF can't be read: no email, no failed attempt recorded, exit 0 (not 1).
    seed_state(
        {"SC:28354": summarized(NETTLES), "SC:28355": summarized(FLEMING), "COA:6170": summarized(MOORE, court="COA")},
        aliases={"CL:11060101": "SC:28354", "CL:11060100": "SC:28355", "CL:11060103": "COA:6170"},
    )
    before = Path("data/processed.json").read_text(encoding="utf-8")
    _stale_search_and_recreated_fleming(web, monkeypatch, 404)
    client = FakeAnthropic()
    assert run(svc=services(client)) == EXIT_OK
    assert client.calls == [] and not buttondown_calls(web)
    assert Path("data/processed.json").read_text(encoding="utf-8") == before
    assert output_dict(tmp_path).get("has_new") == "false"
    assert read_json("output/run_summary.json")["result"] == NOTHING_NEW


def test_sccourts_source_with_orders_end_to_end(caplog, monkeypatch):
    """SOURCE=sccourts: listings, the advance sheet's orders (incl. the one printed before the cover)."""
    monkeypatch.setenv("CONTACT_EMAIL", "ops@example.com")
    html = "text/html; charset=utf-8"
    pdf = "application/pdf"
    opinions = "https://www.sccourts.org/media/opinions"
    pages = [
        (sccourts.listing_url(Court.SUPREME, 2026, 11), fixture_text("sccourts/sc_published_2026-11.html"), html),
        (sccourts.listing_url(Court.APPEALS, 2026, 11), fixture_text("sccourts/coa_published_2026-11.html"), html),
        (sccourts.ADVANCE_SHEETS_URL, fixture_text("sccourts/advance_sheets.html"), html),
        (f"{opinions}/advSheets/no412026.pdf", advance_sheet_41_pdf(), pdf),
        (sccourts.ALL_ORDERS_URL, fixture_text("sccourts/all_court_orders_2026.html"), html),
        (f"{opinions}/HTMLFiles/SC/28354.pdf", opinion_pdf("sc_28354"), pdf),
        (f"{opinions}/HTMLFiles/SC/28355.pdf", opinion_pdf("sc_28355"), pdf),
        (f"{opinions}/HTMLFiles/COA/6170%20.pdf", opinion_pdf("coa_6170"), pdf),  # the court's raw-space file name
    ]
    with responses.RequestsMock(assert_all_requests_are_fired=True) as rsps:  # each page is read, and only these
        for url, body, content_type in pages:
            rsps.get(url, body=body, content_type=content_type)
        assert run("--dry-run", "--mock-llm", "--source", "sccourts") == EXIT_OK

    digest = read_json("output/digest.json")
    assert digest["subject"] == "SC Advance Sheet Digest — November 12, 2026 — 3 opinions + 2 orders"
    keys_ = [i["key"] for i in digest["items"]]
    assert keys_ == [
        "SC:28354",
        "SC:28355",
        "ORDER:2026-11-04:re-fees-for-copying-and-other-related-services",  # signed Nov 4, printed Nov 12
        "ORDER:2026-11-12-01",
        "COA:6170",
    ]
    md = Path("output/digest.md").read_text(encoding="utf-8")
    assert "Advance Sheet No. 41" in md and "## Supreme Court orders" in md
    rule_changes = md.split("## Rule changes this week", 1)[1].split("## ", 1)[0]
    assert "Re: Amendments to Rule 1.15, RPC, Rule 407, SCACR" in rule_changes  # the mock marks amendments
    assert "Opinions and orders: South Carolina Judicial Branch (sccourts.org)." in md
    assert Path("output/texts/ORDER-2026-11-12-01.txt").read_text(encoding="utf-8").count("Rule 1.15") >= 1
    # The week's advance sheet was read, so the orders were really checked: no "missing orders" caveat.
    assert "not included in this digest" not in rule_changes and "may not include" not in rule_changes


def test_default_services_run_a_mock_dry_run(web):
    # The real factories: PoliteSession with retries, MockSummarizer; no Buttondown client is built.
    assert main(["--dry-run", "--mock-llm", "--today", TODAY]) == EXIT_OK
    assert Path("output/digest.md").exists() and not Path("data").exists()
    assert {c.request.headers["User-Agent"] for c in web.calls} == {
        "SC-Advance-Sheet-Digest/1.0.0 (weekly newsletter tool)"
    }


def test_output_and_state_paths_can_be_overridden(web, keys):
    buttondown(web)
    assert run("--output-dir", "elsewhere/out", "--state-file", "elsewhere/state.json", svc=services()) == EXIT_OK
    assert Path("elsewhere/out/digest.md").exists() and Path("elsewhere/out/run_summary.json").exists()
    assert Path("elsewhere/state.json").exists() and not Path("data").exists() and not Path("output").exists()


def test_nothing_found_at_all_exits_0(web):
    # On November 1 the lookback window holds nothing (and November 12 is in the future).
    assert run("--dry-run", "--mock-llm", today="2026-11-01") == EXIT_OK
    assert not Path("output/digest.md").exists()
    assert read_json("output/run_summary.json")["result"] == (
        "No new opinions or orders since the last run. Nothing to send."
    )


# --------------------------------------------------------------------------- regression tests (review fixes)

EMPTY_FEED = '<?xml version="1.0" encoding="utf-8"?><feed xmlns="http://www.w3.org/2005/Atom"><title>x</title></feed>'


def seed_state(items: dict, aliases: dict | None = None, digests: list | None = None) -> None:
    Path("data").mkdir(exist_ok=True)
    data = {"version": 1, "items": items, "aliases": aliases or {}, "digests": digests or []}
    Path("data/processed.json").write_text(json.dumps(data), encoding="utf-8")


def summarized(case_name: str, filed: str = "2026-11-12", court: str = "SC", **extra) -> dict:
    return {"kind": "opinion", "court": court, "case_name": case_name, "filed_date": filed,
            "status": "summarized", "attempts": 1, "email_id": "em_old", "digest_subject": "Old digest", **extra}  # fmt: skip


def test_first_real_run_records_the_older_items_it_left_out(caplog, web, keys):
    # req-3 / state-1: the first run keeps only the most recent week. The older items it leaves out
    # are recorded, so the next run does not summarize and email last week's backlog.
    buttondown(web)
    client = FakeAnthropic()
    assert run("--lookback-days", "30", svc=services(client)) == EXIT_OK
    state = read_state()
    taylor = state["items"]["SC:28353"]
    assert taylor["status"] == "skipped_first_run" and taylor["attempts"] == 0 and taylor["email_id"] is None
    assert "SC:28353" not in state["digests"][-1]["items"]  # it was never in an email
    assert TAYLOR_PDF not in pdf_downloads(web)

    caplog.clear()
    assert run("--lookback-days", "30", svc=services(client), today="2026-11-17") == EXIT_OK
    assert NOTHING_NEW in messages(caplog, logging.INFO)
    assert len(calls_to(web, "POST", EMAILS_URL)) == 1  # no second, stale email
    assert "In the Matter of Jason F. Taylor" not in client.calls


def test_first_dry_run_records_nothing(web):
    assert run("--dry-run", "--mock-llm", "--lookback-days", "30") == EXIT_OK
    assert not Path("data/processed.json").exists()


def test_late_posting_on_thursday_is_a_supplement(web, keys):
    # req-1 / reader-1: Wednesday's run sees only the Supreme Court opinions; the Court of Appeals
    # opinion posts late and Thursday's run emails it. That email is labeled a supplement.
    web.replace(responses.GET, COA_FEED, body=EMPTY_FEED, content_type="application/atom+xml")
    buttondown(web)
    assert run(svc=services()) == EXIT_OK
    wednesday = read_state()["digests"][-1]["subject"]
    assert wednesday == "SC Advance Sheet Digest — November 12, 2026 — 2 opinions"

    web.replace(responses.GET, COA_FEED, body=fixture_bytes("courtlistener/feed_scctapp.xml"),
                content_type="application/atom+xml")  # fmt: skip
    web.replace(responses.POST, EMAILS_URL, json={"id": "em_test_2", "status": "draft", "absolute_url": None})
    assert run(svc=services(), today="2026-11-17") == EXIT_OK
    posts = calls_to(web, "POST", EMAILS_URL)
    assert len(posts) == 2
    thursday = json.loads(posts[1].request.body)
    assert thursday["subject"] == "SC Advance Sheet Digest — November 12, 2026 — 1 opinion (supplement)"
    assert f"Supplement to this week's earlier issue (“{wednesday}”)" in thursday["body"]
    assert "no published Supreme Court opinions" not in thursday["body"]
    assert "## Supreme Court\n" not in thursday["body"]
    assert read_json("output/run_summary.json")["supplement_to"] == wednesday


def test_a_later_week_is_not_a_supplement(web, keys):
    seed_state(
        {"SC:28340": summarized("Old v. Case", filed="2026-11-05", digest_subject="Last week", email_id="em_old")},
        digests=[{"subject": "Last week", "email_id": "em_old", "status": "sent", "items": ["SC:28340"]}],
    )
    buttondown(web)
    assert run(svc=services()) == EXIT_OK
    sent = json.loads(calls_to(web, "POST", EMAILS_URL)[0].request.body)
    assert sent["subject"] == "SC Advance Sheet Digest — November 12, 2026 — 3 opinions"
    assert "Supplement" not in sent["body"] and "New this week:" in sent["body"]


def _seed_this_week_except_moore() -> None:
    seed_state(
        {"SC:28354": summarized(NETTLES), "SC:28355": summarized(FLEMING)},
        aliases={"CL:11060100": "SC:28355"},
    )


def test_a_lone_failing_item_is_capped_and_then_listed_in_the_next_digest(tmp_path, caplog, web, keys):
    # req-4 / docs-3: a lone opinion whose summary keeps failing used to be paid for on every run
    # (exit 1, nothing recorded) and never emailed. Now its attempts are counted even though the
    # run sends nothing, and once they are used up the next digest lists it with its link.
    _seed_this_week_except_moore()
    buttondown(web)
    client = FakeAnthropic(fails_for(MOORE))
    assert run(svc=services(client)) == EXIT_FAILED  # Thursday: alone, fails; no email
    record = read_state()["items"]["COA:6170"]
    assert (record["status"], record["attempts"], record["email_id"]) == ("failed", 1, None)
    assert output_dict(tmp_path)["state_changed"] == "true"  # the workflow commits the attempt
    assert any("attempts were recorded" in m for m in messages(caplog, logging.ERROR))

    assert run(svc=services(client), today="2026-11-18") == EXIT_FAILED  # one more try, then given up
    assert read_state()["items"]["COA:6170"]["attempts"] == 2
    assert run(svc=services(client), today="2026-11-19") == EXIT_OK  # not retried again
    assert client.calls.count(MOORE) == 4  # two requests per run, two runs: the cap holds
    assert not calls_to(web, "POST", EMAILS_URL)

    # The next digest (a new opinion arrived: Taylor, inside a longer window) lists Moore too.
    assert run("--lookback-days", "40", svc=services(client), today="2026-11-19") == EXIT_OK
    assert client.calls.count(MOORE) == 4  # listed, not tried again
    body = json.loads(calls_to(web, "POST", EMAILS_URL)[0].request.body)["body"]
    assert "### Gary Moore v. State" in body
    assert "The AI summary isn't available this week (the AI response was not valid)" in body
    record = read_state()["items"]["COA:6170"]
    assert record["email_id"] == EMAIL_ID and record["attempts"] == 2
    assert read_json("output/run_summary.json")["counts"]["listed_after_earlier_failures"] == 1
    assert State.load("data/processed.json").unlisted_failures(2) == []  # nothing is owed a listing now


def test_an_outage_in_an_all_failed_run_does_not_use_up_attempts(web, keys):
    _seed_this_week_except_moore()
    before = Path("data/processed.json").read_bytes()
    client = FakeAnthropic(lambda case, attempt: api_error(anthropic.InternalServerError, 529, "overloaded"))
    assert run(svc=services(client)) == EXIT_FAILED
    assert Path("data/processed.json").read_bytes() == before  # "the AI service was unavailable": not counted


def test_a_pdf_download_outage_in_an_all_failed_run_does_not_use_up_attempts(web, keys):
    _seed_this_week_except_moore()
    before = Path("data/processed.json").read_bytes()
    web.replace(responses.GET, MOORE_PDF, status=503)
    client = FakeAnthropic()
    assert run(svc=services(client)) == EXIT_FAILED
    assert Path("data/processed.json").read_bytes() == before  # a server error may pass: not counted
    assert not client.calls


def test_a_pdf_download_outage_is_described_as_such(web, keys):
    web.replace(responses.GET, MOORE_PDF, status=503)
    buttondown(web)
    assert run(svc=services()) == EXIT_OK
    md = Path("output/digest.md").read_text(encoding="utf-8")
    assert "The AI summary isn't available this week (the opinion PDF could not be downloaded)" in md


def test_an_all_failed_first_run_records_nothing(web, keys):
    client = FakeAnthropic(lambda case, attempt: api_response(case, text="{not json"))
    assert run(svc=services(client)) == EXIT_FAILED
    assert not Path("data/processed.json").exists()  # the next run is again a first run


def test_a_refiled_opinion_that_fails_alone_is_not_mistaken_for_a_listed_retry(caplog, web, keys):
    # state-6: the old version was emailed; its refiled version (later filing date) fails alone.
    seed_state(
        {"SC:28354": summarized(NETTLES, filed="2026-10-21"), "SC:28355": summarized(FLEMING),
         "COA:6170": summarized(MOORE, court="COA")},
        aliases={"CL:11060100": "SC:28355"},
    )  # fmt: skip
    web.replace(responses.GET, NETTLES_PDF, status=404)
    assert run(svc=services()) == EXIT_FAILED  # was: exit 0, "an earlier digest already listed it"
    assert not any("already listed" in m for m in messages(caplog))
    record = read_state()["items"]["SC:28354"]
    assert record["filed_date"] == "2026-11-12" and record["attempts"] == 1 and record["email_id"] is None


def test_courtlistener_refiled_opinion_is_marked_in_the_email(web, keys):
    # state-5 / reader-3: a later filing of an opinion already emailed is summarized again, and
    # the email says it was withdrawn, substituted, and refiled.
    seed_state({"SC:28354": summarized(NETTLES, filed="2026-10-21")})
    buttondown(web)
    assert run(svc=services()) == EXIT_OK
    md = Path("output/digest.md").read_text(encoding="utf-8")
    assert "**Op. No. 28354** · Filed November 12, 2026 (withdrawn, substituted, and refiled)" in md
    assert read_json("output/digest.json")["items"][0]["refiled"] is True


def test_a_slot_freed_by_an_already_recorded_item_goes_to_the_next_one(caplog, web, keys):
    # state-7: Fleming turns out to be recorded once its PDF header is read, so Moore takes its slot
    # instead of being listed without a summary (and never summarized).
    seed_state({"SC:28355": summarized(FLEMING)})
    buttondown(web)
    client = FakeAnthropic()
    assert run("--max-opinions", "2", svc=services(client)) == EXIT_OK
    assert client.calls == [NETTLES, MOORE]
    md = Path("output/digest.md").read_text(encoding="utf-8")
    assert "Also filed this week" not in md
    assert read_state()["items"]["COA:6170"]["status"] == "summarized"


def test_overflow_note_names_the_configured_cap(web, keys):
    buttondown(web)
    assert run("--max-opinions", "1", svc=services()) == EXIT_OK
    md = Path("output/digest.md").read_text(encoding="utf-8")
    assert "This week had more items than the 1-summary limit; these are listed without summaries." in md
    rule = md.split("## Rule changes this week", 1)[1].split("## ", 1)[0]
    assert "2 opinions were not summarized, so they were not checked for rule changes" in rule  # req-2


def test_a_pdf_page_neither_library_can_read_fails_the_item_without_an_api_call(monkeypatch, caplog, web, keys):
    # extract-1: summarizing part of an opinion as if it were all of it could misstate the holding.
    real = main_mod.extract_text

    def partly_unreadable(data):
        result = real(data)
        if "Gary Moore" in result.text:
            result.missing_pages = [2]
        return result

    monkeypatch.setattr(main_mod, "extract_text", partly_unreadable)
    buttondown(web)
    client = FakeAnthropic()
    assert run(svc=services(client)) == EXIT_OK
    assert MOORE not in client.calls
    md = Path("output/digest.md").read_text(encoding="utf-8")
    assert "The AI summary isn't available this week (part of the opinion PDF could not be read)" in md
    assert any("could not be read by pdfplumber or pypdf" in m for m in messages(caplog, logging.WARNING))


def test_a_model_without_a_profile_warns_that_summary_effort_is_not_sent(monkeypatch, caplog, web, keys):
    monkeypatch.setenv("CLAUDE_MODEL", "claude-unknown-9")
    buttondown(web)
    client = FakeAnthropic()
    assert run(svc=services(client)) == EXIT_OK
    assert any("SUMMARY_EFFORT=low is not sent" in m for m in messages(caplog, logging.WARNING))
    assert all("effort" not in kw["output_config"] for kw in client.kwargs)
    assert read_json("output/run_summary.json")["effort"] == "API default (SUMMARY_EFFORT not sent)"
    assert read_json("output/digest.json")["effort"] == "API default (SUMMARY_EFFORT not sent)"


def test_a_known_model_reports_its_effort(caplog, web, keys):
    buttondown(web)
    assert run(svc=services()) == EXIT_OK
    assert read_json("output/run_summary.json")["effort"] == "low"
    assert not any("is not sent" in m for m in messages(caplog, logging.WARNING))


def _sccourts_pages(*, orders_status: int = 200, sheets_status: int = 200) -> list[tuple]:
    html = "text/html; charset=utf-8"
    pdf = "application/pdf"
    opinions = "https://www.sccourts.org/media/opinions"
    return [
        (sccourts.listing_url(Court.SUPREME, 2026, 11), fixture_text("sccourts/sc_published_2026-11.html"), html, 200),
        (sccourts.listing_url(Court.APPEALS, 2026, 11), fixture_text("sccourts/coa_published_2026-11.html"), html, 200),
        (sccourts.ADVANCE_SHEETS_URL, fixture_text("sccourts/advance_sheets.html"), html, sheets_status),
        (f"{opinions}/advSheets/no412026.pdf", advance_sheet_41_pdf(), pdf, 200),
        (sccourts.ALL_ORDERS_URL, fixture_text("sccourts/all_court_orders_2026.html"), html, orders_status),
        (f"{opinions}/HTMLFiles/SC/28354.pdf", opinion_pdf("sc_28354"), pdf, 200),
        (f"{opinions}/HTMLFiles/SC/28355.pdf", opinion_pdf("sc_28355"), pdf, 200),
        (f"{opinions}/HTMLFiles/COA/6170%20.pdf", opinion_pdf("coa_6170"), pdf, 200),
    ]


def _register(rsps, pages) -> None:
    for url, body, content_type, status in pages:
        rsps.get(url, body=body, content_type=content_type, status=status)


def test_sccourts_order_is_emailed_once_even_if_the_orders_page_failed_the_first_time(monkeypatch, keys):
    # state-3: run 1 cannot read the All Court Orders page, so the Rule 1.15 order is keyed by date and
    # title; run 2 can, so it gets its number. It is the same order: no second email.
    monkeypatch.setenv("CONTACT_EMAIL", "ops@example.com")
    client = FakeAnthropic()
    with responses.RequestsMock(assert_all_requests_are_fired=False) as rsps:
        _register(rsps, _sccourts_pages(orders_status=503))
        buttondown(rsps)
        assert run("--source", "sccourts", svc=services(client)) == EXIT_OK
        assert "ORDER:2026-11-12:re-amendments-to-rule-1-15-rpc-rule-407-scacr" in read_state()["items"]
    with responses.RequestsMock(assert_all_requests_are_fired=False) as rsps:
        _register(rsps, _sccourts_pages())
        buttondown(rsps)
        assert run("--source", "sccourts", svc=services(client), today="2026-11-17") == EXIT_OK
        assert not calls_to(rsps, "POST", EMAILS_URL)
    assert client.calls.count("Re: Amendments to Rule 1.15, RPC, Rule 407, SCACR") == 1


def test_sccourts_unreadable_advance_sheets_do_not_claim_no_order_changed_a_rule(monkeypatch):
    # state-4 / reader-6: the orders were never checked, so the email must not say none changed a rule.
    monkeypatch.setenv("CONTACT_EMAIL", "ops@example.com")
    with responses.RequestsMock(assert_all_requests_are_fired=False) as rsps:
        _register(rsps, _sccourts_pages(sheets_status=503))
        assert run("--dry-run", "--mock-llm", "--source", "sccourts") == EXIT_OK
    md = Path("output/digest.md").read_text(encoding="utf-8")
    rule = md.split("## Rule changes this week", 1)[1].split("## ", 1)[0]
    assert "No opinion or order this week" not in rule
    assert "_No opinion this week changes or clarifies a rule._" in rule
    assert "Supreme Court orders and rule amendments are not included in this digest" in rule
    warnings = read_json("output/run_summary.json")["warnings"]
    assert any("could not be checked for this digest's week" in w for w in warnings)


def test_sccourts_without_a_contact_email_exits_1_before_any_request(caplog):
    # req-7 / cfg-1: requests to the court's server must carry a contact email.
    with responses.RequestsMock() as rsps:  # nothing registered: any request would fail the test
        assert run("--dry-run", "--mock-llm", "--source", "sccourts") == EXIT_FAILED
        assert len(rsps.calls) == 0
    assert any("SOURCE=sccourts needs CONTACT_EMAIL" in m for m in messages(caplog, logging.ERROR))


def test_item_from_record_rebuilds_each_key_form():
    rebuilt = main_mod.item_from_record
    base = {"kind": "opinion", "court": "SC", "case_name": "Reid Fleming v. LG Chem Ltd.", "filed_date": "2026-11-12",
            "link_url": "https://example.com/x.pdf", "source": "courtlistener-feed"}  # fmt: skip
    assert rebuilt("SC:28355", base).number == "28355"
    assert rebuilt("CL:11060100", base).source_id == "11060100"
    slug = {**base, "source": "sccourts"}
    assert rebuilt("SC:2026-11-12:reid-fleming-v-lg-chem-ltd", slug).number is None
    order = {**base, "kind": "order", "case_name": "Re: Fees", "source": "sccourts"}
    assert rebuilt("ORDER:2026-11-12-01", order).number == "2026-11-12-01"
    assert rebuilt("ORDER:2026-11-12:re-fees", order).number is None
    assert rebuilt("SC:28355", {**base, "link_url": ""}) is None  # incomplete record
    assert rebuilt("SC:28355", {**base, "court": "XX"}) is None
    assert rebuilt("COA:6170", base) is None  # the key does not match the record


# --------------------------------------------------------------------------- helpers


def _item(kind=ItemKind.OPINION, court=Court.SUPREME, number=None, filed=date(2026, 11, 12), name="X", sheet=None):
    return Item(kind=kind, court=court, case_name=name, filed_date=filed, number=number, pdf_url=None,
                link_url="https://example.com/x.pdf", source="sccourts", advance_sheet=sheet)  # fmt: skip


def test_pipeline_order_is_supreme_court_then_orders_then_court_of_appeals():
    items = [
        _item(court=Court.APPEALS, number="6170"),
        _item(kind=ItemKind.ORDER, number="2026-11-12-01"),
        _item(number="28355"),
        _item(number="28354", filed=date(2026, 11, 12)),
        _item(number="9999", filed=date(2026, 11, 5)),
        _item(kind=ItemKind.ORDER, number=None, filed=date(2026, 11, 4), name="Re: Fees"),
    ]
    ordered = [(i.kind.value, i.court.value, i.number) for i in sorted(items, key=pipeline_order)]
    assert ordered == [
        ("opinion", "SC", "9999"),
        ("opinion", "SC", "28354"),
        ("opinion", "SC", "28355"),
        ("order", "SC", None),
        ("order", "SC", "2026-11-12-01"),
        ("opinion", "COA", "6170"),
    ]


def test_most_recent_week_uses_the_advance_sheet_date_for_items_printed_in_one():
    sheets = {41: date(2026, 11, 12)}
    newest = _item(number="28355", filed=date(2026, 11, 12), sheet=41)
    order = _item(kind=ItemKind.ORDER, filed=date(2026, 11, 4), sheet=41, name="Re: Fees")
    edge = _item(number="28350", filed=date(2026, 11, 7))
    old = _item(number="28340", filed=date(2026, 11, 6))
    assert most_recent_week([newest, order, edge, old], sheets) == [newest, order, edge]
    assert most_recent_week([order], {}) == [order]
    assert most_recent_week([], sheets) == []
