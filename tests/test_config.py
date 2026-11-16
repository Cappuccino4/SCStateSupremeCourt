"""Tests for src/config.py: parsing, validation, secrets handling, and the User-Agent."""

from __future__ import annotations

import dataclasses
import logging

import pytest

from src import __version__
from src.config import (
    CHEAP_MODEL,
    DEFAULT_MODEL,
    DEFAULT_PROFILE,
    MODEL_PRICING,
    REFUSAL_FALLBACK_BETA,
    VALID_EFFORTS,
    ConfigError,
    Settings,
    load_settings,
    model_pricing,
    model_profile,
    parse_bool,
    parse_float,
    parse_int,
    user_agent,
)

SETTING_NAMES = (
    "SOURCE", "CLAUDE_MODEL", "SUMMARY_EFFORT", "SUMMARY_REFUSAL_FALLBACK", "MAX_OUTPUT_TOKENS",
    "MAX_INPUT_TOKENS_PER_OPINION", "MAX_OPINIONS_PER_RUN", "AUTO_SEND", "INCLUDE_SUPREME_COURT",
    "INCLUDE_COURT_OF_APPEALS", "INCLUDE_ORDERS", "LOOKBACK_DAYS", "MAX_ATTEMPTS_PER_ITEM", "CONTACT_EMAIL",
    "BUTTONDOWN_TEST_RECIPIENTS", "STATE_FILE", "OUTPUT_DIR", "SUMMARY_CACHE_DIR", "REQUEST_DELAY_SECONDS",
    "LOG_LEVEL", "GITHUB_REPOSITORY",
)  # fmt: skip


@pytest.fixture
def clean_env(monkeypatch):
    """No setting from the developer's shell (conftest already removes the API keys)."""
    for name in SETTING_NAMES:
        monkeypatch.delenv(name, raising=False)


def load(**env: str) -> Settings:
    return load_settings(env=env)


# --------------------------------------------------------------------------- defaults


def test_defaults():
    s = load()
    assert s == Settings()
    assert s.source == "courtlistener"
    assert s.claude_model == DEFAULT_MODEL == "claude-sonnet-5-5"
    assert CHEAP_MODEL == "claude-haiku-5-5"
    assert s.claude_effort == "low"
    assert s.max_output_tokens == 4000 and s.max_input_tokens_per_opinion == 30000
    assert s.max_opinions_per_run == 25 and s.lookback_days == 10 and s.max_attempts_per_item == 2
    assert s.auto_send is False  # drafts by default
    assert s.include_supreme_court and s.include_court_of_appeals and s.include_orders
    assert s.state_file == "data/processed.json" and s.output_dir == "output"
    assert s.summary_cache_dir == ".cache/summaries"
    assert s.request_delay_seconds == 2.0
    assert s.anthropic_api_key_present is False and s.buttondown_api_key is None
    assert s.courtlistener_api_token is None and s.buttondown_test_recipients == ()
    assert s.in_actions is False and s.repository is None


def test_empty_strings_mean_unset():
    # GitHub Actions passes an unset secret or variable as an empty string.
    blank = {name: "" for name in SETTING_NAMES}
    blank.update(ANTHROPIC_API_KEY="", BUTTONDOWN_API_KEY="", COURTLISTENER_API_TOKEN="", GITHUB_ACTIONS="")
    assert load(**blank) == Settings()


def test_whitespace_only_values_mean_unset():
    s = load(AUTO_SEND="  ", MAX_OPINIONS_PER_RUN=" \t", CLAUDE_MODEL=" ", ANTHROPIC_API_KEY="   ")
    assert s.auto_send is False and s.max_opinions_per_run == 25 and s.claude_model == DEFAULT_MODEL
    assert s.anthropic_api_key_present is False


def test_values_are_stripped():
    s = load(COURTLISTENER_API_TOKEN="  tok  ", BUTTONDOWN_API_KEY=" key\n", SOURCE=" SCCourts ")
    assert s.courtlistener_api_token == "tok" and s.buttondown_api_key == "key" and s.source == "sccourts"


def test_settings_are_frozen():
    with pytest.raises(dataclasses.FrozenInstanceError):
        Settings().auto_send = True  # type: ignore[misc]


# --------------------------------------------------------------------------- parsing and validation


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "True", "yes", "y", "on", " on "])
def test_parse_bool_true(value):
    assert parse_bool("AUTO_SEND", value, False) is True


@pytest.mark.parametrize("value", ["0", "false", "FALSE", "no", "n", "off"])
def test_parse_bool_false(value):
    assert parse_bool("AUTO_SEND", value, True) is False


@pytest.mark.parametrize("value", [None, "", "   "])
def test_parse_bool_default(value):
    assert parse_bool("AUTO_SEND", value, True) is True


