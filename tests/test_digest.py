"""Tests for src/digest.py: the digest email's subject, Markdown body, and HTML preview.

The sample week is the real one in tests/fixtures (opinions filed November 12, 2026,
Advance Sheet No. 41): SC 28354 (Nettles), SC 28355 (Fleming), COA 6170 (Gary Moore,
whose court PDF URL contains a raw space), and the two orders printed in the sheet.
"""

from __future__ import annotations

import re
from datetime import date

import markdown
import pytest

from src.digest import (
    DISCLAIMER,
    EDITOR_MODE_COMMENT,
    MOCK_BANNER,
    NO_NEW_OPINIONS,
    Digest,
    DigestInput,
    build_digest,
    build_subject,
    digest_counts,
    format_date,
    md_escape,
    md_url,
    render_html,
    render_markdown,
)
from src.models import AdvanceSheet, Court, Item, ItemKind, ResultStatus, Summary, SummaryResult

FILED = date(2026, 11, 12)
EM = "—"
SC, COA = Court.SUPREME, Court.APPEALS
BOTH_COURTS = (SC, COA)

SHEET_41 = AdvanceSheet(41, FILED, "https://www.sccourts.org/media/opinions/advSheets/no412026.pdf")
SHEET_40 = AdvanceSheet(40, date(2026, 11, 5), "https://www.sccourts.org/media/opinions/advSheets/no402026.pdf")

URL_28354 = "https://www.sccourts.org/media/opinions/HTMLFiles/SC/28354.pdf"
URL_28355 = "https://www.sccourts.org/media/opinions/HTMLFiles/SC/28355.pdf"
URL_6170_RAW = "https://www.sccourts.org/media/opinions/HTMLFiles/COA/6170 .pdf"  # raw space, as served
URL_6170 = "https://www.sccourts.org/media/opinions/HTMLFiles/COA/6170%20.pdf"
CL_28354 = "https://storage.courtlistener.com/pdf/2026/11/12/in_the_matter_of_louis_david_nettles.pdf"
CL_28355 = "https://storage.courtlistener.com/pdf/2026/11/12/reid_fleming_v._lg_chem_ltd..pdf"
CL_6170 = "https://storage.courtlistener.com/pdf/2026/11/12/gary_moore_v._state.pdf"
ORDER_115_URL = "https://www.sccourts.org/media/courtOrders/PDFs/2026-11-12-01.pdf"
RULE_115 = "Re: Amendments to Rule 1.15, RPC, Rule 407, SCACR"
FEES = "Re: Fees for Copying and Other Related Services"

FOOTER_FIRST = (
    "**AI-generated summaries for informational purposes only; not legal advice; always read the full opinion.**"
)


# --- builders -----------------------------------------------------------------------


def opinion(number: str | None, case_name: str, court: Court = SC, **kw) -> Item:
    kw.setdefault("link_url", f"https://www.sccourts.org/media/opinions/HTMLFiles/{court.value}/{number}.pdf")
    kw.setdefault("pdf_url", kw["link_url"])
    kw.setdefault("filed_date", FILED)
    kw.setdefault("source", "courtlistener-api")
    return Item(kind=ItemKind.OPINION, court=court, case_name=case_name, number=number, **kw)


def order(case_name: str, number: str | None = None, **kw) -> Item:
    kw.setdefault("link_url", f"{SHEET_41.pdf_url}#page=26")
    kw.setdefault("filed_date", FILED)
    kw.setdefault("source", "sccourts")
    return Item(kind=ItemKind.ORDER, court=SC, case_name=case_name, number=number, pdf_url=None, **kw)


def summary(case_name: str = "Case", **kw) -> Summary:
    fields = {
        "case_name": case_name,
        "opinion_number": "unclear from the opinion",
        "court": "Supreme Court",
        "date": "November 12, 2026",
        "author": "Justice Hill",
        "one_line": "One-line takeaway.",
        "holding": "The holding.",
        "what_changed": "No change to existing law.",
        "change_type": "none",
        "practice_areas": ["Civil Procedure"],
        "practice_tip": "A practice tip.",
        "disposition": "Affirmed",
    }
    fields.update(kw)
    return Summary(**fields)


def ok(item: Item, **kw) -> SummaryResult:
    result_kw = {k: kw.pop(k) for k in ("truncated", "kept_fraction", "model", "mock") if k in kw}
    return SummaryResult(
        item=item, status=ResultStatus.SUMMARIZED, summary=summary(item.case_name, **kw), **result_kw
    )


def failed(item: Item, error: str | None = "the AI service was unavailable") -> SummaryResult:
    return SummaryResult(item=item, status=ResultStatus.FAILED, error=error)


def make_input(results=(), overflow=(), **kw) -> DigestInput:
    kw.setdefault("digest_date", FILED)
    kw.setdefault("model", "claude-sonnet-5-5")
    kw.setdefault("courts_included", BOTH_COURTS)
    kw.setdefault("orders_included", True)
    return DigestInput(results=list(results), overflow=list(overflow), **kw)


def render(results=(), overflow=(), **kw) -> str:
    inp = make_input(results, overflow, **kw)
    return render_markdown(inp, build_subject(inp))


def nettles() -> SummaryResult:
    item = opinion("28354", "In the Matter of Louis David Nettles", link_url=URL_28354, mirror_url=CL_28354,
                   source_id="11060101", advance_sheet=41)
    return ok(
        item,
        author="Per Curiam",
        disposition="Public reprimand",
        one_line="Lawyer publicly reprimanded for missed deadlines and an improper malpractice settlement.",
        practice_areas=["Professional Responsibility"],
    )


def fleming() -> SummaryResult:
    item = opinion("28355", "Reid Fleming v. LG Chem Ltd.", link_url=URL_28355, mirror_url=CL_28355,
                   source_id="11060100", advance_sheet=41)
    return ok(
        item,
        author="Justice Hill",
        disposition="Reversed",
        one_line="South Carolina may exercise specific personal jurisdiction over LG Chem in a vape battery case.",
        holding="Due process permits specific jurisdiction; relatedness under Ford does not require causation.",
        what_changed="Clarifies: Relatedness does not require proof that in-state conduct caused the injury.",
        change_type="clarifies",
        practice_areas=["Civil Procedure", "Torts"],
        practice_tip="Plead the manufacturer's knowledge of in-state sales and prior in-state suits.",
    )


