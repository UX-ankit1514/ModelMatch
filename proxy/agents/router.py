"""The ModelMatch router, for Codex and Copilot.

Same routers as the Claude Code proxy (proxy/router_client.py). The only difference:
the planned /api/route contract says which tool is asking, so the cloud router can
answer with a model that tool runs:

    POST /api/route  {"prompt": "...", "client": "codex" | "copilot"}
"""

from typing import Optional

import httpx

from ..config import Settings
from ..router_client import CloudAnalyzeClient, CloudRouteClient, MockRouter


class ClientRouteClient(CloudRouteClient):
    def __init__(self, settings: Settings, client: str, transport: Optional[httpx.AsyncBaseTransport] = None):
        super().__init__(settings, transport)
        self.client = client

    def _payload(self, prompt: str) -> dict:
        return {"prompt": prompt, "client": self.client}


def build_agent_router(settings: Settings, client: str, transport: Optional[httpx.AsyncBaseTransport] = None):
    if not settings.api_url:
        return MockRouter()
    if settings.api_style == "analyze":
        return CloudAnalyzeClient(settings, transport)
    return ClientRouteClient(settings, client, transport)
