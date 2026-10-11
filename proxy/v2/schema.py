"""The structured objects V2 passes around.

Unknown values stay None / "unknown": nothing here is ever filled with a guess
that looks like a measurement.
"""

import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# Vocabularies
# ---------------------------------------------------------------------------

TASK_TYPES = (
    "architecture", "system_design", "frontend_ui", "frontend_implementation", "backend_implementation",
    "debugging", "complex_debugging", "refactor", "documentation", "testing", "code_review", "research",
    "planning", "devops", "small_edit", "simple_dev_operation", "general_question", "unknown",
)
LEVELS = ("low", "medium", "high")
SCOPES = ("none", "single_file", "multi_file", "cross_system", "unknown")
OUTPUT_SIZES = ("short", "medium", "long")
TOOL_STEPS = ("few", "several", "many")
FOLLOWUPS = ("new_task", "continuation", "retry_or_correction", "escalation_request")

# Capability tiers. A tier is what a task *needs*; which model provides it is
# decided per tool (see adapters.py). Rank order matters.
TIERS = ("light", "standard", "heavy")
TIER_RANK = {"light": 1, "standard": 2, "heavy": 3}

# Model families and how capable each is (Claude). Fable is at least Opus.
FAMILY_RANK = {"haiku": 1, "sonnet": 2, "opus": 3, "fable": 4}


class CacheState:
    WARM = "WARM"  # measured: this model read or wrote this session's cache recently
    LIKELY_WARM = "LIKELY_WARM"  # used recently, within its cache lifetime, but no cache numbers seen
    COLD = "COLD"  # not used in this session, or its cache lifetime has passed
    UNKNOWN = "UNKNOWN"  # no measurements at all (e.g. a tool whose traffic we can't see)


class ImpactType:
    MONEY = "MONEY"  # API billing, US dollars
    TOKENS = "TOKENS"  # token counts only, no reliable price
    SUBSCRIPTION_USAGE = "SUBSCRIPTION_USAGE"  # plan allowance; shown in words, never dollars
    LATENCY = "LATENCY"
    UNKNOWN = "UNKNOWN"


class Intervention:
    SILENT = "SILENT"  # keep going, don't interrupt
    ASK = "ASK"  # show the popup
    AUTO = "AUTO"  # apply without asking, tell the developer how to override


class Reason:
    TASK_LOW_COMPLEXITY = "TASK_LOW_COMPLEXITY"
    TASK_MEDIUM_COMPLEXITY = "TASK_MEDIUM_COMPLEXITY"
    TASK_HIGH_COMPLEXITY = "TASK_HIGH_COMPLEXITY"
    ARCHITECTURE_REASONING = "ARCHITECTURE_REASONING"
    COMPLEX_DEBUGGING = "COMPLEX_DEBUGGING"
    HIGH_RISK = "HIGH_RISK"
    AMBIGUOUS_TASK = "AMBIGUOUS_TASK"
    LOW_ROUTING_CONFIDENCE = "LOW_ROUTING_CONFIDENCE"
    EXPLICIT_MODEL_REQUEST = "EXPLICIT_MODEL_REQUEST"
    MODEL_UNAVAILABLE = "MODEL_UNAVAILABLE"
    MODEL_RESTRICTED = "MODEL_RESTRICTED"
    ROUTING_OFF = "ROUTING_OFF"
    CURRENT_MODEL_ALREADY_GOOD = "CURRENT_MODEL_ALREADY_GOOD"
    CURRENT_MODEL_TOO_WEAK = "CURRENT_MODEL_TOO_WEAK"
    SAME_TIER = "SAME_TIER"
    SAME_TASK_LEASE = "SAME_TASK_LEASE"
    TASK_CHANGED = "TASK_CHANGED"
    CURRENT_CACHE_WARM = "CURRENT_CACHE_WARM"
    CANDIDATE_CACHE_COLD = "CANDIDATE_CACHE_COLD"
    SWITCH_USAGE_PENALTY = "SWITCH_USAGE_PENALTY"
    SWITCH_EXPECTED_SAVINGS = "SWITCH_EXPECTED_SAVINGS"
    SWITCH_BENEFIT_TOO_SMALL = "SWITCH_BENEFIT_TOO_SMALL"
    USAGE_LIMIT_PRESSURE = "USAGE_LIMIT_PRESSURE"
    PAST_ESCALATION = "PAST_ESCALATION"  # suffixed with the family, e.g. PAST_SONNET_ESCALATION
    PAST_SUCCESS_LOWER = "PAST_SUCCESS_LOWER"  # suffixed, e.g. PAST_SONNET_SUCCESS
    PAST_OVERPROVISIONING = "PAST_OPUS_OVERPROVISIONING"
    USER_PREFERS = "USER_HISTORICALLY_PREFERS"  # suffixed, e.g. USER_HISTORICALLY_PREFERS_OPUS
    USER_KEEPS_DECLINING = "USER_KEEPS_DECLINING"
    ROUTER_UNAVAILABLE = "ROUTER_UNAVAILABLE"
    AUTO_ROUTE_EVIDENCE = "AUTO_ROUTE_EVIDENCE"


