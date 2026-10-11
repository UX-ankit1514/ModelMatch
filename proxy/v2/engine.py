"""The V2 decision engine: which model, and whether to interrupt the developer.

decide() is a pure function of its inputs (no I/O), applied in this priority order:

 1. explicit developer instruction ("use opus for this") always wins
 2. organisation restrictions (denied / allowed models)
 3. model availability and whether the tool can switch at all
 4. task capability: the analyst's tier, raised when this developer's history shows
    that tier repeatedly needed rescuing on this kind of task, lowered by one only
    when the lower tier has a clean, proven record here (never for high complexity)
 5. session / cache economics: never switch away from a warm, capable model when
    that costs more than staying
 6. developer preference: only changes *when* we ask (e.g. a trade-off popup when the
    developer usually picks the other model; fewer popups after repeated "no"),
    never *what* we recommend
 7. no switch for tiny or same-tier differences

Then the intervention: SILENT (keep going), ASK (popup), or AUTO (apply and say so).
Auto-routing is off unless MODELMATCH_AUTO_ROUTE=1, and even then needs mature
evidence, high confidence and a clear saving.
"""

import re
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Optional

from . import explain
from .adapters import AgentAdapter
from .profiles import escalation_rate, proven
from .schema import (TIERS, CacheState, Economics, Intervention, Option, Reason, RouteDecision, SessionSnapshot,
                     TaskAnalysis, TaskPreferenceProfile, new_id)

MIN_CONFIDENCE_TO_ACT = 0.3  # below this the analyst is guessing: keep the model, say nothing
ASK_BELOW_CONFIDENCE = 0.6
AUTO_MIN_CONFIDENCE = 0.8
ESCALATION_MIN_SAMPLES = 2.0
ESCALATION_RATE = 0.4
NAG_LIMIT = 3  # after this many declined suggestions in a row, suggest with a note instead of a popup
REOFFER_EVERY = 6  # ...and ask again every 6th time, in case the developer's mind changed
PREFERENCE_MIN_SHARE = 0.5
PREFERENCE_MIN_SAMPLES = 3.0

EXPLICIT_RE = re.compile(
    r"(?i)(?:^|[\s,.;:!(])(?:please\s+)?(?:use|using|switch\s+to|switch\s+over\s+to|go\s+with|run\s+(?:this|it)\s+"
    r"(?:on|with))\s+(?:claude\s+)?(opus|sonnet|haiku|fable)\b")
_LEVEL_CODE = {"low": Reason.TASK_LOW_COMPLEXITY, "medium": Reason.TASK_MEDIUM_COMPLEXITY,
               "high": Reason.TASK_HIGH_COMPLEXITY}


def explicit_request(prompt: str) -> Optional[str]:
    """A plain "use <model>" instruction in the prompt (works even when the analyst is down)."""
    match = EXPLICIT_RE.search(prompt or "")
    return match.group(1).lower() if match else None


@dataclass
class DecisionInputs:
    client: str
    session_id: str
    adapter: AgentAdapter
    current_model: Optional[str]  # the tool's own (configured) model
    effective_model: Optional[str]  # what runs now (a lease's model for a continuing task)
    analysis: Optional[TaskAnalysis]
    analysis_error: Optional[str] = None
    prompt: str = ""  # memory only: used to spot an explicit "use <model>"
    same_task: bool = False
    profile: Optional[TaskPreferenceProfile] = None
    economics: Optional[Callable[[str, str], Economics]] = None  # (stay model, switch model) -> Economics
    snapshot: Optional[SessionSnapshot] = None
    routing_enabled: Optional[bool] = None
    auto_route: bool = False
    task_id: str = field(default_factory=new_id)
    decision_id: str = field(default_factory=new_id)


def _family_code(prefix: str, family: Optional[str], suffix: str) -> str:
    return "{}_{}_{}".format(prefix, (family or "model").upper(), suffix)


def _keep(adapter: AgentAdapter, model: Optional[str], label: str = "Keep {}") -> Option:
    return Option(id="keep", label=label.format(adapter.short(model)), model_id=None)


def _switch(adapter: AgentAdapter, model: str, label: str = "Use {}") -> Option:
    return Option(id=model, label=label.format(adapter.short(model)), model_id=model)


