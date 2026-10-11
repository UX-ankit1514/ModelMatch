"""Personal evidence per task type: outcomes over clicks, recency over history."""

from proxy.v2.profiles import build_profile, escalation_rate, proven

NOW = 1_800_000_000.0
DAY = 86400.0


def row(family, outcome="no_negative_signal", semantic_tier="heavy", days_ago=1.0, **extra):
    data = {"ts": NOW - days_ago * DAY, "ran_family": family, "outcome": outcome, "semantic_tier": semantic_tier}
    data.update(extra)
    return data


def test_opus_repeatedly_successful_for_architecture():
    profile = build_profile([row("opus", days_ago=i) for i in range(11)], "claude-code", "architecture", NOW)
    opus = profile.families["opus"]
    assert profile.preferred_family == "opus" and profile.preference_share == 1.0
    assert opus.rate(opus.success) == 1.0 and escalation_rate(opus) == 0.0 and proven(opus)
    assert opus.overprovisioned == 0  # the analyst said heavy: Opus was the right size
    assert profile.observation_count == 11 and 0.5 < profile.confidence < 1


def test_opus_for_trivial_tasks_is_recorded_as_overprovisioning():
    profile = build_profile([row("opus", semantic_tier="light", days_ago=i) for i in range(18)],
                            "claude-code", "small_edit", NOW)
    opus = profile.families["opus"]
    assert profile.preferred_family == "opus"  # a preference...
    assert opus.rate(opus.overprovisioned) == 1.0  # ...but not evidence that Opus is needed


def test_sonnet_escalating_to_opus_lowers_sonnet_and_raises_opus():
    rows = []
    for i in range(5):  # Sonnet -> retry -> Opus -> done, five times
        rows.append(row("opus", "no_negative_signal", "standard", days_ago=i + 0.01,
                        followup_kind="retry_or_correction"))
        rows.append(row("sonnet", "escalated", "standard", days_ago=i + 0.02))
    profile = build_profile(rows, "claude-code", "complex_debugging", NOW)
    sonnet, opus = profile.families["sonnet"], profile.families["opus"]
    assert escalation_rate(sonnet) == 1.0 and not proven(sonnet)
    assert opus.rate(opus.success) == 1.0 and proven(opus)
    assert opus.overprovisioned == 0  # rescuing a failed attempt is not over-provisioning


def test_preference_follows_recent_behaviour():
    old = [row("opus", days_ago=60 + i) for i in range(10)]
    recent = [row("sonnet", days_ago=i) for i in range(5)]
    profile = build_profile(recent + old, "claude-code", "architecture", NOW)
    assert profile.preferred_family == "sonnet"
    assert profile.recent_choices[:5] == ["sonnet"] * 5


def test_recent_evidence_outweighs_old_evidence():
    old_failures = [row("sonnet", "escalated", days_ago=90 + i) for i in range(6)]
    recent_successes = [row("sonnet", days_ago=i) for i in range(3)]
    sonnet = build_profile(recent_successes + old_failures, "claude-code", "debugging", NOW).families["sonnet"]
    assert sonnet.rate(sonnet.success) > 0.8
    assert escalation_rate(sonnet) < 0.2


def test_open_decisions_count_only_once_stale():
    fresh_open = [row("sonnet", None, days_ago=0.01)]
    stale_open = [row("sonnet", None, days_ago=0.5)]
    assert "sonnet" not in build_profile(fresh_open, "c", "t", NOW).families
    stale = build_profile(stale_open, "c", "t", NOW).families["sonnet"]
    assert 0 < stale.samples < 0.6 and stale.success == stale.samples


def test_declines_in_a_row_stop_at_the_last_accept():
    rows = [row("opus", suggested_lower=1, accepted=0, user_choice="keep", intervention="ASK", days_ago=0.1),
            row("opus", suggested_lower=1, accepted=0, user_choice="keep", intervention="ASK", days_ago=0.2),
            row("haiku", suggested_lower=1, accepted=1, user_choice="claude-haiku-4-5", intervention="ASK",
                days_ago=0.3),
            row("opus", suggested_lower=1, accepted=0, intervention="ASK", days_ago=0.4)]
    profile = build_profile(rows, "claude-code", "small_edit", NOW)
    assert profile.declines_in_a_row == 2
    assert profile.reject_rate == 0.75
