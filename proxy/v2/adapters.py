"""One normalised view of each coding tool (the AgentAdapter layer).

The decision engine only ever asks an adapter: which model gives this capability
tier, how capable is that model, may I use it here, and what can this tool
measure. Fields a tool can't provide are reported as unsupported, never faked.

* ClaudeCodeAdapter  Anthropic models, switched by the local proxy; usage and cache
                     are measured from the API responses.
* ListAdapter        Codex / Copilot: the tool's own model list and its light /
                     standard / heavy picks (proxy/agents/catalog.py). Codex traffic
                     passes through the proxy (cached tokens measured); Copilot's
                     doesn't (usage and cache UNKNOWN).
"""

import json
import os
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from ..provider import adapter_for, resolve_model
from .schema import FAMILY_RANK, TIER_RANK, TIERS

DEFAULT_POLICY_FILES: Tuple[Path, ...] = (
    Path("/Library/Application Support/ClaudeCode/managed-settings.json"),
    Path.home() / ".claude" / "settings.json",
)
_FAMILY_TIER = {"haiku": "light", "sonnet": "standard", "opus": "heavy", "fable": "heavy"}
_LIGHT_WORDS = ("mini", "nano", "haiku", "flash", "lite", "luna", "small")
_HEAVY_WORDS = ("opus", "fable", "pro", "max", "sol", "astra")


def _short_display(display: str) -> str:
    return display[len("Claude "):] if display.startswith("Claude ") else display


def _matches(name: str, entries: Iterable[str]) -> bool:
    """A model matches a policy entry by exact id, family alias ("sonnet") or display name."""
    lower = name.lower()
    resolved = resolve_model(name)
    for entry in entries:
        entry = entry.strip().lower()
        if not entry:
            continue
        if entry in (lower, (resolved.model_id or "").lower(), resolved.display.lower()):
            return True
        if resolved.family and entry == resolved.family:
            return True
    return False


def read_policy_models(files: Sequence[Path]) -> Tuple[Tuple[str, ...], Tuple[str, ...]]:
    """(availableModels, deniedModels) from Claude Code settings files, best effort."""
    available: List[str] = []
    denied: List[str] = []
    for path in files:
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        for key, bucket in (("availableModels", available), ("deniedModels", denied)):
            values = data.get(key)
            if isinstance(values, list):
                bucket.extend(str(v) for v in values if isinstance(v, str))
    return tuple(available), tuple(denied)


class AgentAdapter:
    client = "base"
    supports_model_switch = True
    supports_cache_metrics = False
    supports_usage_metrics = False

    def model_for_tier(self, tier: str) -> Optional[str]:
        raise NotImplementedError

    def tier_of(self, model: Optional[str]) -> Optional[str]:
        raise NotImplementedError

    def rank(self, model: Optional[str]) -> Optional[int]:
        tier = self.tier_of(model)
        return TIER_RANK.get(tier) if tier else None

    def family(self, model: Optional[str]) -> Optional[str]:
        return self.tier_of(model)

    def display(self, model: Optional[str]) -> str:
        return model or "your current model"

    def short(self, model: Optional[str]) -> str:
        return self.display(model)

    def available(self, model: Optional[str]) -> Tuple[bool, str]:
        raise NotImplementedError

    def resolve_explicit(self, name: str) -> Optional[str]:
        raise NotImplementedError

    def same(self, a: Optional[str], b: Optional[str]) -> bool:
        return bool(a and b and a.lower() == b.lower())