def moore() -> SummaryResult:
    item = opinion("6170", "Gary Moore v. State", COA, link_url=URL_6170_RAW, mirror_url=CL_6170,
                   source_id="11060103", advance_sheet=41)
    return ok(
        item,
        author="Judge Konduros",
        disposition="Reversed",
        what_changed="Reaffirms: Attempted murder requires a specific intent to kill.",
        change_type="reaffirms",
        practice_areas=["Criminal", "Post-Conviction Relief"],
    )


def rule_115_order() -> SummaryResult:
    item = order(RULE_115, "2026-11-12-01", link_url=ORDER_115_URL, mirror_url=f"{SHEET_41.pdf_url}#page=26",
                 advance_sheet=41)
    return ok(
        item,
        author="Order of the Court",
        disposition="Rule amended",
        what_changed="Changes: Comment 4 to Rule 1.15 now addresses charging liens. Effective immediately.",
        change_type="changes",
        practice_areas=["Professional Responsibility"],
    )


def fees_order() -> SummaryResult:
    item = order(FEES, None, filed_date=date(2026, 11, 4), link_url=f"{SHEET_41.pdf_url}#page=1", advance_sheet=41)
    return ok(item, author="Order of the Court", disposition="Order issued", practice_areas=["Appellate Procedure"])


def real_week() -> list[SummaryResult]:
    """Results in a deliberately scrambled order: the digest sorts them itself."""
    return [moore(), rule_115_order(), fleming(), fees_order(), nettles()]


def section(md: str, heading: str) -> str:
    """The text of '## heading' up to the next '## ' heading or the footer."""
    start = md.index(f"\n## {heading}\n") + 1
    rest = md[start:]
    ends = [i for i in (rest.find("\n## ", 1), rest.find("\n---\n\n**AI-generated")) if i != -1]
    return rest[: min(ends)] if ends else rest


def to_html(md: str) -> str:
    return markdown.markdown(md, extensions=["extra", "sane_lists"])


# --- formatting helpers -------------------------------------------------------------


def test_format_date_has_no_leading_zero():
    assert format_date(FILED) == "November 12, 2026"
    assert format_date(date(2026, 3, 1)) == "March 1, 2026"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("a*b", r"a\*b"),
        ("snake_case_name", r"snake\_case\_name"),
        ("[bracketed]", r"\[bracketed\]"),
        ("`code`", r"\`code\`"),
        ("back\\slash", r"back\\slash"),
        ("a | b", r"a \| b"),
        ("a > b", r"a \> b"),
        ("[MOCK SUMMARY — no API call was made] text", r"\[MOCK SUMMARY — no API call was made\] text"),
    ],
)
def test_md_escape_backslash_escapes_markdown_characters(text, expected):
    assert md_escape(text) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("# Not a heading", r"\# Not a heading"),
        ("+ not a list", r"\+ not a list"),
        ("- not a list", r"\- not a list"),
        ("---", r"\---"),
        ("1. Not a list", r"1\. Not a list"),
        ("12) Not a list", r"12\) Not a list"),
        ("2026.5 percent", "2026.5 percent"),  # not a list marker: no space after the dot
        ("Section 1. applies", "Section 1. applies"),  # only a LEADING marker is escaped
        ("John Doe Distributor #1", "John Doe Distributor #1"),  # mid-text # is harmless
        ("well-known - dash", "well-known - dash"),
    ],
)
def test_md_escape_leading_block_markers(text, expected):
    assert md_escape(text) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("{{ subscriber.email }}", "{ { subscriber.email }}"),
        ("{% if subscriber %}hi{% endif %}", "{ % if subscriber %}hi{ % endif %}"),
        ("{# hidden #}", "{ # hidden #}"),
        ("{{{ triple }}}", "{ { { triple }}}"),
        ("a {single} brace", "a {single} brace"),
    ],
)
def test_md_escape_neutralizes_template_syntax(text, expected):
    assert md_escape(text) == expected


def test_md_escape_collapses_whitespace_and_handles_empty_values():
    assert md_escape("  two\n\nlines\tand   spaces ") == "two lines and spaces"
    assert md_escape("first line\n# second line") == "first line # second line"  # no heading can sneak in
    assert md_escape(None) == ""
    assert md_escape("   ") == ""


def test_md_escape_angle_brackets_and_entities_render_literally():
    assert md_escape("a <b> c") == r"a &lt;b\> c"
    assert md_escape("&copy; and Smith & Griffith") == "&amp;copy; and Smith & Griffith"
    html = to_html(md_escape("<script>x</script> &sect; R&D"))
    assert "<script>" not in html
    assert "&lt;script&gt;x&lt;/script&gt; &amp;sect; R&amp;D" in html
    assert "\\" not in html


def test_md_url_percent_encodes_unsafe_characters():
    assert md_url(URL_6170_RAW) == URL_6170
    assert md_url(f"  {URL_6170_RAW}  ") == URL_6170
    assert md_url("https://x.example/a(b)<c>") == "https://x.example/a%28b%29%3Cc%3E"
    assert md_url(URL_6170) == URL_6170  # existing %20 is not double-encoded
    assert md_url(f"{SHEET_41.pdf_url}#page=26") == f"{SHEET_41.pdf_url}#page=26"
    assert md_url("https://x.example/{{ subscriber.email }}") == "https://x.example/%7B%7B%20subscriber.email%20%7D%7D"
    assert md_url(None) == ""
    assert md_url("") == ""


# --- subject ------------------------------------------------------------------------


def test_subject_one_opinion_is_singular():
    inp = make_input([fleming()])
    assert build_subject(inp) == f"SC Advance Sheet Digest {EM} November 12, 2026 {EM} 1 opinion"


def test_subject_opinions_plus_orders():
    inp = make_input(real_week())
    assert build_subject(inp) == f"SC Advance Sheet Digest {EM} November 12, 2026 {EM} 3 opinions + 2 orders"


def test_subject_one_order_is_singular():
    inp = make_input([fleming(), nettles(), rule_115_order()])
    assert build_subject(inp) == f"SC Advance Sheet Digest {EM} November 12, 2026 {EM} 2 opinions + 1 order"


