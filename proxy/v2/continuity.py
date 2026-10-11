"""Task continuity: is this prompt the same task as the last one?

A follow-up ("How should token rotation work?" after "Design authentication
architecture") keeps the routing lease: no new popup, no model bouncing. A new
phase or kind of work ("Now implement the React login form") ends it, and the
routing is evaluated afresh.

The analyst's same_task_probability alone is not enough: language models tend to
call any work on the same feature a "continuation". So a change of work group
(design -> frontend, frontend -> backend, ...) always counts as a new phase.
"""

from typing import Optional, Tuple

from .schema import RoutingLease, TaskAnalysis, new_id, now

SAME_TASK_THRESHOLD = 0.6
LEASE_IDLE_SECONDS = 2 * 3600  # a lease left alone this long has lost its context anyway

TASK_GROUPS = {
    "architecture": "design", "system_design": "design", "planning": "design", "research": "design",
    "frontend_ui": "frontend", "frontend_implementation": "frontend",
    "backend_implementation": "backend",
    "debugging": "debugging", "complex_debugging": "debugging",
    "refactor": "maintenance", "testing": "maintenance", "code_review": "maintenance",
    "documentation": "docs",
    "devops": "ops",
}
# Follow-ups of these kinds fit inside whatever task is running.
NEUTRAL_TYPES = {"general_question", "small_edit", "simple_dev_operation", "unknown"}


def work_group(task_type: Optional[str]) -> Optional[str]:
    if not task_type or task_type in NEUTRAL_TYPES:
        return None
    return TASK_GROUPS.get(task_type, task_type)


def phase_changed(previous_type: Optional[str], new_type: Optional[str]) -> bool:
    before, after = work_group(previous_type), work_group(new_type)
    return before is not None and after is not None and before != after


def same_task(previous_type: Optional[str], last_seen_at: Optional[float], analysis: TaskAnalysis,
              at: Optional[float] = None) -> Tuple[bool, str]:
    """(is the same task, why). previous_type/last_seen_at describe the session's current task."""
    at = at or now()
    if previous_type is None or last_seen_at is None:
        return False, "first task in this session"
    if at - last_seen_at > LEASE_IDLE_SECONDS:
        return False, "the previous task went idle"
    if phase_changed(previous_type, analysis.task_type):
        return False, "new phase of work ({} -> {})".format(previous_type, analysis.task_type)
    if analysis.followup_kind in ("retry_or_correction", "escalation_request"):
        return True, analysis.followup_kind.replace("_", " ")
    probability = analysis.same_task_probability
    if probability is not None:
        if probability >= SAME_TASK_THRESHOLD:
            return True, "follow-up of the same task"
        return False, "different task"
    if analysis.followup_kind == "continuation":
        return True, "follow-up of the same task"
    if analysis.followup_kind == "new_task":
        return False, "different task"
    # The analyst said nothing about continuity: same work group (or a neutral follow-up) counts as the same task.
    return work_group(previous_type) == work_group(analysis.task_type) or work_group(analysis.task_type) is None, \
        "judged from the kind of work"


def new_lease(model: Optional[str], source: str, analysis: TaskAnalysis, task_id: Optional[str] = None,
              tier: Optional[str] = None) -> RoutingLease:
    return RoutingLease(task_id=task_id or new_id(), model=model, source=source, task_type=analysis.task_type,
                        tier=tier or analysis.required_tier, confidence=analysis.confidence)