def decide(inp: DecisionInputs) -> RouteDecision:
    A = inp.adapter
    current = inp.current_model
    running = inp.effective_model or current
    analysis = inp.analysis
    d = RouteDecision(decision_id=inp.decision_id, client=inp.client, session_id=inp.session_id, task_id=inp.task_id,
                      current_model=current, effective_model=running, final_model=running,
                      final_display=A.short(running), intervention=Intervention.SILENT, confidence=None,
                      reason_codes=[], explanation="")
    codes = d.reason_codes

    def finish(final: Optional[str], intervention: str, explanation: str = "", note: Optional[str] = None):
        d.final_model, d.final_display, d.intervention = final, A.short(final), intervention
        d.explanation, d.note = explanation, note
        return _routing_check(inp, d)

    # 1. Explicit instruction wins, even without an analysis.
    asked_for = explicit_request(inp.prompt) or (analysis.explicit_model_request if analysis else None)
    if asked_for:
        target = A.resolve_explicit(asked_for)
        if target:
            ok, why = A.available(target)
            if not ok:
                codes.append(Reason.MODEL_RESTRICTED if why == "restricted" else Reason.MODEL_UNAVAILABLE)
                return finish(running, Intervention.SILENT,
                              note="{} isn't available here ({}), so {} was kept.".format(
                                  A.short(target), why, A.short(running)))
            codes.append(Reason.EXPLICIT_MODEL_REQUEST)
            d.candidate_model, d.candidate_display = target, A.short(target)
            d.confidence = 1.0
            if A.same(target, running):
                return finish(running, Intervention.SILENT)
            return finish(target, Intervention.AUTO, explain.explicit(A.short(target)),
                          note="ModelMatch: " + explain.explicit(A.short(target)))

    if analysis is None:
        codes.append(Reason.ROUTER_UNAVAILABLE)
        return finish(running, Intervention.SILENT,
                      note="ModelMatch couldn't analyse this prompt ({}), so {} was kept.".format(
                          inp.analysis_error or "router unavailable", A.short(running)))

    d.confidence = analysis.confidence
    d.semantic_tier = analysis.required_tier
    d.semantic_model = A.model_for_tier(analysis.required_tier)
    codes.append(_LEVEL_CODE.get(analysis.complexity, Reason.TASK_MEDIUM_COMPLEXITY))
    if analysis.task_type in ("architecture", "system_design"):
        codes.append(Reason.ARCHITECTURE_REASONING)
    if analysis.task_type == "complex_debugging":
        codes.append(Reason.COMPLEX_DEBUGGING)
    if analysis.risk == "high":
        codes.append(Reason.HIGH_RISK)
    snapshot = inp.snapshot
    if snapshot and (snapshot.usage_pressure or 0) >= 0.8:
        codes.append(Reason.USAGE_LIMIT_PRESSURE)

    # 4. Capability floor, adjusted by OUTCOMES on this kind of task (not by clicks).
    tier = analysis.required_tier
    escalated_before = proven_lower = False
    profile = inp.profile
    if profile is not None:
        model = A.model_for_tier(tier)
        evidence = profile.families.get(A.family(model) or "")
        rescue = escalation_rate(evidence)
        if (evidence is not None and evidence.samples >= ESCALATION_MIN_SAMPLES and rescue is not None
                and rescue >= ESCALATION_RATE and tier != "heavy"):
            tier = TIERS[TIERS.index(tier) + 1]
            escalated_before = True
            codes.append(_family_code("PAST", A.family(model), "ESCALATION"))
        elif analysis.complexity != "high" and tier != "light":
            lower_tier = TIERS[TIERS.index(tier) - 1]
            lower = A.model_for_tier(lower_tier)
            if lower and proven(profile.families.get(A.family(lower) or "")) and A.available(lower)[0]:
                tier, proven_lower = lower_tier, True
                codes.append(_family_code("PAST", A.family(lower), "SUCCESS"))
        preferred = profile.preferred_family
        habit = profile.families.get(preferred or "")
        if (habit is not None and habit.samples >= PREFERENCE_MIN_SAMPLES
                and (habit.rate(habit.overprovisioned) or 0) >= 0.5):
            codes.append(_family_code("PAST", preferred, "OVERPROVISIONING"))
    d.personal_signal = profile.to_dict() if profile else {}

    # 2-3. The cheapest available model that meets the floor (stronger ones if it's restricted).
    target = None
    for candidate_tier in TIERS[TIERS.index(tier):]:
        model = A.model_for_tier(candidate_tier)
        ok, why = A.available(model) if model else (False, "")
        if ok:
            target = model
            break
        if model:
            codes.append(Reason.MODEL_RESTRICTED if why == "restricted" else Reason.MODEL_UNAVAILABLE)
    if target is None:
        return finish(running, Intervention.SILENT)
    d.candidate_model, d.candidate_display = target, A.short(target)

    running_rank, target_rank = A.rank(running), A.rank(target)
    confidence = analysis.confidence if analysis.confidence is not None else 0.5

    # Same task: keep the lease unless the task now needs more than the leased model gives.
    if inp.same_task and running_rank is not None and target_rank is not None and running_rank >= target_rank:
        codes.append(Reason.SAME_TASK_LEASE)
        moved = running and current and not A.same(running, current)
        return finish(running, Intervention.SILENT, note=("ModelMatch: " + explain.lease(A.short(running)))
                      if moved else None)

    if running_rank is None or target_rank is None:
        if running and A.same(target, running):
            codes.append(Reason.CURRENT_MODEL_ALREADY_GOOD)
            return finish(running, Intervention.SILENT)
        if confidence < MIN_CONFIDENCE_TO_ACT:
            codes.append(Reason.LOW_ROUTING_CONFIDENCE)
            return finish(running, Intervention.SILENT)
        d.options = [_switch(A, target), _keep(A, running)]
        return finish(target, Intervention.ASK, "Recommended for {}.".format(explain.a_task(analysis)))

    if A.same(target, running) or running_rank == target_rank:
        codes.append(Reason.CURRENT_MODEL_ALREADY_GOOD if A.same(target, running) else Reason.SAME_TIER)
        return finish(running, Intervention.SILENT)

    if running_rank > target_rank:
        return _downgrade(inp, d, running, target, confidence, proven_lower, finish)
    return _upgrade(inp, d, running, target, confidence, escalated_before, finish)