def test_subject_orders_only():
    one = make_input([rule_115_order()])
    assert build_subject(one) == f"SC Advance Sheet Digest {EM} November 12, 2026 {EM} 1 order"
    two = make_input([rule_115_order(), fees_order()])
    assert build_subject(two) == f"SC Advance Sheet Digest {EM} November 12, 2026 {EM} 2 orders"


def test_subject_counts_failed_and_overflow_items():
    extra = opinion("6171", "Overflow v. Case", COA)
    inp = make_input([fleming(), failed(moore().item)], overflow=[extra, order(FEES)])
    assert build_subject(inp) == f"SC Advance Sheet Digest {EM} November 12, 2026 {EM} 3 opinions + 1 order"


def test_subject_uses_em_dashes_and_the_digest_date():
    subject = build_subject(make_input([fleming()], digest_date=date(2026, 3, 4)))
    assert subject.count("—") == 2
    assert "–" not in subject and " - " not in subject
    assert "March 4, 2026" in subject


@pytest.mark.parametrize(
    ("previous", "suffix"),
    [
        (set(), ""),
        ({"SC Advance Sheet Digest — November 5, 2026 — 1 opinion"}, ""),  # a different week
        ({"BASE"}, " (update)"),
        ({"BASE", "BASE (update)"}, " (update 2)"),
        ({"BASE", "BASE (update)", "BASE (update 2)"}, " (update 3)"),
        ({"BASE (update)"}, ""),  # only an exact match of the base subject triggers a suffix
    ],
)
def test_subject_update_suffixes(previous, suffix):
    base = f"SC Advance Sheet Digest {EM} November 12, 2026 {EM} 1 opinion"
    prev = frozenset(p.replace("BASE", base) for p in previous)
    assert build_subject(make_input([fleming()], previous_subjects=prev)) == base + suffix


# --- overall structure --------------------------------------------------------------


@pytest.mark.parametrize(
    "inp",
    [
        make_input(real_week(), advance_sheets=[SHEET_41], source="sccourts"),
        make_input([], []),
        make_input([fleming()], mock=True),
        make_input([failed(moore().item)], overflow=[opinion("6171", "X v. Y", COA)]),
    ],
)
def test_markdown_starts_with_editor_mode_comment_never_front_matter(inp):
    md = render_markdown(inp, build_subject(inp))
    assert md.startswith(EDITOR_MODE_COMMENT + "\n\n")
    assert EDITOR_MODE_COMMENT == "<!-- buttondown-editor-mode: plaintext -->"
    assert not md.lstrip().startswith("---")
    assert md.endswith("\n") and not md.endswith("\n\n")


def test_sections_appear_in_spec_order():
    extra = opinion("6171", "Harbor Pointe v. Kiawah Shoals, LLC", COA)
    md = render(real_week(), overflow=[extra], mock=True, advance_sheets=[SHEET_41], source="sccourts")
    markers = [
        EDITOR_MODE_COMMENT,
        "**SC Advance Sheet Digest** · November 12, 2026",
        MOCK_BANNER,
        "New this week:",
        "\n## Rule changes this week\n",
        "\n## Supreme Court\n",
        "\n## Supreme Court orders\n",
        "\n## Court of Appeals\n",
        "\n## Also filed this week (not summarized)\n",
        FOOTER_FIRST,
        "You can unsubscribe at any time with the link at the bottom of this email.",
    ]
    positions = [md.index(m) for m in markers]
    assert positions == sorted(positions)
    assert md.count("\n## ") == 5


def test_masthead_names_the_advance_sheet_when_known():
    md = render(real_week(), advance_sheets=[SHEET_40, SHEET_41], source="sccourts")
    assert md.splitlines()[2] == (
        "**SC Advance Sheet Digest** · November 12, 2026 · "
        "[Advance Sheet No. 41](https://www.sccourts.org/media/opinions/advSheets/no412026.pdf)"
    )


def test_masthead_without_advance_sheet():
    # CourtListener items never know their advance sheet.
    md = render([ok(opinion("28355", "Reid Fleming v. LG Chem Ltd."))])
    assert md.splitlines()[2] == "**SC Advance Sheet Digest** · November 12, 2026"
    assert "Advance Sheet No" not in md


def test_masthead_names_an_items_sheet_even_without_its_url():
    md = render([fleming()])  # the item says it is in sheet 41, but no AdvanceSheet was passed
    assert md.splitlines()[2] == "**SC Advance Sheet Digest** · November 12, 2026 · Advance Sheet No. 41"


def test_masthead_ignores_sheets_none_of_the_items_appear_in():
    # Sheet 40 (Nov 5) was fetched, but this week's items are not in it and its date is not the digest date.
    md = render([ok(opinion("28356", "New v. Case"))], advance_sheets=[SHEET_40], source="sccourts")
    assert md.splitlines()[2] == "**SC Advance Sheet Digest** · November 12, 2026"


def test_masthead_falls_back_to_the_sheet_dated_on_the_digest_date():
    md = render([ok(opinion("28356", "New v. Case"))], advance_sheets=[SHEET_40, SHEET_41], source="sccourts")
    assert md.splitlines()[2].endswith(f"[Advance Sheet No. 41]({SHEET_41.pdf_url})")


def test_masthead_lists_several_sheets():
    items = [
        ok(opinion("28350", "Old v. Case", advance_sheet=40)),
        ok(opinion("28355", "New v. Case", advance_sheet=41)),
    ]
    md = render(items, advance_sheets=[SHEET_41, SHEET_40], source="sccourts")
    assert md.splitlines()[2].endswith(
        f"Advance Sheet Nos. [40]({SHEET_40.pdf_url}) and [41]({SHEET_41.pdf_url})"
    )


def test_intro_sentence_counts_courts_and_orders():
    md = render(real_week())
    assert (
        "New this week: 2 published Supreme Court opinions, 2 Supreme Court orders, "
        "and 1 published Court of Appeals opinion." in md
    )


def test_intro_sentence_mentions_empty_included_courts_and_overflow():
    md = render([fleming()], overflow=[opinion("28356", "Later v. Case")])
    assert (
        "New this week: 2 published Supreme Court opinions and no published Court of Appeals opinions "
        "(1 is listed at the end without a summary)." in md
    )
    md = render([fleming()], courts_included=(SC,))
    assert "New this week: 1 published Supreme Court opinion." in md


# --- rule changes -------------------------------------------------------------------


