"""Runtime settings, read from environment variables.

In GitHub Actions the values come from repository Secrets and Variables (see
.github/workflows/weekly.yml). Locally they come from your shell or an optional
``.env`` file (see .env.example). This project never needs the Anthropic API key on
your own machine: local runs use ``--mock-llm``, and real summaries are made in
GitHub Actions, where the key lives only as a GitHub Secret.
"""

from __future__ import annotations

import logging
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path

from . import __version__

log = logging.getLogger(__name__)

DEFAULT_MODEL = "claude-sonnet-5-5"
CHEAP_MODEL = "claude-haiku-5-5"  # documented low-cost fallback

# Prices in US dollars per million tokens. They are used ONLY for the cost estimate in
# the end-of-run summary. VERIFY THEM against current Anthropic pricing before relying on
# the estimate: https://platform.claude.com/docs/en/about-claude/pricing (prices change).
# "cache_write" is the 5-minute cache-write rate. Haiku 5.5 bills prompts over 100K tokens at
# its higher "long_" rates (the long cache rates are scaled the same way; verify them), and
# estimate_cost applies that tier: a MAX_INPUT_TOKENS_PER_OPINION above about 110,000 can cross
# it on a very long opinion. claude-sonnet-5, claude-opus-5, and claude-opus-4-8 are listed
# because the server-side refusal fallback can hand a declined request to them.
MODEL_PRICING: dict[str, dict[str, float]] = {
    "claude-sonnet-5-5": {"input": 2.00, "output": 10.00, "cache_read": 0.10, "cache_write": 2.50},
    "claude-sonnet-5": {"input": 2.00, "output": 10.00, "cache_read": 0.20, "cache_write": 2.50},
    "claude-haiku-5-5": {
        "input": 0.10, "output": 0.50, "cache_read": 0.01, "cache_write": 0.125,
        "long_prompt_threshold": 100_000,
        "long_input": 0.50, "long_output": 2.50, "long_cache_read": 0.05, "long_cache_write": 0.625,
    },  # fmt: skip
    "claude-opus-5-5": {"input": 4.00, "output": 20.00, "cache_read": 0.20, "cache_write": 5.00},
    "claude-opus-5": {"input": 5.00, "output": 25.00, "cache_read": 0.50, "cache_write": 6.25},
    "claude-opus-4-8": {"input": 5.00, "output": 25.00, "cache_read": 0.50, "cache_write": 6.25},
    "claude-fable-5-2": {"input": 8.00, "output": 40.00, "cache_read": 0.20, "cache_write": 10.00},
}

# Request features each model accepts. A model not listed here gets DEFAULT_PROFILE
# (no `effort`, no server-side refusal fallback), which every current model accepts; the
# run log warns that SUMMARY_EFFORT is then not sent (the API's own default applies).
MODEL_PROFILES: dict[str, dict[str, bool]] = {
    "claude-sonnet-5-5": {"effort": True, "refusal_fallback": True},
    "claude-opus-5-5": {"effort": True, "refusal_fallback": True},
    "claude-haiku-5-5": {"effort": True, "refusal_fallback": False},  # Haiku 5.5 has no server-side fallback
    "claude-sonnet-5": {"effort": True, "refusal_fallback": False},  # itself Sonnet 5.5's fallback model
    "claude-opus-5": {"effort": True, "refusal_fallback": True},
    "claude-fable-5-2": {"effort": True, "refusal_fallback": True},
}
DEFAULT_PROFILE: dict[str, bool] = {"effort": False, "refusal_fallback": False}
REFUSAL_FALLBACK_BETA = "server-side-fallback-2026-07-01"

VALID_EFFORTS = ("low", "medium", "high", "xhigh", "max")
VALID_SOURCES = ("courtlistener", "sccourts")

# Secrets that are ignored if they appear in a local .env file: the Anthropic key is
# billed to your API account and belongs only in GitHub Secrets, and Buttondown sending
# happens only in GitHub Actions.
_DOTENV_FORBIDDEN = ("ANTHROPIC_API_KEY", "BUTTONDOWN_API_KEY")


class ConfigError(ValueError):
    """A setting has an invalid value. The message names the variable."""


def model_profile(model: str) -> dict[str, bool]:
    return MODEL_PROFILES.get(model, DEFAULT_PROFILE)


EFFORT_NOT_SENT = "API default (SUMMARY_EFFORT not sent)"


def effort_label(model: str, effort: str) -> str:
    """The effort a request to ``model`` really uses: SUMMARY_EFFORT, or the API's default when it is not sent."""
    return effort if model_profile(model).get("effort") else EFFORT_NOT_SENT


def model_pricing(model: str | None) -> dict[str, float] | None:
    return MODEL_PRICING.get(model or "")