def _downgrade(inp: DecisionInputs, d: RouteDecision, running: str, target: str, confidence: float,
               proven_lower: bool, finish) -> RouteDecision:
    A, analysis, codes, profile = inp.adapter, inp.analysis, d.reason_codes, inp.profile
    economics = inp.economics(running, target) if inp.economics else None
    d.economics = economics
    preferred = None
    if (profile and profile.preferred_family and (profile.preference_share or 0) >= PREFERENCE_MIN_SHARE
            and profile.effective_samples >= PREFERENCE_MIN_SAMPLES):
        preferred = profile.preferred_family

    if economics is not None and economics.verdict == "stay_cheaper":
        codes.append(Reason.SWITCH_USAGE_PENALTY)
        if economics.cache_current in (CacheState.WARM, CacheState.LIKELY_WARM):
            codes.append(Reason.CURRENT_CACHE_WARM)
        if economics.cache_candidate == CacheState.COLD:
            codes.append(Reason.CANDIDATE_CACHE_COLD)
        if preferred and preferred == A.family(target):
            # A real trade-off: the developer usually picks the lighter model here. Ask, staying first.
            codes.append("{}_{}".format(Reason.USER_PREFERS, preferred.upper()))
            text = explain.stay_for_cache(analysis, A.short(running), A.short(target), A.short(target))
            d.options = [_keep(A, running, "Stay on {}"), _switch(A, target, "Use {} anyway")]
            d.options[0].primary = True
            return finish(running, Intervention.ASK, text)
        return finish(running, Intervention.SILENT,
                      explain.stay_for_cache(analysis, A.short(running), A.short(target), None))
    if economics is not None and economics.verdict == "similar":
        codes.append(Reason.SWITCH_BENEFIT_TOO_SMALL)
        return finish(running, Intervention.SILENT)
    if economics is not None and economics.verdict == "switch_cheaper":
        codes.append(Reason.SWITCH_EXPECTED_SAVINGS)

    d.suggested_lower = True
    if confidence < MIN_CONFIDENCE_TO_ACT:
        codes.append(Reason.LOW_ROUTING_CONFIDENCE)
        return finish(running, Intervention.SILENT)
    text = explain.downgrade(analysis, A.short(target), proven_lower)
    d.options = [_switch(A, target), _keep(A, running)]
    d.options[0].primary = True
    if analysis.ambiguous or confidence < ASK_BELOW_CONFIDENCE:
        codes.append(Reason.AMBIGUOUS_TASK if analysis.ambiguous else Reason.LOW_ROUTING_CONFIDENCE)
        return finish(target, Intervention.ASK, explain.ambiguous(analysis) if analysis.ambiguous
                      else explain.unsure(analysis) + " " + text)
    evidence = profile.families.get(A.family(target) or "") if profile else None
    if (inp.auto_route and confidence >= AUTO_MIN_CONFIDENCE and proven(evidence)
            and economics is not None and economics.verdict == "switch_cheaper"):
        codes.append(Reason.AUTO_ROUTE_EVIDENCE)
        d.options = []
        return finish(target, Intervention.AUTO, text, note="ModelMatch: using {} for this task. {} (Say \"use {}\" "
                      "to switch back.)".format(A.short(target), text, A.family(running) or A.short(running)))
    if profile and profile.declines_in_a_row >= NAG_LIMIT and profile.declines_in_a_row % REOFFER_EVERY:
        # The developer keeps saying no: don't block them with a popup, just mention it.
        codes.append(Reason.USER_KEEPS_DECLINING)
        d.options = []
        return finish(running, Intervention.SILENT, text,
                      note="ModelMatch: {} would likely handle this too ({}). Staying on {}.".format(
                          A.short(target), explain.a_task(analysis), A.short(running)))
    return finish(target, Intervention.ASK, text)