def test_rule_changes_lists_only_changes_and_clarifies_orders_first():
    coa_change = ok(
        opinion("6175", "Doe v. Roe", COA),
        change_type="changes",
        what_changed="Changes: Overrules an earlier Court of Appeals decision.",
    )
    sc_none = ok(opinion("28357", "None v. Case"), change_type="none")
    md = render([coa_change, sc_none, failed(opinion("28358", "Failed v. Case"))] + real_week())
    rule = section(md, "Rule changes this week")
    bullets = [line for line in rule.splitlines() if line.startswith("- ")]
    assert len(bullets) == 3
    assert bullets[0].startswith(f"- **{md_escape(RULE_115)}** (Order) — **Changes:** Comment 4 to Rule 1.15")
    assert bullets[1].startswith("- **Reid Fleming v. LG Chem Ltd.** (Supreme Court) — **Clarifies:** Relatedness")
    assert bullets[2].startswith("- **Doe v. Roe** (Court of Appeals) — **Changes:** Overrules")
    for excluded in ("Gary Moore", "Nettles", "None v. Case", "Fees for Copying"):
        assert excluded not in rule
    # The failed one is never listed as a rule change, but readers are told it was not checked.
    assert not any("Failed v. Case" in bullet for bullet in bullets)
    assert "_1 more opinion was not summarized, so it was not checked for rule changes: Failed v. Case._" in rule


def test_rule_change_line_strips_the_label_and_links_the_pdf():
    md = render([fleming()])
    assert (
        "- **Reid Fleming v. LG Chem Ltd.** (Supreme Court) — **Clarifies:** Relatedness does not require "
        f"proof that in-state conduct caused the injury. [PDF]({URL_28355})"
    ) in md
    assert "**Clarifies:** Clarifies:" not in md


def test_rule_change_label_follows_change_type_even_without_a_text_label():
    result = ok(opinion("28360", "Label v. Less"), change_type="changes", what_changed="Overrules Smith v. Jones.")
    assert "— **Changes:** Overrules Smith v. Jones. [PDF](" in render([result])


def test_rule_change_line_never_swaps_a_disagreeing_label():
    # validate_summary reconciles the two fields; if a pair still disagreed, the model's own
    # "Reaffirms:" must not be silently relabeled "Clarifies:".
    result = ok(opinion("28361", "Swap v. Label"), change_type="clarifies", what_changed="Reaffirms: settled law applies.")
    md = render([result])
    assert "— **Clarifies:** Reaffirms: settled law applies." in md
    assert "— **Clarifies:** settled law applies." not in md


@pytest.mark.parametrize(
    ("change_type", "what_changed", "listed"),
    [
        ("changes", "No change to existing law.", False),
        ("changes", "Reaffirms: the settled four-factor test.", False),
        ("reaffirms", "Changes: overrules Smith v. Jones.", True),
        ("none", "Clarifies: the rule reaches online sellers.", True),
    ],
)
def test_rule_changes_follow_what_changed_after_validation(change_type, what_changed, listed):
    from src.summarize import validate_summary

    raw = summary("Pair v. Mismatch").to_dict() | {"change_type": change_type, "what_changed": what_changed}
    result = SummaryResult(item=opinion("28362", "Pair v. Mismatch"), status=ResultStatus.SUMMARIZED,
                           summary=validate_summary(raw))  # fmt: skip
    rule = section(render([result]), "Rule changes this week")
    assert ("Pair v. Mismatch" in rule) is listed
    assert "**Changes:** No change to existing law." not in rule
    assert "**Changes:** the settled four-factor test." not in rule


def test_rule_changes_empty_message_when_orders_were_checked():
    md = render([nettles(), moore()], source="sccourts", orders_included=True)
    rule = section(md, "Rule changes this week")
    assert "_No opinion or order this week changes or clarifies a rule._" in rule
    assert "not included in this digest" not in rule


def test_rule_changes_say_orders_are_not_covered_in_courtlistener_mode():
    # CourtListener never returns orders, even when INCLUDE_ORDERS is on.
    md = render([nettles(), moore()], source="courtlistener-api", orders_included=True)
    rule = section(md, "Rule changes this week")
    assert "_No opinion this week changes or clarifies a rule._" in rule
    assert "No opinion or order" not in rule
    assert (
        "_Supreme Court orders and rule amendments are not included in this digest; check the "
        "[Supreme Court's orders page](https://www.sccourts.org/opinions-orders/court-orders/all-court-orders/)._"
    ) in rule


def test_rule_changes_note_when_orders_are_switched_off():
    md = render([fleming()], source="sccourts", orders_included=False)
    rule = section(md, "Rule changes this week")
    assert "**Clarifies:**" in rule
    assert "not included in this digest" in rule


def test_rule_changes_say_orders_may_be_missing_when_this_weeks_sheet_was_not_read():
    # SOURCE=sccourts, but this week's advance sheet could not be read: an older sheet's order is
    # in the digest, yet the week's orders were not checked, so "no order changes a rule" is not claimed.
    md = render([nettles(), moore(), fees_order()], source="sccourts", orders_included=False)
    rule = section(md, "Rule changes this week")
    assert "_No opinion or order this week changes or clarifies a rule._" in rule
    assert "this digest may not include every Supreme Court order or rule amendment" in rule
    assert "not included in this digest" not in rule  # orders ARE in it (the fees order)


def test_rule_changes_when_orders_were_not_checked_and_none_were_found():
    md = render([nettles(), moore()], source="sccourts", orders_included=False)
    rule = section(md, "Rule changes this week")
    assert "_No opinion this week changes or clarifies a rule._" in rule
    assert "_Supreme Court orders and rule amendments are not included in this digest; check the" in rule
    assert "No opinion or order" not in rule


# --- rule changes: opinions nobody classified ---------------------------------------


def test_rule_changes_never_claims_no_change_for_failed_and_overflow_items():
    a = ok(opinion("28400", "A v. B"))
    c = failed(opinion("28401", "C v. D"), "the AI service was unavailable")
    e = opinion("28402", "E v. F")
    rule = section(render([a, c], overflow=[e]), "Rule changes this week")
    assert "No opinion this week changes" not in rule and "No opinion or order this week" not in rule
    assert (
        "_None of the summarized opinions this week changes or clarifies a rule. 2 opinions were not "
        "summarized, so they were not checked for rule changes: C v. D; E v. F. Read them directly (links below)._"
    ) in rule


