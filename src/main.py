"""Run the weekly digest: ``python -m src.main [--dry-run] [--mock-llm] ...``.

Pipeline: find new opinions (src/scrape.py) -> download each PDF and extract its text ->
read the opinion header -> shorten very long texts -> summarize (Claude, or the offline
mock) -> build the digest -> create a Buttondown DRAFT -> record what was processed in
data/processed.json.

Exit codes:
    0  success, or nothing new to send
    1  failure: nothing was emailed. data/processed.json is unchanged, except that a run in
       which every summary failed records the failed attempts (see below)
    2  partial: the draft was created and data/processed.json saved, but a later step
       (the test send, or sending it with AUTO_SEND) failed

Safeguards (each is covered by tests/test_main.py):
- A dry run never emails and never writes the state file.
- --mock-llm never calls the API and needs no key. It always runs as a dry run, so
  placeholder summaries can never reach subscribers or the state file.
- The email is created as a DRAFT. Only AUTO_SEND=true sends it, with a separate request
  made after the draft exists.
- Items already recorded in data/processed.json are never summarized again. The first
  run records the older items it leaves out, so the next run does not email them either.
- At most MAX_OPINIONS_PER_RUN items are summarized; the rest are listed by title.
- Each item gets at most one retry (src/summarize.py), and at most MAX_ATTEMPTS_PER_ITEM
  runs: a run in which every summary failed sends nothing but counts the attempts of
  failures that would repeat (not outages), and an item that runs out of attempts is
  listed, without a summary, in the next digest. An API error that would hit every item
  (bad key, spend limit, no credits) stops the run before any email is created.
- A second digest for the same week (a late posting, or a summary retried on Thursday)
  is labeled a supplement and never claims to describe the whole week.
- Token usage is logged for every request, and the run ends with a cost summary.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, field, replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import __version__
from .config import (
    ConfigError,
    Settings,
    effort_label,
    load_settings,
    model_pricing,
    model_profile,
    parse_bool,
    user_agent,
)
from .digest import Digest, DigestInput, build_digest, md_escape
from .extract import (
    MIN_USEFUL_CHARS,
    ExtractionError,
    estimate_tokens,
    extract_text,
    parse_opinion_header,
    prepare_for_model,
)
from .logutil import group, notice, set_output, setup_logging, step_summary
from .models import Court, Item, ItemKind, OpinionHeader, ResultStatus, ScrapeResult, SummaryResult, Usage
from .net import FetchError, PoliteSession
from .scrape import enabled_courts, find_items
from .send import DASHBOARD_URL, ButtondownClient, ButtondownError, CreatedEmail
from .sources import SourceError
from .state import State, StateError
from .summarize import (
    ERR_INCOMPLETE,
    ERR_INVALID,
    ERR_REFUSED,
    ERR_TOO_LONG,
    PROMPT_VERSION,
    SYSTEM_PROMPT,
    ClaudeSummarizer,
    FatalAPIError,
    MockSummarizer,
    Summarizer,
    SummaryCache,
    build_user_message,
    estimate_cost,
)

# Not __name__: under "python -m src.main" that is "__main__".
log = logging.getLogger("src.main")

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_PARTIAL = 2

TIMEZONE = "America/New_York"  # "today" is the courts' calendar day
FIRST_RUN_WINDOW_DAYS = 5  # first run: only items within this many days of the newest one
MOCK_OUTPUT_TOKENS_PER_ITEM = 1500  # mock mode's assumption for one summary's output tokens

NO_ANTHROPIC_KEY = (
    "ANTHROPIC_API_KEY is not set. Real summaries run in GitHub Actions, where the key is a repository "
    "secret. To test locally without the API, add --mock-llm."
)
NO_BUTTONDOWN_KEY = (
    "BUTTONDOWN_API_KEY is not set, so the digest cannot be created in Buttondown. Add it as a repository "
    "secret, or use --dry-run (DRY_RUN=true) to build the digest without emailing."
)
NOTHING_NEW = "No new opinions or orders since the last run. Nothing to send."
ERR_PDF_UNREADABLE = "the opinion PDF could not be read"
ERR_PDF_DOWNLOAD = "the opinion PDF could not be downloaded"  # a network or server error: may pass
ERR_PDF_PARTIAL = "part of the opinion PDF could not be read"
ERR_NO_TEXT = "no readable text (scanned PDF?)"
ERR_EARLIER_FAILURE = "the summary failed in earlier runs"
PRICES_NOTE = "verify against current Anthropic pricing"

# Failures that would happen again for the same document, as opposed to an outage. A run in
# which every summary failed still counts these attempts, so MAX_ATTEMPTS_PER_ITEM bounds them.
DOCUMENT_ERRORS = frozenset(
    {ERR_PDF_UNREADABLE, ERR_PDF_PARTIAL, ERR_NO_TEXT, ERR_REFUSED, ERR_INVALID, ERR_INCOMPLETE, ERR_TOO_LONG}
)


# --------------------------------------------------------------------------- command line


class _Parser(argparse.ArgumentParser):
    """Usage errors exit with 1 (exit code 2 means "partial success" for this program)."""

    def error(self, message: str) -> None:  # type: ignore[override]
        self.print_usage(sys.stderr)
        self.exit(EXIT_FAILED, f"{self.prog}: error: {message}\n")


def _iso_date(value: str) -> date:
    try:
        return date.fromisoformat(value.strip())
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected a date like 2026-11-16, got {value!r}") from None


def _int_between(lo: int, hi: int) -> Callable[[str], int]:
    def parse(value: str) -> int:
        try:
            n = int(value.strip())
        except ValueError:
            raise argparse.ArgumentTypeError(f"expected a whole number, got {value!r}") from None
        if not lo <= n <= hi:
            raise argparse.ArgumentTypeError(f"must be between {lo} and {hi}, got {n}")
        return n

    return parse


def build_parser() -> argparse.ArgumentParser:
    parser = _Parser(
        prog="python -m src.main",
        description=(
            "Build the SC Advance Sheet Digest: find newly published South Carolina Supreme\n"
            "Court and Court of Appeals opinions, summarize each one, and create the email as a\n"
            "Buttondown draft (sent only when AUTO_SEND=true). Settings come from environment\n"
            "variables; see .env.example."
        ),
        epilog=(
            "examples:\n"
            "  python -m src.main --dry-run --mock-llm     local preview: no API key, no email\n"
            "  python -m src.main --dry-run --mock-llm --today 2026-11-16 --lookback-days 14\n"
            "\n"
            "exit codes: 0 success or nothing new; 1 failure (nothing emailed; the state file is\n"
            "            unchanged, except that a run in which every summary failed records the attempts);\n"
            "            2 draft created and state saved, but the test send or AUTO_SEND failed"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="build the digest in the output directory, but send no email and leave the state file "
        "unchanged (same as DRY_RUN=true)",
    )
    parser.add_argument(
        "--mock-llm",
        action="store_true",
        help="use placeholder summaries instead of the Claude API; needs no API key and always runs as "
        "a dry run (same as MOCK_LLM=true)",
    )
    parser.add_argument(
        "--today",
        type=_iso_date,
        metavar="YYYY-MM-DD",
        help=f"treat this date as today (default: today in {TIMEZONE})",
    )
    parser.add_argument(
        "--lookback-days",
        type=_int_between(1, 60),
        metavar="N",
        help="look for items filed in the last N days, 1-60 (default: LOOKBACK_DAYS, or 10)",
    )
    parser.add_argument(
        "--max-opinions",
        type=_int_between(1, 200),
        metavar="N",
        help="summarize at most N items this run, 1-200 (default: MAX_OPINIONS_PER_RUN, or 25)",
    )
    parser.add_argument(
        "--source",
        choices=("courtlistener", "sccourts"),
        help="where opinions come from (default: SOURCE, or courtlistener)",
    )
    parser.add_argument(
        "--output-dir",
        metavar="PATH",
        help="where digest.md, digest.html, digest.json, and run_summary.json are written "
        "(default: OUTPUT_DIR, or output)",
    )
    parser.add_argument(
        "--state-file",
        metavar="PATH",
        help="the record of processed items (default: STATE_FILE, or data/processed.json)",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="log debugging details")
    return parser


# --------------------------------------------------------------------------- outside world


def _default_session(settings: Settings) -> PoliteSession:
    return PoliteSession(user_agent(settings), min_interval=settings.request_delay_seconds)


def _default_summarizer(settings: Settings, mock: bool) -> Summarizer:
    if mock:
        return MockSummarizer(settings)
    cache = SummaryCache(settings.summary_cache_dir, settings.claude_model, settings.claude_effort)
    return ClaudeSummarizer(settings, cache=cache)


def _default_buttondown(settings: Settings) -> ButtondownClient:
    return ButtondownClient(settings.buttondown_api_key, user_agent(settings))


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class Services:
    """Factories for everything that talks to the outside world. Tests replace them with fakes."""

    make_session: Callable[[Settings], PoliteSession] = _default_session
    make_summarizer: Callable[[Settings, bool], Summarizer] = _default_summarizer
    make_buttondown: Callable[[Settings], ButtondownClient] = _default_buttondown
    now: Callable[[], datetime] = _utc_now


# --------------------------------------------------------------------------- helpers


def pipeline_order(item: Item) -> tuple:
    """Supreme Court opinions, then Supreme Court orders, then Court of Appeals opinions; each by date and number."""
    if item.kind is ItemKind.ORDER:
        bucket = 1
    else:
        bucket = 0 if item.court is Court.SUPREME else 2
    number = (item.number or "").strip()
    number_key = (0, int(number), "") if number.isdigit() else (1, 0, number)
    return (bucket, item.filed_date, number_key, (item.case_name or "").casefold())


def week_date(item: Item, sheet_dates: dict[int, date]) -> date:
    """The date an item counts as published: its filing date, or its advance sheet's date when later.

    An order signed on November 4 but printed in the November 12 advance sheet belongs to
    the week of November 12.
    """
    sheet_date = sheet_dates.get(item.advance_sheet) if item.advance_sheet is not None else None
    return max(item.filed_date, sheet_date) if sheet_date else item.filed_date


def most_recent_week(items: list[Item], sheet_dates: dict[int, date]) -> list[Item]:
    """Items within FIRST_RUN_WINDOW_DAYS of the newest one (the first run's "most recent week")."""
    if not items:
        return []
    cutoff = max(week_date(item, sheet_dates) for item in items) - timedelta(days=FIRST_RUN_WINDOW_DAYS)
    return [item for item in items if week_date(item, sheet_dates) >= cutoff]


def _fmt_date(d: date) -> str:
    return f"{d:%B} {d.day}, {d.year}"


def _fmt_cost(cost: float | None) -> str:
    return "unknown" if cost is None else f"${cost:.4f}"


def _fmt_int(n: int) -> str:
    return f"{n:,}"


def _text_filename(key: str) -> str:
    """'SC:28355' -> 'SC-28355.txt' (any other character Windows forbids becomes '-' too)."""
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]', "-", key) + ".txt"


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def _write_json(path: Path, data: Any) -> None:
    _write_text(path, json.dumps(data, indent=2, ensure_ascii=False, default=str) + "\n")


