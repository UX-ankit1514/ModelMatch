"""Model catalog and provider adapters.

Recommendation and execution are deliberately separate. ModelMatch may
recommend any model; a recommendation is only *applied* when an adapter that
can really serve Claude Code's requests exists for it. Today only the
Anthropic adapter can do that. Other providers are registered as "not
configured" so the user is told the truth instead of seeing fake routing.
"""

import re
from dataclasses import dataclass
from typing import Dict, Optional, Tuple

# ---------------------------------------------------------------------------
# Model resolution
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ResolvedModel:
    raw: str  # exactly what the router returned
    display: str  # human-friendly name, e.g. "Claude Sonnet 5.5"
    provider: str  # anthropic | openai | google | deepseek | meta | mistral | xai | unknown
    model_id: Optional[str] = None  # API id sent upstream (Anthropic only)
    family: Optional[str] = None  # fable | opus | sonnet | haiku
    substituted_from: Optional[str] = None  # set when a retired/unknown name was mapped


# (family, version) -> (api model id, display name, status)
# Source: Anthropic models overview, cached 2026-09-25.
_ANTHROPIC_CATALOG: Dict[Tuple[str, str], Tuple[Optional[str], str, str]] = {
    ("fable", "5.1"): ("claude-fable-5-1", "Claude Fable 5.1", "active"),
    ("fable", "5"): ("claude-fable-5", "Claude Fable 5", "active"),
    ("opus", "5.5"): ("claude-opus-5-5", "Claude Opus 5.5", "active"),
    ("opus", "5"): ("claude-opus-5", "Claude Opus 5", "active"),
    ("opus", "4.8"): ("claude-opus-4-8", "Claude Opus 4.8", "active"),
    ("opus", "4.7"): ("claude-opus-4-7", "Claude Opus 4.7", "active"),
    ("opus", "4.6"): ("claude-opus-4-6", "Claude Opus 4.6", "active"),
    ("opus", "4.5"): ("claude-opus-4-5", "Claude Opus 4.5", "active"),
    ("opus", "4.1"): (None, "Claude Opus 4.1", "retired"),
    ("opus", "4"): ("claude-opus-4-0", "Claude Opus 4", "deprecated"),
    ("opus", "3"): (None, "Claude Opus 3", "retired"),
    ("sonnet", "5.5"): ("claude-sonnet-5-5", "Claude Sonnet 5.5", "active"),
    ("sonnet", "5"): ("claude-sonnet-5", "Claude Sonnet 5", "active"),
    ("sonnet", "4.6"): ("claude-sonnet-4-6", "Claude Sonnet 4.6", "active"),
    ("sonnet", "4.5"): ("claude-sonnet-4-5", "Claude Sonnet 4.5", "active"),
    ("sonnet", "4"): ("claude-sonnet-4-0", "Claude Sonnet 4", "deprecated"),
    ("sonnet", "3.7"): (None, "Claude Sonnet 3.7", "retired"),
    ("sonnet", "3.5"): (None, "Claude Sonnet 3.5", "retired"),
    ("sonnet", "3"): (None, "Claude Sonnet 3", "retired"),
    ("haiku", "4.5"): ("claude-haiku-4-5", "Claude Haiku 4.5", "active"),
    ("haiku", "3.5"): (None, "Claude Haiku 3.5", "retired"),
    ("haiku", "3"): (None, "Claude Haiku 3", "retired"),
}

# The current model of each family, used when an older name is recommended.
_FAMILY_CURRENT = {
    "fable": ("fable", "5.1"),
    "opus": ("opus", "5.5"),
    "sonnet": ("sonnet", "5.5"),
    "haiku": ("haiku", "4.5"),
}

