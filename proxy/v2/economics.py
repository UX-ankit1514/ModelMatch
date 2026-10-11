"""Stay on the current model, or switch? (System 3: switch economics)

Each model keeps its own prompt cache. Staying on a warm model re-reads the
conversation at the cheap cache-read price; a model that hasn't seen this
conversation must first write all of it into its own cache, at full price. So
mid-conversation, a "cheaper" model is often the more expensive choice.

For a task of k model calls (estimated from the analyst's expected_tool_steps):

    stay   = first call reads ctx (cache read if warm, cache write if cold)
             + (k-1) further calls reading ctx from cache
             + k x (new tokens written + output)
    switch = the same, with the candidate's prices and cache state (normally cold)

Output is estimated from expected_output (with a low/high band), raised for
reasoning-heavy tasks because these models count thinking as output.

Honesty rules:
* MONEY (dollars) only for API-key billing, known prices AND a measured context size.
* Subscriptions: SUBSCRIPTION_USAGE, a relative weight shown in words only.
* No prices: TOKENS (how much context would be processed uncached) or UNKNOWN.
"""

from dataclasses import replace
from typing import Optional

from .pricing import price_for
from .schema import CacheState, Economics, ImpactType, ResourceImpact, SessionSnapshot, TaskAnalysis

STEPS = {"few": 2, "several": 5, "many": 12}
OUTPUT_TOKENS = {"short": 400, "medium": 1500, "long": 5000}
THINKING = {"low": 1.0, "medium": 1.2, "high": 1.6}
NEW_TOKENS_PER_STEP = 1500  # tool results and the new message added each call
START_OF_SESSION_CONTEXT = 20000  # system prompt + tools, roughly; an assumption, so confidence stays low
STAY_CHEAPER = 1.25  # switching must cost 25% more before we call staying cheaper
SWITCH_CHEAPER = 0.75  # and save at least 25% before we call switching cheaper
SWITCH_CHEAPER_UNDER_PRESSURE = 0.9  # close to a plan limit, smaller savings are worth it
LARGE_UNCACHED_CONTEXT = 30000


def _warm(state: str) -> bool:
    return state in (CacheState.WARM, CacheState.LIKELY_WARM)


def _cost(price, ctx: int, warm: bool, steps: int, output: float, ttl_kind: str) -> float:
    write = price["write_1h" if ttl_kind == "1h" else "write_5m"]
    first = ctx * (price["read"] if warm else write)
    rest = (steps - 1) * ctx * price["read"]
    per_step = NEW_TOKENS_PER_STEP * write + output * price["output"]
    return (first + rest + steps * per_step) / 1_000_000


def estimate(current: Optional[str], candidate: Optional[str], snapshot: SessionSnapshot,
             analysis: Optional[TaskAnalysis]) -> Economics:
    result = _estimate(current, candidate, snapshot, analysis)
    return replace(result, cache_current=snapshot.cache_state_current, cache_candidate=snapshot.cache_state_candidate)


