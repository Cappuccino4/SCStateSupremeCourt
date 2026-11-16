"""Tests for src/scrape.py: source dispatch, the lookback window, and filtering."""

from __future__ import annotations

import logging
from datetime import date

import pytest
import responses

from src import extract, scrape
from src.config import ConfigError, Settings
from src.models import AdvanceSheet, Court, Item, ItemKind, ScrapeResult
from src.net import PoliteSession
from src.sources import SourceError, courtlistener
from tests.conftest import fixture_bytes, fixture_json

TODAY = date(2026, 11, 16)
CONTACT = "ops@example.com"
UA = "SC-Advance-Sheet-Digest/test (pytest)"


def session() -> PoliteSession:
    return PoliteSession(UA, min_interval=0, max_retries=0)


def item(name: str, filed: date, *, court: Court = Court.SUPREME, number: str | None = None,
         kind: ItemKind = ItemKind.OPINION, source: str = "courtlistener-feed") -> Item:  # fmt: skip
    return Item(
        kind=kind,
        court=court,
        case_name=name,
        filed_date=filed,
        number=number,
        pdf_url=f"https://storage.courtlistener.com/{name}.pdf",
        link_url=f"https://storage.courtlistener.com/{name}.pdf",
        source=source,
    )


class Recorder:
    """Stands in for a source's fetch(): records the call and returns a canned result."""

    def __init__(self, result: ScrapeResult | Exception):
        self.result = result
        self.calls: list[tuple[tuple, dict]] = []

    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


@pytest.fixture
def cl_fetch(monkeypatch):
    def install(result: ScrapeResult | Exception) -> Recorder:
        recorder = Recorder(result)
        monkeypatch.setattr(scrape.courtlistener, "fetch", recorder)
        return recorder

    return install


@pytest.fixture
def sc_fetch(monkeypatch):
    def install(result: ScrapeResult | Exception) -> Recorder:
        recorder = Recorder(result)
        monkeypatch.setattr(scrape.sccourts, "fetch", recorder)
        return recorder

    return install


# --------------------------------------------------------------------------- enabled_courts


@pytest.mark.parametrize(
    ("supreme", "appeals", "expected"),
    [
        (True, True, [Court.SUPREME, Court.APPEALS]),
        (True, False, [Court.SUPREME]),
        (False, True, [Court.APPEALS]),
        (False, False, []),
    ],
)
def test_enabled_courts(supreme, appeals, expected):
    settings = Settings(include_supreme_court=supreme, include_court_of_appeals=appeals)
    assert scrape.enabled_courts(settings) == expected


# --------------------------------------------------------------------------- dispatch


def test_courtlistener_dispatch_passes_the_window_courts_and_token(cl_fetch):
    recorder = cl_fetch(ScrapeResult(items=[], source="courtlistener-api"))
    s = session()
    settings = Settings(lookback_days=10, courtlistener_api_token="tok", include_orders=False)
    result = scrape.find_items(settings, s, TODAY)
    assert recorder.calls == [((s, [Court.SUPREME, Court.APPEALS], date(2026, 11, 6), "tok"), {})]
    assert result.source == "courtlistener-api" and result.items == []


def test_courtlistener_dispatch_without_a_token_passes_none(cl_fetch):
    recorder = cl_fetch(ScrapeResult(items=[], source="courtlistener-feed"))
    scrape.find_items(Settings(lookback_days=3, include_court_of_appeals=False), session(), TODAY)
    args, _ = recorder.calls[0]
    assert args[1:] == ([Court.SUPREME], date(2026, 11, 13), None)


def test_sccourts_dispatch_passes_orders_the_pdf_reader_and_today(sc_fetch, cl_fetch):
    sheets = [AdvanceSheet(41, date(2026, 11, 12), "https://www.sccourts.org/media/opinions/advSheets/no412026.pdf")]
    recorder = sc_fetch(ScrapeResult(items=[], source="sccourts", advance_sheets=sheets))
    cl = cl_fetch(ScrapeResult(items=[], source="courtlistener-feed"))
    s = session()
    settings = Settings(source="sccourts", lookback_days=10, include_orders=True, contact_email=CONTACT)
    result = scrape.find_items(settings, s, TODAY)
    assert recorder.calls == [
        (
            (s, [Court.SUPREME, Court.APPEALS], date(2026, 11, 6), True),
            {"pdf_to_pages": extract.pdf_to_layout_pages, "today": TODAY},  # keeps wrapped page numbers
        )
    ]
    assert not cl.calls
    assert result.source == "sccourts" and result.advance_sheets == sheets


