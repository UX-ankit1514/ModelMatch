"""Task continuity: follow-ups keep the lease, a new phase re-evaluates."""

from proxy.v2.continuity import LEASE_IDLE_SECONDS, phase_changed, same_task
from proxy.v2.schema import TaskAnalysis


def analysis(task_type, followup=None, probability=None, tier="heavy"):
    return TaskAnalysis(task_type=task_type, complexity="high", reasoning_requirement="high", required_tier=tier,
                        confidence=0.9, followup_kind=followup, same_task_probability=probability)


def test_follow_up_question_is_the_same_task():
    assert same_task("architecture", 100.0, analysis("architecture", "continuation", 0.85), at=160.0)[0]
    assert same_task("architecture", 100.0, analysis("general_question", "continuation", 0.8), at=160.0)[0]


def test_new_phase_is_a_new_task_even_if_the_analyst_says_continuation():
    same, why = same_task("architecture", 100.0, analysis("frontend_implementation", "continuation", 0.9), at=160.0)
    assert not same and "new phase" in why
    assert phase_changed("frontend_ui", "backend_implementation")
    assert not phase_changed("debugging", "complex_debugging")
    assert not phase_changed("architecture", "small_edit")


def test_retries_and_escalations_stay_on_the_task():
    assert same_task("debugging", 100.0, analysis("debugging", "retry_or_correction", 0.3), at=160.0)[0]
    assert same_task("debugging", 100.0, analysis("complex_debugging", "escalation_request"), at=160.0)[0]


def test_low_probability_or_new_task_ends_the_lease():
    assert not same_task("refactor", 100.0, analysis("refactor", "continuation", 0.3), at=160.0)[0]
    assert not same_task("refactor", 100.0, analysis("refactor", "new_task"), at=160.0)[0]


def test_idle_tasks_and_first_prompts_start_fresh():
    assert not same_task(None, None, analysis("architecture"))[0]
    assert not same_task("architecture", 0.0, analysis("architecture", "continuation", 0.9),
                         at=LEASE_IDLE_SECONDS + 1.0)[0]


def test_no_continuity_signal_uses_the_kind_of_work():
    assert same_task("frontend_ui", 100.0, analysis("frontend_implementation"), at=160.0)[0]
    assert not same_task("frontend_ui", 100.0, analysis("devops"), at=160.0)[0]
