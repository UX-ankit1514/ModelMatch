"""Stay-vs-switch economics: cache-aware, and honest about what it doesn't know."""

from proxy.v2.economics import describe, estimate
from proxy.v2.schema import CacheState, ImpactType, SessionSnapshot, TaskAnalysis

OPUS, SONNET, HAIKU = "claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-4-5"


def snap(ctx=None, current=CacheState.UNKNOWN, candidate=CacheState.UNKNOWN, billing="subscription", measured=None,
         observable=True):
    return SessionSnapshot(session_id="s", client="claude-code", current_model=OPUS, effective_model=OPUS,
                           estimated_context_tokens=ctx, cache_state_current=current, cache_state_candidate=candidate,
                           billing_mode=billing, observable=observable,
                           measured=bool(ctx) if measured is None else measured)


SMALL_EDIT = TaskAnalysis(task_type="frontend_ui", complexity="low", reasoning_requirement="low", required_tier="light",
                          confidence=0.9, expected_output="short", expected_tool_steps="few")


def test_warm_opus_vs_cold_sonnet_staying_is_cheaper():
    e = estimate(OPUS, SONNET, snap(100000, CacheState.WARM, CacheState.COLD, "api_key"), SMALL_EDIT)
    assert e.verdict == "stay_cheaper" and e.ratio > 3
    assert e.stay.type == ImpactType.MONEY and e.stay.confidence == "medium"
    assert e.stay.min <= e.stay.estimated_value <= e.stay.max
    assert e.cache_current == CacheState.WARM and e.cache_candidate == CacheState.COLD
    assert describe(e, "Opus 5.5", "Sonnet 5.5").startswith("Estimated for this task: staying on Opus 5.5 ~$")


def test_haiku_is_also_more_expensive_mid_conversation():
    e = estimate(OPUS, HAIKU, snap(100000, CacheState.WARM, CacheState.COLD, "api_key"), SMALL_EDIT)
    assert e.verdict == "stay_cheaper"


def test_both_cold_the_lighter_model_is_cheaper():
    e = estimate(OPUS, SONNET, snap(40000, CacheState.COLD, CacheState.COLD, "api_key"), SMALL_EDIT)
    assert e.verdict == "switch_cheaper" and e.ratio < 0.75


def test_small_context_switching_pays_off_even_when_warm():
    e = estimate(OPUS, SONNET, snap(3000, CacheState.WARM, CacheState.COLD, "api_key"), SMALL_EDIT)
    assert e.verdict == "switch_cheaper"


def test_large_context_costs_more_to_switch_than_small():
    small = estimate(OPUS, SONNET, snap(10000, CacheState.WARM, CacheState.COLD, "api_key"), SMALL_EDIT)
    large = estimate(OPUS, SONNET, snap(150000, CacheState.WARM, CacheState.COLD, "api_key"), SMALL_EDIT)
    assert large.ratio > small.ratio


def test_start_of_session_uses_a_low_confidence_assumption_and_no_dollars():
    e = estimate(OPUS, SONNET, snap(None, billing="unknown", measured=False), SMALL_EDIT)
    assert e.verdict == "switch_cheaper" and "start of session" in " ".join(e.notes)
    assert e.stay.type != ImpactType.MONEY and e.stay.confidence == "low"
    assert "$" not in (describe(e, "Opus", "Sonnet") or "")


def test_unknown_cache_state_with_events_is_unknown():
    e = estimate(OPUS, SONNET, snap(None, measured=True), SMALL_EDIT)
    assert e.verdict == "unknown" and e.stay.type == ImpactType.UNKNOWN


def test_subscription_is_shown_as_usage_not_dollars():
    e = estimate(OPUS, SONNET, snap(100000, CacheState.WARM, CacheState.COLD, "subscription"), SMALL_EDIT)
    assert e.stay.type == ImpactType.SUBSCRIPTION_USAGE and e.verdict == "stay_cheaper"
    text = describe(e, "Opus 5.5", "Sonnet 5.5")
    assert "$" not in text and "usage" in text


def test_unknown_prices_fall_back_to_tokens_or_unknown():
    measured = estimate("gpt-5.6-sol", "gpt-5.6-luna", snap(90000, CacheState.WARM, CacheState.COLD), SMALL_EDIT)
    assert measured.stay.type == ImpactType.TOKENS and measured.verdict == "stay_cheaper"
    blind = estimate("gpt-5.6-sol", "gpt-5.6-luna", snap(None, measured=False), SMALL_EDIT)
    assert blind.verdict == "unknown"


def test_unobservable_tools_have_unknown_economics():
    e = estimate(OPUS, SONNET, snap(observable=False), SMALL_EDIT)
    assert e.verdict == "unknown" and describe(e, "a", "b") is None


def test_usage_pressure_makes_smaller_savings_worth_it():
    analysis = TaskAnalysis(task_type="frontend_ui", complexity="medium", reasoning_requirement="medium",
                            required_tier="standard", confidence=0.9, expected_output="medium",
                            expected_tool_steps="several")
    relaxed = snap(20000, CacheState.WARM, CacheState.COLD, "subscription")  # switching saves ~17%
    pressured = snap(20000, CacheState.WARM, CacheState.COLD, "subscription")
    pressured.usage_pressure = 0.92  # 92% of the plan limit used
    calm, tight = estimate(OPUS, SONNET, relaxed, analysis), estimate(OPUS, SONNET, pressured, analysis)
    assert calm.ratio == tight.ratio and 0.75 < calm.ratio < 0.9
    assert calm.verdict == "similar" and tight.verdict == "switch_cheaper"