def test_sccourts_orders_only_still_fetches_when_both_courts_are_off(sc_fetch):
    recorder = sc_fetch(ScrapeResult(items=[], source="sccourts"))
    settings = Settings(source="sccourts", include_supreme_court=False, include_court_of_appeals=False,
                        contact_email=CONTACT)  # fmt: skip
    scrape.find_items(settings, session(), TODAY)
    assert recorder.calls[0][0][1] == []


def test_no_courts_and_no_orders_fetches_nothing(cl_fetch, sc_fetch, caplog):
    cl, sc = cl_fetch(ScrapeResult([], "x")), sc_fetch(ScrapeResult([], "x"))
    caplog.set_level(logging.WARNING, logger="src.scrape")
    off = {"include_supreme_court": False, "include_court_of_appeals": False}
    result = scrape.find_items(Settings(**off), session(), TODAY)
    assert result.items == [] and result.source == "courtlistener-feed"
    result = scrape.find_items(Settings(source="sccourts", include_orders=False, **off), session(), TODAY)
    assert result.items == [] and result.source == "sccourts"
    assert not cl.calls and not sc.calls
    assert sum(scrape.NO_COURTS in r.getMessage() for r in caplog.records) == 2


def test_sccourts_without_a_contact_email_is_refused_before_any_request(sc_fetch, cl_fetch):
    # The requirement: requests to the court's server carry a contact email in the User-Agent.
    sc, cl = sc_fetch(ScrapeResult([], "sccourts")), cl_fetch(ScrapeResult([], "x"))
    with responses.RequestsMock() as rsps:  # nothing registered: any request would fail the test
        with pytest.raises(ConfigError, match="SOURCE=sccourts needs CONTACT_EMAIL"):
            scrape.find_items(Settings(source="sccourts", repository="owner/repo"), session(), TODAY)
        assert len(rsps.calls) == 0
    assert not sc.calls and not cl.calls


def test_courtlistener_does_not_need_a_contact_email(cl_fetch):
    recorder = cl_fetch(ScrapeResult(items=[], source="courtlistener-feed"))
    scrape.find_items(Settings(), session(), TODAY)
    assert len(recorder.calls) == 1


def test_sccourts_passes_on_the_sheets_whose_orders_were_checked(sc_fetch):
    sheet = AdvanceSheet(41, date(2026, 11, 12), "https://www.sccourts.org/media/opinions/advSheets/no412026.pdf")
    sc_fetch(ScrapeResult(items=[], source="sccourts", advance_sheets=[sheet], checked_advance_sheets=[sheet]))
    result = scrape.find_items(Settings(source="sccourts", contact_email=CONTACT), session(), TODAY)
    assert result.checked_advance_sheets == [sheet]


def test_unknown_source_raises_config_error():
    with pytest.raises(ConfigError, match="SOURCE"):
        scrape.find_items(Settings(source="westlaw"), session(), TODAY)


def test_source_errors_propagate(cl_fetch):
    cl_fetch(SourceError("CourtListener rejected the API token (HTTP 401). Check the COURTLISTENER_API_TOKEN secret."))
    with pytest.raises(SourceError, match="HTTP 401"):
        scrape.find_items(Settings(courtlistener_api_token="bad"), session(), TODAY)


# --------------------------------------------------------------------------- filtering


def test_items_dated_after_today_are_dropped_and_duplicates_removed(cl_fetch):
    a = item("a", date(2026, 11, 12), number="28354")
    future = item("future", date(2026, 11, 17), number="28360")
    dup = item("a again", date(2026, 11, 12), number="28354")  # same key SC:28354
    today_item = item("today", TODAY, number="28361")
    cl_fetch(ScrapeResult(items=[a, future, dup, today_item], source="courtlistener-feed", warnings=["w1"]))
    result = scrape.find_items(Settings(), session(), TODAY)
    assert result.items == [a, today_item]
    assert result.warnings == ["w1"]


def test_courtlistener_warnings_are_passed_on_but_not_logged_twice(cl_fetch, caplog):
    cl_fetch(ScrapeResult(items=[], source="courtlistener-feed", warnings=[courtlistener.NO_TOKEN_WARNING]))
    caplog.set_level(logging.INFO, logger="src.scrape")
    result = scrape.find_items(Settings(include_orders=False), session(), TODAY)
    assert result.warnings == [courtlistener.NO_TOKEN_WARNING]
    # courtlistener.fetch has already logged it (here it is faked, so nothing logs it at all).
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


