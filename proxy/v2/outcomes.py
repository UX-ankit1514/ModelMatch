"""Closing the loop: what happened after a routing decision (behavioural evidence).

A decision is judged when the developer's NEXT prompt in the session arrives (or
after a while with nothing further). These are signals, not proof of quality:

    refused         the model refused (stop_reason "refusal")
    error           the model's requests failed, or the routed model was rejected
    escalated       same task, and the developer then needed a stronger model
    switched_back   ModelMatch moved the model, and the developer asked to go back
    retry           same task, "that didn't work / try again", or the same prompt again
    manual_override the developer changed the tool's model themselves
    no_negative_signal  none of the above (the task continued or moved on normally)

RoutingRegret = any of the first five after a decision ModelMatch made or suggested.
"""

from typing import Any, Dict, List, Optional

from .schema import Intervention

PRIORITY = ("refused", "error", "escalated", "switched_back", "retry", "manual_override")
REGRET = {"refused", "error", "escalated", "switched_back", "retry"}


def judge(prev: Dict[str, Any], usage: List[Dict[str, Any]], *, same_task: bool, followup: Optional[str],
          prompt_fp: Optional[str], current_model_now: Optional[str], new_model: Optional[str],
          explicit_family_now: Optional[str], rank, same_model) -> Dict[str, Any]:
    """The outcome fields for `prev`, given what the next prompt shows. Pure function."""
    signals: Dict[str, Any] = {
        "refusal_detected": int(any(u.get("stop_reason") == "refusal" for u in usage)),
        "error_detected": int(bool(prev.get("route_failed")) or any((u.get("status") or 0) >= 500 for u in usage)),
        "retry_detected": int(bool(prompt_fp and prompt_fp == prev.get("prompt_fp"))
                              or (same_task and followup == "retry_or_correction")),
        "manual_override": int(bool(prev.get("current_model") and current_model_now
                                    and not same_model(prev["current_model"], current_model_now))),
        "next_followup": followup,
        "escalated_to": None,
        "switched_back": 0,
    }
    ran = prev.get("ran_model") or prev.get("effective_model") or prev.get("current_model")
    ran_rank, new_rank = rank(ran), rank(new_model)
    if same_task and new_model and ran_rank is not None and new_rank is not None and new_rank > ran_rank:
        signals["escalated_to"] = new_model
    elif same_task and followup == "escalation_request":
        signals["escalated_to"] = new_model or "stronger model"
    moved_by_us = bool(ran and prev.get("current_model") and not same_model(ran, prev["current_model"])
                       and prev.get("intervention") in (Intervention.AUTO, Intervention.ASK, Intervention.SILENT))
    if moved_by_us and (signals["manual_override"] or
                        (explicit_family_now and prev.get("current_model")
                         and rank(prev["current_model"]) is not None
                         and new_model and same_model(new_model, prev["current_model"]))):
        signals["switched_back"] = 1

    flags = {"refused": signals["refusal_detected"], "error": signals["error_detected"],
             "escalated": signals["escalated_to"] is not None, "switched_back": signals["switched_back"],
             "retry": signals["retry_detected"], "manual_override": signals["manual_override"]}
    signals["outcome"] = next((name for name in PRIORITY if flags[name]), "no_negative_signal")
    return signals


def regret(outcome: Optional[str]) -> bool:
    return outcome in REGRET
