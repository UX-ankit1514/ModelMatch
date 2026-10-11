"""The V2 decision engine: what to recommend, and when to interrupt."""

import pytest

from proxy.v2.adapters import ClaudeCodeAdapter
from proxy.v2.economics import estimate
from proxy.v2.engine import DecisionInputs, decide, explicit_request
from proxy.v2.profiles import build_profile
from proxy.v2.schema import CacheState, Intervention, SessionSnapshot, TaskAnalysis

OPUS, SONNET, HAIKU = "claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-4-5"
NOW = 1_800_000_000.0


def adapter(**kw):
    tiers = {"light": HAIKU, "standard": SONNET, "heavy": OPUS}
    return ClaudeCodeAdapter(tiers, kw.get("denied", ()), kw.get("allowed", ()), policy_files=[])


def task(tier="standard", task_type="frontend_implementation", complexity="medium", confidence=0.9, **kw):
    return TaskAnalysis(task_type=task_type, complexity=complexity, reasoning_requirement=complexity,
                        required_tier=tier, confidence=confidence, expected_output=kw.pop("expected_output", "short"),
                        expected_tool_steps=kw.pop("expected_tool_steps", "few"), source="test", **kw)


def snapshot(ctx=None, current=CacheState.UNKNOWN, candidate=CacheState.UNKNOWN, billing="subscription",
             measured=None, observable=True):
    return SessionSnapshot(session_id="s", client="claude-code", current_model=OPUS, effective_model=OPUS,
                           estimated_context_tokens=ctx, cache_state_current=current, cache_state_candidate=candidate,
                           billing_mode=billing, observable=observable,
                           measured=bool(ctx) if measured is None else measured)


def economics_for(snap, analysis):
    return lambda stay, switch: estimate(stay, switch, snap, analysis)


def row(family, outcome="no_negative_signal", semantic_tier="heavy", days_ago=1.0, **extra):
    data = {"ts": NOW - days_ago * 86400, "ran_family": family, "outcome": outcome, "semantic_tier": semantic_tier}
    data.update(extra)
    return data


def run(analysis, running=OPUS, current=OPUS, snap=None, profile=None, prompt="", same_task=False, auto=False,
        routing=True, a=None):
    snap = snap or snapshot()
    return decide(DecisionInputs(client="claude-code", session_id="s", adapter=a or adapter(), current_model=current,
                                 effective_model=running, analysis=analysis, prompt=prompt, same_task=same_task,
                                 profile=profile, economics=economics_for(snap, analysis), snapshot=snap,
                                 routing_enabled=routing, auto_route=auto))


# -- the five cases from the spec ---------------------------------------------------


def test_case1_current_model_already_good_is_silent():
    d = run(task("heavy", "architecture", "high"))
    assert d.intervention == Intervention.SILENT and d.final_model == OPUS and not d.options
    assert "CURRENT_MODEL_ALREADY_GOOD" in d.reason_codes and "ARCHITECTURE_REASONING" in d.reason_codes


def test_case2_tiny_difference_is_silent():
    same_tier = run(task("standard"), running="claude-sonnet-5", current="claude-sonnet-5")
    assert same_tier.intervention == Intervention.SILENT and "SAME_TIER" in same_tier.reason_codes
    # A lighter model that would save only ~10% in this session (Opus warm, Sonnet cold): don't interrupt.
    snap = snapshot(ctx=60000, current=CacheState.WARM, candidate=CacheState.COLD, billing="api_key")
    marginal = run(task("standard", expected_output="medium", expected_tool_steps="several"), snap=snap)
    assert marginal.economics.verdict == "similar" and 0.8 < marginal.economics.ratio < 1.25
    assert marginal.intervention == Intervention.SILENT and marginal.final_model == OPUS
    assert "SWITCH_BENEFIT_TOO_SMALL" in marginal.reason_codes


def test_case3_strong_benefit_asks_while_calibrating_and_auto_routes_with_evidence():
    analysis = task("standard", "frontend_ui", "low")
    ask = run(analysis)  # start of session: nothing cached, switching is cheap
    assert ask.intervention == Intervention.ASK and ask.final_model == SONNET
    assert ask.options[0].model_id == SONNET and ask.options[0].primary and ask.options[1].model_id is None
    assert "SWITCH_EXPECTED_SAVINGS" in ask.reason_codes
    evidence = build_profile([row("sonnet", semantic_tier="standard", days_ago=i) for i in range(6)],
                             "claude-code", "frontend_ui", NOW)
    auto = run(analysis, profile=evidence, auto=True)
    assert auto.intervention == Intervention.AUTO and auto.final_model == SONNET
    assert auto.note and "use opus" in auto.note.lower()
    assert run(analysis, profile=evidence, auto=False).intervention == Intervention.ASK  # auto-route off by default