@dataclass(frozen=True)
class Settings:
    source: str = "courtlistener"
    courtlistener_api_token: str | None = field(default=None, repr=False)
    anthropic_api_key_present: bool = False
    buttondown_api_key: str | None = field(default=None, repr=False)
    claude_model: str = DEFAULT_MODEL
    claude_effort: str = "low"
    refusal_fallback: bool = True
    max_output_tokens: int = 4000
    max_input_tokens_per_opinion: int = 30000
    max_opinions_per_run: int = 25
    auto_send: bool = False
    include_supreme_court: bool = True
    include_court_of_appeals: bool = True
    include_orders: bool = True
    lookback_days: int = 10
    max_attempts_per_item: int = 2
    contact_email: str | None = None
    buttondown_test_recipients: tuple[str, ...] = ()
    state_file: str = "data/processed.json"
    output_dir: str = "output"
    summary_cache_dir: str = ".cache/summaries"
    request_delay_seconds: float = 2.0
    log_level: str = "INFO"
    repository: str | None = None  # GITHUB_REPOSITORY, e.g. "owner/sc-advance-sheet-digest"
    in_actions: bool = False  # GITHUB_ACTIONS == "true"

    def with_overrides(self, **changes) -> Settings:
        """Copy with some fields replaced (used for CLI flags). None values are ignored."""
        return replace(self, **{k: v for k, v in changes.items() if v is not None})

    def describe(self) -> list[tuple[str, str]]:
        """Non-secret settings for the run log. Secrets show only whether they are set."""
        return [
            ("SOURCE", self.source),
            ("COURTLISTENER_API_TOKEN", "set" if self.courtlistener_api_token else "not set"),
            ("ANTHROPIC_API_KEY", "set" if self.anthropic_api_key_present else "not set"),
            ("BUTTONDOWN_API_KEY", "set" if self.buttondown_api_key else "not set"),
            ("CLAUDE_MODEL", self.claude_model),
            ("SUMMARY_EFFORT", self.claude_effort),
            ("SUMMARY_REFUSAL_FALLBACK", _onoff(self.refusal_fallback)),
            ("MAX_OUTPUT_TOKENS", str(self.max_output_tokens)),
            ("MAX_INPUT_TOKENS_PER_OPINION", str(self.max_input_tokens_per_opinion)),
            ("MAX_OPINIONS_PER_RUN", str(self.max_opinions_per_run)),
            ("AUTO_SEND", _onoff(self.auto_send)),
            ("INCLUDE_SUPREME_COURT", _onoff(self.include_supreme_court)),
            ("INCLUDE_COURT_OF_APPEALS", _onoff(self.include_court_of_appeals)),
            ("INCLUDE_ORDERS", _onoff(self.include_orders)),
            ("LOOKBACK_DAYS", str(self.lookback_days)),
            ("MAX_ATTEMPTS_PER_ITEM", str(self.max_attempts_per_item)),
            ("CONTACT_EMAIL", "set" if self.contact_email else "not set"),
            ("BUTTONDOWN_TEST_RECIPIENTS", f"{len(self.buttondown_test_recipients)} address(es)"),
            ("STATE_FILE", self.state_file),
            ("OUTPUT_DIR", self.output_dir),
        ]


def _onoff(value: bool) -> str:
    # "on"/"off" rather than "true"/"false": if a user stores a setting such as
    # AUTO_SEND as a GitHub *Secret*, GitHub masks that value ("false") everywhere in
    # the log, so printing true/false would turn into "***".
    return "on" if value else "off"


def parse_bool(name: str, value: str | None, default: bool) -> bool:
    if value is None or value.strip() == "":
        return default
    v = value.strip().lower()
    if v in ("1", "true", "yes", "y", "on"):
        return True
    if v in ("0", "false", "no", "n", "off"):
        return False
    raise ConfigError(f"{name} must be true or false (got {value!r})")


def parse_int(name: str, value: str | None, default: int, lo: int, hi: int) -> int:
    if value is None or value.strip() == "":
        return default
    try:
        n = int(value.strip())
    except ValueError:
        raise ConfigError(f"{name} must be a whole number (got {value!r})") from None
    if not lo <= n <= hi:
        raise ConfigError(f"{name} must be between {lo} and {hi} (got {n})")
    return n


def parse_float(name: str, value: str | None, default: float, lo: float, hi: float) -> float:
    if value is None or value.strip() == "":
        return default
    try:
        x = float(value.strip())
    except ValueError:
        raise ConfigError(f"{name} must be a number (got {value!r})") from None
    if not lo <= x <= hi:
        raise ConfigError(f"{name} must be between {lo} and {hi} (got {x})")
    return x


def _clean(value: str | None) -> str | None:
    """GitHub passes unset secrets/variables as empty strings; treat those as unset."""
    if value is None:
        return None
    value = value.strip()
    return value or None


def _read_dotenv(path: Path) -> dict[str, str]:
    if not path.is_file():
        return {}
    try:
        from dotenv import dotenv_values
    except ImportError:  # python-dotenv is in requirements.txt; tolerate its absence
        return {}
    values = {k: v for k, v in dotenv_values(path).items() if v is not None}
    for secret in _DOTENV_FORBIDDEN:
        if values.pop(secret, None):
            log.warning(
                "%s was found in %s and was IGNORED. Remove it from that file: this project "
                "never needs it on your computer (it belongs only in GitHub Secrets).",
                secret,
                path,
            )
    return values