def _failed(item: Item, error: str) -> SummaryResult:
    return SummaryResult(item=item, status=ResultStatus.FAILED, error=error)


_DATED_KEY_RE = re.compile(r"\d{4}-\d{2}-\d{2}:.+")  # "2026-11-12:slug": a key without a number


def item_from_record(key: str, record: dict[str, Any]) -> Item | None:
    """Rebuild an item from its data/processed.json record (None if the record cannot be trusted).

    Used to list an earlier failure that no digest has listed yet, even after it has left
    the lookback window. The rebuilt item must have exactly the record's key.
    """
    try:
        kind = ItemKind(str(record.get("kind") or "opinion"))
        court = Court(str(record.get("court") or ""))
        filed = date.fromisoformat(str(record.get("filed_date") or "")[:10])
    except ValueError:
        return None
    case_name = str(record.get("case_name") or "").strip()
    link_url = str(record.get("link_url") or "").strip()
    if not case_name or not link_url:
        return None
    prefix, _, rest = key.partition(":")
    number = source_id = None
    if prefix == "CL":
        source_id = rest
    elif rest and not _DATED_KEY_RE.fullmatch(rest):
        number = rest
    item = Item(
        kind=kind,
        court=court,
        case_name=case_name,
        filed_date=filed,
        number=number,
        pdf_url=None,
        link_url=link_url,
        source=str(record.get("source") or ""),
        source_id=source_id,
    )
    return item if item.key == key else None


