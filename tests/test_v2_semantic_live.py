"""Live check of the real task analyst (Curvet). Off by default; costs a few cents.

    MODELMATCH_LIVE_TESTS=1 .venv/bin/python -m pytest tests/test_v2_semantic_live.py -q

Uses the router settings in .env. These prompts illustrate semantic behaviour; the
analyst is never given keyword rules for them.
"""

import asyncio
import os

import pytest

from proxy.config import get_settings
from proxy.v2.analyzer import CurvetAnalyzer
from proxy.v2.continuity import same_task

pytestmark = pytest.mark.skipif(os.environ.get("MODELMATCH_LIVE_TESTS") != "1",
                                reason="live Curvet test (set MODELMATCH_LIVE_TESTS=1)")

CASES = [
    # (prompt, acceptable tiers, acceptable task types)
    ("Get me the localhost link.", {"light"}, None),
    ("Change the button padding from 12px to 16px.", {"light"}, None),
    ("Design a multi-region payment architecture with retries, idempotency, webhooks, reconciliation and "
     "disaster recovery.", {"heavy"}, {"architecture", "system_design"}),
    ("Our React checkout occasionally sends duplicate payment requests after a backend retry. Trace the "
     "interaction and identify the race condition.", {"heavy"}, {"complex_debugging", "debugging"}),
    ("Write a short README section explaining how to run the tests.", {"light", "standard"}, {"documentation"}),
    ("Refactor the user service so the database calls go through a repository class.", {"standard", "heavy"},
     {"refactor", "backend_implementation"}),
    ("Now implement the React login form with email and password validation.", {"standard"},
     {"frontend_implementation", "frontend_ui"}),
]


@pytest.fixture(scope="module")
def analyzer():
    settings = get_settings()
    if settings.api_style != "curvet" or not settings.api_key:
        pytest.skip("Curvet is not configured in .env")
    return CurvetAnalyzer(settings)


@pytest.mark.parametrize("prompt, tiers, types", CASES)
def test_semantic_tiers(analyzer, prompt, tiers, types):
    result = asyncio.run(analyzer.analyze(prompt, "claude-code"))
    assert result.required_tier in tiers, result
    if types:
        assert result.task_type in types, result
    assert result.latency_ms is not None and result.latency_ms < 8000


def test_ambiguous_task_is_flagged(analyzer):
    result = asyncio.run(analyzer.analyze(
        "Fix this React performance issue. It may require changing our state architecture.", "claude-code"))
    assert result.required_tier in ("standard", "heavy")
    assert result.ambiguous or (result.confidence or 1) < 0.85, result


def test_follow_up_is_recognised_as_the_same_task(analyzer):
    previous = {"task_type": "architecture", "subtype": "authentication architecture design", "minutes_ago": 3}
    follow_up = asyncio.run(analyzer.analyze("How should token rotation work?", "claude-code", previous))
    assert same_task("architecture", 1.0, follow_up, at=60.0)[0], follow_up
    next_phase = asyncio.run(analyzer.analyze("Now implement the React login form.", "claude-code", previous))
    assert not same_task("architecture", 1.0, next_phase, at=60.0)[0], next_phase