class ClaudeCodeAdapter(AgentAdapter):
    client = "claude-code"
    supports_cache_metrics = True
    supports_usage_metrics = True

    def __init__(self, tier_models: Dict[str, str], denied: Sequence[str] = (), allowed: Sequence[str] = (),
                 policy_files: Optional[Sequence[Path]] = None):
        self.tier_models = dict(tier_models)
        files = DEFAULT_POLICY_FILES if policy_files is None else policy_files
        policy_available, policy_denied = read_policy_models(files)
        self.denied = tuple(denied) + policy_denied
        self.allowed = tuple(allowed) + policy_available

    @classmethod
    def from_settings(cls, settings, policy_files: Optional[Sequence[Path]] = None) -> "ClaudeCodeAdapter":
        if policy_files is None and os.environ.get("MODELMATCH_CLAUDE_POLICY_FILES") is not None:
            policy_files = [Path(p) for p in os.environ["MODELMATCH_CLAUDE_POLICY_FILES"].split(os.pathsep) if p]
        return cls({"light": settings.claude_light_model, "standard": settings.claude_standard_model,
                    "heavy": settings.claude_heavy_model}, settings.denied_models, settings.allowed_models,
                   policy_files)

    def model_for_tier(self, tier: str) -> Optional[str]:
        name = self.tier_models.get(tier)
        return resolve_model(name).model_id if name else None

    def family(self, model: Optional[str]) -> Optional[str]:
        return resolve_model(model).family if model else None

    def tier_of(self, model: Optional[str]) -> Optional[str]:
        return _FAMILY_TIER.get(self.family(model) or "")

    def rank(self, model: Optional[str]) -> Optional[int]:
        family = self.family(model)
        if family is None:
            return None
        return min(FAMILY_RANK[family], TIER_RANK["heavy"])  # Fable counts as heavy

    def display(self, model: Optional[str]) -> str:
        return resolve_model(model).display if model else "your current model"

    def short(self, model: Optional[str]) -> str:
        return _short_display(self.display(model))

    def same(self, a: Optional[str], b: Optional[str]) -> bool:
        if not a or not b:
            return False
        ra, rb = resolve_model(a), resolve_model(b)
        return bool(ra.model_id and ra.model_id == rb.model_id)

    def available(self, model: Optional[str]) -> Tuple[bool, str]:
        if not model:
            return False, "no model"
        if self.denied and _matches(model, self.denied):
            return False, "restricted"
        if self.allowed and not _matches(model, self.allowed):
            return False, "restricted"
        resolved = resolve_model(model)
        routable, note = adapter_for(resolved).can_route(resolved)
        return (True, "") if routable else (False, note or "unavailable")

    def resolve_explicit(self, name: str) -> Optional[str]:
        resolved = resolve_model(name)
        return resolved.model_id if resolved.provider == "anthropic" else None


class ListAdapter(AgentAdapter):
    """Codex CLI / Copilot CLI: only the models in the tool's own list can be used."""

    def __init__(self, client: str, models: Sequence, tiers: Dict[str, str], measures_cache: bool,
                 denied: Sequence[str] = (), allowed: Sequence[str] = ()):
        self.client = client
        self.models = list(models)  # ModelInfo objects
        self.tiers = dict(tiers)
        self.supports_cache_metrics = measures_cache
        self.supports_usage_metrics = measures_cache
        self.denied = tuple(d.lower() for d in denied)
        self.allowed = tuple(a.lower() for a in allowed)

    def _info(self, model: Optional[str]):
        if not model:
            return None
        return next((m for m in self.models if m.id.lower() == model.lower()), None)

    def model_for_tier(self, tier: str) -> Optional[str]:
        return self.tiers.get(tier)

    def tier_of(self, model: Optional[str]) -> Optional[str]:
        if not model:
            return None
        # The tool's own picks first. A model picked for two sizes counts as the larger one.
        picked = [t for t in TIERS if (self.tiers.get(t) or "").lower() == model.lower()]
        if picked:
            return max(picked, key=lambda t: TIER_RANK[t])
        info = self._info(model)
        text = " ".join([model, getattr(info, "name", "") or "", getattr(info, "category", "") or ""]).lower()
        if any(word in text for word in _LIGHT_WORDS) or "lightweight" in text:
            return "light"
        if any(word in text for word in _HEAVY_WORDS) or "powerful" in text:
            return "heavy"
        return "standard" if info is not None else None

    def display(self, model: Optional[str]) -> str:
        info = self._info(model)
        return (info.display if info else model) or "your current model"

    def available(self, model: Optional[str]) -> Tuple[bool, str]:
        if not model:
            return False, "no model"
        lower = model.lower()
        if lower in self.denied or (self.allowed and lower not in self.allowed):
            return False, "restricted"
        if self._info(model) is None and lower not in {m.lower() for m in self.tiers.values()}:
            return False, "not in {}'s model list".format(self.client)
        return True, ""

    def resolve_explicit(self, name: str) -> Optional[str]:
        lower = name.lower().strip()
        for model in self.models:
            if model.id.lower() == lower or (model.name and model.name.lower() == lower):
                return model.id
        family = [m for m in self.models if lower in m.id.lower() or lower in (m.name or "").lower()]
        return family[0].id if len(family) == 1 else None