def _item_json(item: Item) -> dict[str, Any]:
    return {
        "key": item.key,
        "alias_keys": item.alias_keys,
        "kind": item.kind.value,
        "court": item.court.value,
        "case_name": item.case_name,
        "number": item.number,
        "filed_date": item.filed_date.isoformat(),
        "author": item.author,
        "disposition_hint": item.disposition_hint,
        "advance_sheet": item.advance_sheet,
        "refiled": item.refiled,
        "link_url": item.link_url,
        "mirror_url": item.mirror_url,
        "pdf_url": item.pdf_url,
        "source": item.source,
    }


def _result_json(result: SummaryResult) -> dict[str, Any]:
    data = _item_json(result.item)
    data.update(
        status="summarized" if result.ok else "failed",
        error=None if result.ok else result.error,
        summary=result.summary.to_dict() if result.ok else None,
        model=result.model,
        from_cache=result.from_cache,
        mock=result.mock,
        truncated=result.truncated,
        kept_fraction=result.kept_fraction,
        usage=asdict(result.usage),
    )
    return data


# --------------------------------------------------------------------------- the run


@dataclass
class _Report:
    """Facts gathered during the run, for the end-of-run summary."""

    dry_run: bool
    mock: bool
    today: date | None = None
    source: str | None = None
    found: int = 0
    already_processed: int = 0  # including items recognized only after their PDF header was read
    first_run_left_out: int = 0
    duplicates: int = 0
    new: int = 0
    has_new: bool | None = None
    results: list[SummaryResult] = field(default_factory=list)
    overflow: list[Item] = field(default_factory=list)
    owed: list[SummaryResult] = field(default_factory=list)  # earlier failures listed now, without a new attempt
    supplement_to: str | None = None  # subject of this week's earlier digest, when this one supplements it
    usage: Usage = field(default_factory=Usage)
    served_models: set[str] = field(default_factory=set)
    request_input_tokens: int = 0  # estimated input of every request a real run would send
    request_items: int = 0
    warnings: list[str] = field(default_factory=list)
    subject: str | None = None
    email: CreatedEmail | None = None
    sent: bool = False
    state_saved: bool = False
    outcome: str = ""
    exit_code: int = EXIT_OK


