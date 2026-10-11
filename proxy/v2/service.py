"""V2 behind the proxies' endpoints: one prompt in, one decision out, evidence recorded.

    recommend()  PromptEvent -> analysis -> continuity -> profile -> session snapshot
                 -> decision -> (apply the route now unless we must ask) -> lease ->
                 close the previous decision's outcome -> record
    select()     the developer's popup answer
    on_usage()   measured usage of each model request (from the usage tap)

Routes are applied through the V1 session store (set_recommendation + set_selection),
so the passthrough code that swaps the model is unchanged. Whatever goes wrong here,
the answer is "keep the current model" (fail open).
"""

import hashlib
import logging
from typing import Any, Dict, Optional

from ..router_client import RouterError
from ..session_store import SessionStore
from .adapters import AgentAdapter
from .analyzer import TaskAnalyzer, build_analyzer
from .cache import session_snapshot
from .continuity import LEASE_IDLE_SECONDS, NEUTRAL_TYPES, same_task
from .economics import describe, estimate
from . import explain
from .engine import DecisionInputs, decide, explicit_request
from .outcomes import judge
from .profiles import LOOKBACK_DAYS, build_profile
from .schema import TIER_RANK, Intervention, RouteDecision, new_id, now
from .store import Store

USAGE_WINDOW_SECONDS = 2 * 3600
SCHEMA = "modelmatch.recommendation.v2"


def fingerprint(prompt: str) -> str:
    return hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:12]


def project_id(cwd: Optional[str]) -> Optional[str]:
    return hashlib.sha256(cwd.encode("utf-8")).hexdigest()[:12] if cwd else None


