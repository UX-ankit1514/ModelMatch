"""What has actually worked for this developer, per kind of task (System 2).

Built fresh from the decisions table each time (so it can never drift from the
evidence), per (tool, task type) and per model family:

* every closed decision counts once, weighted by recency (half-life of 21 days),
  so a developer who changes habits is followed, never locked in;
* the evidence is the OUTCOME: no retry / escalation / switch-back / refusal /
  error is a success for the family that actually ran; an escalation counts
  against the family that needed rescuing;
* which family the developer picks is kept as a *preference*, a separate signal.
  It only changes when ModelMatch asks, never what it recommends. Picking Opus
  for trivial edits, however often, therefore never teaches "trivial = Opus":
  those decisions show up as over-provisioning instead.

No fixed calibration period: confidence grows with the amount of evidence.
"""

from typing import Any, Dict, Iterable, Optional

from .schema import Intervention, TIER_RANK, TaskPreferenceProfile, FamilyEvidence

HALF_LIFE_DAYS = 21.0
LOOKBACK_DAYS = 180
STALE_SECONDS = 2 * 3600  # an open decision this old is treated as "nothing went wrong" (half weight)
CONFIDENCE_K = 5.0
_FAMILY_RANK = {"light": 1, "standard": 2, "heavy": 3, "haiku": 1, "sonnet": 2, "opus": 3, "fable": 3}
_NEGATIVE = {"escalated": "escalated", "retry": "retried", "switched_back": "switched_back",
             "refused": "failed", "error": "failed"}


def _weight(age_seconds: float) -> float:
    return 0.5 ** (max(0.0, age_seconds) / 86400.0 / HALF_LIFE_DAYS)


def build_profile(rows: Iterable[Dict[str, Any]], client: str, task_type: str, at: float) -> TaskPreferenceProfile:
    """rows: decisions for this client and task type, newest first."""
    profile = TaskPreferenceProfile(client=client, task_type=task_type)
    chosen: Dict[str, float] = {}
    asked = rejected = offered = overridden = 0.0
    counting_declines = True

    for row in rows:
        age = at - (row.get("ts") or at)
        weight = _weight(age)
        family = row.get("ran_family")

        # Interruption history (preference signals).
        if row.get("suggested_lower"):
            took_it = row.get("accepted") == 1 and row.get("user_choice") not in (None, "keep")
            if counting_declines:
                if took_it:
                    counting_declines = False
                else:
                    profile.declines_in_a_row += 1
        if row.get("intervention") == Intervention.ASK and row.get("accepted") is not None:
            asked += weight
            if row.get("accepted") == 0:
                rejected += weight
        if row.get("intervention") in (Intervention.ASK, Intervention.AUTO):
            offered += weight
            if row.get("accepted") == 0 or row.get("manual_override"):
                overridden += weight

        if not family:
            continue
        outcome = row.get("outcome")
        if outcome is None:
            if age < STALE_SECONDS:
                continue  # still running: no evidence yet
            outcome, weight = "no_negative_signal", weight * 0.5
        evidence = profile.families.setdefault(family, FamilyEvidence(family=family))
        evidence.samples += weight
        evidence.chosen += weight
        chosen[family] = chosen.get(family, 0.0) + weight
        if outcome == "no_negative_signal":
            evidence.success += weight
            semantic = TIER_RANK.get(row.get("semantic_tier") or "")
            rescue = row.get("followup_kind") in ("retry_or_correction", "escalation_request")
            if semantic and semantic < _FAMILY_RANK.get(family, 0) and not rescue:
                evidence.overprovisioned += weight
        elif outcome in _NEGATIVE:
            setattr(evidence, _NEGATIVE[outcome], getattr(evidence, _NEGATIVE[outcome]) + weight)
        elif outcome == "manual_override":
            evidence.switched_back += weight * 0.5
        profile.observation_count += 1
        if len(profile.recent_choices) < 5:
            profile.recent_choices.append(family)

    total = sum(chosen.values())
    profile.effective_samples = round(total, 3)
    if total > 0:
        family, weight = max(chosen.items(), key=lambda kv: kv[1])
        profile.preferred_family, profile.preference_share = family, round(weight / total, 3)
    profile.reject_rate = round(rejected / asked, 3) if asked else None
    profile.override_rate = round(overridden / offered, 3) if offered else None
    profile.confidence = round(total / (total + CONFIDENCE_K), 3)
    return profile


def escalation_rate(evidence: Optional[FamilyEvidence]) -> Optional[float]:
    """How often this family needed rescuing: escalations, failures, and (half) retries."""
    if evidence is None or evidence.samples <= 0:
        return None
    return round((evidence.escalated + evidence.failed + 0.5 * evidence.retried + 0.5 * evidence.switched_back)
                 / evidence.samples, 3)


def proven(evidence: Optional[FamilyEvidence], min_samples: float = 4.0, min_success: float = 0.85) -> bool:
    """Enough recent, clean successes to trust this family for this kind of task."""
    if evidence is None or evidence.samples < min_samples:
        return False
    rescue = escalation_rate(evidence) or 0.0
    return (evidence.rate(evidence.success) or 0.0) >= min_success and rescue <= 0.1
