"""Shared pytest helpers. Tests never use the network or a real API key."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


def fixture_text(relpath: str) -> str:
    return (FIXTURES / relpath).read_text(encoding="utf-8")


def fixture_bytes(relpath: str) -> bytes:
    return (FIXTURES / relpath).read_bytes()


def fixture_json(relpath: str):
    return json.loads(fixture_text(relpath))


@pytest.fixture(autouse=True)
def _no_real_credentials(monkeypatch):
    """Make sure no test can reach a real API with a real key, even if one is set locally."""
    for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "BUTTONDOWN_API_KEY", "COURTLISTENER_API_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    for name in ("GITHUB_ACTIONS", "GITHUB_OUTPUT", "GITHUB_STEP_SUMMARY"):
        monkeypatch.delenv(name, raising=False)