def now() -> float:
    return round(time.time(), 3)


def new_id() -> str:
    return uuid.uuid4().hex[:12]


def _plain(obj: Any) -> Any:
    if hasattr(obj, "to_dict"):
        return obj.to_dict()
    if isinstance(obj, (list, tuple)):
        return [_plain(x) for x in obj]
    if isinstance(obj, dict):
        return {k: _plain(v) for k, v in obj.items()}
    return obj


# ---------------------------------------------------------------------------
# Objects
# ---------------------------------------------------------------------------


@dataclass
class PromptEvent:
    """One human prompt, normalised across Claude Code / Codex / Copilot.

    `prompt` lives in memory only: to_dict() never includes it.
    """

    client: str
    session_id: str
    prompt: str
    current_model: Optional[str]
    prompt_fingerprint: str
    turn_id: Optional[str] = None
    project_id: Optional[str] = None  # hash of the working folder, never the path
    available_models: Tuple[str, ...] = ()
    routing_enabled: Optional[bool] = None
    event_id: str = field(default_factory=new_id)
    timestamp: float = field(default_factory=now)
    source_type: str = "human"

    @property
    def prompt_chars(self) -> int:
        return len(self.prompt)

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        data.pop("prompt")
        data["prompt_chars"] = self.prompt_chars
        return data


@dataclass(frozen=True)
class TaskAnalysis:
    task_type: str
    complexity: str
    reasoning_requirement: str
    required_tier: str
    confidence: Optional[float]
    subtype: str = ""  # a few words; kept in memory/session state, never in the learning database
    secondary_task_type: Optional[str] = None
    secondary_probability: Optional[float] = None
    scope: str = "unknown"
    risk: str = "unknown"
    expected_output: Optional[str] = None
    expected_tool_steps: Optional[str] = None
    same_task_probability: Optional[float] = None
    followup_kind: Optional[str] = None
    explicit_model_request: Optional[str] = None  # a family ("opus") or a model name the developer asked for
    reason_codes: Tuple[str, ...] = ()
    reason: str = ""
    source: str = ""  # curvet | route | mock
    analyzer_model: str = ""
    latency_ms: Optional[int] = None

    @property
    def ambiguous(self) -> bool:
        return (self.secondary_probability is not None and self.secondary_probability >= 0.35
                and bool(self.secondary_task_type) and self.secondary_task_type != self.task_type)

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        data["reason_codes"] = list(self.reason_codes)
        return data


@dataclass(frozen=True)
class ModelUsage:
    """What one model has done in one session, from measured API responses."""

    model: str
    last_used_at: float
    requests: int = 0
    context_tokens: Optional[int] = None  # input + cache read + cache write of the latest main request
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    ttl_seconds: Optional[int] = None  # requested cache lifetime (from cache_control)


@dataclass
class SessionSnapshot:
    session_id: str
    client: str
    current_model: Optional[str]  # what the tool is configured to use
    effective_model: Optional[str]  # what actually answered last (after any ModelMatch route)
    estimated_context_tokens: Optional[int] = None
    cache_state_current: str = CacheState.UNKNOWN
    cache_state_candidate: str = CacheState.UNKNOWN
    cached_input_tokens: Optional[int] = None
    cache_creation_tokens: Optional[int] = None
    billing_mode: str = "unknown"  # api_key | subscription | unknown
    usage_pressure: Optional[float] = None  # 0..1 highest plan-limit utilization seen, if any
    observable: bool = False  # the tool's API traffic passes through ModelMatch (usage can be measured)
    measured: bool = False  # usage has been measured in this session
    timestamp: float = field(default_factory=now)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ResourceImpact:
    type: str
    estimated_value: Optional[float] = None
    min: Optional[float] = None
    max: Optional[float] = None
    confidence: str = "low"  # low | medium | high
    unit: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class Economics:
    """Stay on the current model vs switch to a candidate, for this task."""

    stay: ResourceImpact
    switch: ResourceImpact
    ratio: Optional[float] = None  # switch / stay (> 1 means switching costs more)
    verdict: str = "unknown"  # stay_cheaper | switch_cheaper | similar | unknown
    candidate: Optional[str] = None
    notes: Tuple[str, ...] = ()
    cache_current: Optional[str] = None  # CacheState of the model we'd stay on
    cache_candidate: Optional[str] = None  # CacheState of the model we'd switch to

    def to_dict(self) -> Dict[str, Any]:
        return {"stay": self.stay.to_dict(), "switch": self.switch.to_dict(), "ratio": self.ratio,
                "verdict": self.verdict, "candidate": self.candidate, "notes": list(self.notes),
                "cache_current": self.cache_current, "cache_candidate": self.cache_candidate}