def test_parse_bool_invalid_names_the_variable():
    with pytest.raises(ConfigError, match=r"AUTO_SEND must be true or false \(got 'sometimes'\)"):
        parse_bool("AUTO_SEND", "sometimes", False)


def test_parse_int():
    assert parse_int("N", " 7 ", 1, 1, 10) == 7
    assert parse_int("N", "", 3, 1, 10) == 3
    with pytest.raises(ConfigError, match=r"N must be between 1 and 10 \(got 11\)"):
        parse_int("N", "11", 3, 1, 10)
    with pytest.raises(ConfigError, match="N must be a whole number"):
        parse_int("N", "2.5", 3, 1, 10)


def test_parse_float():
    assert parse_float("D", "0", 2.0, 0.0, 60.0) == 0.0
    assert parse_float("D", "1.5", 2.0, 0.0, 60.0) == 1.5
    with pytest.raises(ConfigError, match="D must be a number"):
        parse_float("D", "fast", 2.0, 0.0, 60.0)
    with pytest.raises(ConfigError, match="between"):
        parse_float("D", "61", 2.0, 0.0, 60.0)


@pytest.mark.parametrize(
    ("name", "value", "message"),
    [
        ("SOURCE", "westlaw", "SOURCE must be one of courtlistener, sccourts"),
        ("CLAUDE_MODEL", "Claude Sonnet", "CLAUDE_MODEL does not look like a model id"),
        ("CLAUDE_MODEL", "x", "CLAUDE_MODEL does not look like a model id"),
        ("SUMMARY_EFFORT", "extreme", "SUMMARY_EFFORT must be one of"),
        ("MAX_OUTPUT_TOKENS", "999", "MAX_OUTPUT_TOKENS must be between 1000 and 16000"),
        ("MAX_INPUT_TOKENS_PER_OPINION", "1999", "MAX_INPUT_TOKENS_PER_OPINION must be between 2000 and 150000"),
        ("MAX_OPINIONS_PER_RUN", "0", "MAX_OPINIONS_PER_RUN must be between 1 and 200"),
        ("MAX_OPINIONS_PER_RUN", "ten", "MAX_OPINIONS_PER_RUN must be a whole number"),
        ("LOOKBACK_DAYS", "61", "LOOKBACK_DAYS must be between 1 and 60"),
        ("MAX_ATTEMPTS_PER_ITEM", "6", "MAX_ATTEMPTS_PER_ITEM must be between 1 and 5"),
        ("AUTO_SEND", "maybe", "AUTO_SEND must be true or false"),
        ("INCLUDE_ORDERS", "2", "INCLUDE_ORDERS must be true or false"),
        ("SUMMARY_REFUSAL_FALLBACK", "nah", "SUMMARY_REFUSAL_FALLBACK must be true or false"),
        ("REQUEST_DELAY_SECONDS", "-1", "REQUEST_DELAY_SECONDS must be between 0.0 and 60.0"),
        ("CONTACT_EMAIL", "not-an-email", "CONTACT_EMAIL is not a valid email address"),
        ("BUTTONDOWN_TEST_RECIPIENTS", "me@example.com, nope", "invalid email address: 'nope'"),
    ],
)
def test_invalid_values_raise_config_error_naming_the_variable(name, value, message):
    with pytest.raises(ConfigError) as exc:
        load(**{name: value})
    assert message in str(exc.value)


def test_config_error_is_a_value_error():
    assert issubclass(ConfigError, ValueError)


def test_valid_values():
    s = load(
        SOURCE="sccourts",
        CLAUDE_MODEL="claude-haiku-5-5",
        SUMMARY_EFFORT="HIGH",
        SUMMARY_REFUSAL_FALLBACK="false",
        MAX_OUTPUT_TOKENS="16000",
        MAX_INPUT_TOKENS_PER_OPINION="2000",
        MAX_OPINIONS_PER_RUN="200",
        AUTO_SEND="true",
        INCLUDE_SUPREME_COURT="no",
        INCLUDE_COURT_OF_APPEALS="off",
        INCLUDE_ORDERS="0",
        LOOKBACK_DAYS="60",
        MAX_ATTEMPTS_PER_ITEM="5",
        STATE_FILE="state/p.json",
        OUTPUT_DIR="out",
        SUMMARY_CACHE_DIR="cache",
        REQUEST_DELAY_SECONDS="0.5",
        LOG_LEVEL="debug",
        GITHUB_ACTIONS="true",
        GITHUB_REPOSITORY="owner/repo",
    )
    assert s.source == "sccourts" and s.claude_model == "claude-haiku-5-5" and s.claude_effort == "high"
    assert s.refusal_fallback is False and s.max_output_tokens == 16000
    assert s.max_input_tokens_per_opinion == 2000 and s.max_opinions_per_run == 200
    assert s.auto_send is True
    assert not (s.include_supreme_court or s.include_court_of_appeals or s.include_orders)
    assert s.lookback_days == 60 and s.max_attempts_per_item == 5
    assert (s.state_file, s.output_dir, s.summary_cache_dir) == ("state/p.json", "out", "cache")
    assert s.request_delay_seconds == 0.5 and s.log_level == "DEBUG"
    assert s.in_actions is True and s.repository == "owner/repo"