def test_case4_trade_off_asks_with_stay_recommended():
    analysis = task("standard", "frontend_implementation", "medium")
    warm = snapshot(ctx=95000, current=CacheState.WARM, candidate=CacheState.COLD)
    habit = build_profile([row("sonnet", semantic_tier="standard", days_ago=i) for i in range(5)],
                          "claude-code", "frontend_implementation", NOW)
    d = run(analysis, snap=warm, profile=habit)
    assert d.intervention == Intervention.ASK and d.final_model == OPUS
    assert d.options[0].label == "Stay on Opus 5.5" and d.options[0].primary and d.options[0].model_id is None
    assert d.options[1].label == "Use Sonnet 5.5 anyway" and d.options[1].model_id == SONNET
    assert "You normally use Sonnet 5.5 for frontend work" in d.explanation
    assert {"SWITCH_USAGE_PENALTY", "CURRENT_CACHE_WARM", "CANDIDATE_CACHE_COLD"} <= set(d.reason_codes)


def test_case5_ambiguous_task_asks():
    analysis = task("heavy", "debugging", "high", secondary_task_type="architecture", secondary_probability=0.45)
    d = run(analysis, running=SONNET, current=SONNET)
    assert d.intervention == Intervention.ASK and d.final_model == OPUS
    assert "crosses debugging and architecture work" in d.explanation and "AMBIGUOUS_TASK" in d.reason_codes


# -- the critical test -------------------------------------------------------------------


@pytest.mark.parametrize("billing", ["subscription", "api_key"])
def test_critical_warm_opus_session_is_not_blindly_routed_to_sonnet(billing):
    """Opus, frontend task, Sonnet preference, large warm Opus context, cold Sonnet: stay or ask, never route."""
    analysis = task("standard", "frontend_ui", "low")
    warm = snapshot(ctx=95000, current=CacheState.WARM, candidate=CacheState.COLD, billing=billing)
    habit = build_profile([row("sonnet", semantic_tier="standard", days_ago=i) for i in range(8)],
                          "claude-code", "frontend_ui", NOW)
    for auto in (False, True):
        d = run(analysis, snap=warm, profile=habit, auto=auto)
        assert d.final_model == OPUS, d
        assert d.intervention in (Intervention.SILENT, Intervention.ASK)
        assert "SWITCH_USAGE_PENALTY" in d.reason_codes
        assert d.economics.verdict == "stay_cheaper" and d.economics.ratio > 2
    no_habit = run(analysis, snap=warm)
    assert no_habit.intervention == Intervention.SILENT and no_habit.final_model == OPUS


# -- learning from outcomes, not clicks ------------------------------------------------------


def test_beginner_bad_habit_is_not_learned():
    """Opus chosen again and again for small UI edits: ModelMatch keeps offering the lighter model."""
    rows = [row("opus", semantic_tier="light", days_ago=i * 0.5) for i in range(15)]
    habit = build_profile(rows, "claude-code", "small_edit", NOW)
    assert habit.preferred_family == "opus" and habit.families["opus"].overprovisioned > 5
    d = run(task("light", "small_edit", "low"), profile=habit)
    assert d.candidate_model == HAIKU and d.suggested_lower
    assert "PAST_OPUS_OVERPROVISIONING" in d.reason_codes
    assert d.intervention == Intervention.ASK and d.final_model == HAIKU


def test_escalation_history_raises_the_capability_floor():
    rows = [row("sonnet", "escalated", "standard", days_ago=i) for i in range(3)]
    rows += [row("opus", "no_negative_signal", "standard", days_ago=i + 0.1) for i in range(3)]
    profile = build_profile(rows, "claude-code", "complex_debugging", NOW)
    d = run(task("standard", "complex_debugging", "medium"), running=SONNET, current=SONNET, profile=profile)
    assert d.candidate_model == OPUS and d.intervention == Intervention.ASK
    assert "PAST_SONNET_ESCALATION" in d.reason_codes
    assert "needed a stronger model before" in d.explanation


def test_proven_lower_tier_can_be_suggested_but_not_for_high_complexity():
    rows = [row("sonnet", semantic_tier="heavy", days_ago=i) for i in range(6)]
    profile = build_profile(rows, "claude-code", "architecture", NOW)
    medium = run(task("heavy", "architecture", "medium"), profile=profile)
    assert medium.candidate_model == SONNET and "PAST_SONNET_SUCCESS" in medium.reason_codes
    high = run(task("heavy", "architecture", "high"), profile=profile)
    assert high.candidate_model == OPUS and high.intervention == Intervention.SILENT


