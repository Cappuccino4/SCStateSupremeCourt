"""Tests for src/extract.py: PDF text, cleaning, quality, header parsing, and sizing for Claude."""

from __future__ import annotations

import functools
import io
import logging
import random
import re
from datetime import date

import pypdf
import pytest
from pdfplumber.page import Page

from src import extract
from src.extract import (
    CHARS_PER_TOKEN,
    MIN_USEFUL_CHARS,
    SEPARATE_OPINION_MARKER,
    ExtractedText,
    ExtractionError,
    clean_text,
    estimate_tokens,
    extract_text,
    parse_opinion_header,
    pdf_to_pages,
    prepare_for_model,
    slice_pages,
    text_quality,
)
from src.models import OpinionHeader, PreparedText
from tests.conftest import fixture_bytes, fixture_json, fixture_text
from tests.pdfgen import make_pdf, make_pdf_from_text

NOTE_RE = re.compile(r"^Long opinion: this summary is based on about \d{1,2}% of its text\.$")
MIDDLE_RE = re.compile(
    r"\n\n\[\.\.\. about ([\d,]+) characters omitted from the middle of this long opinion"
    r" to fit the input limit \.\.\.\]\n\n"
)
SEPARATE_SUFFIX = "\n\n" + SEPARATE_OPINION_MARKER
LIBRARY_LOGGERS = ("pdfminer", "pdfplumber", "pypdf")

BODY_LINES = [
    "The circuit court granted summary judgment, and the court of appeals affirmed.",
    "We granted certiorari to decide whether S.C. Code Ann. § 15-78-60 applies here.",
    "The “Tort Claims Act” waives immunity only as the statute provides – no more.",
    "Because the record shows no genuine issue of material fact, we affirm the result.",
]


# --------------------------------------------------------------------------- helpers


@functools.lru_cache(maxsize=None)
def opinion(name: str) -> str:
    return fixture_text(f"opinion_texts/{name}.txt")


def synthetic_dissent(heading: str = "THOMAS, J., dissenting:", lines: int = 300) -> str:
    body = [f"{heading} I respectfully dissent because the record supports the PCR court."]
    body += [f"Dissent line {i:03d} explains the dissenting view of the record and the law at length." for i in range(lines)]
    body.append("For these reasons, I would affirm.")
    return "\n".join(body)


def with_dissent(majority: str, heading: str = "THOMAS, J., dissenting:", lines: int = 300) -> str:
    return majority + "\n" + synthetic_dissent(heading, lines)


def header_text(
    author_line: str = "JUSTICE HILL:",
    court: str | None = "Supreme Court",
    disposition_lines: tuple[str, ...] | list[str] = ("AFFIRMED",),
) -> str:
    """A synthetic opinion first page laid out like the real ones."""
    lines = ["THE STATE OF SOUTH CAROLINA"]
    if court:
        lines.append(f"In The {court}")
    lines += [
        "Jane Roe, Appellant,",
        "v.",
        "John Doe, Respondent.",
        "Appellate Case No. 2025-001234",
        "Opinion No. 28400",
        "Heard May 5, 2026 – Filed June 10, 2026",
        *disposition_lines,
        "Jane Lawyer, of Columbia, for Appellant.",
        "John Lawyer, of Greenville, for Respondent.",
        f"{author_line} We consider whether the trial court erred.",
        "BACKGROUND",
        "The facts are these.",
    ]
    return "\n".join(lines)


def page_lines(repeat: int, number: int) -> list[str]:
    """One generated page: body lines followed by a printed page number."""
    return BODY_LINES * repeat + [str(number)]


def empty_pdf_without_pages() -> bytes:
    buffer = io.BytesIO()
    pypdf.PdfWriter().write(buffer)
    return buffer.getvalue()


def raiser(exc: Exception):
    def _raise(*args, **kwargs):
        raise exc

    return _raise


# --------------------------------------------------------------------------- constants & dataclass


def test_constants():
    assert MIN_USEFUL_CHARS == 500
    assert CHARS_PER_TOKEN == 3.0


def test_extracted_text_ok_needs_min_useful_chars():
    assert ExtractedText("x" * 500, ["x" * 500], "pdfplumber", 1.0, []).ok
    assert not ExtractedText("x" * 499, ["x" * 499], "pdfplumber", 1.0, []).ok
    assert not ExtractedText("\n  " + "x" * 499 + "  \n\n", [], "pypdf", 1.0, []).ok
    assert ExtractedText("x" * 600, [], "pdfplumber", 1.0).warnings == []  # warnings default to []


# --------------------------------------------------------------------------- clean_text


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Sec(cid:3)tion 1(cid:12)", "Section 1"),
        ("(cid:72)(cid:101)", ""),
        ("a\x00b\x07c\x1bd\x7fe\x9ff", "abcdef"),
        ("col1\tcol2", "col1 col2"),
        ("one\r\ntwo\rthree\ffour\vfive", "one\ntwo\nthree\nfour\nfive"),
        ("trailing   \nspaces \t\nkept", "trailing\nspaces\nkept"),
        ("no\u00a0break space", "no break space"),
        ("body\n12\nmore\n  7  \n123\n1234\n12 Angry Men\n§ 12\n0", "body\nmore\n1234\n12 Angry Men\n§ 12"),
        ("a\n\n\n\n\n\nb", "a\n\n\nb"),
        ("a\n\n\nb", "a\n\n\nb"),
        ("a\n\nb", "a\n\nb"),
        ("a\n \n\t\n  \n \nb", "a\n\n\nb"),
        ("text\n\n5\n\n\nmore", "text\n\n\nmore"),
        ("\n\n  \nstart\nend\n\n", "start\nend"),
        ("  indented first line", "  indented first line"),
        ("§ 16-3-29 – “quoted” ‘single’ — dash … \ufffd", "§ 16-3-29 – “quoted” ‘single’ — dash … \ufffd"),
        ("", ""),
    ],
)
def test_clean_text(raw, expected):
    assert clean_text(raw) == expected