class V2Service:
    def __init__(self, settings, sessions: SessionStore, log: logging.Logger,
                 analyzer: Optional[TaskAnalyzer] = None, transport=None, db: Optional[Store] = None):
        self.settings = settings
        self.sessions = sessions
        self.log = log
        self.analyzer = analyzer or build_analyzer(settings, transport)
        self.db = db if db is not None else Store(settings.db_file)
        if not self.db.ok:
            log.warning("v2 database unavailable (%s) | decisions won't be remembered", self.db.error)
        self._subtypes: Dict[str, str] = {}  # (client, session) -> the task's few-word label; memory only
        self._told_outage: set = set()
        self._last_decision: Dict[str, str] = {}

    # -- sessions -----------------------------------------------------------------

    def session_started(self, client: str, session_id: str, source: Optional[str]) -> None:
        """/clear, /compact or a fresh start: the conversation's context is new, so is the task."""
        if source != "resume":
            self.db.clear_task(client, session_id)
            self._subtypes.pop(client + ":" + session_id, None)

    # -- recommend ----------------------------------------------------------------

    async def recommend(self, *, client: str, session_id: str, store_key: str, prompt: str,
                        turn_id: Optional[str], cwd: Optional[str], current_model: Optional[str],
                        adapter: AgentAdapter, routing_enabled: Optional[bool]) -> Dict[str, Any]:
        at = now()
        fp = fingerprint(prompt)
        self.sessions.new_turn(store_key, turn_id, fp)
        if current_model:
            self.sessions.note_seen_model(store_key, current_model)
        current = current_model or self.sessions.current_model(store_key)
        key = client + ":" + session_id

        task = self.db.task(client, session_id)
        if task and at - (task.get("last_validated") or 0) > LEASE_IDLE_SECONDS:
            task = None
        previous_task = None
        if task:
            previous_task = {"task_type": task.get("task_type"), "subtype": self._subtypes.get(key, ""),
                             "minutes_ago": round((at - (task.get("last_validated") or at)) / 60.0, 1)}

        analysis, error = None, None
        try:
            analysis = await self.analyzer.analyze(prompt, client, previous_task)
        except RouterError as exc:
            error = str(exc)
        except Exception as exc:  # an analyst bug must never block the prompt
            error = "analyst error: " + type(exc).__name__
            self.log.exception("v2 analyst crashed | session=%s", session_id[:8])

        same, same_why = (same_task(task.get("task_type"), task.get("last_validated"), analysis, at)
                          if analysis and task else (False, "no current task" if analysis else "no analysis"))
        # A lease (a ModelMatch-chosen model) lasts for the task; a new task starts from the tool's own model.
        lease_model = task.get("model") if (task and same) else None
        if lease_model and not adapter.available(lease_model)[0]:
            lease_model = None
        running = lease_model or current

        profile = None
        if analysis and self.settings.personalization:
            rows = self.db.decisions_for(client, analysis.task_type, at - LOOKBACK_DAYS * 86400)
            profile = build_profile(rows, client, analysis.task_type, at)

        events = []
        if self.settings.cache_aware and adapter.supports_cache_metrics:
            events = self.db.usage_for_session(client, session_id, at - USAGE_WINDOW_SECONDS)
        snapshot = session_snapshot(events, client, session_id, current, running, None, adapter.same,
                                    adapter.supports_cache_metrics and self.settings.cache_aware, at)
        snapshots = {}

        def economics(stay_model: str, switch_model: str):
            snap = session_snapshot(events, client, session_id, current, stay_model, switch_model, adapter.same,
                                    adapter.supports_cache_metrics and self.settings.cache_aware, at)
            snapshots["last"] = snap
            return estimate(stay_model, switch_model, snap, analysis)

        decision = decide(DecisionInputs(
            client=client, session_id=session_id, adapter=adapter, current_model=current, effective_model=running,
            analysis=analysis, analysis_error=error, prompt=prompt, same_task=same, profile=profile,
            economics=economics if self.settings.cache_aware else None, snapshot=snapshot,
            routing_enabled=routing_enabled, auto_route=self.settings.auto_route,
            task_id=(task.get("task_id") if task and same else new_id())))
        if decision.note and analysis is None:
            if key in self._told_outage:
                decision.note = None  # say it once per session, not on every prompt
            self._told_outage.add(key)
        elif analysis is not None:
            self._told_outage.discard(key)
        snap_used = snapshots.get("last") or snapshot

        # What "Use" would run (for a popup), or what runs now (silent / auto).
        offered = next((o.model_id for o in decision.options if o.model_id), None) or decision.final_model
        ok, note = adapter.available(offered) if offered else (False, "")
        record = {
            "recommended_model": adapter.display(offered) if offered else "your current model",
            "recommended_raw": offered,
            "model_id": offered,
            "provider": "anthropic" if client == "claude-code" else client,
            # Only ModelMatch's own template text is persisted: the analyst's sentence can echo the prompt.
            "recommendation_reason": decision.explanation or ("Matched to {}.".format(explain.a_task(analysis))
                                                              if analysis else ""),
            "confidence": decision.confidence,
            "routable": ok,
            "routing_note": note,
            "substituted_from": None,
            "router_source": analysis.source if analysis else "unavailable",
            "v2_intervention": decision.intervention,
        }
        rec_id = self.sessions.set_recommendation(store_key, record)
        decision.decision_id = rec_id
        self._last_decision[key] = rec_id

        ran = None
        if decision.intervention in (Intervention.SILENT, Intervention.AUTO):
            ran = decision.final_model or current
            self._apply(store_key, rec_id, ran, current, adapter,
                        "auto" if decision.intervention == Intervention.AUTO else "lease" if same else "silent")

        previous = self.db.last_decision(client, session_id)
        if previous and previous.get("outcome") is None:
            self._close(previous, adapter, same=same, followup=analysis.followup_kind if analysis else None,
                        fp=fp, current=current, new_model=ran if decision.intervention != Intervention.ASK else None,
                        explicit=explicit_request(prompt) or (analysis.explicit_model_request if analysis else None))

        self._save_task(client, session_id, task if same else None, decision, analysis, ran, current, adapter, at)
        if analysis and analysis.subtype and not same:
            self._subtypes[key] = analysis.subtype

        self.db.insert_decision(self._row(decision, analysis, error, same, fp, prompt, cwd, current, routing_enabled,
                                          ran, adapter, snap_used, previous, at))
        self.log.info("v2 decision | %s | session=%s | task=%s/%s tier=%s conf=%s | same_task=%s | %s -> %s | %s | %s",
                      client, session_id[:8], analysis.task_type if analysis else "-",
                      analysis.complexity if analysis else "-", analysis.required_tier if analysis else "-",
                      analysis.confidence if analysis else "-", same, running or "-", decision.final_model or "-",
                      decision.intervention, ",".join(decision.reason_codes))
        return self._response(decision, record, rec_id, current, adapter, analysis, same, snap_used)

    def _apply(self, store_key: str, rec_id: str, model: Optional[str], current: Optional[str],
               adapter: AgentAdapter, decided_by: str) -> bool:
        route = bool(model and not adapter.same(model, current))
        result = self.sessions.set_selection(store_key, rec_id, route, decided_by)
        return bool(result.get("applied"))

    def _close(self, previous: Dict[str, Any], adapter: AgentAdapter, *, same: bool, followup: Optional[str],
               fp: str, current: Optional[str], new_model: Optional[str], explicit: Optional[str]) -> None:
        usage = self.db.usage_for_decision(previous["id"])
        fields = judge(previous, usage, same_task=same, followup=followup, prompt_fp=fp, current_model_now=current,
                       new_model=new_model, explicit_family_now=explicit, rank=adapter.rank, same_model=adapter.same)
        self.db.update_decision(previous["id"], outcome_at=now(), **fields)

    def _save_task(self, client, session_id, task, decision: RouteDecision, analysis, ran, current, adapter, at):
        if analysis is None and not decision.reason_codes.count("EXPLICIT_MODEL_REQUEST"):
            return
        model = ran if ran is not None else (task.get("model") if task else None)
        lease = None if (model is None or adapter.same(model, current)) else model
        source = "explicit" if "EXPLICIT_MODEL_REQUEST" in decision.reason_codes else \
            "auto" if decision.intervention == Intervention.AUTO else "router"
        if task:
            task_type = task.get("task_type")
            if analysis and analysis.task_type not in NEUTRAL_TYPES:
                task_type = analysis.task_type
            tiers = [t for t in (task.get("tier"), analysis.required_tier if analysis else None) if t]
            tier = max(tiers, key=lambda t: TIER_RANK.get(t, 0)) if tiers else None
            self.db.set_task(client, session_id, task_id=task["task_id"], task_type=task_type, tier=tier, model=lease,
                             source=source if ran is not None and lease else task.get("source"),
                             created_at=task.get("created_at"), last_validated=at,
                             confidence=analysis.confidence if analysis else task.get("confidence"))
        else:
            self.db.set_task(client, session_id, task_id=decision.task_id,
                             task_type=analysis.task_type if analysis else "unknown",
                             tier=analysis.required_tier if analysis else None, model=lease, source=source,
                             created_at=at, last_validated=at, confidence=analysis.confidence if analysis else None)

    def _row(self, d: RouteDecision, analysis, error, same, fp, prompt, cwd, current, routing_enabled, ran, adapter,
             snapshot, previous, at) -> Dict[str, Any]:
        row: Dict[str, Any] = {
            "id": d.decision_id, "ts": at, "client": d.client, "session_id": d.session_id, "task_id": d.task_id,
            "project_id": project_id(cwd), "prompt_fp": fp, "prompt_chars": len(prompt),
            "analysis_error": (error or "")[:120] or None, "same_task": int(same),
            "current_model": current, "effective_model": d.effective_model, "candidate_model": d.candidate_model,
            "final_model": d.final_model, "intervention": d.intervention, "confidence": d.confidence,
            "reason_codes": d.reason_codes, "explanation": d.explanation[:300], "suggested_lower": int(d.suggested_lower),
            "routing_enabled": None if routing_enabled is None else int(routing_enabled),
            "ran_model": ran, "ran_family": adapter.family(ran) if ran else None,
            "signals": {
                "options": [o.to_dict() for o in d.options],
                "economics": d.economics.to_dict() if d.economics else None,
                "snapshot": {k: v for k, v in snapshot.to_dict().items() if k != "session_id"} if snapshot else None,
                "profile": {k: d.personal_signal.get(k) for k in ("preferred_family", "preference_share",
                                                                   "effective_samples", "declines_in_a_row",
                                                                   "confidence")} if d.personal_signal else None,
                "previous_decision_id": previous.get("id") if previous else None,
            },
        }
        if ran is not None:
            row.update(decided_by="auto" if d.intervention == Intervention.AUTO else "silent", decided_at=at)
        if analysis is not None:
            row.update(analysis_source=analysis.source, analysis_model=analysis.analyzer_model,
                       analysis_latency_ms=analysis.latency_ms, task_type=analysis.task_type,
                       secondary_task_type=analysis.secondary_task_type,
                       secondary_probability=analysis.secondary_probability, complexity=analysis.complexity,
                       reasoning=analysis.reasoning_requirement, scope=analysis.scope, risk=analysis.risk,
                       expected_output=analysis.expected_output, expected_steps=analysis.expected_tool_steps,
                       semantic_tier=analysis.required_tier, semantic_confidence=analysis.confidence,
                       same_task_probability=analysis.same_task_probability, followup_kind=analysis.followup_kind,
                       explicit_model=analysis.explicit_model_request)
        return row

    def _response(self, d: RouteDecision, record, rec_id, current, adapter: AgentAdapter, analysis, same,
                  snapshot) -> Dict[str, Any]:
        offered = record["model_id"]
        switch_offered = d.intervention == Intervention.ASK and any(o.model_id for o in d.options)
        cost = describe(d.economics, adapter.short(d.effective_model), adapter.short(d.candidate_model)) \
            if d.economics and d.candidate_model else None
        return {
            "ok": True,
            "recommendation_id": rec_id,
            **{k: v for k, v in record.items() if k != "v2_intervention"},
            "current_model": current,
            "current_model_display": adapter.display(current) if current else None,
            # V1 hooks: no popup unless V2 wants to ask about a switch.
            "same_as_current": not switch_offered and not (d.intervention == Intervention.AUTO and offered
                                                           and not adapter.same(offered, current)),
            "v2": {
                "schema": SCHEMA,
                "decision_id": rec_id,
                "intervention": d.intervention,
                "final_model": d.final_model,
                "final_display": d.final_display,
                "running_model": d.effective_model,
                "candidate_model": d.candidate_model,
                "candidate_display": d.candidate_display,
                "headline": self._headline(d, adapter),
                "explanation": d.explanation,
                "analyst_reason": analysis.reason if analysis else None,  # in this response only, never stored
                "cost_note": cost,
                "note": d.note,
                "options": [o.to_dict() for o in d.options],
                "reason_codes": d.reason_codes,
                "confidence": d.confidence,
                "task": {"type": analysis.task_type, "complexity": analysis.complexity, "tier": analysis.required_tier,
                         "same_task": same} if analysis else None,
                "economics": d.economics.to_dict() if d.economics else None,
                "cache": {"context_tokens": snapshot.estimated_context_tokens, "current": snapshot.cache_state_current,
                          "candidate": snapshot.cache_state_candidate, "billing": snapshot.billing_mode}
                if snapshot else None,
            },
        }

    @staticmethod
    def _headline(d: RouteDecision, adapter: AgentAdapter) -> str:
        if d.intervention == Intervention.ASK and d.final_model and adapter.same(d.final_model, d.effective_model):
            return "Recommended: Stay on " + adapter.short(d.final_model)
        return "Recommended: " + adapter.short(d.final_model or d.candidate_model)

    # -- select -----------------------------------------------------------------------

    def select(self, *, client: str, session_id: str, store_key: str, recommendation_id: str,
               accepted: bool, decided_by: str, choice: Optional[str], adapter: AgentAdapter) -> Dict[str, Any]:
        d = self.db.decision(recommendation_id)
        if d is None:  # not a V2 decision (or the database is down): the V1 behaviour
            return self.sessions.set_selection(store_key, recommendation_id, accepted, decided_by)
        current, running = d.get("current_model"), d.get("effective_model") or d.get("current_model")
        options = (d.get("signals") or {}).get("options") or []
        primary = next((o for o in options if o.get("primary")), options[0] if options else None)
        if choice:
            chosen = running if choice == "keep" else choice
        elif accepted:
            chosen = (primary or {}).get("model_id") or running
        else:
            other = next((o for o in options if o is not primary), None)
            chosen = (other or {}).get("model_id") or running
        if chosen and not any(o.get("model_id") == chosen for o in options) and not adapter.same(chosen, running):
            chosen = running  # only what was offered can be chosen
        undecided = decided_by in ("timeout", "unavailable", "none")

        route = bool(chosen and not adapter.same(chosen, current))
        rec_id = recommendation_id
        if route and (self.sessions.get(store_key) or {}).get("turn", {}).get("model_id") != chosen:
            rec_id = self.sessions.set_recommendation(store_key, {"recommended_model": adapter.display(chosen),
                                                                  "model_id": chosen, "routable": True,
                                                                  "provider": d.get("client")})
        result = self.sessions.set_selection(store_key, rec_id, route, decided_by)
        applied = bool(result.get("applied"))
        if route and not applied:
            chosen = current  # couldn't apply: the tool's own model runs
        took_primary = bool(primary) and adapter.same(chosen, (primary or {}).get("model_id") or running)
        self.db.update_decision(recommendation_id, user_choice="keep" if adapter.same(chosen, running) else chosen,
                                accepted=None if undecided else int(took_primary), decided_by=decided_by,
                                decided_at=now(), ran_model=chosen, ran_family=adapter.family(chosen))

        task = self.db.task(client, session_id)
        if task and task.get("task_id") == d.get("task_id"):
            lease = None if adapter.same(chosen, current) else chosen
            fields = {k: v for k, v in task.items() if k not in ("client", "session_id")}
            self.db.set_task(client, session_id, **dict(fields, model=lease, source="user", last_validated=now()))

        # Choosing a stronger model on the same task means the previous one needed rescuing.
        previous_id = (d.get("signals") or {}).get("previous_decision_id")
        if d.get("same_task") and previous_id:
            previous = self.db.decision(previous_id)
            ran_before = previous.get("ran_model") if previous else None
            if (previous and adapter.rank(chosen) is not None and adapter.rank(ran_before) is not None
                    and adapter.rank(chosen) > adapter.rank(ran_before)
                    and previous.get("outcome") in (None, "no_negative_signal", "retry")):
                self.db.update_decision(previous_id, outcome="escalated", escalated_to=chosen, outcome_at=now())

        short = adapter.short(chosen)
        if undecided:
            message = "ModelMatch: no answer, so {} was kept.".format(short)
        elif adapter.same(chosen, running):
            message = "ModelMatch: staying on {}.".format(short)
        else:
            message = "ModelMatch: using {} for this task.".format(short)
        self.log.info("v2 choice | %s | session=%s | %s | chose %s | applied=%s", client, session_id[:8],
                      decided_by, chosen or "-", applied)
        return {"ok": True, "applied": applied, "active_model": chosen if applied else None, "message": message}

    # -- measured usage -------------------------------------------------------------------

    def on_usage(self, *, client: str, session_id: str, original_model: Optional[str], sent_model: Optional[str],
                 routed: bool, status: int, usage: Dict[str, Any], requested_ttl: Optional[int],
                 billing: str, request_class: Optional[str], latency_ms: int, limits: Dict[str, Any]) -> None:
        try:
            self.db.add_usage({
                "ts": now(), "client": client, "session_id": session_id,
                "decision_id": self._last_decision.get(client + ":" + session_id),
                "original_model": original_model, "sent_model": sent_model, "routed": int(routed), "status": status,
                "stop_reason": usage.get("stop_reason"), "input_tokens": usage.get("input_tokens"),
                "output_tokens": usage.get("output_tokens"), "cache_read_tokens": usage.get("cache_read_tokens"),
                "cache_write_tokens": usage.get("cache_write_tokens"), "cache_write_5m": usage.get("cache_write_5m"),
                "cache_write_1h": usage.get("cache_write_1h"), "requested_ttl": requested_ttl, "billing_mode": billing,
                "request_class": (request_class or "")[:24] or None, "latency_ms": latency_ms,
                "rate_limits": limits or None,
            })
        except Exception:
            self.log.exception("v2 usage record failed")

    def note_route_failed(self, client: str, session_id: str) -> None:
        decision_id = self._last_decision.get(client + ":" + session_id)
        if decision_id:
            self.db.update_decision(decision_id, route_failed=1)
