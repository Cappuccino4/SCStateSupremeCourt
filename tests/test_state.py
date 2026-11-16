"""Tests for src/state.py (data/processed.json)."""

from __future__ import annotations

import json
import logging
import os
from datetime import date, datetime, timedelta, timezone

import pytest

from src.models import Court, Item, ItemKind, ResultStatus, Summary, SummaryResult
from src.state import (
    MAX_DIGESTS,
    STATE_VERSION,
    STATUS_FAILED,
    STATUS_LISTED_ONLY,
    STATUS_SKIPPED_FIRST_RUN,
    STATUS_SUMMARIZED,
    State,
    StateError,
    utc_timestamp,
)

NOW = datetime(2026, 11, 18, 20, 31, 2, tzinfo=timezone.utc)
LATER = datetime(2026, 11, 19, 13, 5, 0, tzinfo=timezone.utc)
SUBJECT = "SC Advance Sheet Digest — November 12, 2026 — 3 opinions + 1 order"
SUBJECT_2 = "SC Advance Sheet Digest — November 19, 2026 — 2 opinions"


# ---------------------------------------------------------------- helpers


def opinion(
    number: str | None = "28355",
    *,
    court: Court = Court.SUPREME,
    case_name: str = "Reid Fleming v. LG Chem Ltd.",
    filed: date = date(2026, 11, 12),
    source: str = "courtlistener-api",
    source_id: str | None = "11060100",
    link_url: str = "https://www.sccourts.org/media/opinions/HTMLFiles/SC/28355.pdf",
) -> Item:
    """An opinion Item shaped like the CourtListener source builds them (real values of this week)."""
    return Item(
        kind=ItemKind.OPINION,
        court=court,
        case_name=case_name,
        filed_date=filed,
        number=number,
        pdf_url="https://storage.courtlistener.com/pdf/2026/11/12/reid_fleming_v._lg_chem_ltd..pdf",
        link_url=link_url,
        mirror_url="https://www.courtlistener.com/opinion/11060100/reid-fleming-v-lg-chem-ltd/",
        source=source,
        source_id=source_id,
    )


def nettles() -> Item:
    return opinion(
        "28354",
        case_name="In the Matter of Louis David Nettles",
        source_id="11060101",
        link_url="https://www.sccourts.org/media/opinions/HTMLFiles/SC/28354.pdf",
    )


def moore(number: str | None = None) -> Item:
    """COA 6170. In API mode its docket is 2024-000459, so the number is unknown until the PDF is read."""
    return opinion(
        number,
        court=Court.APPEALS,
        case_name="Gary Moore v. State",
        source_id="11060103",
        link_url="https://www.sccourts.org/media/opinions/HTMLFiles/COA/6170%20.pdf",
    )


def rule_order(filed: date = date(2026, 11, 12)) -> Item:
    return Item(
        kind=ItemKind.ORDER,
        court=Court.SUPREME,
        case_name="Re: Amendments to Rule 1.15, RPC, Rule 407, SCACR",
        filed_date=filed,
        number="2026-11-12-01",
        pdf_url=None,
        link_url="https://www.sccourts.org/media/orders/2026-11-12-01.pdf",
        source="sccourts",
    )


def summary_for(item: Item) -> Summary:
    return Summary(
        case_name=item.case_name,
        opinion_number=item.number or "unclear from the opinion",
        court=item.court.display_name,
        date="November 12, 2026",
        author="Justice Hill",
        one_line="A takeaway.",
        holding="A holding.",
        what_changed="No change to existing law.",
        change_type="none",
        practice_areas=["Torts"],
        practice_tip="A tip.",
        disposition="Reversed",
    )


def ok(item: Item) -> SummaryResult:
    return SummaryResult(item=item, status=ResultStatus.SUMMARIZED, summary=summary_for(item))


def failed(item: Item, error: str = "the AI service was unavailable") -> SummaryResult:
    return SummaryResult(item=item, status=ResultStatus.FAILED, error=error)


def record_ok(state: State, item: Item, now: datetime = NOW, subject: str = SUBJECT, email_id: str = "em-1"):
    state.record_result(ok(item), digest_subject=subject, email_id=email_id, now=now)


def record_failed(state: State, item: Item, now: datetime = NOW, subject: str = SUBJECT, email_id: str = "em-1"):
    state.record_result(failed(item), digest_subject=subject, email_id=email_id, now=now)


def record_listed(state: State, item: Item, now: datetime = NOW, subject: str = SUBJECT, email_id: str = "em-1"):
    state.record_listed_only(item, digest_subject=subject, email_id=email_id, now=now)


def digest_record(created_at: str, items: list[str], subject: str = SUBJECT) -> dict:
    return {"subject": subject, "email_id": "em-1", "status": "draft", "created_at": created_at, "items": items}