def load_settings(env: Mapping[str, str] | None = None, dotenv_path: str | Path | None = ".env") -> Settings:
    """Build Settings from the environment (plus an optional .env file for local runs).

    Process environment variables win over .env values. Raises ConfigError on bad values.
    """
    if env is None:
        merged: dict[str, str] = {}
        if dotenv_path:
            merged.update(_read_dotenv(Path(dotenv_path)))
        merged.update(os.environ)
        env = merged

    def get(name: str) -> str | None:
        return _clean(env.get(name))

    source = (get("SOURCE") or "courtlistener").lower()
    if source not in VALID_SOURCES:
        raise ConfigError(f"SOURCE must be one of {', '.join(VALID_SOURCES)} (got {source!r})")

    model = get("CLAUDE_MODEL") or DEFAULT_MODEL
    if not re.fullmatch(r"[a-z0-9][a-z0-9.\-]{2,80}", model):
        raise ConfigError(f"CLAUDE_MODEL does not look like a model id (got {model!r})")

    effort = (get("SUMMARY_EFFORT") or "low").lower()
    if effort not in VALID_EFFORTS:
        raise ConfigError(f"SUMMARY_EFFORT must be one of {', '.join(VALID_EFFORTS)} (got {effort!r})")

    recipients_raw = get("BUTTONDOWN_TEST_RECIPIENTS") or ""
    recipients = tuple(r.strip() for r in re.split(r"[,;\s]+", recipients_raw) if r.strip())
    for r in recipients:
        if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", r):
            raise ConfigError(f"BUTTONDOWN_TEST_RECIPIENTS contains an invalid email address: {r!r}")

    contact = get("CONTACT_EMAIL")
    if contact and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", contact):
        raise ConfigError(f"CONTACT_EMAIL is not a valid email address (got {contact!r})")

    return Settings(
        source=source,
        courtlistener_api_token=get("COURTLISTENER_API_TOKEN"),
        anthropic_api_key_present=bool(get("ANTHROPIC_API_KEY")),
        buttondown_api_key=get("BUTTONDOWN_API_KEY"),
        claude_model=model,
        claude_effort=effort,
        refusal_fallback=parse_bool("SUMMARY_REFUSAL_FALLBACK", get("SUMMARY_REFUSAL_FALLBACK"), True),
        max_output_tokens=parse_int("MAX_OUTPUT_TOKENS", get("MAX_OUTPUT_TOKENS"), 4000, 1000, 16000),
        max_input_tokens_per_opinion=parse_int(
            "MAX_INPUT_TOKENS_PER_OPINION", get("MAX_INPUT_TOKENS_PER_OPINION"), 30000, 2000, 150000
        ),
        max_opinions_per_run=parse_int("MAX_OPINIONS_PER_RUN", get("MAX_OPINIONS_PER_RUN"), 25, 1, 200),
        auto_send=parse_bool("AUTO_SEND", get("AUTO_SEND"), False),
        include_supreme_court=parse_bool("INCLUDE_SUPREME_COURT", get("INCLUDE_SUPREME_COURT"), True),
        include_court_of_appeals=parse_bool("INCLUDE_COURT_OF_APPEALS", get("INCLUDE_COURT_OF_APPEALS"), True),
        include_orders=parse_bool("INCLUDE_ORDERS", get("INCLUDE_ORDERS"), True),
        lookback_days=parse_int("LOOKBACK_DAYS", get("LOOKBACK_DAYS"), 10, 1, 60),
        max_attempts_per_item=parse_int("MAX_ATTEMPTS_PER_ITEM", get("MAX_ATTEMPTS_PER_ITEM"), 2, 1, 5),
        contact_email=contact,
        buttondown_test_recipients=recipients,
        state_file=get("STATE_FILE") or "data/processed.json",
        output_dir=get("OUTPUT_DIR") or "output",
        summary_cache_dir=get("SUMMARY_CACHE_DIR") or ".cache/summaries",
        request_delay_seconds=parse_float("REQUEST_DELAY_SECONDS", get("REQUEST_DELAY_SECONDS"), 2.0, 0.0, 60.0),
        log_level=(get("LOG_LEVEL") or "INFO").upper(),
        repository=get("GITHUB_REPOSITORY"),
        in_actions=(get("GITHUB_ACTIONS") or "").lower() == "true",
    )


def user_agent(settings: Settings) -> str:
    """Identifying User-Agent for every outbound request, with a way to reach the operator."""
    details = []
    if settings.repository:
        details.append(f"+https://github.com/{settings.repository}")
    if settings.contact_email:
        details.append(settings.contact_email)
    if not details:
        details.append("weekly newsletter tool")
    return f"SC-Advance-Sheet-Digest/{__version__} ({'; '.join(details)})"
