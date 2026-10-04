"""Proxy settings, read from environment variables and the project's .env file.

Real environment variables always win over .env values. Secrets (API keys)
are read here but never logged anywhere.
"""

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Optional
from urllib.parse import urlparse

PROJECT_ROOT = Path(__file__).resolve().parent.parent

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8787
DEFAULT_UPSTREAM = "https://api.anthropic.com"


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
    if api_style not in ("route", "analyze"):
        # The existing analyzer website exposes /api/analyze (free-text
        # answer); anything else is assumed to follow the /api/route contract.
        api_style = "analyze" if urlparse(api_url).path.rstrip("/").endswith("/analyze") else "route"

    return Settings(
        host=get("MODELMATCH_HOST", DEFAULT_HOST),
        port=_int(get("MODELMATCH_PORT"), DEFAULT_PORT),
        api_url=api_url,
        api_style=api_style,
        api_key=get("MODELMATCH_API_KEY"),
        api_timeout=_float(get("MODELMATCH_API_TIMEOUT"), 25.0),
        max_prompt_chars=_int(get("MODELMATCH_MAX_PROMPT_CHARS"), 8000),
        upstream_url=get("MODELMATCH_UPSTREAM_URL", DEFAULT_UPSTREAM).rstrip("/"),
        state_file=Path(get("MODELMATCH_STATE_FILE", str(root / "state" / "sessions.json"))),
        log_dir=Path(get("MODELMATCH_LOG_DIR", str(root / "logs"))),
        log_prompts=_flag(get("MODELMATCH_LOG_PROMPTS", "0")),
    )