def test_rule_changes_with_only_a_failed_item_unclassified():
    rule = section(render([nettles(), failed(moore().item)], source="courtlistener-api"), "Rule changes this week")
    assert (
        "_None of the summarized opinions this week changes or clarifies a rule. 1 opinion was not summarized, so "
        "it was not checked for rule changes: Gary Moore v. State. Read it directly (links below)._"
    ) in rule


def test_rule_changes_with_only_overflow_unclassified():
    rule = section(render([nettles()], overflow=[opinion("6171", "Overflow v. Case", COA)]), "Rule changes this week")
    assert "1 opinion was not summarized, so it was not checked for rule changes: Overflow v. Case." in rule


def test_rule_changes_list_plus_unclassified_items_and_orders_say_items():
    rule = section(render([fleming(), failed(rule_115_order().item)], overflow=[order(FEES)]), "Rule changes this week")
    assert "**Clarifies:**" in rule
    assert (
        f"_2 more items were not summarized, so they were not checked for rule changes: {FEES}; "
        f"{md_escape(RULE_115)}._"
    ) in rule


def test_rule_changes_with_nothing_summarized_at_all():
    rule = section(render([failed(moore().item)]), "Rule changes this week")
    assert "None of the summarized" not in rule
    assert "_1 opinion was not summarized, so it was not checked for rule changes: Gary Moore v. State." in rule


def test_rule_changes_unclassified_names_are_capped():
    extra = [opinion(str(6200 + n), f"Case {n:02d} v. State", COA) for n in range(14)]
    rule = section(render([nettles()], overflow=extra), "Rule changes this week")
    assert "14 opinions were not summarized" in rule
    assert "Case 09 v. State; and 4 more." in rule and "Case 10 v. State" not in rule


# --- supplements (a second digest for the same week) ----------------------------------

EARLIER = f"SC Advance Sheet Digest {EM} November 12, 2026 {EM} 3 opinions"


def test_supplement_is_labeled_and_never_denies_the_earlier_issue():
    md = render([moore()], supplement_to=EARLIER)
    subject = build_subject(make_input([moore()], supplement_to=EARLIER))
    assert subject == f"SC Advance Sheet Digest {EM} November 12, 2026 {EM} 1 opinion (supplement)"
    assert md.splitlines()[2] == "**SC Advance Sheet Digest** · November 12, 2026 · Advance Sheet No. 41 · Supplement"
    assert f"Supplement to this week's earlier issue (“{EARLIER}”): 1 published Court of Appeals opinion." in md
    # Nothing that would contradict the earlier issue, which had the Supreme Court opinions.
    assert "no published Supreme Court opinions" not in md
    assert "## Supreme Court\n" not in md
    assert NO_NEW_OPINIONS not in md
    rule = section(md, "Rule changes this week")
    assert (
        "_No opinion in this supplement changes or clarifies a rule; this week's earlier issue covers the rest of "
        "the week._"
    ) in rule
    assert "No opinion this week" not in rule


def test_supplement_rule_changes_say_the_earlier_issue_lists_the_others():
    rule = section(render([fleming()], supplement_to=EARLIER), "Rule changes this week")
    assert "**Clarifies:**" in rule
    assert "_Only this supplement's items are listed here; this week's earlier issue covers the rest of the week._" in rule


def test_supplement_with_an_unclassified_item():
    rule = section(render([nettles(), failed(moore().item)], supplement_to=EARLIER), "Rule changes this week")
    assert "None of the summarized opinions in this supplement changes or clarifies a rule." in rule
    assert "Read it directly (links below). This week's earlier issue covers the rest of the week._" in rule


@pytest.mark.parametrize(
    ("previous", "suffix"),
    [(set(), " (supplement)"), ({"BASE (supplement)"}, " (supplement 2)"), ({"BASE"}, " (supplement)")],
)
def test_supplement_subject_suffixes(previous, suffix):
    base = f"SC Advance Sheet Digest {EM} November 12, 2026 {EM} 1 opinion"
    prev = frozenset(p.replace("BASE", base) for p in previous)
    assert build_subject(make_input([moore()], supplement_to=EARLIER, previous_subjects=prev)) == base + suffix


def test_overflow_note_uses_the_configured_cap():
    md = render([fleming()], overflow=[opinion("6171", "Overflow v. Case", COA)], cap=25)
    assert "This week had more items than the 25-summary limit; these are listed without summaries." in md


# --- entries ------------------------------------------------------------------------


def test_opinion_entry_layout():
    md = render([fleming()])
    expected = "\n".join(
        [
            "### Reid Fleming v. LG Chem Ltd.",
            "**Op. No. 28355** · Filed November 12, 2026 · Justice Hill · **Reversed**",
            "",
            "**Takeaway:** South Carolina may exercise specific personal jurisdiction over LG Chem in a vape "
            "battery case.",
            "",
            "**Holding:** Due process permits specific jurisdiction; relatedness under Ford does not require "
            "causation.",
            "",
            "**What changed:** Clarifies: Relatedness does not require proof that in-state conduct caused the "
            "injury.",
            "",
            "**Why it matters:** Plead the manufacturer's knowledge of in-state sales and prior in-state suits.",
            "",
            "_Practice areas: Civil Procedure, Torts_",
            "",
            f"[Read the full opinion (PDF)]({URL_28355}) · [CourtListener copy]({CL_28355})",
        ]
    )
    assert expected in md


def test_entries_are_sorted_and_separated_by_a_rule():
    sc = section(render(real_week()), "Supreme Court")
    assert sc.index("### In the Matter of Louis David Nettles") < sc.index("### Reid Fleming v. LG Chem Ltd.")
    assert "\n\n---\n\n### Reid Fleming v. LG Chem Ltd." in sc
    assert sc.count("\n---\n") == 1


def test_order_entry_uses_order_labels():
    orders = section(render(real_week()), "Supreme Court orders")
    assert f"### {md_escape(RULE_115)}\n**Order No. 2026-11-12-01** · Filed November 12, 2026" in orders
    assert f"[Read the order (PDF)]({ORDER_115_URL}) · [Advance sheet copy]({SHEET_41.pdf_url}#page=26)" in orders
    # The copying-fees order has no number and was signed November 4; it sorts first.
    assert f"### {FEES}\nFiled November 4, 2026 · Order of the Court · **Order issued**" in orders
    assert orders.index(FEES) < orders.index("Rule 1.15")
    assert "Op. No." not in orders and "Read the full opinion" not in orders


