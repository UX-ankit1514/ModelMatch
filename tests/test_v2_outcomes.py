"""Outcome signals after a routing decision (behavioural evidence, not proof)."""

from proxy.v2.adapters import ClaudeCodeAdapter
from proxy.v2.outcomes import judge, regret

OPUS, SONNET, HAIKU = "claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-4-5"
A = ClaudeCodeAdapter({"light": HAIKU, "standard": SONNET, "heavy": OPUS}, policy_files=[])


def prev(ran=SONNET, current=OPUS, intervention="ASK", accepted=1, **extra):
    data = {"id": "d1", "prompt_fp": "aaa", "ran_model": ran, "current_model": current, "intervention": intervention,
            "accepted": accepted, "route_failed": 0}
    data.update(extra)
    return data


def outcome(previous, usage=(), same=True, followup="continuation", fp="bbb", current=OPUS, new_model=None,
            explicit=None):
    return judge(previous, list(usage), same_task=same, followup=followup, prompt_fp=fp, current_model_now=current,
                 new_model=new_model, explicit_family_now=explicit, rank=A.rank, same_model=A.same)


def test_accepted_recommendation_that_continues_is_a_success():
    result = outcome(prev(), new_model=SONNET)
    assert result["outcome"] == "no_negative_signal" and not regret(result["outcome"])


def test_rejected_recommendation_then_new_task_is_a_success_for_the_kept_model():
    result = outcome(prev(ran=OPUS, accepted=0), same=False, followup="new_task", new_model=OPUS)
    assert result["outcome"] == "no_negative_signal"


def test_immediate_switch_back_is_regret():
    result = outcome(prev(intervention="AUTO"), explicit="opus", new_model=OPUS, followup="escalation_request")
    assert result["outcome"] in ("escalated", "switched_back") and regret(result["outcome"])
    assert result["switched_back"] == 1


def test_retry_on_the_same_model_is_a_retry():
    same_prompt = outcome(prev(), fp="aaa", new_model=SONNET)
    assert same_prompt["outcome"] == "retry" and same_prompt["retry_detected"] == 1
    correction = outcome(prev(), followup="retry_or_correction", new_model=SONNET)
    assert correction["outcome"] == "retry"


def test_escalation_to_a_stronger_model_on_the_same_task():
    result = outcome(prev(), followup="retry_or_correction", new_model=OPUS)
    assert result["outcome"] == "escalated" and result["escalated_to"] == OPUS
    assert regret("escalated")


def test_a_stronger_model_for_a_new_task_is_not_an_escalation():
    result = outcome(prev(), same=False, followup="new_task", new_model=OPUS)
    assert result["outcome"] == "no_negative_signal" and result["escalated_to"] is None


def test_refusals_errors_and_failed_routes_are_failures():
    refused = outcome(prev(), usage=[{"stop_reason": "refusal", "status": 200}], new_model=SONNET)
    assert refused["outcome"] == "refused"
    errored = outcome(prev(), usage=[{"status": 529}], new_model=SONNET)
    assert errored["outcome"] == "error"
    route_failed = outcome(prev(route_failed=1), new_model=SONNET)
    assert route_failed["outcome"] == "error"


def test_manual_model_change_is_an_override():
    result = outcome(prev(ran=OPUS, current=OPUS, intervention="SILENT", accepted=None), current=SONNET,
                     same=False, followup="new_task")
    assert result["outcome"] == "manual_override" and result["manual_override"] == 1