_OTHER_PROVIDERS = (
    (re.compile(r"\b(gpt|chatgpt|openai|codex|o1|o3|o4)\b"), "openai"),
    (re.compile(r"\b(gemini|gemma|google)\b"), "google"),
    (re.compile(r"\bdeepseek\b"), "deepseek"),
    (re.compile(r"\b(llama|meta)\b"), "meta"),
    (re.compile(r"\b(mistral|mixtral|codestral)\b"), "mistral"),
    (re.compile(r"\b(grok|xai)\b"), "xai"),
    (re.compile(r"\bqwen\b"), "alibaba"),
)

_FAMILY_RE = re.compile(r"\b(fable|opus|sonnet|haiku)\b")
_API_ID_RE = re.compile(r"^claude-[a-z0-9][a-z0-9.-]*$")
# ModelMatch's analyzer appends a tier ("low", "max", ...). Tiers are
# not a user-facing concept in this product, so they are dropped.
_TIER_RE = re.compile(r"\s+(ultra\s*max|low|medium|high|max)\s*$", re.IGNORECASE)


def strip_tier(name: str) -> str:
    return _TIER_RE.sub("", " ".join(str(name).split())).strip()


def _claude_version(text: str) -> Optional[str]:
    text = re.sub(r"\[.*?\]", " ", text)  # "[1m]" context suffix
    text = re.sub(r"\d{8}", " ", text)  # date snapshots like 20251001
    numbers = re.findall(r"\d+", text)[:2]
    if not numbers:
        return None
    if len(numbers) == 2 and numbers[1] == "0":
        numbers = numbers[:1]
    return ".".join(numbers)


def _from_catalog(key: Tuple[str, str], raw: str, substituted_from: Optional[str] = None) -> ResolvedModel:
    model_id, display, _ = _ANTHROPIC_CATALOG[key]
    return ResolvedModel(raw=raw, display=display, provider="anthropic", model_id=model_id,
                         family=key[0], substituted_from=substituted_from)


def resolve_model(raw: str) -> ResolvedModel:
    """Turn any model name ("Claude 4 Sonnet max", "gpt-4o", "claude-haiku-4-5-20251001") into a ResolvedModel."""
    name = strip_tier(raw)
    lower = name.lower()

    family_match = _FAMILY_RE.search(lower)
    if family_match or lower.startswith("claude"):
        if not family_match:
            return ResolvedModel(raw=raw, display=name, provider="anthropic")
        family = family_match.group(1)
        version = _claude_version(lower)
        current = _FAMILY_CURRENT[family]

        if version is None:  # "sonnet", "Claude Opus" -> the family's current model
            return _from_catalog(current, raw)

        entry = _ANTHROPIC_CATALOG.get((family, version))
        if entry is not None:
            if entry[2] == "active":
                return _from_catalog((family, version), raw)
            # Retired/deprecated: use the family's current model, and say so.
            return _from_catalog(current, raw, substituted_from="{} ({})".format(entry[1], entry[2]))

        if _API_ID_RE.match(lower):  # a newer id we don't know yet: try it as-is
            return ResolvedModel(raw=raw, display=lower, provider="anthropic", model_id=lower, family=family)
        return _from_catalog(current, raw, substituted_from="{} (unrecognized version)".format(name))

    for pattern, provider in _OTHER_PROVIDERS:
        if pattern.search(lower):
            return ResolvedModel(raw=raw, display=name, provider=provider)
    return ResolvedModel(raw=raw, display=name, provider="unknown")


def is_small_fast_model(model: object) -> bool:
    """Claude Code's background helpers (titles, summaries, ...) run on Haiku."""
    return isinstance(model, str) and "haiku" in model.lower()


def same_model(a: Optional[str], b: Optional[str]) -> bool:
    if not a or not b:
        return False
    ra, rb = resolve_model(a), resolve_model(b)
    if ra.substituted_from or rb.substituted_from:
        return False
    return ra.model_id is not None and ra.model_id == rb.model_id


# ---------------------------------------------------------------------------
# Provider adapters
# ---------------------------------------------------------------------------

