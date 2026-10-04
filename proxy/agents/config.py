"""Settings for the agents proxy (Codex CLI + GitHub Copilot CLI).

It reads the same .env as the Claude Code proxy and shares its keys (router URL and
key, timeouts, log folder, prompt logging). Only what has to differ has its own key:

    MODELMATCH_AGENTS_PORT         default 8788 (the Claude Code proxy keeps 8787)
    MODELMATCH_AGENTS_STATE_FILE   default state/agents-sessions.json
    MODELMATCH_CODEX_UPSTREAM_URL  where Codex's model requests go (empty = automatic)
    MODELMATCH_CODEX_HOME          Codex's settings folder (default $CODEX_HOME or ~/.codex)
    MODELMATCH_<TOOL>_<SIZE>_MODEL pin the model used for a size, e.g.
                                         MODELMATCH_CODEX_LIGHT_MODEL=gpt-5.6-luna
"""

import os
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Dict, Optional

from ..config import PROJECT_ROOT, Settings, _int, get_settings, read_dotenv

DEFAULT_AGENTS_PORT = 8788
CLIENTS = ("codex", "copilot")
TIERS = ("light", "standard", "heavy")


@dataclass(frozen=True)
class AgentSettings:
    base: Settings  # shared settings, with the agents proxy's own port and state file
    codex_upstream_url: str
    codex_home: Path
    tier_overrides: Dict[str, Dict[str, str]]  # client -> size -> model id

    @property
    def proxy_url(self) -> str:
        return self.base.proxy_url


def codex_home_from(get) -> Path:
    return Path(get("MODELMATCH_CODEX_HOME", get("CODEX_HOME", str(Path.home() / ".codex")))).expanduser()


def get_agent_settings(env: Optional[Dict[str, str]] = None, root: Path = PROJECT_ROOT) -> AgentSettings:
    if env is None:
        merged = read_dotenv(root / ".env")
        merged.update(os.environ)
    else:
        merged = dict(env)

    def get(name: str, default: str = "") -> str:
        value = merged.get(name)
        return default if value is None or str(value).strip() == "" else str(value).strip()

    base = replace(get_settings(env=merged, root=root),
                   port=_int(get("MODELMATCH_AGENTS_PORT"), DEFAULT_AGENTS_PORT),
                   state_file=Path(get("MODELMATCH_AGENTS_STATE_FILE",
                                       str(root / "state" / "agents-sessions.json"))))
    overrides = {}
    for client in CLIENTS:
        picks = {tier: get("MODELMATCH_{}_{}_MODEL".format(client.upper(), tier.upper())) for tier in TIERS}
        overrides[client] = {tier: model for tier, model in picks.items() if model}
    return AgentSettings(base=base, codex_upstream_url=get("MODELMATCH_CODEX_UPSTREAM_URL").rstrip("/"),
                         codex_home=codex_home_from(get), tier_overrides=overrides)