def test_entry_without_a_number_omits_op_no():
    md = render([ok(opinion(None, "Unnumbered v. Case", link_url=URL_28355))])
    assert "### Unnumbered v. Case\nFiled November 12, 2026 · Justice Hill · **Affirmed**" in md
    assert "Op. No." not in md


def test_entry_uses_the_models_opinion_number_only_when_it_looks_like_one():
    item = opinion(None, "Numbered v. Model", link_url=URL_28355)
    assert "**Op. No. 28361**" in render([ok(item, opinion_number="28361")])
    assert "Op. No." not in render([ok(item, opinion_number="unclear from the opinion")])


def test_mirror_link_is_omitted_when_missing_or_identical():
    no_mirror = opinion("28356", "Single v. Link", link_url=URL_28355)
    same = opinion("28357", "Same v. Link", link_url=URL_28354, mirror_url=URL_28354)
    md = render([ok(no_mirror), ok(same)])
    assert f"[Read the full opinion (PDF)]({URL_28355})\n" in md
    assert f"[Read the full opinion (PDF)]({URL_28354})\n" in md
    assert "copy](" not in md and "CourtListener](" not in md


@pytest.mark.parametrize(
    ("mirror", "label"),
    [
        (CL_28355, "CourtListener copy"),
        ("https://www.courtlistener.com/opinion/11060100/reid-fleming-v-lg-chem-ltd/", "View on CourtListener"),
        (f"{SHEET_41.pdf_url}#page=15", "Advance sheet copy"),
        ("https://example.org/copy.pdf", "Alternate copy"),
    ],
)
def test_mirror_link_labels_say_what_the_second_link_is(mirror, label):
    md = render([ok(opinion("28355", "Reid Fleming v. LG Chem Ltd.", link_url=URL_28355, mirror_url=mirror))])
    assert f" · [{label}]({mirror})" in md


def test_non_pdf_link_is_not_labeled_pdf():
    page = "https://www.courtlistener.com/opinion/11060100/reid-fleming-v-lg-chem-ltd/"
    md = render([ok(opinion("28355", "Reid Fleming v. LG Chem Ltd.", link_url=page))])
    assert f"[Read the full opinion]({page})" in md


def test_truncated_result_notes_the_share_of_text_used():
    md = render([ok(moore().item, truncated=True, kept_fraction=0.62)])
    assert "_Long opinion: this summary is based on about 62% of its text._" in md
    assert "Long opinion" not in render([moore()])


def test_refiled_opinion_is_marked():
    item = opinion("28340", "Refiled v. Case", refiled=True)
    md = render([ok(item)], overflow=[opinion("28341", "Also Refiled v. Case", refiled=True)])
    assert "**Op. No. 28340** · Filed November 12, 2026 (withdrawn, substituted, and refiled) · Justice Hill" in md
    assert "Also Refiled v. Case** · Supreme Court · Op. No. 28341 · Filed November 12, 2026 (withdrawn" in md


def test_failed_entry_text():
    item = opinion("6170", "Gary Moore v. State", COA, link_url=URL_6170_RAW, mirror_url=CL_6170)
    md = render([failed(item, "the AI service was unavailable")])
    expected = "\n".join(
        [
            "### Gary Moore v. State",
            "**Op. No. 6170** · Filed November 12, 2026",
            "",
            "_The AI summary isn't available this week (the AI service was unavailable). Please read the full "
            "opinion._",
            "",
            f"[Read the full opinion (PDF)]({URL_6170}) · [CourtListener copy]({CL_6170})",
        ]
    )
    assert expected in section(md, "Court of Appeals")
    assert "**Takeaway:**" not in md


def test_failed_entry_shows_header_hints_and_tidies_the_disposition():
    item = opinion("6170", "Gary Moore v. State", COA, author="Judge Konduros", disposition_hint="REVERSED")
    md = render([failed(item, "the AI response was not valid.")])
    assert "**Op. No. 6170** · Filed November 12, 2026 · Judge Konduros · **Reversed**" in md
    assert "(the AI response was not valid). Please read" in md


def test_failed_entry_without_an_error_message():
    md = render([failed(opinion("28362", "Quiet v. Failure"), error=None)])
    assert "_The AI summary isn't available this week (the summary could not be generated)." in md


def test_failed_order_points_to_the_order():
    md = render([failed(order(RULE_115, "2026-11-12-01"), "the AI service was unavailable")])
    assert "Please read the full order._" in md
    assert "[Read the order (PDF)](" in md


def test_summarized_status_without_a_summary_is_rendered_as_failed():
    broken = SummaryResult(item=opinion("28363", "Broken v. Result"), status=ResultStatus.SUMMARIZED, summary=None)
    inp = make_input([broken])
    assert "The AI summary isn't available this week" in render_markdown(inp, "s")
    assert digest_counts(inp)["failed"] == 1


def test_unclear_author_and_disposition_are_left_out_of_the_meta_line():
    result = ok(
        opinion("28364", "Vague v. Case"), author="unclear from the opinion", disposition="Unclear from the opinion."
    )
    md = render([result])
    assert "### Vague v. Case\n**Op. No. 28364** · Filed November 12, 2026\n" in md


def test_all_caps_disposition_is_tidied():
    md = render([ok(opinion("28365", "Caps v. Case"), disposition="AFFIRMED IN PART, REVERSED IN PART")])
    assert "**Affirmed in part, reversed in part**" in md


def test_practice_areas_given_as_a_string_or_empty():
    md = render([ok(opinion("28366", "String v. Areas"), practice_areas="Insurance")])
    assert "_Practice areas: Insurance_" in md
    md = render([ok(opinion("28367", "No v. Areas"), practice_areas=[])])
    assert "Practice areas" not in md


def test_malformed_urls_never_crash_rendering():
    item = opinion("28370", "Bad v. Url", link_url="https://[broken/a.pdf", mirror_url="https://[also-broken/")
    md = render([ok(item)], overflow=[opinion("28371", "Bad v. Overflow", link_url="https://[x/y.pdf")])
    assert "[Read the full opinion](https://[broken/a.pdf) · [Alternate copy](https://[also-broken/)" in md