class _Run:
    """One pipeline run. ``execute()`` returns the exit code."""

    def __init__(self, settings: Settings, *, dry_run: bool, mock: bool, today: date, services: Services):
        self.settings = settings
        self.dry_run = dry_run
        self.mock = mock
        self.today = today
        self.services = services
        self.report = _Report(dry_run=dry_run, mock=mock, today=today)
        self.out_dir = Path(settings.output_dir)
        self.session: PoliteSession | None = None
        self.summarizer: Summarizer | None = None
        self.state: State | None = None
        self.seen_keys: set[str] = set()
        self.first_run = False  # data/processed.json had no items when the run started
        self.first_run_left_out: list[Item] = []  # recorded as skipped once the first digest exists
        self.owed_keys: set[str] = set()  # earlier failures this digest lists without a new attempt

    # ----------------------------------------------------------------- steps

    def execute(self) -> int:
        settings = self.settings
        # Missing keys are checked before anything is fetched, so a misconfigured run fails fast.
        if not self.mock and not settings.anthropic_api_key_present:
            return self._fail(NO_ANTHROPIC_KEY)
        if not self.dry_run and not settings.buttondown_api_key:
            return self._fail(NO_BUTTONDOWN_KEY)
        try:
            self.state = State.load(settings.state_file)
        except StateError as exc:
            return self._fail(str(exc))
        self.first_run = self.state.is_empty

        self.session = self.services.make_session(settings)
        problem: str | None = None
        with group("Finding opinions"):
            try:
                found = find_items(settings, self.session, self.today)
            except (SourceError, ConfigError) as exc:
                problem = str(exc)
        if problem is not None:
            return self._fail(problem)
        self.report.source = found.source
        self.report.found = len(found.items)
        self.report.warnings.extend(found.warnings)

        with group("Choosing what to summarize"):
            ordered = self._choose(found)
        if not ordered:
            return self._nothing_new()
        self.report.has_new = True

        self.summarizer = self.services.make_summarizer(settings, self.mock)
        cap = settings.max_opinions_per_run
        results: list[SummaryResult] = []
        fatal: FatalAPIError | None = None
        position = 0
        # Items are processed in order until `cap` of them have a result. One that turns out to be
        # already processed (or a duplicate) once its PDF header is read frees its slot for the next.
        while position < len(ordered) and len(results) < cap:
            item = ordered[position]
            position += 1
            with group(f"Item {position} of {len(ordered)}: {item.label}"):
                try:
                    result = self._process(item)
                except FatalAPIError as exc:
                    fatal = exc
                    result = None
            if fatal is not None:
                self.report.usage = self.report.usage + (fatal.usage or Usage())
                break
            if result is None:
                continue
            results.append(result)
            self.report.usage = self.report.usage + result.usage
            if result.ok and result.model and not result.mock:
                self.report.served_models.add(result.model)
        overflow = [] if fatal is not None else ordered[position:]
        self.report.results = results
        self.report.overflow = overflow
        if fatal is not None:
            return self._fail(f"{fatal} Nothing was emailed and {settings.state_file} is unchanged.")
        if overflow:
            log.warning(
                "%d new items exceed MAX_OPINIONS_PER_RUN=%d: %d were summarized (or attempted); the other %d are "
                "listed in the digest by title, without summaries: %s",
                len(results) + len(overflow),
                cap,
                len(results),
                len(overflow),
                "; ".join(item.label for item in overflow),
            )

        if not results and not overflow:  # every item turned out to be already processed
            return self._nothing_new()
        if results and not any(r.ok for r in results):
            return self._all_failed(results, overflow)

        owed = self._owed_results(results, overflow)
        self.report.owed = owed
        results = results + owed
        digest = self._build_digest(found, results, overflow)
        self._write_outputs(digest, results, overflow)
        with group("Digest (Markdown)"):
            print(digest.markdown, flush=True)

        if self.dry_run:
            log.info("Dry run: not emailing and not updating %s", settings.state_file)
            self.report.outcome = f"Dry run: digest written to {self.out_dir.as_posix()}/ (no email sent)"
            return self._finish(EXIT_OK)
        return self._deliver(digest, results, overflow)

    def _only_retries(self, results: list[SummaryResult]) -> bool:
        """True when every result is a retry of an item an earlier digest already listed as failed.

        "Listed" means the record names the digest's email; a refiled version never was.
        """
        assert self.state is not None
        return all(self.state.is_listed(r.item) for r in results)

    def _all_failed(self, results: list[SummaryResult], overflow: list[Item]) -> int:
        """Every summary failed: no email. A real run still counts the attempts that would repeat."""
        message = f"All {len(results)} summaries failed; not creating an email."
        if self.dry_run:
            return self._fail(message)
        if not overflow and self._only_retries(results):
            return self._record_failed_retries(results)
        return self._record_failed_run(results, message)

    def _record_failed_run(self, results: list[SummaryResult], message: str) -> int:
        """Record the attempts of an all-failed run (no email), then fail the run (exit 1).

        Counted: retries of items a digest already listed, and failures that would repeat for
        the same document (DOCUMENT_ERRORS). An outage ("the AI service was unavailable") is
        not counted, so it never uses up an item's attempts. Without this, a lone opinion
        whose summary keeps failing would be paid for on every run and never emailed. An item
        that runs out of attempts this way is listed, without a summary, in the next digest.
        A first run records nothing, so the next run is again a first run.
        """
        assert self.state is not None
        settings = self.settings
        counted = [r for r in results if self.state.is_listed(r.item) or r.error in DOCUMENT_ERRORS]
        if self.first_run or not counted:
            return self._fail(message)
        now = self.services.now()
        for result in counted:
            self.state.record_result(result, digest_subject=None, email_id=None, now=now)
        try:
            self.state.save()
        except OSError as exc:
            log.warning("Could not record the failed attempts in %s (%s).", settings.state_file, exc)
            return self._fail(message)
        self.report.state_saved = True
        set_output("state_changed", "true")
        for result in counted:
            log.warning(
                "%s: failed (%s); attempt %d of %d recorded in %s.",
                result.item.label,
                result.error,
                self.state.attempts(result.item),
                settings.max_attempts_per_item,
                settings.state_file,
            )
        return self._fail(
            f"{message} The failed attempts were recorded in {settings.state_file}: an opinion is tried in at most "
            f"{settings.max_attempts_per_item} runs, then listed without a summary in the next digest."
        )

    def _owed_results(self, results: list[SummaryResult], overflow: list[Item]) -> list[SummaryResult]:
        """Earlier failures no digest has listed and no run will retry: this digest lists them, without a summary."""
        assert self.state is not None
        settings = self.settings
        taken = set(self.seen_keys) | {it.key for it in overflow}
        for item in [r.item for r in results] + overflow:
            taken.add(item.key)
            taken.update(item.alias_keys)
        courts = set(enabled_courts(settings))
        owed: list[SummaryResult] = []
        for key, record in self.state.unlisted_failures(settings.max_attempts_per_item):
            if key in taken:
                continue
            item = item_from_record(key, record)
            if item is None:
                log.warning("%s failed in earlier runs, but its record in %s is incomplete, so it is not listed.",
                            key, settings.state_file)  # fmt: skip
                continue
            if (item.kind is ItemKind.OPINION and item.court not in courts) or (
                item.kind is ItemKind.ORDER and not settings.include_orders
            ):
                continue
            owed.append(_failed(item, str(record.get("error") or ERR_EARLIER_FAILURE)))
            self.owed_keys.add(item.key)
        if owed:
            log.info(
                "%d item(s) whose summaries failed in earlier runs that created no email are listed in this "
                "digest without a summary: %s",
                len(owed),
                "; ".join(r.item.label for r in owed),
            )
        return owed

    def _record_failed_retries(self, results: list[SummaryResult]) -> int:
        """A run whose only work was retrying earlier failures, and they failed again.

        There is nothing new to email (the earlier digest already listed these items with a
        link), so no email is created. The attempts ARE recorded, so an item that keeps failing
        stops being retried after MAX_ATTEMPTS_PER_ITEM instead of failing every run.
        """
        assert self.state is not None
        now = self.services.now()
        for result in results:
            self.state.record_result(result, digest_subject=None, email_id=None, now=now)
        try:
            self.state.save()
        except OSError as exc:
            return self._fail(f"The retried summaries failed again, and {self.settings.state_file} could not be saved ({exc}).")
        self.report.state_saved = True
        set_output("state_changed", "true")
        for result in results:
            done = self.state.attempts(result.item)
            if done >= self.settings.max_attempts_per_item:
                log.warning(
                    "%s: the retried summary failed again (%s); giving up after %d attempts. An earlier digest "
                    "already listed it with a link to the full opinion.",
                    result.item.label,
                    result.error,
                    done,
                )
            else:
                log.warning(
                    "%s: the retried summary failed again (%s); attempt %d of %d recorded.",
                    result.item.label,
                    result.error,
                    done,
                    self.settings.max_attempts_per_item,
                )
        self.report.outcome = (
            "Retried earlier failed summaries; they failed again. No email (nothing new); attempts recorded."
        )
        return self._finish(EXIT_OK)

    def _nothing_new(self) -> int:
        log.info("%s", NOTHING_NEW)
        self.report.has_new = False
        self.report.outcome = NOTHING_NEW
        return self._finish(EXIT_OK)

    def _choose(self, found: ScrapeResult) -> list[Item]:
        """The new items, in pipeline order. The run summarizes them until MAX_OPINIONS_PER_RUN have a result."""
        state, settings = self.state, self.settings
        assert state is not None
        candidates = [item for item in found.items if state.is_new(item, settings.max_attempts_per_item)]
        for item in candidates:
            if state.is_refiled(item):
                item.refiled = True  # the email says so: it replaces an opinion readers may have seen
                log.info("%s was filed again on %s (withdrawn, substituted, and refiled).", item.label,
                         _fmt_date(item.filed_date))  # fmt: skip
        self.report.already_processed = len(found.items) - len(candidates)
        log.info(
            "%d item(s) found, %d already processed, %d new.",
            len(found.items),
            self.report.already_processed,
            len(candidates),
        )
        if state.is_empty and candidates:
            sheet_dates = {sheet.number: sheet.date for sheet in found.advance_sheets}
            recent = most_recent_week(candidates, sheet_dates)
            newest = max(week_date(item, sheet_dates) for item in candidates)
            left_out = [item for item in candidates if not any(item is kept for kept in recent)]
            log.info(
                "First run (%s has no items yet): keeping only the most recent week, the items within %d days "
                "of %s; %d older item(s) left out.",
                settings.state_file,
                FIRST_RUN_WINDOW_DAYS,
                _fmt_date(newest),
                len(left_out),
            )
            for item in left_out:
                log.info("  left out: %s (filed %s)", item.label, _fmt_date(item.filed_date))
            if left_out:
                log.info(
                    "If this run creates the digest, the left-out item(s) are recorded in %s as skipped, so later "
                    "runs do not email them either.",
                    settings.state_file,
                )
            self.report.first_run_left_out = len(left_out)
            self.first_run_left_out = left_out
            candidates = recent
        self.report.new = len(candidates)
        return sorted(candidates, key=pipeline_order)

    def _process(self, item: Item) -> SummaryResult | None:
        """Text -> header -> prepare -> summarize. None when the item turns out to be already processed."""
        missing_pages: list[int] = []
        if item.text is not None:
            text = item.text
            log.info("%s: using the text taken from the advance sheet (%s characters).", item.label, f"{len(text):,}")
        elif not item.pdf_url:
            log.info("%s: there is no PDF to download, so it is listed without a summary.", item.label)
            self.seen_keys.add(item.key)
            return _failed(item, ERR_PDF_UNREADABLE)
        else:
            assert self.session is not None
            try:
                extracted = extract_text(self.session.get_bytes(item.pdf_url))
            except (FetchError, ExtractionError) as exc:
                log.warning("%s: could not read the opinion PDF (%s); it is listed without a summary.", item.label, exc)
                self.seen_keys.add(item.key)
                transient = isinstance(exc, FetchError) and (exc.status is None or exc.status >= 500)
                return _failed(item, ERR_PDF_DOWNLOAD if transient else ERR_PDF_UNREADABLE)
            for note in extracted.warnings:
                log.info("%s: %s", item.label, note)
            text = extracted.text
            missing_pages = list(extracted.missing_pages)

        header = parse_opinion_header(text)
        if item.kind is ItemKind.ORDER:
            header = replace(header, opinion_number=None)  # orders have no opinion number; a cited one must not stick
        if not self._apply_header(item, header):
            return None
        _write_text(self.out_dir / "texts" / _text_filename(item.key), text)

        if missing_pages:
            # A summary of part of an opinion would read as a summary of all of it (the missing page
            # may hold the holding or the disposition), so the opinion is listed with its link instead.
            log.warning(
                "%s: page(s) %s of the PDF could not be read by pdfplumber or pypdf, so it is listed without a "
                "summary rather than summarized from part of its text.",
                item.label,
                ", ".join(str(n) for n in missing_pages),
            )
            return _failed(item, ERR_PDF_PARTIAL)
        if len(text.strip()) < MIN_USEFUL_CHARS:
            log.warning(
                "%s: the PDF has almost no readable text (%d characters; it may be a scanned image), so it is "
                "listed without a summary.",
                item.label,
                len(text.strip()),
            )
            return _failed(item, ERR_NO_TEXT)

        prepared = prepare_for_model(text, self.settings.max_input_tokens_per_opinion)
        # For mock mode's estimate: the request a real run would send (user message + system prompt).
        message = build_user_message(item, prepared, header)
        self.report.request_input_tokens += estimate_tokens(message) + estimate_tokens(SYSTEM_PROMPT)
        self.report.request_items += 1
        assert self.summarizer is not None
        return self.summarizer.summarize(item, prepared, header)

    def _apply_header(self, item: Item, header: OpinionHeader) -> bool:
        """Fill the item's number, author, and disposition from its PDF header. False to skip the item."""
        old_key = item.key
        if item.kind is ItemKind.OPINION and header.opinion_number:
            if not item.number:
                item.number = header.opinion_number
            elif item.number != header.opinion_number:
                log.warning(
                    "%s: the PDF says Opinion No. %s; keeping the number %s from the source.",
                    item.label,
                    header.opinion_number,
                    item.number,
                )
        if not item.author and header.author:
            item.author = header.author
        if not item.disposition_hint and header.disposition:
            item.disposition_hint = header.disposition

        key = item.key
        if key != old_key:
            log.info("%s: opinion number read from the PDF; its key is now %s (was %s).", item.label, key, old_key)
            assert self.state is not None
            if not self.state.is_new(item, self.settings.max_attempts_per_item):
                log.info(
                    "%s is already recorded in %s as %s, so it is not summarized again.",
                    item.label,
                    self.settings.state_file,
                    key,
                )
                self.report.already_processed += 1
                self.report.new -= 1
                return False
            if self.state.is_refiled(item):
                item.refiled = True
        if key in self.seen_keys:
            log.warning("%s appears twice in this run (as %s); the duplicate is skipped.", item.label, key)
            self.report.duplicates += 1
            self.report.new -= 1
            return False
        self.seen_keys.add(key)
        return True

    def _build_digest(self, found: ScrapeResult, results: list[SummaryResult], overflow: list[Item]) -> Digest:
        assert self.state is not None
        # Earlier failures listed now can be old; this run's own items decide the digest's date.
        current = [r.item for r in results if r.item.key not in self.owed_keys] + overflow
        digest_date = max(item.filed_date for item in (current or [r.item for r in results]))
        supplement_to = self.state.earlier_issue(digest_date, FIRST_RUN_WINDOW_DAYS)
        if supplement_to:
            log.info(
                "An earlier digest already covered this week (%s), so this one is labeled a supplement to it.",
                supplement_to,
            )
            self.report.supplement_to = supplement_to
        inp = DigestInput(
            results=results,
            overflow=overflow,
            digest_date=digest_date,
            model=self.settings.claude_model,
            courts_included=tuple(enabled_courts(self.settings)),
            orders_included=self._orders_checked(found, digest_date),
            advance_sheets=list(found.advance_sheets),
            mock=self.mock,
            previous_subjects=self.state.previous_subjects(),
            source=found.source,
            supplement_to=supplement_to,
            cap=self.settings.max_opinions_per_run,
        )
        digest = build_digest(inp)
        self.report.subject = digest.subject
        set_output("digest_subject", digest.subject)
        return digest

    def _orders_checked(self, found: ScrapeResult, digest_date: date) -> bool:
        """Whether this week's Supreme Court orders were really checked (SOURCE=sccourts read the week's sheet)."""
        settings = self.settings
        if not (settings.include_orders and settings.source == "sccourts"):
            return False
        floor = digest_date - timedelta(days=FIRST_RUN_WINDOW_DAYS)
        if any(sheet.date >= floor for sheet in found.checked_advance_sheets):
            return True
        message = (
            "Supreme Court orders and rule amendments could not be checked for this digest's week: no advance "
            f"sheet dated on or after {_fmt_date(floor)} was read. The email says that orders may be missing."
        )
        log.warning("%s", message)
        self.report.warnings.append(message)
        return False

    def _write_outputs(self, digest: Digest, results: list[SummaryResult], overflow: list[Item]) -> None:
        out = self.out_dir
        _write_text(out / "digest.md", digest.markdown)
        _write_text(out / "digest.html", digest.html)
        listed = []
        for item in overflow:
            data = _item_json(item)
            data.update(status="listed_only", error=None, summary=None)
            listed.append(data)
        _write_json(
            out / "digest.json",
            {
                "subject": digest.subject,
                "counts": digest.counts,
                "dry_run": self.dry_run,
                "mock": self.mock,
                "source": self.report.source,
                "model": self.settings.claude_model,
                "effort": effort_label(self.settings.claude_model, self.settings.claude_effort),
                "prompt_version": PROMPT_VERSION,
                "items": [_result_json(r) for r in results] + listed,
            },
        )
        log.info("Wrote the digest to %s/ (digest.md, digest.html, digest.json).", out.as_posix())

    def _deliver(self, digest: Digest, results: list[SummaryResult], overflow: list[Item]) -> int:
        """Create the Buttondown draft and record the state; then the optional test send and AUTO_SEND."""
        settings, state = self.settings, self.state
        assert state is not None
        item_keys = [r.item.key for r in results] + [item.key for item in overflow]
        try:
            client = self.services.make_buttondown(settings)
            # No Buttondown metadata: it is a paid-only feature for accounts created since
            # 2026-04-20, and data/processed.json already records which items each email covered.
            created = client.create_draft(digest.subject, digest.markdown)
        except ButtondownError as exc:  # includes DuplicateEmailError
            return self._fail(f"{exc} Nothing was emailed and {settings.state_file} is unchanged.")
        self.report.email = created
        set_output("email_created", "true")

        # Record the state right away: the draft exists, so these items must never be emailed again.
        now = self.services.now()
        for result in results:
            if result.item.key in self.owed_keys:  # listed now, without a new attempt
                state.record_listed_failure(
                    result.item, error=result.error, digest_subject=digest.subject, email_id=created.id, now=now
                )
            else:
                state.record_result(result, digest_subject=digest.subject, email_id=created.id, now=now)
        for item in overflow:
            state.record_listed_only(item, digest_subject=digest.subject, email_id=created.id, now=now)
        # The first run's older items: recorded, so the next run does not email last week's backlog.
        for item in self.first_run_left_out:
            state.record_skipped(item, now=now)
        state.record_digest(digest.subject, created.id, created.status or "draft", item_keys, now)
        try:
            state.save()
        except OSError as exc:
            return self._fail(
                f"Draft {created.id} was created in Buttondown, but {settings.state_file} could not be saved "
                f"({exc}). Before the next run, delete that draft or change its subject ({DASHBOARD_URL}); "
                "otherwise the next run stops with a duplicate-subject error."
            )
        self.report.state_saved = True
        set_output("state_changed", "true")
        notice(f"Draft created in Buttondown: {digest.subject}. Review it at {DASHBOARD_URL}")

        code = EXIT_OK
        problems: list[str] = []
        # The test copy goes out while the email is still a draft, before any AUTO_SEND.
        recipients = list(settings.buttondown_test_recipients)
        if recipients:
            try:
                client.send_test(created.id, recipients)
            except ButtondownError as exc:
                log.warning("The draft was created, but the test send failed: %s", exc)
                self.report.warnings.append(f"Test send failed: {exc}")
                problems.append("the test send failed")
                code = EXIT_PARTIAL

        if settings.auto_send:
            try:
                client.send_now(created.id)
            except ButtondownError as exc:
                log.error("The draft was created, but AUTO_SEND could not send it: %s", exc)
                self.report.warnings.append(f"Sending failed: {exc}")
                problems.append("sending it FAILED")
                code = EXIT_PARTIAL
            else:
                self.report.sent = True
                notice(f"AUTO_SEND: Buttondown is sending the digest to subscribers: {digest.subject}")
                state.update_digest_status(created.id, "about_to_send", now=self.services.now())
                try:
                    state.save()
                except OSError as exc:
                    log.error("The email is being sent, but %s could not be updated: %s", settings.state_file, exc)
                    problems.append(f"{settings.state_file} could not be updated after sending")
                    code = EXIT_PARTIAL

        if self.report.sent:
            outcome = "Draft created in Buttondown and sent to subscribers (AUTO_SEND)"
        else:
            outcome = "Draft created in Buttondown" + ("" if settings.auto_send else " (AUTO_SEND is off)")
        self.report.outcome = outcome + (f"; {'; '.join(problems)}" if problems else "")
        return self._finish(code)

    # ----------------------------------------------------------------- endings

    def _fail(self, message: str) -> int:
        log.error("%s", message)
        self.report.outcome = f"FAILED: {message}"
        return self._finish(EXIT_FAILED)

    def _finish(self, code: int) -> int:
        report = self.report
        report.exit_code = code
        rows = summary_rows(report, self.settings)
        with group("Run summary"):
            width = max(len(name) for name, _ in rows) + 1
            for name, value in rows:
                log.info("%s %s", f"{name}:".ljust(width), value)
        try:
            _write_json(self.out_dir / "run_summary.json", run_summary_json(report, self.settings))
        except OSError as exc:
            log.warning("Could not write %s: %s", (self.out_dir / "run_summary.json").as_posix(), exc)
        step_summary(summary_markdown(report, rows))
        if report.has_new is not None:
            set_output("has_new", "true" if report.has_new else "false")
        if report.email is None:
            set_output("email_created", "false")
        return code