@dataclass
class FamilyEvidence:
    """Decayed evidence for one model family on one kind of task."""

    family: str
    samples: float = 0.0  # effective (recency-weighted) number of closed observations
    chosen: float = 0.0  # how often the developer ended up on this family (weighted)
    success: float = 0.0  # no retry, escalation, switch-back, refusal or error
    escalated: float = 0.0
    retried: float = 0.0
    switched_back: float = 0.0
    failed: float = 0.0
    overprovisioned: float = 0.0  # analyst said a lower tier was enough, nothing went wrong

    def rate(self, value: float) -> Optional[float]:
        return round(value / self.samples, 3) if self.samples > 0 else None

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        for name in ("success", "escalated", "retried", "switched_back", "failed", "overprovisioned"):
            data[name + "_rate"] = self.rate(getattr(self, name))
        return {k: (round(v, 3) if isinstance(v, float) else v) for k, v in data.items()}


@dataclass
class TaskPreferenceProfile:
    client: str
    task_type: str
    observation_count: int = 0
    effective_samples: float = 0.0
    families: Dict[str, FamilyEvidence] = field(default_factory=dict)
    preferred_family: Optional[str] = None
    preference_share: Optional[float] = None
    recent_choices: List[str] = field(default_factory=list)
    override_rate: Optional[float] = None
    reject_rate: Optional[float] = None
    declines_in_a_row: int = 0  # recent rejections of a lower-model suggestion, newest first
    confidence: float = 0.0

    def family(self, name: str) -> FamilyEvidence:
        return self.families.get(name) or FamilyEvidence(family=name)

    def to_dict(self) -> Dict[str, Any]:
        data = {k: v for k, v in asdict(self).items() if k != "families"}
        data["families"] = {k: v.to_dict() for k, v in self.families.items()}
        return data


@dataclass
class RoutingLease:
    task_id: str
    model: Optional[str]  # None: the tool's own model (no route)
    source: str  # user | router | auto | explicit
    task_type: str = "unknown"
    tier: Optional[str] = None
    created_at: float = field(default_factory=now)
    last_validated: float = field(default_factory=now)
    confidence: Optional[float] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class Option:
    """One button in the popup."""

    id: str  # "keep" or a model id
    label: str
    model_id: Optional[str]  # None = keep the current model
    primary: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class RouteDecision:
    decision_id: str
    client: str
    session_id: str
    task_id: str
    current_model: Optional[str]
    effective_model: Optional[str]
    final_model: Optional[str]  # None = keep the tool's own model
    final_display: str
    intervention: str
    confidence: Optional[float]
    reason_codes: List[str]
    explanation: str
    semantic_tier: Optional[str] = None
    semantic_model: Optional[str] = None
    candidate_model: Optional[str] = None  # the model a switch would go to (for ASK / notes)
    candidate_display: Optional[str] = None
    personal_signal: Dict[str, Any] = field(default_factory=dict)
    economics: Optional[Economics] = None
    options: List[Option] = field(default_factory=list)
    note: Optional[str] = None  # a short non-blocking message for the developer
    lease: Optional[RoutingLease] = None
    routable: bool = True
    suggested_lower: bool = False  # a lighter model was suggested (the anti-nag counter uses this)

    @property
    def changes_model(self) -> bool:
        return self.final_model is not None and self.final_model != self.effective_model

    def to_dict(self) -> Dict[str, Any]:
        data = {k: _plain(getattr(self, k)) for k in self.__dataclass_fields__
                if k not in ("economics", "options", "lease")}
        data["economics"] = self.economics.to_dict() if self.economics else None
        data["options"] = [o.to_dict() for o in self.options]
        data["lease"] = self.lease.to_dict() if self.lease else None
        return data