@pytest.mark.parametrize("name", ["sc_28354", "sc_28355", "coa_6170"])
def test_clean_text_leaves_clean_opinion_text_alone(name):
    text = opinion(name)
    assert clean_text(text) == text


def test_clean_text_is_idempotent():
    messy = "A(cid:1)\x00B \t\n\n\n\n\n7\nC\r\nD   \n\f12\n\n\n\n\nE\u00a0F"
    once = clean_text(messy)
    assert once == "AB\n\n\nC\nD\n\n\nE F"
    assert clean_text(once) == once


# --------------------------------------------------------------------------- text_quality


def test_text_quality_of_real_opinion_text_is_high():
    assert text_quality(opinion("sc_28355"), 11) >= 0.9
    assert text_quality(opinion("coa_6170"), 16) >= 0.9


@pytest.mark.parametrize("text", ["", " \n\t ", "\n\n\n"])
def test_text_quality_is_zero_without_text(text):
    assert text_quality(text, 3) == 0.0


def test_text_quality_penalizes_unmapped_glyphs():
    words = "The court reverses the order of the circuit court. " * 40
    clean = text_quality(words, 1)
    a_few_cids = text_quality(words.replace("e", "(cid:72)", 10), 1)
    many_cids = text_quality(words.replace("e", "(cid:72)"), 1)
    replacement_chars = text_quality(words.replace("o", "\ufffd"), 1)
    assert clean == 1.0
    assert clean > a_few_cids > many_cids
    assert a_few_cids >= 0.5
    assert many_cids < 0.5
    assert replacement_chars < 0.5


def test_text_quality_penalizes_symbol_garbage():
    assert text_quality("!#$%&*+<=>?@^~|" * 200, 1) < 0.2
    assert text_quality("0123456789 " * 300, 1) < 0.2


def test_text_quality_penalizes_sparse_pages():
    sentence = "The court reverses the judgment below. "
    assert text_quality(sentence, 1) < 0.5
    assert text_quality(sentence * 30, 1) > text_quality(sentence * 30, 10)


def test_text_quality_stays_between_zero_and_one():
    rng = random.Random(7)
    alphabet = "abcXYZ 019.,;§–\n\ufffd(cid:5)"
    for _ in range(200):
        text = "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 3000)))
        score = text_quality(text, rng.randint(0, 20))
        assert 0.0 <= score <= 1.0


# --------------------------------------------------------------------------- extract_text: real and generated PDFs


def test_extract_real_pdf_with_pdfplumber():
    result = extract_text(fixture_bytes("pdfs/sc_28354.pdf"))
    assert result.method == "pdfplumber"
    assert result.ok
    assert "PER CURIAM" in result.text
    assert len(result.pages) == 4
    assert result.text == "\n".join(result.pages)
    assert result.text == opinion("sc_28354")  # the fixture is pdfplumber's text of this PDF
    assert "Submitted October 16, 2026 – Filed November 12, 2026" in result.text
    assert result.quality >= 0.9
    assert result.warnings == []


def test_real_pdf_header_end_to_end():
    header = parse_opinion_header(extract_text(fixture_bytes("pdfs/sc_28354.pdf")).text)
    assert header == OpinionHeader(
        opinion_number="28354",
        filed_date=date(2026, 11, 12),
        heard_or_submitted="Submitted October 16, 2026",
        disposition="PUBLIC REPRIMAND",
        author="Per Curiam",
        appellate_case_no="2026-000394",
    )


def test_extract_generated_pdf_keeps_special_characters_and_drops_page_numbers():
    result = extract_text(make_pdf([page_lines(2, 1), page_lines(2, 2), page_lines(2, 3)]))
    assert result.method == "pdfplumber"
    assert result.pages == ["\n".join(BODY_LINES * 2)] * 3
    assert result.text == "\n".join(result.pages)
    assert "§ 15-78-60" in result.text and "– no more" in result.text and "“Tort Claims Act”" in result.text
    assert result.ok
    assert result.warnings == []


def test_extract_generated_long_pdf_and_parse_its_header():
    text = header_text("JUSTICE HILL:", disposition_lines=["REVERSED"]) + "\n" + "\n".join(BODY_LINES * 40)
    result = extract_text(make_pdf_from_text(text))
    assert result.method == "pdfplumber"
    assert len(result.pages) == 4  # 175 lines at 48 lines per page
    assert result.ok
    assert parse_opinion_header(result.text) == OpinionHeader(
        "28400", date(2026, 6, 10), "Heard May 5, 2026", "REVERSED", "Justice Hill", "2025-001234"
    )


def test_pdf_without_text_returns_an_empty_result_instead_of_raising():
    result = extract_text(make_pdf([[""], [""]]))  # what a scanned PDF looks like: pages but no text layer
    assert result.text == ""
    assert result.pages == ["", ""]
    assert result.quality == 0.0
    assert not result.ok
    assert any("no extractable text" in note for note in result.warnings)


def test_pdf_to_pages_returns_cleaned_page_texts():
    pages = fixture_json("sccourts/advance_sheet_40_pages.json")
    result = pdf_to_pages(make_pdf([page.splitlines() for page in pages]))
    assert result == [clean_text(page) for page in pages]
    assert len(result) == 7
    assert "ADVANCE SHEET NO. 40" in "\n".join(result)
    for number, page in enumerate(result, start=1):
        assert page.splitlines()[-1] != str(number)  # printed page numbers are dropped


def test_pdf_to_pages_raises_on_unreadable_bytes():
    with pytest.raises(ExtractionError):
        pdf_to_pages(b"not a pdf")


# --------------------------------------------------------------------------- extract_text: failures and fallbacks