# --------------------------------------------------------------------------- run summary


def _counts(report: _Report) -> dict[str, int]:
    ok = [r for r in report.results if r.ok]
    return {
        "found": report.found,
        "already_processed": report.already_processed,
        "first_run_left_out": report.first_run_left_out,
        "duplicates": report.duplicates,
        "new": report.new,
        "summarized": len(ok),
        "from_cache": sum(1 for r in ok if r.from_cache),
        "failed": sum(1 for r in report.results if not r.ok),
        "listed_only": len(report.overflow),
        "listed_after_earlier_failures": len(report.owed),
    }


def _models_in_use(report: _Report, settings: Settings) -> list[str]:
    return [settings.claude_model] + sorted(m for m in report.served_models if m != settings.claude_model)


def _price_text(model: str) -> str:
    prices = model_pricing(model)
    if prices is None:
        return f"{model}: no price in src/config.py MODEL_PRICING"
    return f"{model}: ${prices['input']:.2f} input / ${prices['output']:.2f} output per million tokens"


def _mock_estimate(report: _Report, settings: Settings) -> tuple[int, int, float | None]:
    """(input tokens, output tokens, dollars) a real API run would have used for this run's items."""
    output_tokens = report.request_items * MOCK_OUTPUT_TOKENS_PER_ITEM
    cost = estimate_cost(settings.claude_model, report.request_input_tokens, output_tokens)
    return report.request_input_tokens, output_tokens, cost