def test_non_web_links_are_dropped():
    item = opinion("28368", "Odd v. Link", link_url="javascript:alert(1)", mirror_url=CL_28355)
    md = render([ok(item)], overflow=[opinion("28369", "Odd v. Overflow", link_url="ftp://x.example/a.pdf")])
    assert "javascript:" not in md and "ftp:" not in md
    assert f"_Practice areas: Civil Procedure_\n\n[CourtListener copy]({CL_28355})" in md
    assert "- **Odd v. Overflow** · Supreme Court · Op. No. 28369 · Filed November 12, 2026\n" in md


# --- court sections and overflow ----------------------------------------------------


def test_included_court_with_no_items_says_so():
    md = render([fleming(), nettles()])
    assert "## Court of Appeals\n\n_No new published opinions this week._\n" in md


def test_excluded_court_has_no_section():
    md = render([fleming()], courts_included=(SC,))
    assert "## Court of Appeals" not in md
    assert "Court of Appeals opinion" not in md  # not in the intro either
    md = render([moore()], courts_included=(COA,))
    assert "## Supreme Court\n" not in md and "## Supreme Court orders" not in md


def test_items_from_an_excluded_court_are_still_shown():
    md = render([fleming(), moore()], courts_included=(SC,))
    assert "## Court of Appeals" in md and "### Gary Moore v. State" in md


def test_orders_section_only_when_orders_exist():
    assert "## Supreme Court orders" not in render([fleming(), moore()])
    assert "## Supreme Court orders" in render([fleming(), rule_115_order()])


def test_overflow_section_lists_items_without_summaries():
    extra = [
        opinion("6173", "Linda M. Barfield v. S.C. Dep't of Motor Vehicles", COA),
        order(FEES, None, link_url=f"{SHEET_41.pdf_url}#page=1"),
        opinion("6172", "Harbor Pointe v. Kiawah Shoals, LLC", COA, link_url=URL_6170_RAW),
    ]
    md = render(real_week()[:4], overflow=extra)
    over = section(md, "Also filed this week (not summarized)")
    assert "This week had more items than the 4-summary limit; these are listed without summaries." in over
    lines = [line for line in over.splitlines() if line.startswith("- ")]
    assert lines == [
        f"- **{FEES}** · Supreme Court order · Filed November 12, 2026 · [PDF]({SHEET_41.pdf_url}#page=1)",
        f"- **Harbor Pointe v. Kiawah Shoals, LLC** · Court of Appeals · Op. No. 6172 · Filed November 12, 2026 · "
        f"[PDF]({URL_6170})",
        "- **Linda M. Barfield v. S.C. Dep't of Motor Vehicles** · Court of Appeals · Op. No. 6173 · Filed "
        "November 12, 2026 · [PDF](https://www.sccourts.org/media/opinions/HTMLFiles/COA/6173.pdf)",
    ]
    assert "**Takeaway:**" not in over
    assert "(3 are listed at the end without summaries)." in md
    coa = section(md, "Court of Appeals")
    assert "_2 more opinions from this court are listed at the end without summaries._" in coa
    assert "_1 more order is listed at the end without a summary._" in section(md, "Supreme Court orders")


def test_court_with_only_overflow_items_points_to_the_list():
    md = render([fleming()], overflow=[opinion("6171", "Overflow v. Case", COA)])
    coa = section(md, "Court of Appeals")
    assert "_This court's new opinions are listed at the end without summaries._" in coa
    assert "No new published opinions" not in coa


def test_no_overflow_section_without_overflow():
    assert "Also filed this week" not in render(real_week())


# --- escaping in the rendered digest ------------------------------------------------


def test_dynamic_text_is_escaped_everywhere():
    caption = "Reid Fleming v. The Planet Vape, LLC; John Doe Distributor #1; and John Doe Distributor #2"
    result = ok(
        opinion("28355", caption, link_url=URL_28355),
        one_line="Uses *emphasis* and snake_case_words.",
        holding="Mentions {{ subscriber.email }} and {% if x %} tags.",
        what_changed="Clarifies: [brackets] and a | pipe.",
        change_type="clarifies",
        practice_areas=["Torts_Area"],
        practice_tip="# Not a heading",
        author="Justice *Hill*",
    )
    md = render([result])
    assert f"### {caption}\n" in md  # '#1' and '#2' stay as written
    assert r"**Takeaway:** Uses \*emphasis\* and snake\_case\_words." in md
    assert "{ { subscriber.email }}" in md and "{ % if x %}" in md
    assert "{{" not in md and "{%" not in md
    assert r"**What changed:** Clarifies: \[brackets\] and a \| pipe." in md
    assert r"_Practice areas: Torts\_Area_" in md
    assert r"**Why it matters:** \# Not a heading" in md
    assert r"Justice \*Hill\*" in md
    assert f"- **{caption}** (Supreme Court) — **Clarifies:** \\[brackets\\] and a \\| pipe." in md


def test_escaped_text_renders_cleanly_in_html():
    caption = "Reid Fleming v. The Planet Vape, LLC; John Doe Distributor #1"
    result = ok(
        opinion("28355", caption, link_url=URL_28355),
        one_line="Uses *emphasis*, snake_case, [brackets], a | pipe, and a < sign.",
        holding="Mentions {{ subscriber.email }}.",
    )
    page = render_html(render([result]), "s")
    assert f"<h3>{caption}</h3>" in page
    assert "Uses *emphasis*, snake_case, [brackets], a | pipe, and a &lt; sign." in page
    assert "{ { subscriber.email }}" in page and "{{" not in page
    body = page[page.index('<main class="email">') :]
    assert "\\" not in body  # no stray backslashes from escaping


def test_url_with_a_space_is_percent_encoded():
    md = render([moore()])
    assert f"[Read the full opinion (PDF)]({URL_6170})" in md
    assert "6170 .pdf" not in md
    page = render_html(md, "s")
    assert f'href="{URL_6170}"' in page


# --- footer and banner --------------------------------------------------------------