def _upgrade(inp: DecisionInputs, d: RouteDecision, running: str, target: str, confidence: float,
             escalated_before: bool, finish) -> RouteDecision:
    A, analysis, codes = inp.adapter, inp.analysis, d.reason_codes
    codes.append(Reason.CURRENT_MODEL_TOO_WEAK)
    d.economics = inp.economics(running, target) if inp.economics else None
    if confidence < MIN_CONFIDENCE_TO_ACT:
        codes.append(Reason.LOW_ROUTING_CONFIDENCE)
        return finish(running, Intervention.SILENT)
    text = explain.upgrade(analysis, A.short(running), A.short(target), escalated_before)
    if analysis.ambiguous:
        codes.append(Reason.AMBIGUOUS_TASK)
        text = explain.ambiguous(analysis) + " " + text
    d.options = [_switch(A, target), _keep(A, running)]
    d.options[0].primary = True
    if (inp.auto_route and confidence >= AUTO_MIN_CONFIDENCE and not analysis.ambiguous
            and (escalated_before or analysis.followup_kind == "escalation_request")):
        codes.append(Reason.AUTO_ROUTE_EVIDENCE)
        d.options = []
        return finish(target, Intervention.AUTO, text,
                      note="ModelMatch: using {} for this task. {}".format(A.short(target), text))
    return finish(target, Intervention.ASK, text)


def _routing_check(inp: DecisionInputs, d: RouteDecision) -> RouteDecision:
    """If the tool can't switch here, a change becomes a suggestion in a note."""
    wants_change = d.intervention == Intervention.ASK or (d.final_model and not inp.adapter.same(
        d.final_model, d.effective_model) and d.intervention == Intervention.AUTO)
    if not wants_change:
        return d
    if inp.routing_enabled is False or not inp.adapter.supports_model_switch:
        d.reason_codes.append(Reason.ROUTING_OFF)
        suggestion = d.final_model  # a trade-off popup that recommends staying needs no note
        if suggestion and not inp.adapter.same(suggestion, d.effective_model):
            d.note = "ModelMatch suggests {}. {} (Routing is off, so nothing was switched.)".format(
                inp.adapter.short(suggestion), d.explanation).replace("  ", " ")
        d.final_model, d.final_display = d.effective_model, inp.adapter.short(d.effective_model)
        d.intervention, d.options = Intervention.SILENT, []
    return d