def _mode_text(report: _Report, settings: Settings) -> str:
    if report.dry_run:
        mode = f"dry run (no email; {settings.state_file} unchanged)"
    elif settings.auto_send:
        mode = "real run (draft, then AUTO_SEND)"
    else:
        mode = "real run (draft only)"
    if report.mock:
        mode += "; MOCK summaries, no API calls"
    return mode


def summary_rows(report: _Report, settings: Settings) -> list[tuple[str, str]]:
    """The end-of-run summary as (label, value) rows, for the log and the job summary page."""
    counts = _counts(report)
    usage = report.usage
    rows: list[tuple[str, str]] = [
        ("Result", report.outcome or "(not finished)"),
        ("Mode", _mode_text(report, settings)),
        ("Source", report.source or settings.source),
        ("Model", f"{settings.claude_model} (effort {effort_label(settings.claude_model, settings.claude_effort)})"),
        ("Today", _fmt_date(report.today) if report.today else "n/a"),
        ("Items found", _fmt_int(counts["found"])),
        ("Already processed", _fmt_int(counts["already_processed"])),
    ]
    if counts["first_run_left_out"]:
        rows.append(("Left out (first run: older weeks)", _fmt_int(counts["first_run_left_out"])))
    rows += [
        ("New this run", _fmt_int(counts["new"])),
        ("Summarized", f"{_fmt_int(counts['summarized'])} ({_fmt_int(counts['from_cache'])} from the cache)"),
        ("Failed", _fmt_int(counts["failed"])),
        ("Listed only (over the cap)", _fmt_int(counts["listed_only"])),
    ]
    if counts["listed_after_earlier_failures"]:
        rows.append(("Listed after failing in earlier runs", _fmt_int(counts["listed_after_earlier_failures"])))
    rows += [
        ("API requests", _fmt_int(usage.requests)),
        ("Input tokens", _fmt_int(usage.input_tokens)),
        ("Output tokens", _fmt_int(usage.output_tokens)),
    ]
    if usage.cache_read_input_tokens or usage.cache_creation_input_tokens:
        read, written = _fmt_int(usage.cache_read_input_tokens), _fmt_int(usage.cache_creation_input_tokens)
        rows.append(("Prompt-cache tokens", f"{read} read / {written} written"))
    rows.append(("Estimated cost", _fmt_cost(usage.cost_usd)))
    prices = "; ".join(_price_text(m) for m in _models_in_use(report, settings))
    rows.append(("Prices used", f"{prices} ({PRICES_NOTE})"))
    if report.mock:
        tokens_in, tokens_out, cost = _mock_estimate(report, settings)
        rows.append(
            (
                "Estimated cost with the API",
                f"about {_fmt_cost(cost)} for {_fmt_int(report.request_items)} item(s): about "
                f"{_fmt_int(tokens_in)} input + {_fmt_int(tokens_out)} output tokens at {settings.claude_model} "
                f"prices ({_fmt_int(MOCK_OUTPUT_TOKENS_PER_ITEM)} output tokens assumed per item)",
            )
        )
    if report.subject:
        rows.append(("Subject", report.subject))
    if report.supplement_to:
        rows.append(("Supplement to", report.supplement_to))
    if report.email:
        rows.append(("Buttondown email", f"{report.email.id} (status {report.email.status})"))
    rows.append(("Exit code", str(report.exit_code)))
    return rows


