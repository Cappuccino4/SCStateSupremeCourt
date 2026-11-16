"""Logging that reads well in the GitHub Actions log viewer, and locally.

In GitHub Actions, warnings and errors are written as workflow commands
(``::warning::`` / ``::error::``), so GitHub highlights them in the log and lists them
on the run's summary page. ``group()`` folds a noisy section into a collapsible block,
and ``step_summary()`` writes Markdown to the run's summary page.
"""

from __future__ import annotations

import contextlib
import logging
import os
import sys
from collections.abc import Iterator


def in_actions() -> bool:
    return os.environ.get("GITHUB_ACTIONS") == "true"


def _escape_command(text: str) -> str:
    # GitHub workflow-command escaping for the message part.
    return text.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


class _Formatter(logging.Formatter):
    def __init__(self, actions: bool):
        super().__init__("%(asctime)s %(levelname)-7s [%(shortname)s] %(message)s", "%H:%M:%S")
        self.actions = actions

    def format(self, record: logging.LogRecord) -> str:
        record.shortname = record.name.removeprefix("src.")
        line = super().format(record)
        if self.actions and record.levelno >= logging.WARNING:
            kind = "error" if record.levelno >= logging.ERROR else "warning"
            return f"::{kind}::{_escape_command(line)}"
        return line


def setup_logging(level: str = "INFO") -> None:
    """Configure the root logger once per process. Safe to call again (replaces handlers)."""
    for stream in (sys.stdout, sys.stderr):
        # Windows consoles default to cp1252, which garbles en dashes and section signs.
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(_Formatter(actions=in_actions()))
    root.addHandler(handler)
    root.setLevel(getattr(logging, str(level).upper(), logging.INFO))
    for noisy in ("pdfminer", "pdfplumber", "urllib3", "httpx", "httpx2", "httpcore", "anthropic", "charset_normalizer"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


@contextlib.contextmanager
def group(title: str) -> Iterator[None]:
    """Collapsible log section in GitHub Actions; a plain header line locally."""
    sys.stdout.flush()
    if in_actions():
        print(f"::group::{title}", flush=True)
        try:
            yield
        finally:
            sys.stdout.flush()
            print("::endgroup::", flush=True)
    else:
        print(f"\n=== {title} ===", flush=True)
        yield


def notice(message: str) -> None:
    """A highlighted, non-error line (shown on the Actions run page). Plain log line locally."""
    if in_actions():
        print(f"::notice::{_escape_command(message)}", flush=True)
    else:
        logging.getLogger("src.main").info(message)


def step_summary(markdown: str) -> None:
    """Append Markdown to the GitHub Actions job summary page (no-op locally)."""
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    with open(path, "a", encoding="utf-8") as f:
        f.write(markdown.rstrip() + "\n\n")


def set_output(name: str, value: str) -> None:
    """Set a step output for later workflow steps (no-op locally). Value must be one line."""
    path = os.environ.get("GITHUB_OUTPUT")
    if not path:
        return
    value = str(value).replace("\r", " ").replace("\n", " ")
    with open(path, "a", encoding="utf-8") as f:
        f.write(f"{name}={value}\n")