def test_repeated_declines_turn_the_popup_into_a_note_and_reoffer_later():
    def profile_with(declines):
        rows = [row("opus", semantic_tier="light", suggested_lower=1, accepted=0, intervention="ASK",
                    days_ago=i * 0.1) for i in range(declines)]
        return build_profile(rows, "claude-code", "small_edit", NOW)

    quiet = run(task("light", "small_edit", "low"), profile=profile_with(4))
    assert quiet.intervention == Intervention.SILENT and quiet.final_model == OPUS
    assert quiet.note and "Haiku 4.5 would likely handle this too" in quiet.note
    assert "USER_KEEPS_DECLINING" in quiet.reason_codes
    again = run(task("light", "small_edit", "low"), profile=profile_with(6))
    assert again.intervention == Intervention.ASK


# -- explicit intent, availability, routing --------------------------------------------------


def test_explicit_model_request_always_wins():
    d = run(task("light", "small_edit", "low"), running=SONNET, current=SONNET, prompt="Use Opus for this please")
    assert d.intervention == Intervention.AUTO and d.final_model == OPUS
    assert "EXPLICIT_MODEL_REQUEST" in d.reason_codes and "as you asked" in d.note
    via_analyst = run(task("light", "small_edit", "low", explicit_model_request="haiku"))
    assert via_analyst.final_model == HAIKU and via_analyst.intervention == Intervention.AUTO
    already = run(task("heavy"), prompt="use opus")
    assert already.intervention == Intervention.SILENT


def test_explicit_request_works_even_when_the_analyst_is_down():
    d = decide(DecisionInputs(client="claude-code", session_id="s", adapter=adapter(), current_model=OPUS,
                              effective_model=OPUS, analysis=None, analysis_error="timeout",
                              prompt="switch to sonnet and fix it"))
    assert d.final_model == SONNET and d.intervention == Intervention.AUTO


def test_analyst_failure_keeps_the_model():
    d = decide(DecisionInputs(client="claude-code", session_id="s", adapter=adapter(), current_model=OPUS,
                              effective_model=OPUS, analysis=None, analysis_error="router API timed out"))
    assert d.intervention == Intervention.SILENT and d.final_model == OPUS
    assert "ROUTER_UNAVAILABLE" in d.reason_codes and "timed out" in d.note


def test_restricted_models_are_never_recommended():
    a = adapter(denied=("sonnet",))
    d = run(task("standard"), a=a)
    assert d.final_model == OPUS and d.intervention == Intervention.SILENT
    assert "MODEL_RESTRICTED" in d.reason_codes
    explicit = run(task("standard"), a=a, prompt="use sonnet")
    assert explicit.final_model == OPUS and "isn't available" in explicit.note
    allowed_only = run(task("light", "small_edit", "low"), a=adapter(allowed=("opus", "sonnet")))
    assert allowed_only.candidate_model == SONNET


def test_routing_off_turns_a_switch_into_a_note():
    d = run(task("light", "small_edit", "low"), routing=False)
    assert d.intervention == Intervention.SILENT and d.final_model == OPUS and not d.options
    assert "ROUTING_OFF" in d.reason_codes and "Routing is off" in d.note


def test_low_confidence_keeps_the_model_silently():
    d = run(task("light", "small_edit", "low", confidence=0.2))
    assert d.intervention == Intervention.SILENT and d.final_model == OPUS


def test_upgrade_needs_a_question():
    d = run(task("heavy", "complex_debugging", "high"), running=SONNET, current=SONNET)
    assert d.intervention == Intervention.ASK and d.final_model == OPUS
    assert "CURRENT_MODEL_TOO_WEAK" in d.reason_codes and "deeper reasoning" in d.explanation


# -- leases and flapping -------------------------------------------------------------------------


def test_same_task_keeps_the_lease_and_never_downgrades_inside_it():
    for tier in ("light", "standard", "light", "standard"):
        d = run(task(tier, "frontend_implementation", "low"), running=SONNET, current=OPUS, same_task=True)
        assert d.intervention == Intervention.SILENT and d.final_model == SONNET
        assert "SAME_TASK_LEASE" in d.reason_codes and "Still on Sonnet 5.5" in d.note


def test_same_task_upgrades_when_it_gets_harder():
    d = run(task("heavy", "complex_debugging", "high"), running=SONNET, current=OPUS, same_task=True)
    assert d.intervention == Intervention.ASK and d.final_model == OPUS


def test_explicit_request_parser():
    assert explicit_request("please use opus for this") == "opus"
    assert explicit_request("Switch to Sonnet.") == "sonnet"
    assert explicit_request("The opus model is slow today") is None
    assert explicit_request("compare sonnet and haiku") is None