def summary_markdown(report: _Report, rows: list[tuple[str, str]]) -> str:
    """The job summary page: the rows as a table, then the warnings and the items."""
    lines = ["## SC Advance Sheet Digest run", "", "| | |", "|---|---|"]
    lines += [f"| {md_escape(name)} | {md_escape(value)} |" for name, value in rows]
    if report.warnings:
        lines += ["", "**Warnings**", ""]
        lines += [f"- {md_escape(w)}" for w in report.warnings]
    entries = [(r.item, "summarized" if r.ok else f"failed: {r.error}") for r in report.results]
    entries += [(item, "listed without a summary (over the cap)") for item in report.overflow]
    entries += [(r.item, f"listed without a summary (failed in earlier runs: {r.error})") for r in report.owed]
    if entries:
        lines += ["", "| Item | Filed | Status |", "|---|---|---|"]
        lines += [
            f"| {md_escape(item.label)} | {_fmt_date(item.filed_date)} | {md_escape(status)} |"
            for item, status in entries
        ]
    return "\n".join(lines)


def run_summary_json(report: _Report, settings: Settings) -> dict[str, Any]:
    """run_summary.json: the same facts as the log summary, for scripts and later review."""
    usage = report.usage
    data: dict[str, Any] = {
        "version": __version__,
        "result": report.outcome,
        "exit_code": report.exit_code,
        "dry_run": report.dry_run,
        "mock": report.mock,
        "auto_send": settings.auto_send,
        "today": report.today.isoformat() if report.today else None,
        "source": report.source or settings.source,
        "model": settings.claude_model,
        "effort": effort_label(settings.claude_model, settings.claude_effort),
        "served_models": sorted(report.served_models),
        "prompt_version": PROMPT_VERSION,
        "counts": _counts(report),
        "usage": asdict(usage),
        "estimated_cost_usd": usage.cost_usd,
        "prices_per_million_tokens": {m: model_pricing(m) for m in _models_in_use(report, settings)},
        "prices_note": f"From src/config.py MODEL_PRICING; {PRICES_NOTE}.",
        "subject": report.subject,
        "supplement_to": report.supplement_to,
        "email_id": report.email.id if report.email else None,
        "email_status": report.email.status if report.email else None,
        "sent": report.sent,
        "state_saved": report.state_saved,
        "warnings": report.warnings,
        "items": [
            {
                "key": r.item.key,
                "label": r.item.label,
                "status": "summarized" if r.ok else "failed",
                "error": None if r.ok else r.error,
                "from_cache": r.from_cache,
                "model": r.model,
                "usage": asdict(r.usage),
            }
            for r in report.results
        ]
        + [{"key": item.key, "label": item.label, "status": "listed_only"} for item in report.overflow]
        + [
            {"key": r.item.key, "label": r.item.label, "status": "listed_after_earlier_failures", "error": r.error}
            for r in report.owed
        ],
    }
    if report.mock:
        tokens_in, tokens_out, cost = _mock_estimate(report, settings)
        data["mock_estimate"] = {
            "items": report.request_items,
            "input_tokens": tokens_in,
            "output_tokens": tokens_out,
            "output_tokens_per_item": MOCK_OUTPUT_TOKENS_PER_ITEM,
            "estimated_cost_usd": cost,
        }
    return data