def test_footer_text_courtlistener():
    md = render(real_week(), source="courtlistener-api")
    footer = md[md.rindex("\n---\n") + 1 :]
    assert footer == "\n\n".join(
        [
            "---",
            FOOTER_FIRST,
            "Summaries are written by an AI model (claude-sonnet-5-5) from the text of each opinion and may "
            "contain errors or omissions. Opinions can be revised, withdrawn, or reheard.",
            "Opinions: South Carolina Judicial Branch, retrieved via CourtListener (Free Law Project).",
            "You can unsubscribe at any time with the link at the bottom of this email.",
        ]
    ) + "\n"
    assert FOOTER_FIRST == f"**{DISCLAIMER}**"
    feed = render([], source="courtlistener-feed")
    assert "Opinions: South Carolina Judicial Branch, retrieved via CourtListener" in feed


def test_footer_sources_line_sccourts():
    md = render(real_week(), source="sccourts")
    assert "\n\nOpinions and orders: South Carolina Judicial Branch (sccourts.org).\n\n" in md
    assert "CourtListener (Free Law Project)" not in md


def test_footer_names_a_fallback_model_that_served_a_summary():
    md = render([ok(fleming().item, model="claude-sonnet-5"), ok(nettles().item, model="claude-sonnet-5-5")])
    assert "an AI model (claude-sonnet-5-5; some summaries by claude-sonnet-5) from the text" in md


def test_mock_banner_only_for_mock_runs():
    md = render([fleming()], mock=True)
    assert f"\n\n**{MOCK_BANNER}**\n\n" in md
    assert MOCK_BANNER == "⚠️ Preview with MOCK summaries — no AI calls were made."
    assert MOCK_BANNER not in render([fleming()])
    # A mock result always gets the banner, even if the caller forgot to set mock=True.
    assert MOCK_BANNER in render([ok(fleming().item, mock=True)])


# --- Markdown hygiene ---------------------------------------------------------------


def test_markdown_hygiene():
    extra = [opinion("6172", "Harbor Pointe v. Kiawah Shoals, LLC", COA)]
    md = render(real_week() + [failed(opinion("6171", "The State v. Example", COA))], overflow=extra,
                mock=True, advance_sheets=[SHEET_41], source="sccourts")
    lines = md.split("\n")
    assert all(line == line.rstrip() for line in lines), "no trailing whitespace (it would make hard line breaks)"
    assert "\n\n\n" not in md
    for i, line in enumerate(lines):
        if line == "---":  # thematic break: blank lines around it, or Markdown reads a setext heading
            assert lines[i - 1] == "" and lines[i + 1] == ""
        if line.startswith("#"):
            assert lines[i - 1] == "", f"heading not preceded by a blank line: {line!r}"


def test_rendering_is_deterministic():
    inp = make_input(real_week(), advance_sheets=[SHEET_41], source="sccourts")
    assert render_markdown(inp, "s") == render_markdown(inp, "s")
    assert build_digest(inp) == build_digest(inp)


def test_empty_digest_still_renders():
    md = render([], [])
    assert "New this week: no published Supreme Court opinions and no published Court of Appeals opinions." in md
    assert md.count("_No new published opinions this week._") == 2
    assert FOOTER_FIRST in md


# --- HTML preview -------------------------------------------------------------------


def test_html_is_a_full_document_with_the_links():
    digest = build_digest(make_input(real_week(), advance_sheets=[SHEET_41], source="courtlistener-api"))
    page = digest.html
    assert page.startswith("<!DOCTYPE html>\n<html lang=\"en\">")
    assert page.rstrip().endswith("</html>")
    for tag in ('<meta charset="utf-8">', "<head>", "</head>", "<body>", "</body>", "<style>", "max-width: 680px"):
        assert tag in page
    assert (
        "Preview of the email body. Buttondown wraps it in your newsletter template and adds the unsubscribe link."
        in page
    )
    for url in (URL_28354, URL_28355, URL_6170, CL_28355, ORDER_115_URL, SHEET_41.pdf_url):
        assert f'href="{url}' in page
    assert "<h2>Rule changes this week</h2>" in page
    assert "<h3>Reid Fleming v. LG Chem Ltd.</h3>" in page
    assert "<hr />" in page
    assert f"<strong>{DISCLAIMER}</strong>" in page


def test_html_title_and_subject_are_escaped():
    page = render_html("Body", "Digest <b>&</b> more")
    assert "<title>Digest &lt;b&gt;&amp;&lt;/b&gt; more</title>" in page
    assert "<b>&</b>" not in page


def test_html_links_open_in_a_new_tab():
    page = render_html(render(real_week()), "s")
    anchors = re.findall(r"<a [^>]*>", page)
    assert anchors
    for anchor in anchors:
        assert 'target="_blank"' in anchor and 'rel="noopener noreferrer"' in anchor


# --- counts and build_digest --------------------------------------------------------


def test_counts():
    extra = [opinion("6172", "Harbor Pointe v. Kiawah Shoals, LLC", COA), order(FEES)]
    inp = make_input(real_week() + [failed(opinion("6171", "The State v. Example", COA))], overflow=extra)
    assert digest_counts(inp) == {
        "opinions": 5,  # 28354, 28355, 6170, 6171 (failed), 6172 (overflow)
        "orders": 3,  # Rule 1.15, fees, fees again in overflow
        "summarized": 5,
        "failed": 1,
        "overflow": 2,
        "rule_changes": 2,  # Rule 1.15 order (changes) and Fleming (clarifies)
    }


def test_build_digest_returns_subject_markdown_html_and_counts():
    inp = make_input(real_week(), advance_sheets=[SHEET_41], source="sccourts")
    digest = build_digest(inp)
    assert isinstance(digest, Digest)
    assert digest.subject == build_subject(inp)
    assert digest.markdown == render_markdown(inp, digest.subject)
    assert digest.html == render_html(digest.markdown, digest.subject)
    assert digest.counts == digest_counts(inp)
    assert set(digest.counts) == {"opinions", "orders", "summarized", "failed", "overflow", "rule_changes"}


def test_digest_input_defaults():
    required = {"digest_date": FILED, "model": "m", "courts_included": (SC,), "orders_included": False}
    inp = DigestInput(results=[], overflow=[], **required)
    assert inp.advance_sheets == [] and inp.mock is False
    assert inp.previous_subjects == frozenset() and inp.source == "courtlistener-api"
    other = DigestInput(results=[], overflow=[], **required)
    assert inp.advance_sheets is not other.advance_sheets  # no shared mutable default
