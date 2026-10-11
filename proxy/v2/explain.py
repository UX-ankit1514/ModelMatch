"""Short, plain explanations built from structured reason codes.

The engine decides with codes; only this module turns them into words, so the
popup copy stays consistent and concise.
"""

from typing import Optional

from .schema import TaskAnalysis

TASK_WORDS = {
    "architecture": "architecture work", "system_design": "system design", "frontend_ui": "UI work",
    "frontend_implementation": "frontend work", "backend_implementation": "backend work",
    "debugging": "debugging", "complex_debugging": "complex debugging", "refactor": "refactoring",
    "documentation": "documentation", "testing": "testing", "code_review": "code review",
    "research": "research", "planning": "planning", "devops": "devops work", "small_edit": "small edits",
    "simple_dev_operation": "quick dev tasks", "general_question": "quick questions", "unknown": "this kind of task",
}
_A_TASK = {
    "architecture": "an architecture task", "system_design": "a system design task", "frontend_ui": "a UI change",
    "frontend_implementation": "a frontend task", "backend_implementation": "a backend task",
    "debugging": "a debugging task", "complex_debugging": "a complex debugging task", "refactor": "a refactor",
    "documentation": "a documentation task", "testing": "a testing task", "code_review": "a code review",
    "research": "a research task", "planning": "a planning task", "devops": "a devops task",
    "small_edit": "a small edit", "simple_dev_operation": "a quick dev task", "general_question": "a quick question",
    "unknown": "this task",
}
_LEVEL = {"low": "low-complexity", "medium": "medium-complexity", "high": "high-complexity"}


def task_words(task_type: Optional[str]) -> str:
    return TASK_WORDS.get(task_type or "unknown", "this kind of task")


def a_task(analysis: Optional[TaskAnalysis]) -> str:
    if analysis is None:
        return "this task"
    phrase = _A_TASK.get(analysis.task_type, "this task")
    level = _LEVEL.get(analysis.complexity)
    if level and phrase.startswith(("a ", "an ")):
        return "a " + level + " " + phrase.split(" ", 1)[1]
    return phrase


def downgrade(analysis: TaskAnalysis, candidate: str, proven_lower: bool) -> str:
    because = ("Similar tasks have completed successfully on {} with lower usage.".format(candidate)
               if proven_lower else "{} should handle it reliably with less usage.".format(candidate))
    return "This is {}. {}".format(a_task(analysis), because)


def stay_for_cache(analysis: TaskAnalysis, current: str, candidate: str, preferred: Optional[str]) -> str:
    if preferred:
        start = "You normally use {} for {}, but".format(preferred, task_words(analysis.task_type))
    else:
        start = "{} could handle this, but".format(candidate)
    return ("{} this {} session already has a large reusable context. Switching now is likely to use more "
            "resources.".format(start, current))


def upgrade(analysis: TaskAnalysis, current: str, target: str, escalated_before: bool) -> str:
    text = "This is {} that likely needs {}'s deeper reasoning.".format(a_task(analysis), target)
    if escalated_before:
        text += " Similar tasks on {} needed a stronger model before.".format(current)
    return text


def ambiguous(analysis: TaskAnalysis) -> str:
    return "This task crosses {} and {}.".format(task_words(analysis.task_type),
                                                 task_words(analysis.secondary_task_type))


def unsure(analysis: TaskAnalysis) -> str:
    return "ModelMatch isn't sure how demanding this is ({}).".format(a_task(analysis))


def explicit(target: str) -> str:
    return "Using {} as you asked.".format(target)


def lease(model: str) -> str:
    return "Still on {} for this task.".format(model)