PROVIDER_LABELS = {
    "anthropic": "Anthropic (Claude)",
    "openai": "OpenAI",
    "google": "Google Gemini",
    "deepseek": "DeepSeek",
    "meta": "Meta Llama",
    "mistral": "Mistral",
    "xai": "xAI Grok",
    "alibaba": "Alibaba Qwen",
    "openrouter": "OpenRouter",
    "unknown": "this provider",
}


class ProviderAdapter:
    """Decides whether a recommended model can really be executed, and how."""

    provider = "base"

    def can_route(self, model: ResolvedModel) -> Tuple[bool, str]:
        raise NotImplementedError

    def apply(self, request_body: dict, model: ResolvedModel) -> dict:
        raise NotImplementedError


# Request features differ between Claude models. Claude Code builds each request
# for its own model, so a routed request is adjusted to what the target accepts.
_NO_ADAPTIVE_THINKING = {"claude-haiku-4-5", "claude-sonnet-4-5", "claude-opus-4-5", "claude-sonnet-4-0",
                         "claude-opus-4-0"}
_THINKING_ALWAYS_ON = {"claude-opus-5-5", "claude-fable-5", "claude-fable-5-1"}
_NO_EFFORT = {"claude-haiku-4-5", "claude-sonnet-4-5", "claude-sonnet-4-0", "claude-opus-4-0"}
_EFFORT_CAP = {"claude-opus-4-5": "high", "claude-opus-4-6": "max", "claude-sonnet-4-6": "max"}
_EFFORT_ORDER = ("low", "medium", "high", "xhigh", "max")
_MAX_OUTPUT_TOKENS = {"claude-haiku-4-5": 64000, "claude-sonnet-4-5": 64000, "claude-opus-4-5": 64000,
                      "claude-sonnet-4-0": 64000, "claude-opus-4-0": 32000}
# {"role": "system"} entries inside messages[] (mid-conversation system messages)
_MID_CONVERSATION_SYSTEM = {"claude-opus-5", "claude-opus-5-5", "claude-opus-4-8", "claude-fable-5", "claude-fable-5-1",
                            "claude-sonnet-5-5"}
# output_config on a mid-conversation system message (per-turn effort)
_PER_MESSAGE_EFFORT = {"claude-opus-5", "claude-opus-5-5", "claude-fable-5-1", "claude-sonnet-5-5"}


def _text_blocks(content) -> list:
    if isinstance(content, str):
        return [{"type": "text", "text": content}] if content else []
    return [b for b in content if isinstance(b, dict)] if isinstance(content, list) else []


def _adapt_messages(messages: list, target: str, strip_thinking: bool) -> list:
    adapted: list = []
    for message in messages:
        if not isinstance(message, dict):
            adapted.append(message)
            continue
        role = message.get("role")
        if role == "system" and target not in _PER_MESSAGE_EFFORT and "output_config" in message:
            message = {k: v for k, v in message.items() if k != "output_config"}
            if not _text_blocks(message.get("content")):
                continue  # it only carried the effort setting
        if role == "system" and target not in _MID_CONVERSATION_SYSTEM:
            # Older models don't accept system turns inside messages[]: keep the
            # instruction, delivered the way Claude Code does it for them.
            blocks = []
            for block in _text_blocks(message.get("content")):
                if block.get("type") == "text":
                    blocks.append({"type": "text",
                                   "text": "<system-reminder>\n" + str(block.get("text", "")) + "\n</system-reminder>"})
            if not blocks:
                continue
            if adapted and isinstance(adapted[-1], dict) and adapted[-1].get("role") == "user":
                previous = dict(adapted[-1])
                previous["content"] = _text_blocks(previous.get("content")) + blocks
                adapted[-1] = previous
            else:
                adapted.append({"role": "user", "content": blocks})
            continue
        if strip_thinking and role == "assistant" and isinstance(message.get("content"), list):
            kept = [b for b in message["content"]
                    if not (isinstance(b, dict) and b.get("type") in ("thinking", "redacted_thinking"))]
            if kept and len(kept) != len(message["content"]):
                message = dict(message, content=kept)  # other models' signed thinking can't be verified here
        adapted.append(message)
    return adapted


