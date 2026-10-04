"""Which model to use, among the models a tool can really run.

ModelMatch may name any model. Codex and Copilot can only run the models in
their own list, so a recommendation is matched against that list, in this order:

1. the same model. Names are compared by their words and version, so
   "Claude Haiku 4.5" == "claude-haiku-4.5" and "GPT-5.6-Luna" == "gpt-5.6-luna";
2. the built-in recommender thinks in three sizes (light / standard / heavy); each
   size is one model of the tool, read from the tool's own descriptions or pinned in .env;
3. the newest model of the same family ("Claude Sonnet 5.5" -> "claude-sonnet-4.6"),
   and the note says which model was named.

Anything else is shown as advice and never applied.
"""

import re
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Tuple

from ..provider import strip_tier
from .codex_config import load_model_entries


@dataclass(frozen=True)
class ModelInfo:
    id: str
    name: str = ""
    description: str = ""
    category: str = ""  # Copilot's model-picker category (e.g. lightweight / versatile / powerful)
    reasoning_levels: Optional[Tuple[str, ...]] = None  # None = unknown
    supports_verbosity: Optional[bool] = None
    service_tiers: Optional[Tuple[str, ...]] = None  # None = unknown

    @property
    def display(self) -> str:
        return self.name or self.id


@dataclass(frozen=True)
class Resolution:
    model_id: Optional[str]
    display: str
    routable: bool
    note: str = ""
    substituted_from: Optional[str] = None
    model: Optional[ModelInfo] = None


# What the built-in recommender (proxy/router_client.MockRouter) answers, by size.
MOCK_TIERS = {"claude haiku 4.5": "light", "claude sonnet 5.5": "standard", "claude opus 5.5": "heavy"}

_STOP_WORDS = {"claude", "anthropic", "openai", "google", "model", "latest", "preview"}
_TOKEN_RE = re.compile(r"[a-z]+|\d+(?:\.\d+)*")

# Size hints. Codex describes its models in words; Copilot has a picker category.
_HINTS = {
    "light": {"categories": ("lightweight", "fast", "efficient"),
              "words": {"mini", "nano", "haiku", "flash", "lite"},
              "phrases": ("fast", "affordable", "efficient", "easier", "lightweight", "small", "cheap")},
    "heavy": {"categories": ("powerful", "reasoning"),
              "words": {"opus"},
              "phrases": ("frontier", "most demanding", "most capable", "complex", "hardest", "powerful")},
    "standard": {"categories": ("versatile", "balanced"),
                 "words": {"sonnet"},
                 "phrases": ("workhorse", "everyday", "balanced", "general", "versatile")},
}


def mock_tier(name: str) -> Optional[str]:
    return MOCK_TIERS.get(" ".join(str(name).lower().split()))


def model_key(name: str) -> Tuple[frozenset, Tuple[int, ...]]:
    """(family words, version): the parts that identify a model, whatever the spelling."""
    text = strip_tier(str(name)).lower()
    text = re.sub(r"\[.*?\]", " ", text)  # "[1m]" context suffix
    text = re.sub(r"(?<!\d)\d{8}(?!\d)", " ", text)  # date snapshots like 20251001
    words, numbers = set(), []
    for token in _TOKEN_RE.findall(text):
        if token[0].isdigit():
            numbers.extend(int(part) for part in token.split(".") if part)
        elif token not in _STOP_WORDS:
            words.add(token)
    while len(numbers) > 1 and numbers[-1] == 0:
        numbers.pop()
    return frozenset(words), tuple(numbers)


def _keys(model: ModelInfo):
    keys = [model_key(model.id)]
    if model.name:
        keys.append(model_key(model.name))
    return keys


def find_model(models: Iterable[ModelInfo], model_id: Optional[str]) -> Optional[ModelInfo]:
    if not model_id:
        return None
    wanted = model_id.strip().lower()
    for model in models:
        if model.id.lower() == wanted:
            return model
    return None


def resolve(raw: str, models: List[ModelInfo], tiers: Optional[Dict[str, str]] = None,
            size: Optional[str] = None, where: str = "this tool's model list") -> Resolution:
    """Match a recommended model name to one of `models` (see the module docstring)."""
    name = " ".join(strip_tier(str(raw)).split())
    if not models:
        return Resolution(None, name, False, note="Couldn't read {}, so nothing was switched.".format(where))

    key = model_key(name)
    for model in models:
        if key in _keys(model):
            return Resolution(model.id, model.display, True, model=model)

    if size and tiers:
        model = find_model(models, tiers.get(size))
        if model:
            return Resolution(model.id, model.display, True, model=model)

    words = key[0]
    if words:
        family = [m for m in models if any(k[0] == words for k in _keys(m))]
        if family:
            best = max(family, key=lambda m: max(k[1] for k in _keys(m) if k[0] == words))
            return Resolution(best.id, best.display, True, substituted_from=name, model=best)
    return Resolution(None, name, False, note="{} isn't in {}.".format(name, where))


def detect_tiers(models: List[ModelInfo], current: Optional[str] = None,
                 overrides: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    """Pick a light, standard and heavy model from `models` (best first), honouring .env pins."""

    def matches(model: ModelInfo, tier: str) -> bool:
        hints = _HINTS[tier]
        description = model.description.lower()
        words = set().union(*(k[0] for k in _keys(model)))
        return (model.category.lower() in hints["categories"] or bool(words & hints["words"])
                or any(phrase in description for phrase in hints["phrases"]))

    def pick(tier: str, taken) -> Optional[str]:
        candidates = [m for m in models if m.id not in taken and matches(m, tier)]
        if not candidates:
            return None
        newest = max(model_key(m.id)[1] for m in candidates)
        return next(m.id for m in candidates if model_key(m.id)[1] == newest)

    tiers: Dict[str, str] = {}
    light = pick("light", set())
    heavy = pick("heavy", {light})
    standard = pick("standard", {light, heavy})
    current_model = find_model(models, current)
    if not standard and current_model and current_model.id != light:
        standard = current_model.id
    heavy = heavy or standard
    for tier, model in (("light", light), ("standard", standard), ("heavy", heavy)):
        if model:
            tiers[tier] = model
    tiers.update({tier: model for tier, model in (overrides or {}).items() if model})
    return tiers


def with_pinned(models: List[ModelInfo], tiers: Dict[str, str]) -> List[ModelInfo]:
    """Models pinned in .env count as available even if the tool's list doesn't show them."""
    extra = [ModelInfo(id=model) for model in tiers.values() if not find_model(models, model)]
    return list(models) + extra


def codex_models(home) -> Tuple[List[ModelInfo], object]:
    entries, path = load_model_entries(home)
    return [ModelInfo(id=e["id"], name=e["name"], description=e["description"],
                      reasoning_levels=e["reasoning_levels"], supports_verbosity=e["supports_verbosity"],
                      service_tiers=e["service_tiers"]) for e in entries], path