@pytest.mark.parametrize(
    "data",
    [
        b"",
        bytearray(),
        b"this is not a pdf at all " * 20,
        b"%PDF-1.4\n" + b"\x00\x01garbage" * 50,
        b"<html><body>404 Not Found</body></html>",
    ],
    ids=["empty", "empty-bytearray", "text", "pdf-header-then-garbage", "html"],
)
def test_unreadable_bytes_raise_extraction_error(data):
    with pytest.raises(ExtractionError):
        extract_text(data)


def test_truncated_pdf_raises_extraction_error():
    pdf = make_pdf([page_lines(2, 1)])
    with pytest.raises(ExtractionError):
        extract_text(pdf[: len(pdf) // 2])


def test_pdf_with_no_pages_raises_extraction_error():
    with pytest.raises(ExtractionError, match="no pages"):
        extract_text(empty_pdf_without_pages())


def test_extraction_error_names_both_libraries_and_spots_non_pdf_data():
    with pytest.raises(ExtractionError) as info:
        extract_text(b"<html><body>404 Not Found</body></html>")
    message = str(info.value)
    assert "pdfplumber" in message and "pypdf" in message
    assert "does not look like a PDF" in message


def test_pypdf_fallback_when_pdfplumber_fails(monkeypatch, caplog):
    monkeypatch.setattr(extract.pdfplumber, "open", raiser(RuntimeError("simulated pdfplumber crash")))
    caplog.set_level(logging.INFO, logger="src.extract")
    result = extract_text(fixture_bytes("pdfs/sc_28354.pdf"))
    assert result.method == "pypdf"
    assert result.ok
    assert "PER CURIAM" in result.text
    assert len(result.pages) == 4
    assert result.warnings[0] == (
        "Used pypdf because pdfplumber could not read the PDF (RuntimeError: simulated pdfplumber crash)."
    )
    assert all(line == line.rstrip() for line in result.text.splitlines())  # pypdf's trailing spaces cleaned
    assert "trying pypdf" in caplog.text
    assert "with pypdf" in caplog.text
    # The header parser copes with pypdf's layout (blank lines between header lines).
    header = parse_opinion_header(result.text)
    assert (header.opinion_number, header.disposition, header.author) == ("28354", "PUBLIC REPRIMAND", "Per Curiam")
    assert header.heard_or_submitted == "Submitted October 16, 2026"


def test_pypdf_used_when_pdfplumber_text_is_garbled(monkeypatch):
    garbled = " ".join(["(cid:41)(cid:42)(cid:43)"] * 400)
    monkeypatch.setattr(extract, "_pdfplumber_pages", lambda data: ([garbled] * 4, []))
    result = extract_text(fixture_bytes("pdfs/sc_28354.pdf"))
    assert result.method == "pypdf"
    assert result.quality >= 0.9
    assert result.warnings == ["Used pypdf because pdfplumber's text quality was low (0.00)."]


def test_pdfplumber_text_kept_when_pypdf_is_not_better(monkeypatch):
    monkeypatch.setattr(extract, "_pdfplumber_pages", lambda data: (["Short page."], []))
    monkeypatch.setattr(extract.pypdf, "PdfReader", raiser(ValueError("simulated pypdf failure")))
    result = extract_text(fixture_bytes("pdfs/sc_28354.pdf"))
    assert result.method == "pdfplumber"
    assert result.text == "Short page."
    assert result.warnings[0] == (
        "Kept pdfplumber's text; pypdf could not read the PDF (ValueError: simulated pypdf failure)."
    )
    assert any("looks poor" in note for note in result.warnings)


def test_low_quality_tie_keeps_pdfplumber(monkeypatch):
    monkeypatch.setattr(extract, "_pdfplumber_pages", lambda data: (["Same text."], []))
    monkeypatch.setattr(extract, "_pypdf_pages", lambda data: (["Same text."], []))
    result = extract_text(b"%PDF-1.4 stand-in bytes")
    assert result.method == "pdfplumber"
    assert result.warnings[0].startswith("Kept pdfplumber's text; pypdf's was no better")


def test_both_extractors_failing_raises(monkeypatch):
    monkeypatch.setattr(extract.pdfplumber, "open", raiser(RuntimeError("plumber down")))
    monkeypatch.setattr(extract.pypdf, "PdfReader", raiser(ValueError("pypdf down")))
    with pytest.raises(ExtractionError) as info:
        extract_text(fixture_bytes("pdfs/sc_28354.pdf"))
    message = str(info.value)
    assert "RuntimeError: plumber down" in message
    assert "ValueError: pypdf down" in message
    assert "does not look like a PDF" not in message  # the bytes really are a PDF


def test_one_unreadable_page_does_not_lose_the_others(monkeypatch):
    original = Page.extract_text

    def flaky(self, **kwargs):
        if self.page_number == 2:
            raise ValueError("damaged content stream")
        return original(self, **kwargs)

    monkeypatch.setattr(Page, "extract_text", flaky)
    result = extract_text(make_pdf([page_lines(3, 1), page_lines(3, 2), page_lines(3, 3)]))
    assert result.method == "pdfplumber"  # two good pages are still good enough...
    # ...and the page pdfplumber could not read is filled from pypdf, so no text is silently lost.
    assert result.pages == ["\n".join(BODY_LINES * 3)] * 3
    assert result.missing_pages == []
    assert result.text.count(BODY_LINES[0]) == 9
    assert result.warnings == ["pdfplumber could not read page(s) 2; filled page(s) 2 from pypdf."]


def test_a_page_neither_library_can_read_is_reported_as_missing(monkeypatch):
    original_plumber = Page.extract_text

    def flaky(self, **kwargs):
        if self.page_number == 2:
            raise ValueError("damaged content stream")
        return original_plumber(self, **kwargs)

    original_pypdf = extract._pypdf_pages

    def pypdf_fails_page_2(data):
        pages, failures = original_pypdf(data)
        pages[1] = ""
        return pages, [*failures, (2, "ValueError: damaged too")]

    monkeypatch.setattr(Page, "extract_text", flaky)
    monkeypatch.setattr(extract, "_pypdf_pages", pypdf_fails_page_2)
    result = extract_text(make_pdf([page_lines(3, 1), page_lines(3, 2), page_lines(3, 3)]))
    assert result.method == "pdfplumber"
    assert result.missing_pages == [2]
    assert result.pages[1] == ""
    assert result.warnings == ["pdfplumber could not read page(s) 2; their text is missing."]


def test_pdf_to_layout_pages_keeps_number_only_lines():
    pages = [["CONTENTS", "28400 – The State v. Someone Whose Name", "12", "Other text"], ["Body text", "2"]]
    pdf = make_pdf(pages)
    layout = extract.pdf_to_layout_pages(pdf)
    assert layout[0].splitlines() == pages[0]  # the wrapped page number "12" survives...
    assert layout[1].splitlines() == pages[1]  # ...and so does the printed page number
    assert "12" not in pdf_to_pages(pdf)[0].splitlines()  # the plain reader still drops both
    assert clean_text("a\n12\nb", drop_page_numbers=False) == "a\n12\nb"
    assert clean_text("a\n12\nb") == "a\nb"


def test_all_pages_failing_counts_as_a_pdfplumber_failure(monkeypatch):
    monkeypatch.setattr(Page, "extract_text", raiser(ValueError("bad page")))
    result = extract_text(make_pdf([page_lines(3, 1), page_lines(3, 2)]))
    assert result.method == "pypdf"
    assert "no page could be read (ValueError: bad page)" in result.warnings[0]
    assert result.pages == ["\n".join(BODY_LINES * 3)] * 2


def test_pdf_library_warnings_are_hidden_and_logger_levels_restored(caplog):
    caplog.set_level(logging.INFO)
    before = {name: logging.getLogger(name).level for name in LIBRARY_LOGGERS}
    with pytest.raises(ExtractionError):
        extract_text(b"this is not a pdf at all " * 20)
    noisy = [r for r in caplog.records if r.name.split(".")[0] in LIBRARY_LOGGERS and r.levelno >= logging.WARNING]
    assert noisy == []
    assert {name: logging.getLogger(name).level for name in LIBRARY_LOGGERS} == before


def test_pdf_library_warnings_pass_through_when_debugging(caplog):
    caplog.set_level(logging.DEBUG)
    with pytest.raises(ExtractionError):
        extract_text(b"this is not a pdf at all " * 20)
    assert any(r.name.startswith("pypdf") and r.levelno == logging.WARNING for r in caplog.records)


def test_extraction_logs_one_concise_line_on_success(caplog):
    caplog.set_level(logging.INFO, logger="src.extract")
    extract_text(fixture_bytes("pdfs/sc_28354.pdf"))
    own = [r for r in caplog.records if r.name == "src.extract"]
    assert len(own) == 1
    assert own[0].levelno == logging.INFO
    assert own[0].getMessage() == "Extracted 4 page(s) with pdfplumber: 7,917 characters, quality 1.00."


def test_garbled_but_long_text_is_logged_as_a_warning(monkeypatch, caplog):
    garbled = "!#$%&*+<=>?@^~| " * 100  # long enough to be "ok", but symbols, not words
    monkeypatch.setattr(extract, "_pdfplumber_pages", lambda data: ([garbled], []))
    monkeypatch.setattr(extract, "_pypdf_pages", lambda data: ([garbled], []))
    caplog.set_level(logging.INFO, logger="src.extract")
    result = extract_text(b"%PDF-1.4 stand-in bytes")
    assert result.ok and result.quality < 0.5
    assert [r.levelno for r in caplog.records if r.name == "src.extract"].count(logging.WARNING) == 1


# --------------------------------------------------------------------------- parse_opinion_header

EXPECTED_HEADERS = {
    "sc_28354": OpinionHeader(
        "28354", date(2026, 11, 12), "Submitted October 16, 2026", "PUBLIC REPRIMAND", "Per Curiam", "2026-000394"
    ),
    "sc_28355": OpinionHeader(
        "28355", date(2026, 11, 12), "Heard September 16, 2026", "REVERSED", "Justice Hill", "2025-002086"
    ),
    "coa_6170": OpinionHeader(
        "6170", date(2026, 11, 12), "Heard October 7, 2026", "REVERSED", "Judge Konduros", "2024-000459"
    ),
}


@pytest.mark.parametrize("name", sorted(EXPECTED_HEADERS))
def test_parse_opinion_header_real_texts(name):
    assert parse_opinion_header(opinion(name)) == EXPECTED_HEADERS[name]


@pytest.mark.parametrize(
    ("court", "line", "expected"),
    [
        ("Supreme Court", "JUSTICE HILL:", "Justice Hill"),
        ("Supreme Court", "CHIEF JUSTICE KITTREDGE:", "Chief Justice Kittredge"),
        ("Supreme Court", "ACTING JUSTICE JOHN DOE:", "Acting Justice John Doe"),
        ("Supreme Court", "ACTING CHIEF JUSTICE FEW:", "Acting Chief Justice Few"),
        ("Supreme Court", "ACTING JUSTICE G. THOMAS COOPER, JR.:", "Acting Justice G. Thomas Cooper, Jr."),
        ("Supreme Court", "ACTING JUSTICE JAMES E. MOORE, III:", "Acting Justice James E. Moore, III"),
        ("Supreme Court", "PER CURIAM:", "Per Curiam"),
        ("Supreme Court", "Per Curiam:", "Per Curiam"),
        ("Supreme Court", "JUSTICE MCDONALD:", "Justice McDonald"),
        ("Supreme Court", "JUSTICE O'NEAL:", "Justice O'Neal"),
        ("Supreme Court", "JUSTICE O’NEAL:", "Justice O’Neal"),
        ("Supreme Court", "JUSTICE SMITH-JONES:", "Justice Smith-Jones"),
        ("Court of Appeals", "KONDUROS, J.:", "Judge Konduros"),
        ("Court of Appeals", "WILLIAMS, C.J.:", "Chief Judge Williams"),
        ("Court of Appeals", "HEWITT, A.J.:", "Acting Judge Hewitt"),
        ("Court of Appeals", "MCDONALD, J.:", "Judge McDonald"),
        ("Court of Appeals", "O'NEAL, J.:", "Judge O'Neal"),
        ("Court of Appeals", "PER CURIAM:", "Per Curiam"),
        (None, "KONDUROS, J.:", "Judge Konduros"),  # court unknown: rank style means Court of Appeals
        ("Supreme Court", "HILL, J.:", "Justice Hill"),  # rank style inside a Supreme Court opinion
        ("Supreme Court", "KITTREDGE, C.J.:", "Chief Justice Kittredge"),
    ],
)
def test_author_variants(court, line, expected):
    assert parse_opinion_header(header_text(line, court=court)).author == expected


def test_author_is_none_without_an_author_line():
    text = header_text("Justice Hill wrote:").replace("Justice Hill wrote: ", "")
    assert parse_opinion_header(text).author is None


def test_separate_opinion_lines_are_not_the_author():
    text = header_text("JUSTICE FEW, dissenting:")
    assert parse_opinion_header(text).author is None


@pytest.mark.parametrize(
    ("lines", "expected"),
    [
        (["REVERSED"], "REVERSED"),
        (["AFFIRMED."], "AFFIRMED"),
        (["REVERSED AND REMANDED"], "REVERSED AND REMANDED"),
        (
            ["AFFIRMED IN PART, REVERSED IN PART, AND REMANDED"],
            "AFFIRMED IN PART, REVERSED IN PART, AND REMANDED",
        ),
        (["AFFIRMED IN PART, REVERSED IN PART, AND", "REMANDED"], "AFFIRMED IN PART, REVERSED IN PART, AND REMANDED"),
        (["DEFINITE SUSPENSION"], "DEFINITE SUSPENSION"),
        (["(Withdrawn, Substituted, and Refiled December 3, 2026)", "REVERSED"], "REVERSED"),
        (["(Withdrawn, Substituted, and Refiled", "December 3, 2026)", "VACATED"], "VACATED"),
        ([], None),  # counsel lines follow, then the author; the body heading "BACKGROUND" is never used
        (["A" * 91], None),  # longer than 90 characters
        (["Reversed"], None),  # not all caps
        (["OF"], None),  # too few letters to be a disposition
    ],
)
def test_disposition_variants(lines, expected):
    assert parse_opinion_header(header_text(disposition_lines=lines)).disposition == expected


def test_disposition_scan_stops_before_body_headings_when_the_author_line_is_unrecognized():
    # No disposition, and a mixed-case author line the parser does not recognize: the all-caps
    # "BACKGROUND" heading a few lines later must not be taken for the disposition.
    header = parse_opinion_header(header_text("Justice Hill:", disposition_lines=[]))
    assert header.author is None
    assert header.disposition is None


def test_disposition_requires_a_filed_line():
    text = "Opinion No. 28400\nREVERSED\nJUSTICE HILL: We reverse."
    header = parse_opinion_header(text)
    assert header.disposition is None
    assert header.author == "Justice Hill"
    assert header.opinion_number == "28400"


def test_refiled_line_does_not_replace_the_filed_date():
    text = header_text(disposition_lines=["(Withdrawn, Substituted, and Refiled December 3, 2026)", "REVERSED"])
    header = parse_opinion_header(text)
    assert header.filed_date == date(2026, 6, 10)
    assert header.disposition == "REVERSED"


def test_header_tolerates_line_breaks_and_abbreviated_months():
    text = "Opinion No.\n28400\nHeard Sept. 16, 2026 – Filed Nov.\n12, 2026\nREVERSED\nJUSTICE HILL: We reverse."
    header = parse_opinion_header(text)
    assert header.opinion_number == "28400"
    assert header.filed_date == date(2026, 11, 12)
    assert header.heard_or_submitted == "Heard September 16, 2026"
    assert header.disposition == "REVERSED"
    assert header.author == "Justice Hill"


def test_header_without_filed_line_still_finds_heard_after_the_number():
    header = parse_opinion_header("Opinion No. 6170\nHeard October 7, 2026\nKONDUROS, J.: We reverse.")
    assert header.heard_or_submitted == "Heard October 7, 2026"
    assert header.filed_date is None
    assert header.author == "Judge Konduros"


@pytest.mark.parametrize("line", ["Unpublished Opinion No. 2025-UP-295", "Memorandum Opinion No. 2026-MO-017"])
def test_unpublished_and_memorandum_numbers_are_not_opinion_numbers(line):
    assert parse_opinion_header(line + "\nFiled June 10, 2026").opinion_number is None


def test_invalid_dates_are_ignored():
    header = parse_opinion_header("Opinion No. 28400\nHeard Smarch 5, 2026 – Filed February 30, 2026\nREVERSED")
    assert header.filed_date is None
    assert header.heard_or_submitted == "Heard Smarch 5, 2026"  # kept as printed when it is not a real date
    assert header.disposition == "REVERSED"
    assert header.opinion_number == "28400"


def test_only_the_first_6000_characters_are_searched():
    text = "x " * 3100 + "\n" + header_text()
    assert parse_opinion_header(text) == OpinionHeader()


def test_body_citations_do_not_leak_into_the_header():
    # 28355 cites "Op. No. 2025-UP-295 (S.C. Ct. App. filed August 13, 2025)" in its first paragraph.
    header = parse_opinion_header(opinion("sc_28355"))
    assert header.opinion_number == "28355"
    assert header.filed_date == date(2026, 11, 12)


@pytest.mark.parametrize(
    "text",
    ["", "   ", "\x00\x01\x02", "Opinion No.", "Filed", "JUSTICE", ":" * 1000, "Ω≈ç√∫" * 500, "A" * 100_000,
     "\n" * 10_000, "Filed November 12, 2026", "PER CURIAM", None],
)
def test_parse_opinion_header_never_raises(text):
    assert isinstance(parse_opinion_header(text), OpinionHeader)


def test_parse_opinion_header_survives_an_internal_error(monkeypatch, caplog):
    monkeypatch.setattr(extract, "_find_author", raiser(RuntimeError("boom")))
    header = parse_opinion_header(opinion("sc_28355"))
    assert header.opinion_number == "28355"  # fields parsed before the failure are kept
    assert header.author is None
    assert "Could not fully parse the opinion header" in caplog.text


# --------------------------------------------------------------------------- estimate_tokens


@pytest.mark.parametrize(("chars", "tokens"), [(0, 0), (1, 1), (3, 1), (4, 2), (3000, 1000), (3001, 1001)])
def test_estimate_tokens(chars, tokens):
    assert estimate_tokens("x" * chars) == tokens


# --------------------------------------------------------------------------- prepare_for_model

SEPARATE_HEADINGS = [
    "JUSTICE FEW, dissenting:",
    "JUSTICE JAMES, concurring in part and dissenting in part:",
    "THOMAS, J., dissenting:",
    "CHIEF JUSTICE KITTREDGE, concurring:",
    "WILLIAMS, C.J., and THOMAS, J., dissenting:",
    "ACTING JUSTICE JOHN DOE, concurring in result only:",
]


def test_prepare_returns_text_unchanged_when_it_fits():
    text = opinion("sc_28355")
    assert prepare_for_model(text, 30000) == PreparedText(
        text=text, truncated=False, kept_fraction=1.0, estimated_tokens=estimate_tokens(text), note=None
    )


def test_prepare_exact_fit_is_not_truncated():
    assert not prepare_for_model("x" * 3000, 1000).truncated
    assert prepare_for_model("x" * 3001, 1000).truncated


@pytest.mark.parametrize("heading", SEPARATE_HEADINGS)
def test_prepare_keeps_whole_majority_and_trims_separate_opinion(heading):
    majority = opinion("coa_6170")
    text = with_dissent(majority, heading)
    max_tokens = estimate_tokens(majority) + 1500  # the majority plus part of the dissent
    prepared = prepare_for_model(text, max_tokens)
    assert prepared.truncated
    assert estimate_tokens(prepared.text) <= max_tokens
    assert prepared.text.startswith(majority + "\n" + heading)
    assert prepared.text.endswith(SEPARATE_SUFFIX)
    kept = prepared.text[: -len(SEPARATE_SUFFIX)]
    assert text.startswith(kept) and text[len(kept)] == "\n"  # cut at a line break
    assert "Dissent line 000" in kept
    assert "I would affirm" not in prepared.text
    assert MIDDLE_RE.search(prepared.text) is None
    assert prepared.kept_fraction == round(len(kept) / len(text), 2)
    assert prepared.note == f"Long opinion: this summary is based on about {round(prepared.kept_fraction * 100)}% of its text."


def test_prepare_keeps_only_the_majority_when_no_dissent_line_fits():
    majority = opinion("coa_6170")
    text = with_dissent(majority)
    max_tokens = estimate_tokens(majority + SEPARATE_SUFFIX) + 5  # about 15 spare characters
    prepared = prepare_for_model(text, max_tokens)
    assert prepared.text == majority + SEPARATE_SUFFIX
    assert estimate_tokens(prepared.text) <= max_tokens


def test_prepare_ignores_concurrence_lines_that_are_not_separate_opinions():
    # "THOMAS and CURTIS, JJ., concur." ends coa_6170: not a separate opinion, so the middle is cut.
    prepared = prepare_for_model(opinion("coa_6170"), 3000)
    assert SEPARATE_OPINION_MARKER not in prepared.text
    assert MIDDLE_RE.search(prepared.text)


def test_prepare_keeps_head_and_tail_of_a_long_opinion():
    text = opinion("coa_6170")
    prepared = prepare_for_model(text, 3000)
    assert prepared.truncated
    assert estimate_tokens(prepared.text) <= 3000
    match = MIDDLE_RE.search(prepared.text)
    assert match
    head, tail = prepared.text[: match.start()], prepared.text[match.end() :]
    assert text.startswith(head) and text[len(head)] == "\n"  # the head ends at a line break
    assert text.endswith(tail) and text[-len(tail) - 1] == "\n"  # the tail starts a line
    assert head.startswith("THE STATE OF SOUTH CAROLINA\nIn The Court of Appeals")
    assert tail.endswith("REVERSED.\nTHOMAS and CURTIS, JJ., concur.")
    assert match.group(1) == f"{len(text) - len(head) - len(tail):,}"
    assert 1.6 <= len(head) / len(tail) <= 2.2  # about 65% / 35%
    assert prepared.kept_fraction == round((len(head) + len(tail)) / len(text), 2)
    assert prepared.note == f"Long opinion: this summary is based on about {round(prepared.kept_fraction * 100)}% of its text."


def test_prepare_long_majority_with_dissent_keeps_the_majority_ending():
    majority = opinion("coa_6170")
    text = with_dissent(majority, "JUSTICE FEW, dissenting:", lines=600)
    prepared = prepare_for_model(text, 5000)  # 15,000 characters: the 33,000-character majority cannot fit
    assert prepared.truncated
    assert estimate_tokens(prepared.text) <= 5000
    assert prepared.text.endswith("REVERSED.\nTHOMAS and CURTIS, JJ., concur." + SEPARATE_SUFFIX)
    assert "Dissent line" not in prepared.text
    assert "dissenting:" not in prepared.text
    match = MIDDLE_RE.search(prepared.text)
    assert match
    head = prepared.text[: match.start()]
    assert majority.startswith(head)
    tail = prepared.text[match.end() : -len(SEPARATE_SUFFIX)]
    assert majority.endswith(tail)
    assert prepared.kept_fraction == round((len(head) + len(tail)) / len(text), 2)


LATER_AUTHOR_OPENERS = [
    "JUSTICE FEW: I respectfully dissent.",
    "CHIEF JUSTICE KITTREDGE: I concur in result.",
    "THOMAS, J.: I concur.",
]


def supreme_court_split_decision(opener: str, majority_lines: int = 400) -> tuple[str, str]:
    """(majority, whole text): a majority, the court's closing lines, then a separate opinion in the
    Supreme Court's own style, which opens with an author line and no "dissenting:" label."""
    body = "\n".join(
        f"Majority line {i:03d} explains the Court's reasoning on the jurisdiction question at length."
        for i in range(majority_lines)
    )
    majority = (
        header_text("JUSTICE HILL:", disposition_lines=("REVERSED",)) + "\n" + body + "\n"
        "The decision of the court of appeals is REVERSED.\n"
        "KITTREDGE, C.J., JAMES and VERDIN, JJ., concur. FEW, J., dissenting in a separate opinion."
    )
    separate = "\n".join(
        [opener]
        + [f"Separate line {i:03d} explains why the record supports the court of appeals instead." for i in range(300)]
        + ["For these reasons, I would affirm the court of appeals."]
    )
    return majority, majority + "\n" + separate


@pytest.mark.parametrize("opener", LATER_AUTHOR_OPENERS)
def test_prepare_trims_a_separate_opinion_that_opens_with_an_author_line(opener):
    majority, text = supreme_court_split_decision(opener)
    max_tokens = estimate_tokens(majority) + 1000  # the whole majority fits, plus part of the separate opinion
    prepared = prepare_for_model(text, max_tokens)
    assert estimate_tokens(prepared.text) <= max_tokens
    assert prepared.text.startswith(majority + "\n" + opener)
    assert prepared.text.endswith(SEPARATE_SUFFIX)
    assert "I would affirm the court of appeals" not in prepared.text
    assert MIDDLE_RE.search(prepared.text) is None


@pytest.mark.parametrize("opener", LATER_AUTHOR_OPENERS)
def test_prepare_long_majority_keeps_its_conclusion_before_an_author_line_dissent(opener):
    # The reviewer's case: the majority alone is over the limit. Its head and tail are kept, so the
    # text ends with the Court's REVERSED, never with the separate opinion's "I would affirm".
    majority, text = supreme_court_split_decision(opener, majority_lines=900)
    prepared = prepare_for_model(text, 10_000)
    assert len(majority) > 10_000 * CHARS_PER_TOKEN
    assert estimate_tokens(prepared.text) <= 10_000
    assert prepared.text.endswith(
        "The decision of the court of appeals is REVERSED.\n"
        "KITTREDGE, C.J., JAMES and VERDIN, JJ., concur. FEW, J., dissenting in a separate opinion." + SEPARATE_SUFFIX
    )
    assert "I would affirm the court of appeals" not in prepared.text and opener not in prepared.text
    assert MIDDLE_RE.search(prepared.text)


def test_a_quoted_author_line_without_any_separate_opinion_cue_is_not_a_separate_opinion():
    lines = [f"Majority line {i:03d} explains the reasoning of the Court in this case at length." for i in range(600)]
    lines[400] = "JUSTICE SMITH: Counsel, what is your best case on this point?"  # quoted from the argument
    text = header_text("JUSTICE HILL:") + "\n" + "\n".join(lines) + "\nAFFIRMED."
    prepared = prepare_for_model(text, 5_000)
    assert SEPARATE_OPINION_MARKER not in prepared.text
    assert prepared.text.endswith("AFFIRMED.")


@pytest.mark.parametrize("name", ["sc_28354", "sc_28355", "coa_6170"])
def test_real_opinions_have_no_separate_opinion(name):
    assert extract._first_separate_opinion(opinion(name)) is None


def test_prepare_head_and_tail_without_line_breaks():
    text = "word " * 20_000
    prepared = prepare_for_model(text, 2000)
    assert estimate_tokens(prepared.text) <= 2000
    match = MIDDLE_RE.search(prepared.text)
    head, tail = prepared.text[: match.start()], prepared.text[match.end() :]
    assert head.endswith("word") and tail.startswith("word")  # cut between words


def test_truncated_text_never_claims_to_keep_100_percent():
    text = "\n".join(f"Line {i} of a long opinion with enough words to matter." for i in range(2000))
    prepared = prepare_for_model(text, estimate_tokens(text) - 1)
    assert prepared.truncated
    assert prepared.kept_fraction == 0.99
    assert prepared.note == "Long opinion: this summary is based on about 99% of its text."


def test_zero_token_budget_gives_empty_text():
    prepared = prepare_for_model("Some opinion text.", 0)
    assert prepared == PreparedText(
        text="", truncated=True, kept_fraction=0.0, estimated_tokens=0,
        note="Long opinion: this summary is based on about 0% of its text.",
    )


def test_tiny_budget_keeps_only_the_beginning():
    prepared = prepare_for_model(opinion("sc_28354"), 20)  # 60 characters: no room for a marker
    assert estimate_tokens(prepared.text) <= 20
    assert opinion("sc_28354").startswith(prepared.text)
    assert prepared.text.startswith("THE STATE OF SOUTH CAROLINA")


def test_last_resort_cut_still_honors_the_limit(monkeypatch, caplog):
    # If a shortening step ever overshot, the result is cut hard (with a warning) rather than sent too long.
    monkeypatch.setattr(extract, "_keep_head_and_tail", lambda text, budget, end: (text, len(text), "kept it all"))
    prepared = prepare_for_model(opinion("coa_6170"), 1000)
    assert estimate_tokens(prepared.text) <= 1000
    assert prepared.text == opinion("coa_6170")[:3000]
    assert prepared.kept_fraction == round(3000 / len(opinion("coa_6170")), 2)
    assert extract_warnings(caplog) == ["Shortened opinion text was still over the limit; cutting it to 3,000 characters."]


def test_negative_budget_is_an_error():
    with pytest.raises(ValueError):
        prepare_for_model("text", -1)


def test_surrounding_whitespace_is_dropped_before_truncating():
    text = "\n\n" + "A" * 30 + "\n" * 100
    assert prepare_for_model(text, 10) == PreparedText("A" * 30, False, 1.0, 10, None)


def test_prepare_logs_once_when_it_shortens_text(caplog):
    caplog.set_level(logging.INFO, logger="src.extract")
    prepare_for_model("short text", 100)
    assert caplog.records == []
    prepare_for_model(opinion("coa_6170"), 3000)
    assert len(caplog.records) == 1
    message = caplog.records[0].getMessage()
    assert "over the 3,000-token limit" in message
    assert "kept the beginning and end of the opinion" in message


INVARIANT_TEXT_NAMES = [
    "sc_28354",
    "sc_28355",
    "coa_6170",
    "coa_6170+dissent",
    "coa_6170+long dissent",
    "one long line",
    "no spaces",
    "short lines",
    "padded",
]
INVARIANT_SIZES = [0, 1, 2, 3, 5, 10, 25, 33, 34, 50, 75, 100, 150, 250, 500, 1000, 2000, 2639, 2640,
                   3000, 5000, 8000, 11096, 11097, 11098, 13000, 20000, 30000]


@functools.lru_cache(maxsize=None)
def invariant_text(name: str) -> str:
    if name in ("sc_28354", "sc_28355", "coa_6170"):
        return opinion(name)
    if name == "coa_6170+dissent":
        return with_dissent(opinion("coa_6170"))
    if name == "coa_6170+long dissent":
        return with_dissent(opinion("coa_6170"), "JUSTICE FEW, dissenting:", lines=1500)
    if name == "one long line":
        return "word " * 20_000
    if name == "no spaces":
        return "x" * 50_000
    if name == "short lines":
        return "\n".join(f"L{i}" for i in range(20_000))
    if name == "padded":
        return "\n\n   " + opinion("coa_6170") + "   \n\n\n"
    raise KeyError(name)


def extract_warnings(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.name == "src.extract" and r.levelno >= logging.WARNING]


@pytest.mark.parametrize("max_tokens", INVARIANT_SIZES)
@pytest.mark.parametrize("name", INVARIANT_TEXT_NAMES)
def test_prepare_result_always_fits(name, max_tokens, caplog):
    text = invariant_text(name)
    prepared = prepare_for_model(text, max_tokens)
    assert estimate_tokens(prepared.text) <= max_tokens
    assert extract_warnings(caplog) == []  # fits by construction, not by the last-resort hard cut
    assert prepared.estimated_tokens == estimate_tokens(prepared.text)
    if estimate_tokens(text) <= max_tokens:
        assert prepared == PreparedText(text, False, 1.0, estimate_tokens(text), None)
    elif estimate_tokens(text.strip()) <= max_tokens:
        assert prepared == PreparedText(text.strip(), False, 1.0, estimate_tokens(text.strip()), None)
    else:
        assert prepared.truncated
        assert 0.0 <= prepared.kept_fraction <= 0.99
        assert NOTE_RE.match(prepared.note)
        assert prepared.note == (
            f"Long opinion: this summary is based on about {round(prepared.kept_fraction * 100)}% of its text."
        )


def test_prepare_invariant_on_random_texts(caplog):
    rng = random.Random(20261116)
    for _ in range(300):
        lines = [
            " ".join("w" * rng.randint(1, 12) for _ in range(rng.randint(0, 15))) for _ in range(rng.randint(1, 400))
        ]
        if rng.random() < 0.4 and len(lines) > 20:
            lines.insert(rng.randint(10, len(lines) - 1), rng.choice(SEPARATE_HEADINGS) + " I disagree.")
        text = "\n".join(lines)
        max_tokens = rng.randint(0, estimate_tokens(text) + 50)
        prepared = prepare_for_model(text, max_tokens)
        assert estimate_tokens(prepared.text) <= max_tokens
        if prepared.truncated:
            assert 0.0 <= prepared.kept_fraction <= 0.99
            assert NOTE_RE.match(prepared.note)
        else:
            assert prepared.text in (text, text.strip())
    assert extract_warnings(caplog) == []  # every result fit by construction


# --------------------------------------------------------------------------- slice_pages

PAGES = ["p1", "p2", "p3", "p4"]


@pytest.mark.parametrize(
    ("start", "end", "expected"),
    [
        (2, 3, "p2\np3"),
        (1, 4, "p1\np2\np3\np4"),
        (2, 2, "p2"),
        (4, 4, "p4"),
        (0, 2, "p1\np2"),
        (-5, 1, "p1"),
        (3, 99, "p3\np4"),
        (-1, 100, "p1\np2\np3\np4"),
        (5, 9, ""),
        (3, 2, ""),
    ],
)
def test_slice_pages(start, end, expected):
    assert slice_pages(PAGES, start, end) == expected


def test_slice_pages_of_no_pages():
    assert slice_pages([], 1, 3) == ""


def test_slice_pages_on_a_real_advance_sheet():
    pages = fixture_json("sccourts/advance_sheet_41_pages.json")
    order = slice_pages(pages, 26, 27)
    assert order.startswith("The Supreme Court of South Carolina\nRe: Amendments to Rule 1.15, RPC, Rule 407, SCACR")
    assert order == pages[25] + "\n" + pages[26]
    assert "Re: Fees for Copying and Other Related Services" in slice_pages(pages, 1, 3)
    assert slice_pages(pages, 40, 100) == "\n".join(pages[39:])