def test_sccourts_warnings_are_logged_here(sc_fetch, caplog):
    note = "Could not read Advance Sheet No. 41: timeout. Its orders and rule amendments are not included."
    sc_fetch(ScrapeResult(items=[], source="sccourts", warnings=[note]))
    caplog.set_level(logging.INFO, logger="src.scrape")
    result = scrape.find_items(Settings(source="sccourts", contact_email=CONTACT), session(), TODAY)
    assert result.warnings == [note]
    assert [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING] == [note]


def test_orders_need_sccourts_is_logged_once_at_info_in_courtlistener_mode(cl_fetch, caplog):
    cl_fetch(ScrapeResult(items=[], source="courtlistener-feed"))
    caplog.set_level(logging.INFO, logger="src.scrape")
    scrape.find_items(Settings(include_orders=True), session(), TODAY)
    notes = [r for r in caplog.records if scrape.ORDERS_NEED_SCCOURTS in r.getMessage()]
    assert len(notes) == 1 and notes[0].levelno == logging.INFO
    caplog.clear()
    scrape.find_items(Settings(include_orders=False), session(), TODAY)
    assert not [r for r in caplog.records if scrape.ORDERS_NEED_SCCOURTS in r.getMessage()]


def test_found_items_are_logged_per_court(cl_fetch, caplog):
    cl_fetch(
        ScrapeResult(
            items=[
                item("Nettles", date(2026, 11, 12), number="28354"),
                item("Fleming", date(2026, 11, 12), number="28355"),
                item("Moore", date(2026, 11, 12), number="6170", court=Court.APPEALS),
            ],
            source="courtlistener-feed",
        )
    )
    caplog.set_level(logging.INFO, logger="src.scrape")
    scrape.find_items(Settings(include_orders=False), session(), TODAY)
    lines = [r.getMessage() for r in caplog.records]
    assert "Found 3 item(s): 2 Supreme Court opinion(s), 1 Court of Appeals opinion(s)." in lines
    assert any("COA 6170 Moore (filed November 12, 2026)" in line for line in lines)


# --------------------------------------------------------------------------- with the real sources


@pytest.fixture
def feeds():
    with responses.RequestsMock(assert_all_requests_are_fired=False) as rsps:
        atom = "application/atom+xml; charset=utf-8"
        rsps.get(courtlistener.FEED_URL.format(court_id="sc"), body=fixture_bytes("courtlistener/feed_sc.xml"),
                 content_type=atom)  # fmt: skip
        rsps.get(courtlistener.FEED_URL.format(court_id="scctapp"),
                 body=fixture_bytes("courtlistener/feed_scctapp.xml"), content_type=atom)  # fmt: skip
        yield rsps


def test_feed_mode_with_the_real_feeds(feeds):
    result = scrape.find_items(Settings(lookback_days=10), session(), TODAY)
    assert [i.key for i in result.items] == ["SC:28354", "CL:11060100", "COA:6170"]
    assert result.source == "courtlistener-feed"
    assert result.warnings == [courtlistener.NO_TOKEN_WARNING]


def test_feed_mode_drops_opinions_filed_after_today(feeds):
    # On November 11 the November 12 opinions are in the future; October's are outside the window.
    result = scrape.find_items(Settings(lookback_days=10), session(), date(2026, 11, 11))
    assert result.items == []


def test_feed_mode_respects_the_court_toggles(feeds):
    result = scrape.find_items(Settings(include_supreme_court=False), session(), TODAY)
    assert [i.key for i in result.items] == ["COA:6170"]
    assert [c.request.url for c in feeds.calls] == [courtlistener.FEED_URL.format(court_id="scctapp")]


def test_api_mode_with_the_sample_response():
    with responses.RequestsMock() as rsps:
        rsps.get(courtlistener.API_SEARCH_URL, json=fixture_json("courtlistener/search_v4_sample.json"))
        result = scrape.find_items(Settings(courtlistener_api_token="tok", lookback_days=10), session(), TODAY)
        request = rsps.calls[0].request
    assert request.headers["Authorization"] == "Token tok"
    assert "filed_after=2026-11-06" in request.url
    assert [i.key for i in result.items] == ["SC:28354", "SC:28355", "CL:11060103"]
    assert result.source == "courtlistener-api" and result.warnings == []
