"""What earlier runs already processed: ``data/processed.json``.

The weekly workflow commits this file after every run that created a Buttondown
draft, so each opinion is summarized (and paid for) only once and each digest lists
only new opinions. Layout, version 1::

    {"version": 1, "updated_at": "2026-11-18T20:31:02Z",
     "items": {"SC:28355": {"kind": "opinion", "court": "SC", "case_name": "...",
                            "filed_date": "2026-11-12", "status": "summarized", "attempts": 1,
                            "first_seen": "...", "last_attempt": "...", "digest_subject": "...",
                            "email_id": "...", "link_url": "...", "source": "courtlistener-api",
                            "error": null}},
     "aliases": {"CL:11060100": "SC:28355"},
     "digests": [{"subject": "...", "email_id": "...", "status": "draft",
                  "created_at": "...", "items": ["SC:28355"]}]}

An item's ``status`` is "summarized", "failed" (tried again on later runs until it has
had MAX_ATTEMPTS_PER_ITEM attempts), "listed_only" (over the per-run cap, so it was
listed in a digest without a summary), or "skipped_first_run" (older than the most
recent week when the first run created its digest; never summarized or emailed).
``email_id`` and ``digest_subject`` name the digest that listed the item; a failed item
without them has not been in any email yet, and once it has used up its attempts the
next digest lists it without a summary (see ``unlisted_failures``). ``aliases`` maps
other keys an item was known by (its CourtListener id, before its opinion number was
known) to the key of its record.

A damaged file is never reset automatically (starting over would summarize and email
old opinions again), so loading one raises StateError with instructions instead.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import stat
import tempfile
import time
from collections.abc import Iterable
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .models import Item, ItemKind, SummaryResult

log = logging.getLogger(__name__)

STATE_VERSION = 1
MAX_DIGESTS = 500  # digest records kept; the oldest are dropped first

STATUS_SUMMARIZED = "summarized"
STATUS_FAILED = "failed"
STATUS_LISTED_ONLY = "listed_only"  # over the per-run cap: listed in a digest without a summary
STATUS_SKIPPED_FIRST_RUN = "skipped_first_run"  # older than the first run's most recent week: never emailed

_KNOWN_KEYS = ("version", "updated_at", "items", "aliases", "digests")


class StateError(RuntimeError):
    """The state file cannot be used as it is. The message says how to fix it."""


def utc_timestamp(moment: datetime) -> str:
    """'2026-11-18T20:31:02Z' (UTC, whole seconds). A naive datetime is taken to be UTC already."""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class State:
    """The processed-items record. ``State.load(path)`` reads it and ``save()`` writes it back."""

    def __init__(self, path: str | os.PathLike[str]):
        self._path = Path(path)
        self._items: dict[str, dict[str, Any]] = {}
        self._aliases: dict[str, str] = {}
        self._digests: list[dict[str, Any]] = []
        self._updated_at: str | None = None
        self._extra: dict[str, Any] = {}  # unknown top-level keys, written back unchanged

    @classmethod
    def load(cls, path: str | os.PathLike[str]) -> State:
        """Read the state file. A missing or blank file gives an empty state (the first run).

        Raises StateError for unreadable text, invalid JSON, another version, or a
        malformed layout. It never resets the file.
        """
        state = cls(path)
        p = state._path
        try:
            text = p.read_text(encoding="utf-8")
        except FileNotFoundError:
            log.info("%s does not exist yet. Starting with an empty state: this is a first run.", p)
            return state
        except UnicodeDecodeError as exc:
            raise StateError(f"{p} is not UTF-8 text ({exc}). {_fix_hint(p)}") from exc
        except OSError as exc:
            raise StateError(f"Could not read {p}: {exc}") from exc
        text = text.removeprefix("\ufeff")  # tolerate a byte-order mark added by a Windows editor
        if not text.strip():
            log.info("%s is empty. Starting with an empty state: this is a first run.", p)
            return state
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise StateError(
                f"{p} is not valid JSON ({exc.msg} at line {exc.lineno}, column {exc.colno}). {_fix_hint(p)}"
            ) from exc
        state._populate(data)
        log.info(
            "Loaded %s: %d processed item(s), %d digest(s), last updated %s.",
            p,
            len(state._items),
            len(state._digests),
            state._updated_at or "never",
        )
        return state

    def _populate(self, data: Any) -> None:
        p = self._path
        if not isinstance(data, dict):
            raise StateError(
                f"{p} must contain a JSON object, but it contains a JSON {_json_type(data)}. {_fix_hint(p)}"
            )
        version = data.get("version")
        if isinstance(version, bool) or version != STATE_VERSION:
            raise StateError(
                f"{p} has state version {version!r}, but this program reads only version {STATE_VERSION}. "
                f'If you edited the file by hand, set "version" back to {STATE_VERSION}; if a newer copy of this '
                f"program wrote it, update this copy. {_fix_hint(p)}"
            )
        items = _section(data, "items", dict, p)
        for key, record in items.items():
            if not isinstance(record, dict):
                raise StateError(f'{p}: the record for "{key}" in "items" must be a JSON object. {_fix_hint(p)}')
        aliases = _section(data, "aliases", dict, p)
        for alias, target in aliases.items():
            if not isinstance(target, str):
                raise StateError(
                    f'{p}: "aliases" must map keys to keys (strings), but "{alias}" does not. {_fix_hint(p)}'
                )
        digests = _section(data, "digests", list, p)
        if not all(isinstance(d, dict) for d in digests):
            raise StateError(f'{p}: every entry in "digests" must be a JSON object. {_fix_hint(p)}')
        updated_at = data.get("updated_at")
        self._items = items
        self._aliases = aliases
        self._digests = digests
        self._updated_at = updated_at if isinstance(updated_at, str) else None
        self._extra = {k: v for k, v in data.items() if k not in _KNOWN_KEYS}

    @property
    def path(self) -> Path:
        return self._path

    @property
    def is_empty(self) -> bool:
        """True when no item has been recorded (a first run)."""
        return not self._items

    @property
    def updated_at(self) -> str | None:
        return self._updated_at

    @property
    def items(self) -> dict[str, dict[str, Any]]:
        """A copy of the item records, by key."""
        return {key: dict(record) for key, record in self._items.items()}

    @property
    def aliases(self) -> dict[str, str]:
        """A copy of the alias map (other key -> record key)."""
        return dict(self._aliases)

    @property
    def digests(self) -> list[dict[str, Any]]:
        """A copy of the digest records, oldest first."""
        return [{**d, "items": list(d.get("items") or [])} for d in self._digests]

    def __repr__(self) -> str:
        return f"State({str(self._path)!r}, {len(self._items)} items, {len(self._digests)} digests)"

    def _find(self, item: Item) -> tuple[str | None, dict[str, Any] | None]:
        """(key the record is stored under, record): by key, then alias keys, then the aliases map."""
        key = item.key
        if key in self._items:
            return key, self._items[key]
        for alias in item.alias_keys:
            if alias in self._items:
                return alias, self._items[alias]
        for candidate in (key, *item.alias_keys):
            target = self._aliases.get(candidate)
            if target is not None and target in self._items:
                return target, self._items[target]
        return None, None

    def lookup(self, item: Item) -> dict[str, Any] | None:
        """A copy of the item's record, or None if it was never recorded."""
        record = self._find(item)[1]
        return dict(record) if record is not None else None

    def attempts(self, item: Item) -> int:
        """How many times the item's current version has been sent for summarizing.

        A refiled version (filed later than the record) has had no attempts yet.
        """
        record = self._find(item)[1]
        if record is None or _is_refiled(item, record):
            return 0
        return _count(record.get("attempts"))

    def is_refiled(self, item: Item) -> bool:
        """True when the item is a later-filed (withdrawn, substituted, and refiled) version of a recorded opinion."""
        record = self._find(item)[1]
        return record is not None and _is_refiled(item, record)

    def is_listed(self, item: Item) -> bool:
        """True when a digest already listed this version of the item (its record names the email)."""
        record = self._find(item)[1]
        return record is not None and bool(record.get("email_id")) and not _is_refiled(item, record)

    def is_new(self, item: Item, max_attempts: int) -> bool:
        """Whether this run should process the item.

        Never recorded -> True. "summarized"/"listed_only"/"skipped_first_run" -> False,
        unless the item is an opinion filed later than the recorded date (withdrawn,
        substituted, and refiled). "failed" -> True while it has had fewer than
        ``max_attempts`` attempts.
        """
        record = self._find(item)[1]
        if record is None:
            return True
        if _is_refiled(item, record):
            return True
        status = record.get("status")
        if status in (STATUS_SUMMARIZED, STATUS_LISTED_ONLY, STATUS_SKIPPED_FIRST_RUN):
            return False
        if status == STATUS_FAILED:
            return _count(record.get("attempts")) < max_attempts
        log.warning(
            "%s has an unrecognized status %r in %s, so it is treated as already processed.",
            item.label,
            status,
            self._path,
        )
        return False

    def record_result(
        self,
        result: SummaryResult,
        *,
        digest_subject: str | None,
        email_id: str | None,
        now: datetime,
    ) -> None:
        """Record one summarizing attempt: "summarized" or "failed", attempts + 1.

        Without a digest (``digest_subject`` and ``email_id`` None), an earlier digest's
        listing of the same version is kept.
        """
        ok = result.ok
        self._record(
            result.item,
            utc_timestamp(now),
            status=STATUS_SUMMARIZED if ok else STATUS_FAILED,
            error=None if ok else result.error,
            digest_subject=digest_subject,
            email_id=email_id,
            attempted=True,
        )

    def record_listed_failure(
        self,
        item: Item,
        *,
        error: str | None,
        digest_subject: str | None,
        email_id: str | None,
        now: datetime,
    ) -> None:
        """Record that a digest listed an earlier failure without a summary (no new attempt was made)."""
        self._record(
            item,
            utc_timestamp(now),
            status=STATUS_FAILED,
            error=error,
            digest_subject=digest_subject,
            email_id=email_id,
            attempted=False,
        )

    def record_skipped(self, item: Item, *, now: datetime) -> None:
        """Record an item the first run left out (older than its most recent week), so no later run emails it."""
        self._record(
            item,
            utc_timestamp(now),
            status=STATUS_SKIPPED_FIRST_RUN,
            error=None,
            digest_subject=None,
            email_id=None,
            attempted=False,
        )

    def record_listed_only(
        self,
        item: Item,
        *,
        digest_subject: str | None,
        email_id: str | None,
        now: datetime,
    ) -> None:
        """Record an item that was listed in a digest without a summary (over the per-run cap)."""
        self._record(
            item,
            utc_timestamp(now),
            status=STATUS_LISTED_ONLY,
            error=None,
            digest_subject=digest_subject,
            email_id=email_id,
            attempted=False,
        )

    def _record(
        self,
        item: Item,
        stamp: str,
        *,
        status: str,
        error: str | None,
        digest_subject: str | None,
        email_id: str | None,
        attempted: bool,
    ) -> None:
        key = item.key
        found_key, record = self._find(item)
        if record is None:
            record_key, record = key, {"first_seen": stamp, "attempts": 0, "last_attempt": None}
        elif found_key != key and found_key in item.alias_keys:
            # Recorded under its CourtListener id before its opinion number was known.
            record_key = key
            del self._items[found_key]
            self._retarget_aliases(found_key, key)
            log.info("State: moved the record of %s from %s to %s.", item.label, found_key, key)
        else:
            record_key = found_key  # its own key, or the key an alias points to

        recorded_date = _parse_date(record.get("filed_date"))
        if _is_refiled(item, record):
            log.info(
                "State: %s was refiled on %s (recorded as filed %s), so its attempt count starts over.",
                item.label,
                item.filed_date.isoformat(),
                record.get("filed_date"),
            )
            record["attempts"] = 0
            # The digest named in the record listed the earlier version, not this one.
            record["digest_subject"] = None
            record["email_id"] = None

        for alias in item.alias_keys:
            if alias == record_key:
                continue
            stale = self._items.pop(alias, None)
            if stale is not None:
                # Both keys had a record. The canonical one wins; keep the earlier first_seen.
                stale_first, first = stale.get("first_seen"), record.get("first_seen")
                first_is_earlier = isinstance(first, str) and bool(first) and first <= str(stale_first)
                if isinstance(stale_first, str) and stale_first and not first_is_earlier:
                    record["first_seen"] = stale_first
                self._retarget_aliases(alias, record_key)
                log.info("State: %s had records under %s and %s; kept %s.", item.label, record_key, alias, record_key)
            self._aliases[alias] = record_key
        self._aliases.pop(record_key, None)  # a record key is never also an alias

        # Never move the recorded filing date backwards: a later date marks a refiled version.
        filed = item.filed_date if recorded_date is None or item.filed_date >= recorded_date else recorded_date
        record.update(
            kind=item.kind.value,
            court=item.court.value,
            case_name=item.case_name,
            filed_date=filed.isoformat(),
            status=status,
            link_url=item.link_url,
            source=item.source,
            error=error,
        )
        if digest_subject is not None or email_id is not None:
            record.update(digest_subject=digest_subject, email_id=email_id)
        else:  # recorded without a new digest: keep the earlier digest that listed it, if any
            record.setdefault("digest_subject", None)
            record.setdefault("email_id", None)
        record["attempts"] = _count(record.get("attempts")) + (1 if attempted else 0)
        if attempted:
            record["last_attempt"] = stamp
        else:
            record.setdefault("last_attempt", None)
        if not record.get("first_seen"):
            record["first_seen"] = stamp
        self._items[record_key] = record
        self._updated_at = stamp

    def _retarget_aliases(self, old_key: str, new_key: str) -> None:
        for alias, target in self._aliases.items():
            if target == old_key:
                self._aliases[alias] = new_key

    def record_digest(
        self,
        subject: str,
        email_id: str | None,
        status: str,
        item_keys: Iterable[str],
        now: datetime,
    ) -> None:
        """Record a digest email that was created. Keeps the newest MAX_DIGESTS records."""
        stamp = utc_timestamp(now)
        self._digests.append(
            {
                "subject": subject,
                "email_id": email_id,
                "status": status,
                "created_at": stamp,
                "items": list(dict.fromkeys(item_keys)),
            }
        )
        if len(self._digests) > MAX_DIGESTS:
            del self._digests[: len(self._digests) - MAX_DIGESTS]
        self._updated_at = stamp

    def update_digest_status(self, email_id: str, status: str, now: datetime | None = None) -> bool:
        """Set the status of the digest with this Buttondown email id. False if none is recorded."""
        found = False
        for digest in self._digests:
            if digest.get("email_id") == email_id:
                digest["status"] = status
                found = True
        if not found:
            log.warning(
                "No digest with Buttondown email id %s is recorded in %s, so its status was not updated.",
                email_id,
                self._path,
            )
        elif now is not None:
            self._updated_at = utc_timestamp(now)
        return found

    def previous_subjects(self) -> frozenset[str]:
        """Subjects of digests already created, so a new digest never reuses one."""
        subjects = {d.get("subject") for d in self._digests}
        subjects.update(record.get("digest_subject") for record in self._items.values())
        return frozenset(s for s in subjects if isinstance(s, str) and s)

    def earlier_issue(self, digest_date: date, window_days: int) -> str | None:
        """Subject of the newest digest that covered an item filed in the same week as ``digest_date``.

        "The same week" is the first run's notion of it: on ``digest_date`` or up to
        ``window_days`` before it. A new digest for that week is a supplement to the earlier
        one (for example a later run that adds a late posting, or a summary that failed
        in the first issue's run). None when no such digest is recorded.
        """
        floor = digest_date - timedelta(days=window_days)
        for digest in reversed(self._digests):
            subject = digest.get("subject")
            if not isinstance(subject, str) or not subject.strip():
                continue
            for key in digest.get("items") or []:
                record = self._record_for_key(key) if isinstance(key, str) else None
                filed = _parse_date(record.get("filed_date")) if record is not None else None
                if filed is not None and floor <= filed <= digest_date:
                    return subject
        return None

    def unlisted_failures(self, max_attempts: int) -> list[tuple[str, dict[str, Any]]]:
        """(key, record copy) of failed items no digest has listed and no run will try again.

        Their summaries failed in runs that created no email, until their attempts ran
        out. The next digest lists them without a summary, so every opinion still reaches
        subscribers with its link.
        """
        return [
            (key, dict(record))
            for key, record in sorted(self._items.items())
            if record.get("status") == STATUS_FAILED
            and not record.get("email_id")
            and _count(record.get("attempts")) >= max_attempts
        ]

    def _record_for_key(self, key: str) -> dict[str, Any] | None:
        record = self._items.get(key)
        if record is None:
            target = self._aliases.get(key)
            record = self._items.get(target) if target is not None else None
        return record

    def to_json(self) -> str:
        """The file's contents: sorted keys, 2-space indent, non-ASCII kept as is, trailing newline."""
        data = dict(self._extra)
        data.update(
            version=STATE_VERSION,
            updated_at=self._updated_at,
            items=self._items,
            aliases=self._aliases,
            digests=self._digests,
        )
        return json.dumps(data, sort_keys=True, indent=2, ensure_ascii=False) + "\n"

    def save(self) -> None:
        """Write the file atomically: a temp file in the same directory, fsync, then os.replace."""
        path = self._path
        directory = path.parent
        directory.mkdir(parents=True, exist_ok=True)
        text = self.to_json()
        fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=directory)
        try:
            with open(fd, "w", encoding="utf-8", newline="\n") as f:
                f.write(text)
                f.flush()
                os.fsync(f.fileno())
            _match_mode(tmp_name, path)
            _replace(tmp_name, path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp_name)
            raise
        log.info("Saved %s (%d item(s), %d digest(s)).", path, len(self._items), len(self._digests))