@pytest.mark.parametrize("effort", VALID_EFFORTS)
def test_every_valid_effort_is_accepted(effort):
    assert load(SUMMARY_EFFORT=effort).claude_effort == effort


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("a@example.com", ("a@example.com",)),
        ("a@example.com,b@example.com", ("a@example.com", "b@example.com")),
        ("a@example.com; b@example.com", ("a@example.com", "b@example.com")),
        ("a@example.com b@example.com\nc@example.com", ("a@example.com", "b@example.com", "c@example.com")),
        (" , ", ()),
    ],
)
def test_test_recipients_are_split_on_commas_semicolons_and_spaces(raw, expected):
    assert load(BUTTONDOWN_TEST_RECIPIENTS=raw).buttondown_test_recipients == expected


def test_github_actions_must_be_exactly_true():
    assert load(GITHUB_ACTIONS="TRUE").in_actions is True
    assert load(GITHUB_ACTIONS="1").in_actions is False


# --------------------------------------------------------------------------- secrets


def test_anthropic_key_is_recorded_only_as_present():
    s = load(ANTHROPIC_API_KEY="sk-ant-secret-value")
    assert s.anthropic_api_key_present is True
    assert "sk-ant-secret-value" not in repr(s)
    assert not any("sk-ant-secret-value" in str(getattr(s, f.name)) for f in dataclasses.fields(s))


def test_secrets_never_appear_in_repr_or_describe():
    s = load(
        ANTHROPIC_API_KEY="sk-ant-secret-value",
        BUTTONDOWN_API_KEY="bd-secret-value",
        COURTLISTENER_API_TOKEN="cl-secret-value",
        CONTACT_EMAIL="me@example.com",
    )
    text = repr(s) + " " + " ".join(f"{k}={v}" for k, v in s.describe())
    for secret in ("sk-ant-secret-value", "bd-secret-value", "cl-secret-value"):
        assert secret not in text
    described = dict(s.describe())
    assert described["ANTHROPIC_API_KEY"] == "set"
    assert described["BUTTONDOWN_API_KEY"] == "set"
    assert described["COURTLISTENER_API_TOKEN"] == "set"
    assert described["CONTACT_EMAIL"] == "set"
    assert dict(load().describe())["BUTTONDOWN_API_KEY"] == "not set"


def test_describe_prints_on_off_rather_than_true_false():
    # A setting stored as a GitHub *secret* would mask every "false" in the log as "***".
    described = dict(load(AUTO_SEND="true", INCLUDE_ORDERS="false").describe())
    assert described["AUTO_SEND"] == "on" and described["INCLUDE_ORDERS"] == "off"
    assert "true" not in described.values() and "false" not in described.values()


# --------------------------------------------------------------------------- .env


def test_dotenv_values_are_used_but_secrets_in_it_are_ignored(tmp_path, clean_env, caplog):
    dotenv = tmp_path / ".env"
    dotenv.write_text(
        "ANTHROPIC_API_KEY=sk-ant-from-dotenv\n"
        "BUTTONDOWN_API_KEY=bd-from-dotenv\n"
        "CLAUDE_MODEL=claude-haiku-5-5\n"
        "LOOKBACK_DAYS=14\n"
        "COURTLISTENER_API_TOKEN=cl-from-dotenv\n",
        encoding="utf-8",
    )
    caplog.set_level(logging.WARNING, logger="src.config")
    s = load_settings(dotenv_path=dotenv)
    assert s.anthropic_api_key_present is False
    assert s.buttondown_api_key is None
    assert s.claude_model == "claude-haiku-5-5" and s.lookback_days == 14
    assert s.courtlistener_api_token == "cl-from-dotenv"  # not a billing secret; allowed locally
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 2
    assert all("IGNORED" in w for w in warnings)
    assert any(w.startswith("ANTHROPIC_API_KEY was found") for w in warnings)
    assert not any("sk-ant-from-dotenv" in w or "bd-from-dotenv" in w for w in warnings)