# --------------------------------------------------------------------------- entry point


def _env_flag(name: str) -> bool:
    return parse_bool(name, os.environ.get(name), False)


def _today() -> date:
    try:
        return datetime.now(ZoneInfo(TIMEZONE)).date()
    except ZoneInfoNotFoundError:  # no time-zone data (tzdata is in requirements.txt)
        log.warning("Time-zone data for %s is missing; using today's date in UTC.", TIMEZONE)
        return datetime.now(timezone.utc).date()


def main(argv: Sequence[str] | None = None, services: Services | None = None) -> int:
    """Run the pipeline. Returns the exit code (0 ok or nothing new, 1 failure, 2 partial)."""
    args = build_parser().parse_args(argv)
    level = "DEBUG" if args.verbose else ((os.environ.get("LOG_LEVEL") or "").strip() or "INFO")
    setup_logging(level)
    log.info("SC Advance Sheet Digest %s", __version__)
    try:
        dry_run = args.dry_run or _env_flag("DRY_RUN")
        mock = args.mock_llm or _env_flag("MOCK_LLM")
        settings = load_settings().with_overrides(
            source=args.source,
            lookback_days=args.lookback_days,
            max_opinions_per_run=args.max_opinions,
            output_dir=args.output_dir,
            state_file=args.state_file,
        )
    except ConfigError as exc:
        log.error("Configuration error: %s", exc)
        step_summary(f"## SC Advance Sheet Digest run\n\nFAILED: configuration error: {md_escape(str(exc))}")
        set_output("email_created", "false")
        return EXIT_FAILED
    if not args.verbose and settings.log_level != level.upper():
        setup_logging(settings.log_level)
    if mock and not dry_run:
        log.warning(
            "--mock-llm (MOCK_LLM=true) makes placeholder summaries, so this run is a dry run: nothing is "
            "emailed and %s is not changed.",
            settings.state_file,
        )
        dry_run = True

    today = args.today or _today()
    with group("Settings"):
        for name, value in settings.describe():
            log.info("%-28s %s", name, value)
        log.info("%-28s %s", "Mode", ("dry run" if dry_run else "real run") + (", mock summaries" if mock else ""))
        log.info("%-28s %s", "Today", today.isoformat())
    if not mock and not model_profile(settings.claude_model).get("effort"):
        log.warning(
            "SUMMARY_EFFORT=%s is not sent: %s is not in MODEL_PROFILES (src/config.py), so the API's default "
            "effort applies (often 'high', which bills more thinking as output and can hit MAX_OUTPUT_TOKENS). "
            "Add the model to MODEL_PROFILES if it supports effort.",
            settings.claude_effort,
            settings.claude_model,
        )

    run = _Run(settings, dry_run=dry_run, mock=mock, today=today, services=services or Services())
    try:
        return run.execute()
    except Exception as exc:  # a bug or an unexpected failure: report it with its traceback
        log.exception("The run stopped because of an unexpected error: %s", exc)
        code = EXIT_PARTIAL if run.report.state_saved else EXIT_FAILED
        run.report.outcome = f"FAILED: unexpected error: {type(exc).__name__}: {exc}"
        try:
            return run._finish(code)
        except Exception:  # the summary itself failed; the exit code still matters
            log.exception("Could not write the run summary.")
            return code


if __name__ == "__main__":
    sys.exit(main())
