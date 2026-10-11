"""Proxy settings, read from environment variables and the project's .env file.

Real environment variables always win over .env values. Secrets (API keys)
are read here but never logged anywhere.
"""

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Optional, Tuple
from urllib.parse import urlparse

PROJECT_ROOT = Path(__file__).resolve().parent.parent

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8787
DEFAULT_UPSTREAM = "https://api.anthropic.com"
# The Curvet AI gateway runs the V2 task analyst (proxy/v2/analyzer.py).
# Defaults picked by benchmark on 2026-10-08 (9 example prompts, all tiers right, ~2.2 s median).
CURVET_DEFAULT_MODEL = "gpt-5.4-mini"
CURVET_DEFAULT_FALLBACK = "claude-haiku-4-5-20251001"
CURVET_DEFAULT_TIMEOUT = 6.0


def read_dotenv(path: Path) -> Dict[str, str]:
    """Parse simple KEY=VALUE lines. Comments and blank lines are ignored."""
    values: Dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return values
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key.startswith("export "):
            key = key[len("export "):].strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        values[key] = value
    return values


def _float(value: str, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _int(value: str, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _flag(value: str) -> bool:
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def _names(value: str) -> Tuple[str, ...]:
    return tuple(part.strip() for part in str(value or "").split(",") if part.strip())


@dataclass(frozen=True)
class Settings:
    host: str
    port: int
    api_url: str
    api_style: str
    api_key: str
    api_timeout: float
    max_prompt_chars: int
    upstream_url: str
    state_file: Path
    log_dir: Path
    log_prompts: bool
    # Added for V2. All have defaults so older callers keep working.
    api_secret: str = ""  # Curvet App secret (never logged)
    router_model: str = ""  # task analyst model on Curvet
    router_fallback_model: str = ""
    v2: bool = False  # MODELMATCH_ROUTER_V2: decision engine, learning, economics
    personalization: bool = True
    cache_aware: bool = True
    auto_route: bool = False
    db_file: Optional[Path] = None
    denied_models: Tuple[str, ...] = ()
    allowed_models: Tuple[str, ...] = ()
    claude_light_model: str = "claude-haiku-4-5"
    claude_standard_model: str = "claude-sonnet-5-5"
    claude_heavy_model: str = "claude-opus-5-5"

    @property
    def router_mode(self) -> str:
        if not self.api_url:
            return "mock"
        return "cloud-" + self.api_style

    @property
    def proxy_url(self) -> str:
        return "http://{}:{}".format(self.host, self.port)


def get_settings(env: Optional[Dict[str, str]] = None, root: Path = PROJECT_ROOT) -> Settings:
    if env is None:
        merged = read_dotenv(root / ".env")
        merged.update(os.environ)
    else:
        merged = dict(env)

    def get(name: str, default: str = "") -> str:
        value = merged.get(name)
        return default if value is None or value == "" else value.strip()

    api_url = get("MODELMATCH_API_URL")
    api_style = get("MODELMATCH_API_STYLE").lower()
    if api_style not in ("route", "analyze", "curvet"):
        # The Curvet gateway runs the V2 task analyst; the existing analyzer website
        # exposes /api/analyze (free-text answer); anything else is assumed to follow
        # the /api/route contract.
        parsed = urlparse(api_url)
        if (parsed.hostname or "").endswith("curvet.in"):
            api_style = "curvet"
        else:
            api_style = "analyze" if parsed.path.rstrip("/").endswith("/analyze") else "route"
    curvet = api_style == "curvet"

    return Settings(
        host=get("MODELMATCH_HOST", DEFAULT_HOST),
        port=_int(get("MODELMATCH_PORT"), DEFAULT_PORT),
        api_url=api_url,
        api_style=api_style,
        api_key=get("MODELMATCH_API_KEY"),
        api_timeout=_float(get("MODELMATCH_API_TIMEOUT"), CURVET_DEFAULT_TIMEOUT if curvet else 25.0),
        max_prompt_chars=_int(get("MODELMATCH_MAX_PROMPT_CHARS"), 8000),
        upstream_url=get("MODELMATCH_UPSTREAM_URL", DEFAULT_UPSTREAM).rstrip("/"),
        state_file=Path(get("MODELMATCH_STATE_FILE", str(root / "state" / "sessions.json"))),
        log_dir=Path(get("MODELMATCH_LOG_DIR", str(root / "logs"))),
        log_prompts=_flag(get("MODELMATCH_LOG_PROMPTS", "0")),
        api_secret=get("MODELMATCH_API_SECRET"),
        router_model=get("MODELMATCH_ROUTER_MODEL", CURVET_DEFAULT_MODEL if curvet else ""),
        router_fallback_model=get("MODELMATCH_ROUTER_FALLBACK_MODEL", CURVET_DEFAULT_FALLBACK if curvet else ""),
        v2=_flag(get("MODELMATCH_ROUTER_V2", "0")),
        personalization=_flag(get("MODELMATCH_PERSONALIZATION", "1")),
        cache_aware=_flag(get("MODELMATCH_CACHE_AWARE", "1")),
        auto_route=_flag(get("MODELMATCH_AUTO_ROUTE", "0")),
        db_file=Path(get("MODELMATCH_DB_FILE", str(root / "state" / "modelmatch.db"))),
        denied_models=_names(get("MODELMATCH_DENIED_MODELS")),
        allowed_models=_names(get("MODELMATCH_ALLOWED_MODELS")),
        claude_light_model=get("MODELMATCH_CLAUDE_LIGHT_MODEL", "claude-haiku-4-5"),
        claude_standard_model=get("MODELMATCH_CLAUDE_STANDARD_MODEL", "claude-sonnet-5-5"),
        claude_heavy_model=get("MODELMATCH_CLAUDE_HEAVY_MODEL", "claude-opus-5-5"),
    )