def test_process_environment_wins_over_dotenv(tmp_path, clean_env, monkeypatch):
    dotenv = tmp_path / ".env"
    dotenv.write_text("CLAUDE_MODEL=claude-haiku-5-5\nMAX_OPINIONS_PER_RUN=5\n", encoding="utf-8")
    monkeypatch.setenv("CLAUDE_MODEL", "claude-opus-5-5")
    s = load_settings(dotenv_path=dotenv)
    assert s.claude_model == "claude-opus-5-5" and s.max_opinions_per_run == 5


def test_real_environment_keys_are_still_read(clean_env, monkeypatch, tmp_path):
    # In GitHub Actions the keys come from the process environment (repository secrets).
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-not-a-real-key")
    monkeypatch.setenv("BUTTONDOWN_API_KEY", "bd-test-not-a-real-key")
    s = load_settings(dotenv_path=tmp_path / "missing.env")
    assert s.anthropic_api_key_present is True and s.buttondown_api_key == "bd-test-not-a-real-key"


def test_missing_dotenv_file_and_disabled_dotenv_are_fine(tmp_path, clean_env):
    assert load_settings(dotenv_path=tmp_path / "nope.env") == Settings()
    assert load_settings(dotenv_path=None) == Settings()


def test_an_explicit_env_mapping_never_reads_dotenv(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text("CLAUDE_MODEL=claude-haiku-5-5\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    assert load_settings(env={}).claude_model == DEFAULT_MODEL


# --------------------------------------------------------------------------- helpers


def test_with_overrides_ignores_none():
    s = Settings()
    changed = s.with_overrides(lookback_days=14, source=None, output_dir="x")
    assert changed.lookback_days == 14 and changed.source == "courtlistener" and changed.output_dir == "x"
    assert s.lookback_days == 10  # the original is unchanged


def test_user_agent_names_the_project_and_a_contact():
    base = f"SC-Advance-Sheet-Digest/{__version__}"
    assert user_agent(Settings()) == f"{base} (weekly newsletter tool)"
    assert user_agent(Settings(repository="owner/repo")) == f"{base} (+https://github.com/owner/repo)"
    assert user_agent(Settings(repository="owner/repo", contact_email="me@example.com")) == (
        f"{base} (+https://github.com/owner/repo; me@example.com)"
    )
    assert user_agent(Settings(contact_email="me@example.com")) == f"{base} (me@example.com)"


def test_model_profiles():
    assert model_profile("claude-sonnet-5-5") == {"effort": True, "refusal_fallback": True}
    assert model_profile("claude-haiku-5-5")["refusal_fallback"] is False  # Haiku 5.5 has no fallback
    assert model_profile("claude-unknown-9") == DEFAULT_PROFILE == {"effort": False, "refusal_fallback": False}
    assert REFUSAL_FALLBACK_BETA == "server-side-fallback-2026-07-01"


@pytest.mark.parametrize(
    ("model", "fallback"), [("claude-sonnet-5", False), ("claude-opus-5", True), ("claude-fable-5-2", True)]
)
def test_current_models_that_accept_effort_have_profiles(model, fallback):
    # Without a profile, SUMMARY_EFFORT is silently not sent and the API's default ("high") applies.
    assert model_profile(model) == {"effort": True, "refusal_fallback": fallback}
    assert model_pricing(model) is not None


def test_effort_label_says_when_summary_effort_is_not_sent():
    from src.config import EFFORT_NOT_SENT, effort_label

    assert effort_label("claude-sonnet-5-5", "low") == "low"
    assert effort_label("claude-unknown-9", "low") == EFFORT_NOT_SENT == "API default (SUMMARY_EFFORT not sent)"


def test_haiku_has_a_long_prompt_price_tier():
    prices = MODEL_PRICING["claude-haiku-5-5"]
    assert prices["long_prompt_threshold"] == 100_000
    assert (prices["long_input"], prices["long_output"]) == (0.50, 2.50)


def test_model_pricing():
    assert model_pricing("claude-sonnet-5-5") == MODEL_PRICING["claude-sonnet-5-5"]
    assert model_pricing("claude-sonnet-5") is not None  # the refusal fallback model is priced too
    assert model_pricing("claude-unknown-9") is None
    assert model_pricing(None) is None
    for prices in MODEL_PRICING.values():
        assert {"input", "output", "cache_read", "cache_write"} <= set(prices)
        assert all(value > 0 for value in prices.values())


def test_claude_codes_own_effort_variable_is_ignored():
    # Claude Code exports CLAUDE_EFFORT (e.g. "max") to the processes it starts; it must never
    # leak into the summarizer's effort, which has its own name.
    settings = load_settings(env={"CLAUDE_EFFORT": "max"})
    assert settings.claude_effort == "low"
    assert load_settings(env={"CLAUDE_EFFORT": "max", "SUMMARY_EFFORT": "medium"}).claude_effort == "medium"