def _estimate(current: Optional[str], candidate: Optional[str], snapshot: SessionSnapshot,
              analysis: Optional[TaskAnalysis]) -> Economics:
    unknown = ResourceImpact(ImpactType.UNKNOWN)
    if not current or not candidate:
        return Economics(unknown, unknown, candidate=candidate, notes=("no model to compare",))

    steps = STEPS.get((analysis.expected_tool_steps if analysis else None) or "", 3)
    base_output = OUTPUT_TOKENS.get((analysis.expected_output if analysis else None) or "", 1000)
    output = base_output * THINKING.get((analysis.reasoning_requirement if analysis else None) or "", 1.2)

    if not snapshot.observable:
        return Economics(unknown, unknown, candidate=candidate, notes=("this tool's usage can't be measured",))
    ctx = snapshot.estimated_context_tokens
    measured = ctx is not None
    notes = []
    if ctx is None:
        if snapshot.measured:
            return Economics(unknown, unknown, candidate=candidate, notes=("context size not measured",))
        # Nothing measured yet in this session: little conversation-specific context is cached anywhere.
        ctx = START_OF_SESSION_CONTEXT
        notes.append("start of session: little is cached yet")
    current_warm = _warm(snapshot.cache_state_current)
    candidate_warm = _warm(snapshot.cache_state_candidate)
    if current_warm and not candidate_warm:
        notes.append("current model's cache is warm, the candidate's is cold")

    current_price, candidate_price = price_for(current), price_for(candidate)
    ttl_kind = "1h" if snapshot.billing_mode == "subscription" else "5m"

    if current_price and candidate_price:
        def band(price, warm):
            return (_cost(price, ctx, warm, steps, output * 0.5, ttl_kind),
                    _cost(price, ctx, warm, steps, output, ttl_kind),
                    _cost(price, ctx, warm, steps, output * 2.0, ttl_kind))

        s_lo, s_mid, s_hi = band(current_price, current_warm)
        w_lo, w_mid, w_hi = band(candidate_price, candidate_warm)
        if snapshot.billing_mode == "api_key":
            kind, unit, confidence = ImpactType.MONEY, "USD", "medium" if measured else "low"
        elif snapshot.billing_mode == "subscription":
            kind, unit, confidence = ImpactType.SUBSCRIPTION_USAGE, "relative", "low"
        else:  # can't tell how they pay: relative weights only, never dollars
            kind, unit, confidence = ImpactType.UNKNOWN, "relative", "low"
        stay = ResourceImpact(kind, round(s_mid, 4), round(s_lo, 4), round(s_hi, 4), confidence, unit)
        switch = ResourceImpact(kind, round(w_mid, 4), round(w_lo, 4), round(w_hi, 4), confidence, unit)
        ratio = round(w_mid / s_mid, 2) if s_mid > 0 else None
    elif measured:
        # No prices (e.g. Codex models): compare how much context would be processed without the cache.
        stay_tokens = 0 if current_warm else ctx
        switch_tokens = 0 if candidate_warm else ctx
        stay = ResourceImpact(ImpactType.TOKENS, float(stay_tokens), confidence="low", unit="uncached tokens")
        switch = ResourceImpact(ImpactType.TOKENS, float(switch_tokens), confidence="low", unit="uncached tokens")
        verdict = "stay_cheaper" if switch_tokens - stay_tokens >= LARGE_UNCACHED_CONTEXT else "unknown"
        return Economics(stay, switch, None, verdict, candidate, tuple(notes + ["no price list"]))
    else:
        return Economics(unknown, unknown, candidate=candidate, notes=tuple(notes + ["no prices, no measurements"]))

    cheaper_below = SWITCH_CHEAPER_UNDER_PRESSURE if (snapshot.usage_pressure or 0) >= 0.8 else SWITCH_CHEAPER
    if ratio is None:
        verdict = "unknown"
    elif ratio >= STAY_CHEAPER:
        verdict = "stay_cheaper"
    elif ratio <= cheaper_below:
        verdict = "switch_cheaper"
    else:
        verdict = "similar"
    return Economics(stay, switch, ratio, verdict, candidate, tuple(notes))


def describe(economics: Optional[Economics], current: str, candidate: str) -> Optional[str]:
    """A short, honest sentence about the switch's cost, or None when we don't know enough."""
    if economics is None or economics.verdict == "unknown":
        return None
    stay, switch = economics.stay, economics.switch
    if (stay.type == ImpactType.MONEY and stay.confidence != "low" and stay.estimated_value is not None
            and switch.estimated_value is not None):
        text = "Estimated for this task: staying on {} ~${:.2f}, switching to {} ~${:.2f}".format(
            current, stay.estimated_value, candidate, switch.estimated_value)
        low, high = "{:.2f}".format(switch.min or 0), "{:.2f}".format(switch.max or 0)
        return text + (" (range ${}-${}).".format(low, high) if low != high else ".")
    if economics.verdict == "stay_cheaper":
        if economics.ratio and economics.ratio >= 2.5:
            return "Switching now is likely to use a lot more of your usage than staying."
        return "Switching now is likely to use more of your usage than staying."
    if economics.verdict == "switch_cheaper":
        return "{} is likely to use less of your usage for this task.".format(candidate)
    return None