class AnthropicAdapter(ProviderAdapter):
    """Claude Code already speaks the Anthropic Messages API, so routing is a model swap
    plus small compatibility adjustments for the target model."""

    provider = "anthropic"

    def can_route(self, model: ResolvedModel) -> Tuple[bool, str]:
        if model.model_id:
            return True, "Routed through the local proxy to the Anthropic API."
        return False, "ModelMatch named a Claude model this proxy doesn't recognize."

    def apply(self, request_body: dict, model: ResolvedModel) -> dict:
        target = model.model_id
        routed = dict(request_body)
        routed["model"] = target

        thinking = routed.get("thinking")
        thinking_type = thinking.get("type") if isinstance(thinking, dict) else None
        legacy_target = target in _NO_ADAPTIVE_THINKING
        if legacy_target and thinking_type in ("adaptive", "between_tools"):
            routed.pop("thinking")  # run the older model without extended thinking
            self._drop_thinking_edits(routed)
        elif target in _THINKING_ALWAYS_ON and thinking_type in ("disabled", "between_tools", "enabled"):
            routed.pop("thinking")  # these models always think; explicit off/budget settings are rejected

        if isinstance(routed.get("messages"), list):
            routed["messages"] = _adapt_messages(routed["messages"], target, strip_thinking=legacy_target)

        output_config = routed.get("output_config")
        if isinstance(output_config, dict) and "effort" in output_config:
            output_config = dict(output_config)
            effort = output_config.get("effort")
            if target in _NO_EFFORT:
                output_config.pop("effort")
            elif target in _EFFORT_CAP and effort in _EFFORT_ORDER:
                cap = _EFFORT_CAP[target]
                if _EFFORT_ORDER.index(effort) > _EFFORT_ORDER.index(cap) or (cap == "max" and effort == "xhigh"):
                    output_config["effort"] = "high" if cap == "max" else cap
            if output_config:
                routed["output_config"] = output_config
            else:
                routed.pop("output_config")

        limit = _MAX_OUTPUT_TOKENS.get(target)
        if limit and isinstance(routed.get("max_tokens"), int) and routed["max_tokens"] > limit:
            routed["max_tokens"] = limit
        return routed

    @staticmethod
    def _drop_thinking_edits(routed: dict) -> None:
        management = routed.get("context_management")
        if not isinstance(management, dict) or not isinstance(management.get("edits"), list):
            return
        edits = [e for e in management["edits"]
                 if not (isinstance(e, dict) and str(e.get("type", "")).startswith("clear_thinking"))]
        if edits:
            routed["context_management"] = dict(management, edits=edits)
        else:
            routed.pop("context_management")


class NotConfiguredAdapter(ProviderAdapter):
    """Placeholder for providers that need a translation gateway (planned)."""

    def __init__(self, provider: str):
        self.provider = provider

    def can_route(self, model: ResolvedModel) -> Tuple[bool, str]:
        label = PROVIDER_LABELS.get(self.provider, self.provider)
        return False, "Automatic routing to {} models isn't set up yet.".format(label)

    def apply(self, request_body: dict, model: ResolvedModel) -> dict:
        raise RuntimeError("No adapter configured for provider {!r}".format(self.provider))


ADAPTERS: Dict[str, ProviderAdapter] = {
    "anthropic": AnthropicAdapter(),
    # Future adapters (OpenAI/Codex, Gemini, OpenRouter, ...) replace these.
    "openai": NotConfiguredAdapter("openai"),
    "google": NotConfiguredAdapter("google"),
    "openrouter": NotConfiguredAdapter("openrouter"),
}


def adapter_for(model: ResolvedModel) -> ProviderAdapter:
    return ADAPTERS.get(model.provider) or NotConfiguredAdapter(model.provider)