def write(path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


@pytest.fixture
def state_path(tmp_path):
    return tmp_path / "data" / "processed.json"


# ---------------------------------------------------------------- loading


def test_missing_file_gives_empty_state(state_path):
    state = State.load(state_path)
    assert state.is_empty
    assert state.path == state_path
    assert state.lookup(opinion()) is None
    assert state.is_new(opinion(), max_attempts=2)
    assert state.attempts(opinion()) == 0
    assert state.previous_subjects() == frozenset()
    assert state.updated_at is None
    assert not state_path.exists()  # loading never creates the file


def test_load_accepts_a_string_path(state_path):
    state = State.load(str(state_path))
    assert state.path == state_path


@pytest.mark.parametrize("text", ["", "   ", "\n\n", " \t\r\n "])
def test_empty_or_whitespace_file_gives_empty_state(state_path, text):
    write(state_path, text)
    state = State.load(state_path)
    assert state.is_empty
    assert state.digests == []


@pytest.mark.parametrize(
    "text",
    [
        '{"version": 1, "items": {',
        "not json at all",
        '{"version": 1,}',
        "{'version': 1}",
    ],
)
def test_corrupt_json_raises_state_error_and_leaves_the_file_alone(state_path, text):
    write(state_path, text)
    with pytest.raises(StateError) as excinfo:
        State.load(state_path)
    message = str(excinfo.value)
    assert "processed.json" in message
    assert "not valid JSON" in message
    assert "line 1" in message
    assert "never reset automatically" in message
    assert "git checkout" in message  # says how to restore a good copy
    assert "delete it" in message
    assert state_path.read_text(encoding="utf-8") == text  # never silently reset


@pytest.mark.parametrize("version", [2, 0, "1", None, True, 1.5])
def test_other_version_raises_state_error(state_path, version):
    data = {"items": {}, "aliases": {}, "digests": []}
    if version is not None:
        data["version"] = version
    write(state_path, json.dumps(data))
    with pytest.raises(StateError) as excinfo:
        State.load(state_path)
    message = str(excinfo.value)
    assert f"state version {version!r}" in message
    assert f"reads only version {STATE_VERSION}" in message
    assert "never reset automatically" in message


@pytest.mark.parametrize(
    "data, fragment",
    [
        ([], "contains a JSON array"),
        ("text", "contains a JSON string"),
        ({"version": 1, "items": []}, '"items" must be an object'),
        ({"version": 1, "items": {"SC:28355": "summarized"}}, 'record for "SC:28355"'),
        ({"version": 1, "aliases": ["CL:1"]}, '"aliases" must be an object'),
        ({"version": 1, "aliases": {"CL:1": 5}}, '"CL:1" does not'),
        ({"version": 1, "digests": {}}, '"digests" must be an array'),
        ({"version": 1, "digests": ["subject"]}, 'every entry in "digests"'),
    ],
)
def test_malformed_layout_raises_state_error(state_path, data, fragment):
    write(state_path, json.dumps(data))
    with pytest.raises(StateError) as excinfo:
        State.load(state_path)
    assert fragment in str(excinfo.value)


def test_non_utf8_file_raises_state_error(state_path):
    state_path.parent.mkdir(parents=True)
    state_path.write_bytes(b'{"version": 1, "items": {"SC:1": {"case_name": "caf\xe9"}}}')
    with pytest.raises(StateError, match="not UTF-8"):
        State.load(state_path)


def test_unreadable_path_raises_state_error(tmp_path):
    directory = tmp_path / "processed.json"
    directory.mkdir()  # a directory where the file should be
    with pytest.raises(StateError):
        State.load(directory)


def test_byte_order_mark_is_tolerated(state_path):
    state_path.parent.mkdir(parents=True)
    state_path.write_bytes("\ufeff".encode("utf-8") + json.dumps({"version": 1}).encode("utf-8"))
    state = State.load(state_path)
    assert state.is_empty


def test_minimal_file_with_only_a_version_is_empty(state_path):
    write(state_path, '{"version": 1}')
    assert State.load(state_path).is_empty


def test_loads_the_documented_layout(state_path):
    write(
        state_path,
        json.dumps(
            {
                "version": 1,
                "updated_at": "2026-11-18T20:31:02Z",
                "items": {
                    "SC:28355": {
                        "kind": "opinion",
                        "court": "SC",
                        "case_name": "Reid Fleming v. LG Chem Ltd.",
                        "filed_date": "2026-11-12",
                        "status": "summarized",
                        "attempts": 1,
                        "first_seen": "2026-11-18T20:31:02Z",
                        "last_attempt": "2026-11-18T20:31:02Z",
                        "digest_subject": SUBJECT,
                        "email_id": "em-1",
                        "link_url": "https://www.sccourts.org/media/opinions/HTMLFiles/SC/28355.pdf",
                        "source": "courtlistener-api",
                        "error": None,
                    }
                },
                "aliases": {"CL:11060100": "SC:28355"},
                "digests": [
                    {
                        "subject": SUBJECT,
                        "email_id": "em-1",
                        "status": "draft",
                        "created_at": "2026-11-18T20:31:02Z",
                        "items": ["SC:28355"],
                    }
                ],
            }
        ),
    )
    state = State.load(state_path)
    assert not state.is_empty
    assert state.updated_at == "2026-11-18T20:31:02Z"
    assert state.lookup(opinion())["status"] == "summarized"
    # A feed entry whose number could not be read has key CL:11060100: found through "aliases".
    unnumbered = opinion(number=None)
    assert unnumbered.key == "CL:11060100"
    assert state.lookup(unnumbered)["case_name"] == "Reid Fleming v. LG Chem Ltd."
    assert not state.is_new(unnumbered, max_attempts=2)
    assert state.previous_subjects() == frozenset({SUBJECT})


# ---------------------------------------------------------------- saving


def test_round_trip(state_path):
    state = State.load(state_path)
    record_ok(state, opinion())
    record_failed(state, nettles())
    state.record_listed_only(moore("6170"), digest_subject=SUBJECT, email_id="em-1", now=NOW)
    record_ok(state, rule_order())
    state.record_digest(SUBJECT, "em-1", "draft", ["SC:28355", "SC:28354", "COA:6170", "ORDER:2026-11-12-01"], NOW)
    state.save()

    loaded = State.load(state_path)
    assert loaded.to_json() == state.to_json()
    assert state_path.read_text(encoding="utf-8") == state.to_json()
    assert loaded.items == state.items
    assert loaded.aliases == state.aliases == {
        "CL:11060100": "SC:28355",
        "CL:11060101": "SC:28354",
        "CL:11060103": "COA:6170",
    }
    assert loaded.digests == state.digests
    assert loaded.updated_at == utc_timestamp(NOW)
    assert loaded.lookup(nettles())["status"] == STATUS_FAILED
    assert loaded.lookup(moore("6170"))["status"] == STATUS_LISTED_ONLY
    assert loaded.lookup(rule_order())["kind"] == "order"


def test_saved_file_has_the_documented_layout(state_path):
    state = State(state_path)
    record_ok(state, opinion())
    state.record_digest(SUBJECT, "em-1", "draft", ["SC:28355"], NOW)
    state.save()
    data = json.loads(state_path.read_text(encoding="utf-8"))
    assert set(data) == {"version", "updated_at", "items", "aliases", "digests"}
    assert data["version"] == 1
    assert data["updated_at"] == "2026-11-18T20:31:02Z"
    assert data["items"] == {
        "SC:28355": {
            "kind": "opinion",
            "court": "SC",
            "case_name": "Reid Fleming v. LG Chem Ltd.",
            "filed_date": "2026-11-12",
            "status": "summarized",
            "attempts": 1,
            "first_seen": "2026-11-18T20:31:02Z",
            "last_attempt": "2026-11-18T20:31:02Z",
            "digest_subject": SUBJECT,
            "email_id": "em-1",
            "link_url": "https://www.sccourts.org/media/opinions/HTMLFiles/SC/28355.pdf",
            "source": "courtlistener-api",
            "error": None,
        }
    }
    assert data["aliases"] == {"CL:11060100": "SC:28355"}
    assert data["digests"] == [digest_record("2026-11-18T20:31:02Z", ["SC:28355"])]


def test_output_is_deterministic(tmp_path):
    first = State(tmp_path / "a.json")
    second = State(tmp_path / "b.json")
    for item in (opinion(), nettles(), moore("6170")):
        record_ok(first, item)
    for item in (moore("6170"), opinion(), nettles()):  # different insertion order
        record_ok(second, item)
    for state in (first, second):
        state.record_digest(SUBJECT, "em-1", "draft", ["SC:28354", "SC:28355", "COA:6170"], NOW)
    assert first.to_json() == second.to_json()
    assert first.to_json() == first.to_json()

    text = first.to_json()
    assert text.endswith("}\n") and not text.endswith("\n\n")
    data = json.loads(text)
    assert list(data) == sorted(data)
    assert list(data["items"]) == sorted(data["items"])
    assert all(list(record) == sorted(record) for record in data["items"].values())
    assert text.startswith('{\n  "aliases": {')  # 2-space indent, sorted keys


def test_non_ascii_text_is_written_as_is(state_path):
    state = State(state_path)
    record_ok(state, opinion(case_name="In re § 2–3 “Estate” of O’Neal"))
    state.save()
    raw = state_path.read_bytes()
    assert "§ 2–3 “Estate” of O’Neal".encode("utf-8") in raw
    assert "— November 12, 2026 —".encode("utf-8") in raw  # the subject's em dashes
    assert b"\\u" not in raw  # ensure_ascii=False
    assert b"\r\n" not in raw  # newline="\n" even on Windows


def test_save_creates_the_directory_and_leaves_no_temp_files(tmp_path):
    path = tmp_path / "nested" / "data" / "processed.json"
    state = State(path)
    record_ok(state, opinion())
    state.save()
    assert sorted(p.name for p in path.parent.iterdir()) == ["processed.json"]
    # Overwrite the existing file: still no stray temp files.
    record_ok(state, nettles(), now=LATER)
    state.save()
    assert sorted(p.name for p in path.parent.iterdir()) == ["processed.json"]
    assert State.load(path).lookup(nettles()) is not None


def test_save_to_a_bare_filename_in_the_working_directory(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    state = State("processed.json")
    record_ok(state, opinion())
    state.save()
    assert sorted(p.name for p in tmp_path.iterdir()) == ["processed.json"]


@pytest.mark.parametrize("broken", ["replace", "fsync"])
def test_failed_save_keeps_the_old_file_and_leaves_no_temp_files(state_path, monkeypatch, broken):
    state = State(state_path)
    record_ok(state, opinion())
    state.save()
    before = state_path.read_text(encoding="utf-8")

    record_ok(state, nettles(), now=LATER)

    def boom(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(os, broken, boom)
    with pytest.raises(OSError, match="disk full"):
        state.save()
    monkeypatch.undo()
    assert state_path.read_text(encoding="utf-8") == before
    assert sorted(p.name for p in state_path.parent.iterdir()) == ["processed.json"]


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
def test_save_keeps_the_existing_file_permissions(state_path):
    state = State(state_path)
    record_ok(state, opinion())
    state.save()
    os.chmod(state_path, 0o664)
    state.save()
    assert (state_path.stat().st_mode & 0o777) == 0o664


def test_unknown_fields_survive_a_round_trip(state_path):
    write(
        state_path,
        json.dumps(
            {
                "version": 1,
                "note": "kept by hand",
                "items": {
                    "SC:28355": {"status": "summarized", "filed_date": "2026-11-12", "attempts": 1, "memo": "x"},
                },
            }
        ),
    )
    state = State.load(state_path)
    record_ok(state, opinion(), now=LATER)
    state.save()
    data = json.loads(state_path.read_text(encoding="utf-8"))
    assert data["note"] == "kept by hand"
    assert data["items"]["SC:28355"]["memo"] == "x"
    assert data["items"]["SC:28355"]["attempts"] == 2


# ---------------------------------------------------------------- is_new


def test_is_new_without_a_record():
    state = State("unused.json")
    assert state.is_new(opinion(), max_attempts=2)
    assert state.is_new(rule_order(), max_attempts=1)


def test_is_new_after_summarized():
    state = State("unused.json")
    record_ok(state, opinion())
    assert not state.is_new(opinion(), max_attempts=2)
    assert not state.is_new(opinion(), max_attempts=5)
    assert state.lookup(opinion())["status"] == STATUS_SUMMARIZED


def test_is_new_after_listed_only():
    state = State("unused.json")
    state.record_listed_only(opinion(), digest_subject=SUBJECT, email_id="em-1", now=NOW)
    assert not state.is_new(opinion(), max_attempts=2)
    record = state.lookup(opinion())
    assert record["status"] == STATUS_LISTED_ONLY
    assert record["attempts"] == 0
    assert record["last_attempt"] is None
    assert record["error"] is None


@pytest.mark.parametrize(
    "failures, max_attempts, expected",
    [
        (1, 2, True),  # one more try allowed
        (2, 2, False),  # boundary: attempts == max_attempts
        (3, 2, False),
        (1, 1, False),
        (1, 3, True),
        (2, 3, True),
        (3, 3, False),
    ],
)
def test_is_new_after_failures_respects_max_attempts(failures, max_attempts, expected):
    state = State("unused.json")
    for n in range(failures):
        record_failed(state, opinion(), now=NOW + timedelta(days=n))
    assert state.attempts(opinion()) == failures
    assert state.is_new(opinion(), max_attempts=max_attempts) is expected


def test_unknown_status_counts_as_processed(state_path, caplog):
    items = {"SC:28355": {"status": "skipped", "filed_date": "2026-11-12"}}
    write(state_path, json.dumps({"version": 1, "items": items}))
    state = State.load(state_path)
    with caplog.at_level(logging.WARNING, logger="src.state"):
        assert not state.is_new(opinion(), max_attempts=2)
    assert "unrecognized status 'skipped'" in caplog.text


def test_unusable_attempt_count_counts_as_zero(state_path):
    write(state_path, json.dumps({"version": 1, "items": {"SC:28355": {"status": "failed", "attempts": "lots"}}}))
    state = State.load(state_path)
    assert state.attempts(opinion()) == 0
    assert state.is_new(opinion(), max_attempts=2)


@pytest.mark.parametrize("record", [record_ok, record_listed])
def test_refiled_opinion_is_new_again(record):
    state = State("unused.json")
    record(state, opinion(filed=date(2026, 11, 12)))
    assert not state.is_new(opinion(filed=date(2026, 11, 12)), max_attempts=2)
    assert not state.is_new(opinion(filed=date(2026, 11, 5)), max_attempts=2)  # earlier: not a new version
    assert state.is_new(opinion(filed=date(2026, 11, 19)), max_attempts=2)  # withdrawn, substituted, refiled


def test_refiled_opinion_gets_fresh_attempts():
    state = State("unused.json")
    record_failed(state, opinion())
    record_failed(state, opinion(), now=LATER)
    assert not state.is_new(opinion(), max_attempts=2)  # gave up on the original version

    refiled = opinion(filed=date(2026, 11, 19))
    assert state.is_new(refiled, max_attempts=2)
    record_failed(state, refiled, now=LATER + timedelta(days=1))
    record = state.lookup(refiled)
    assert record["attempts"] == 1  # counting restarted for the new version
    assert record["filed_date"] == "2026-11-19"
    assert state.is_new(refiled, max_attempts=2)  # one retry left for the refiled version

    record_ok(state, refiled, now=LATER + timedelta(days=2))
    assert not state.is_new(refiled, max_attempts=2)
    assert state.lookup(refiled)["attempts"] == 2


def test_recorded_filing_date_never_moves_backwards():
    state = State("unused.json")
    record_failed(state, opinion(filed=date(2026, 11, 19)))
    record_failed(state, opinion(filed=date(2026, 11, 12)), now=LATER)  # an older copy of the same opinion
    assert state.lookup(opinion())["filed_date"] == "2026-11-19"
    assert not state.is_new(opinion(filed=date(2026, 11, 19)), max_attempts=2)


def test_later_dated_order_is_not_treated_as_refiled():
    state = State("unused.json")
    record_ok(state, rule_order(filed=date(2026, 11, 12)))
    assert not state.is_new(rule_order(filed=date(2026, 11, 20)), max_attempts=2)


def test_items_the_first_run_left_out_are_never_new_again():
    state = State("unused.json")
    state.record_skipped(opinion(filed=date(2026, 11, 5)), now=NOW)
    record = state.lookup(opinion(filed=date(2026, 11, 5)))
    assert record["status"] == STATUS_SKIPPED_FIRST_RUN == "skipped_first_run"
    assert record["attempts"] == 0 and record["email_id"] is None and record["digest_subject"] is None
    assert not state.is_new(opinion(filed=date(2026, 11, 5)), max_attempts=2)
    assert state.is_new(opinion(filed=date(2026, 11, 19)), max_attempts=2)  # a refiled version is new
    assert not state.is_empty
    assert state.previous_subjects() == frozenset()  # never emailed


def test_is_refiled_and_attempts_of_a_refiled_version():
    state = State("unused.json")
    record_failed(state, opinion())
    assert state.attempts(opinion()) == 1 and not state.is_refiled(opinion())
    refiled = opinion(filed=date(2026, 11, 19))
    assert state.is_refiled(refiled)
    assert state.attempts(refiled) == 0  # the new version has not been tried
    assert not state.is_refiled(opinion("99999"))  # no record at all


def test_is_listed_only_for_the_version_a_digest_listed():
    state = State("unused.json")
    assert not state.is_listed(opinion())
    record_failed(state, opinion())  # listed as failed in digest em-1
    assert state.is_listed(opinion())
    assert not state.is_listed(opinion(filed=date(2026, 11, 19)))  # its refiled version was never listed
    state.record_result(failed(nettles()), digest_subject=None, email_id=None, now=NOW)  # failed, no email
    assert not state.is_listed(nettles())


def test_recording_a_retry_without_a_digest_keeps_the_earlier_listing():
    state = State("unused.json")
    record_failed(state, opinion(), subject=SUBJECT, email_id="em-1")
    state.record_result(failed(opinion()), digest_subject=None, email_id=None, now=LATER)
    record = state.lookup(opinion())
    assert record["attempts"] == 2
    assert (record["digest_subject"], record["email_id"]) == (SUBJECT, "em-1")


def test_a_refiled_version_does_not_inherit_the_old_listing():
    state = State("unused.json")
    record_ok(state, opinion(), subject=SUBJECT, email_id="em-1")
    state.record_result(failed(opinion(filed=date(2026, 11, 19))), digest_subject=None, email_id=None, now=LATER)
    record = state.lookup(opinion(filed=date(2026, 11, 19)))
    assert record["email_id"] is None and record["digest_subject"] is None and record["attempts"] == 1


def test_unlisted_failures_and_listing_them_without_a_new_attempt():
    state = State("unused.json")
    unlisted = moore("6170")
    for when in (NOW, LATER):  # two all-failed runs: no email
        state.record_result(failed(unlisted, "the AI response was not valid"), digest_subject=None, email_id=None,
                            now=when)  # fmt: skip
    record_failed(state, opinion())  # listed in a digest: not owed
    state.record_result(failed(nettles()), digest_subject=None, email_id=None, now=NOW)  # attempts left: not owed
    owed = state.unlisted_failures(max_attempts=2)
    assert [key for key, _ in owed] == ["COA:6170"]
    assert owed[0][1]["error"] == "the AI response was not valid"
    assert state.unlisted_failures(max_attempts=3) == []

    state.record_listed_failure(unlisted, error="the AI response was not valid", digest_subject=SUBJECT_2,
                                email_id="em-2", now=LATER)  # fmt: skip
    record = state.lookup(unlisted)
    assert record["attempts"] == 2  # no new attempt was made
    assert (record["status"], record["email_id"]) == (STATUS_FAILED, "em-2")
    assert state.unlisted_failures(max_attempts=2) == []
    assert not state.is_new(unlisted, max_attempts=2)


def test_earlier_issue_finds_the_digest_for_the_same_week():
    state = State("unused.json")
    record_ok(state, opinion(filed=date(2026, 11, 5)), subject="Digest Nov 5", email_id="em-0")
    state.record_digest("Digest Nov 5", "em-0", "draft", ["SC:28355"], NOW)
    assert state.earlier_issue(date(2026, 11, 12), 5) is None  # last week's digest is not this week's
    record_ok(state, nettles(), subject=SUBJECT, email_id="em-1")
    state.record_digest(SUBJECT, "em-1", "draft", ["SC:28354"], NOW)
    assert state.earlier_issue(date(2026, 11, 12), 5) == SUBJECT
    assert state.earlier_issue(date(2026, 11, 13), 5) == SUBJECT  # Thursday: still the same week
    assert state.earlier_issue(date(2026, 11, 19), 5) is None  # next week
    assert state.earlier_issue(date(2026, 11, 11), 5) is None  # an earlier week than the digest's items


def test_earlier_issue_follows_aliases_and_ignores_records_without_a_date(state_path):
    write(
        state_path,
        json.dumps(
            {
                "version": 1,
                "items": {"SC:28355": {"status": "summarized", "filed_date": "2026-11-12"},
                          "SC:1": {"status": "summarized"}},
                "aliases": {"CL:11060100": "SC:28355"},
                "digests": [digest_record("2026-11-18T20:31:02Z", ["CL:11060100"]),
                            digest_record("2026-11-19T13:05:00Z", ["SC:1", "SC:404"], subject="Other")],
            }
        ),
    )  # fmt: skip
    assert State.load(state_path).earlier_issue(date(2026, 11, 12), 5) == SUBJECT


# ---------------------------------------------------------------- recording


def test_failure_then_success_updates_the_record():
    state = State("unused.json")
    record_failed(state, opinion(), now=NOW, subject=SUBJECT, email_id="em-1")
    record = state.lookup(opinion())
    assert record["status"] == STATUS_FAILED
    assert record["error"] == "the AI service was unavailable"
    assert record["attempts"] == 1

    record_ok(state, opinion(), now=LATER, subject=SUBJECT_2, email_id="em-2")
    record = state.lookup(opinion())
    assert record["status"] == STATUS_SUMMARIZED
    assert record["error"] is None
    assert record["attempts"] == 2
    assert record["first_seen"] == "2026-11-18T20:31:02Z"
    assert record["last_attempt"] == "2026-11-19T13:05:00Z"
    assert record["digest_subject"] == SUBJECT_2
    assert record["email_id"] == "em-2"
    assert state.updated_at == "2026-11-19T13:05:00Z"


def test_summarized_status_without_summary_is_recorded_as_failed():
    state = State("unused.json")
    result = SummaryResult(item=opinion(), status=ResultStatus.SUMMARIZED, summary=None)
    state.record_result(result, digest_subject=SUBJECT, email_id="em-1", now=NOW)
    assert state.lookup(opinion())["status"] == STATUS_FAILED


def test_listed_only_keeps_an_earlier_attempt_count():
    state = State("unused.json")
    record_failed(state, opinion())
    state.record_listed_only(opinion(), digest_subject=SUBJECT_2, email_id="em-2", now=LATER)
    record = state.lookup(opinion())
    assert record["status"] == STATUS_LISTED_ONLY
    assert record["attempts"] == 1
    assert record["last_attempt"] == "2026-11-18T20:31:02Z"
    assert record["error"] is None


def test_lookup_returns_a_copy():
    state = State("unused.json")
    record_ok(state, opinion())
    state.lookup(opinion())["status"] = "failed"
    state.items["SC:28355"]["status"] = "failed"
    assert state.lookup(opinion())["status"] == STATUS_SUMMARIZED


def test_timestamps_are_utc_with_z():
    eastern = timezone(timedelta(hours=-5))
    assert utc_timestamp(datetime(2026, 11, 18, 15, 31, 2, 123456, tzinfo=eastern)) == "2026-11-18T20:31:02Z"
    assert utc_timestamp(datetime(2026, 11, 18, 20, 31, 2)) == "2026-11-18T20:31:02Z"  # naive = UTC
    state = State("unused.json")
    record_ok(state, opinion(), now=datetime(2026, 11, 18, 15, 31, 2, tzinfo=eastern))
    record = state.lookup(opinion())
    assert record["first_seen"] == record["last_attempt"] == state.updated_at == "2026-11-18T20:31:02Z"


# ---------------------------------------------------------------- aliases


def test_record_adds_the_courtlistener_alias():
    state = State("unused.json")
    record_ok(state, opinion())
    assert state.aliases == {"CL:11060100": "SC:28355"}
    assert set(state.items) == {"SC:28355"}


def test_sccourts_items_have_no_alias():
    state = State("unused.json")
    record_ok(state, rule_order())
    assert state.aliases == {}
    assert set(state.items) == {"ORDER:2026-11-12-01"}


def test_number_learned_from_the_pdf_resolves_later_unnumbered_entries():
    # API mode: Moore's docket is 2024-000459, so the scraper has no number (key CL:11060103).
    scraped = moore(number=None)
    assert scraped.key == "CL:11060103"
    state = State("unused.json")
    assert state.is_new(scraped, max_attempts=2)
    # The integrator reads "Opinion No. 6170" from the PDF header before recording.
    scraped.number = "6170"
    assert scraped.key == "COA:6170" and scraped.alias_keys == ["CL:11060103"]
    record_ok(state, scraped)
    assert set(state.items) == {"COA:6170"}
    assert state.aliases == {"CL:11060103": "COA:6170"}
    # Next run the scraper again has no number: found through the aliases map.
    again = moore(number=None)
    assert state.lookup(again)["case_name"] == "Gary Moore v. State"
    assert not state.is_new(again, max_attempts=2)
    assert state.attempts(again) == 1


def test_record_under_courtlistener_id_migrates_to_the_opinion_number():
    state = State("unused.json")
    # Run 1: the PDF could not be read, so the opinion was recorded as failed under its cluster id.
    unnumbered = opinion(number=None)
    assert unnumbered.key == "CL:11060100"
    record_failed(state, unnumbered, now=NOW)
    assert set(state.items) == {"CL:11060100"}
    assert state.aliases == {}

    # Run 2: the number is known (key SC:28355, alias CL:11060100): the old record is found...
    numbered = opinion("28355")
    assert numbered.alias_keys == ["CL:11060100"]
    assert state.lookup(numbered)["status"] == STATUS_FAILED
    assert state.attempts(numbered) == 1
    assert state.is_new(numbered, max_attempts=2)

    # ...and moved to the canonical key when the new result is recorded.
    record_ok(state, numbered, now=LATER)
    assert set(state.items) == {"SC:28355"}
    assert state.aliases == {"CL:11060100": "SC:28355"}
    record = state.lookup(numbered)
    assert record["status"] == STATUS_SUMMARIZED
    assert record["attempts"] == 2
    assert record["first_seen"] == "2026-11-18T20:31:02Z"
    assert record["last_attempt"] == "2026-11-19T13:05:00Z"
    # An entry that again lacks the number still resolves to the migrated record.
    assert state.lookup(unnumbered)["status"] == STATUS_SUMMARIZED
    assert not state.is_new(unnumbered, max_attempts=2)


def test_migration_survives_save_and_load(state_path):
    state = State(state_path)
    record_failed(state, opinion(number=None))
    state.save()
    state = State.load(state_path)
    record_ok(state, opinion("28355"), now=LATER)
    state.save()
    data = json.loads(state_path.read_text(encoding="utf-8"))
    assert list(data["items"]) == ["SC:28355"]
    assert data["aliases"] == {"CL:11060100": "SC:28355"}


def test_listed_only_also_migrates_an_alias_record():
    state = State("unused.json")
    record_failed(state, opinion(number=None))
    state.record_listed_only(opinion("28355"), digest_subject=SUBJECT, email_id="em-1", now=LATER)
    assert set(state.items) == {"SC:28355"}
    assert state.aliases == {"CL:11060100": "SC:28355"}
    assert state.lookup(opinion("28355"))["attempts"] == 1


def test_stale_alias_record_is_merged_into_the_canonical_one(state_path):
    write(
        state_path,
        json.dumps(
            {
                "version": 1,
                "items": {
                    "SC:28355": {
                        "status": "failed",
                        "attempts": 1,
                        "filed_date": "2026-11-12",
                        "first_seen": "2026-11-18T00:00:00Z",
                    },
                    "CL:11060100": {
                        "status": "failed",
                        "attempts": 1,
                        "filed_date": "2026-11-12",
                        "first_seen": "2026-11-11T00:00:00Z",
                    },
                },
            }
        ),
    )
    state = State.load(state_path)
    record_ok(state, opinion("28355"), now=LATER)
    assert set(state.items) == {"SC:28355"}
    assert state.aliases == {"CL:11060100": "SC:28355"}
    record = state.lookup(opinion("28355"))
    assert record["first_seen"] == "2026-11-11T00:00:00Z"
    assert record["attempts"] == 2


def test_dangling_alias_is_ignored_and_replaced(state_path):
    aliases = {"CL:11060100": "SC:99999", "SC:28355": "SC:99999"}
    write(state_path, json.dumps({"version": 1, "items": {}, "aliases": aliases}))
    state = State.load(state_path)
    assert state.lookup(opinion()) is None
    record_ok(state, opinion())
    assert state.aliases == {"CL:11060100": "SC:28355"}  # a record key is never also an alias


# ---------------------------------------------------------------- digests


def test_record_digest_and_update_status():
    state = State("unused.json")
    state.record_digest(SUBJECT, "em-1", "draft", ["SC:28355", "SC:28354", "SC:28355"], NOW)
    assert state.digests == [digest_record("2026-11-18T20:31:02Z", ["SC:28355", "SC:28354"])]  # deduplicated
    assert state.updated_at == "2026-11-18T20:31:02Z"
    assert state.update_digest_status("em-1", "about_to_send", now=LATER) is True
    assert state.digests[0]["status"] == "about_to_send"
    assert state.updated_at == "2026-11-19T13:05:00Z"


def test_update_status_of_unknown_digest(caplog):
    state = State("unused.json")
    state.record_digest(SUBJECT, "em-1", "draft", [], NOW)
    with caplog.at_level(logging.WARNING, logger="src.state"):
        assert state.update_digest_status("em-404", "about_to_send") is False
    assert "em-404" in caplog.text
    assert state.digests[0]["status"] == "draft"


def test_recording_a_digest_does_not_make_the_state_non_empty():
    state = State("unused.json")
    state.record_digest(SUBJECT, "em-1", "draft", [], NOW)
    assert state.is_empty


def test_previous_subjects():
    state = State("unused.json")
    assert state.previous_subjects() == frozenset()
    state.record_digest(SUBJECT, "em-1", "draft", ["SC:28355"], NOW)
    state.record_digest(SUBJECT_2, "em-2", "about_to_send", ["SC:28356"], LATER)
    subjects = state.previous_subjects()
    assert isinstance(subjects, frozenset)
    assert subjects == {SUBJECT, SUBJECT_2}


def test_previous_subjects_include_item_records(state_path):
    # Digest records are capped, so subjects recorded on items count too.
    items = {"SC:1": {"status": "summarized", "digest_subject": "Old digest"}, "SC:2": {"digest_subject": None}}
    write(state_path, json.dumps({"version": 1, "items": items}))
    assert State.load(state_path).previous_subjects() == frozenset({"Old digest"})


def test_digest_records_are_capped(state_path):
    state = State(state_path)
    total = MAX_DIGESTS + 5
    for n in range(total):
        state.record_digest(f"Digest {n}", f"em-{n}", "draft", [f"SC:{n}"], NOW + timedelta(hours=n))
    digests = state.digests
    assert MAX_DIGESTS == 500
    assert len(digests) == MAX_DIGESTS
    assert digests[0]["subject"] == "Digest 5"  # the five oldest were dropped
    assert digests[-1]["subject"] == f"Digest {total - 1}"
    state.save()
    assert len(State.load(state_path).digests) == MAX_DIGESTS


def test_repr_mentions_path_and_counts():
    state = State("data/processed.json")
    record_ok(state, opinion())
    assert "processed.json" in repr(state) and "1 items" in repr(state)
