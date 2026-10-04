"""ModelMatch for Codex CLI and GitHub Copilot CLI (the "agents proxy").

Kept import-light on purpose: the hooks (standard library only) import
proxy.agents.codex_config, so nothing here may pull in FastAPI or httpx.
"""
