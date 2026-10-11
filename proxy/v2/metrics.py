"""Success metrics, computed from the decisions and usage the proxy recorded.

Savings are never claimed here: "estimated" figures are labelled as such, and the
measured token counts are reported as they are.
"""

from typing import Any, Dict, List, Optional

from .outcomes import REGRET
from .schema import Intervention, now


def _rate(part: float, whole: float) -> Optional[float]:
    return round(part / whole, 3) if whole else None


def _percentile(values: List[int], q: float) -> Optional[int]:
    if not values:
        return None
    values = sorted(values)
    return values[min(len(values) - 1, int(round(q * (len(values) - 1))))]


def summarize(store, days: float = 7.0, client: Optional[str] = None) -> Dict[str, Any]:
    since = now() - days * 86400
    decisions = store.decisions_since(since, client)
    usage = store.usage_since(since, client)
    total = len(decisions)
    by = lambda name: [d for d in decisions if d.get("intervention") == name]  # noqa: E731
    asks, autos, silents = by(Intervention.ASK), by(Intervention.AUTO), by(Intervention.SILENT)
    answered = [d for d in asks if d.get("accepted") is not None]
    accepted = [d for d in answered if d.get("accepted") == 1]
    closed = [d for d in decisions if d.get("outcome")]
    moved = [d for d in closed if d.get("ran_model") and d.get("current_model")
             and d.get("ran_model") != d.get("current_model")]
    latencies = [d["analysis_latency_ms"] for d in decisions if isinstance(d.get("analysis_latency_ms"), int)]

    models: Dict[str, Dict[str, int]] = {}
    for event in usage:
        entry = models.setdefault(event.get("sent_model") or "unknown",
                                  {"requests": 0, "input_tokens": 0, "output_tokens": 0, "cache_read_tokens": 0,
                                   "cache_write_tokens": 0, "refusals": 0, "errors": 0})
        entry["requests"] += 1
        for field in ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens"):
            entry[field] += event.get(field) or 0
        entry["refusals"] += int(event.get("stop_reason") == "refusal")
        entry["errors"] += int((event.get("status") or 0) >= 400)

    outcomes: Dict[str, int] = {}
    for d in closed:
        outcomes[d["outcome"]] = outcomes.get(d["outcome"], 0) + 1
    regrets = [d for d in moved if d.get("outcome") in REGRET]

    return {
        "window_days": days,
        "client": client or "all",
        "prompts": total,
        "router": {
            "latency_ms_p50": _percentile(latencies, 0.5),
            "latency_ms_p95": _percentile(latencies, 0.95),
            "failures": sum(1 for d in decisions if d.get("analysis_error")),
            "failure_rate": _rate(sum(1 for d in decisions if d.get("analysis_error")), total),
        },
        "interventions": {
            "popup_rate": _rate(len(asks), total),
            "silent_keep_rate": _rate(len(silents), total),
            "auto_route_rate": _rate(len(autos), total),
            "accept_rate": _rate(len(accepted), len(answered)),
            "reject_rate": _rate(len(answered) - len(accepted), len(answered)),
            "unanswered_popups": len(asks) - len(answered),
        },
        "outcomes": {
            "closed": len(closed),
            "counts": outcomes,
            "override_rate": _rate(sum(1 for d in closed if d.get("manual_override")), len(closed)),
            "switch_back_rate": _rate(sum(1 for d in closed if d.get("switched_back")), len(closed)),
            "retry_rate": _rate(sum(1 for d in closed if d.get("retry_detected")), len(closed)),
            "escalation_rate": _rate(sum(1 for d in closed if d.get("escalated_to")), len(closed)),
            "routing_regret_rate": _rate(len(regrets), len(moved)),
            "routed_decisions": len(moved),
        },
        "measured_usage_by_model": models,
        "note": "Token counts are measured from API responses. No savings are claimed from these numbers.",
    }