def _fix_hint(path: Path) -> str:
    shown = path.as_posix()
    return (
        f"{shown} records which opinions were already summarized and emailed, so it is never reset "
        "automatically (starting over would repeat old opinions). Fix it by hand, or restore the last good "
        f"copy from git history (`git log -- {shown}` lists the commits that changed it, and "
        f"`git checkout <commit> -- {shown}` restores one), or delete it to start fresh (the next run "
        "then treats the most recent week as new)."
    )


def _json_type(value: Any) -> str:
    if isinstance(value, dict):
        return "object"
    if isinstance(value, list):
        return "array"
    if isinstance(value, str):
        return "string"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, (int, float)):
        return "number"
    return "null"


def _section(data: dict[str, Any], name: str, kind: type, path: Path) -> Any:
    value = data.get(name)
    if value is None:
        return kind()
    if not isinstance(value, kind):
        expected = "an object" if kind is dict else "an array"
        raise StateError(f'{path}: "{name}" must be {expected}, not a JSON {_json_type(value)}. {_fix_hint(path)}')
    return value


def _count(value: Any) -> int:
    """An attempt count from the file; anything unusable counts as 0."""
    if isinstance(value, int) and not isinstance(value, bool):
        return max(value, 0)
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return 0


def _parse_date(value: Any) -> date | None:
    if not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value.strip()[:10])
    except ValueError:
        return None


def _is_refiled(item: Item, record: dict[str, Any]) -> bool:
    """An opinion whose filing date is later than the recorded one is a new (refiled) version."""
    if item.kind is not ItemKind.OPINION:
        return False
    recorded = _parse_date(record.get("filed_date"))
    return recorded is not None and item.filed_date > recorded


def _match_mode(tmp_name: str, path: Path) -> None:
    """Give the new file the old file's permissions (mkstemp creates it private, mode 0600)."""
    if os.name == "nt":
        return
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
    except OSError:
        mode = 0o644
    with contextlib.suppress(OSError):
        os.chmod(tmp_name, mode)


def _replace(src: str, dst: Path) -> None:
    """os.replace, retried briefly on Windows, where a virus scanner can hold the target open."""
    for attempt in range(5):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if os.name != "nt" or attempt == 4:
                raise
            time.sleep(0.1 * (attempt + 1))
