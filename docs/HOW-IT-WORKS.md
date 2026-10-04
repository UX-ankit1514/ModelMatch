# How ModelMatch for Claude Code works

The technical deep dive. For installing, everyday use, settings and commands, see the [README](../README.md).

## Architecture

```
You type a prompt in Claude Code
        │
        ▼
UserPromptSubmit hook ──► local proxy (127.0.0.1:8787) ──► ModelMatch router (mock or cloud)
        │                        ◄── ONE model + short reason
        ▼
macOS popup:  "Recommended model: Claude Haiku 4.5 … Use this model for this prompt?"
        │  Use                                   │ Keep current / no answer / any error
        ▼                                        ▼
proxy swaps the model for this turn        nothing changes
        │                                        │
        └──────────────► Claude answers your prompt ◄─┘
```

- **SessionStart hook** makes sure the proxy is running (starts it if needed) and registers the
  session. It never routes.
- **UserPromptSubmit hook** sends the prompt to the proxy's `/recommend`, asks Use / Keep, and
  saves the answer via `/selection`. It always exits 0 and prints at most one
  `{"systemMessage": …}`, so Claude always continues.
- **Routing**: the installer sets `ANTHROPIC_BASE_URL=http://127.0.0.1:8787`. Claude Code's API
  requests pass through the proxy, which forwards them to Anthropic untouched, except during a
  turn where you accepted a recommendation. Then `POST /v1/messages` requests for that session
  use the recommended model, adjusted for what that model supports (e.g. Haiku 4.5 has no
  adaptive thinking, per-turn effort or mid-conversation system messages). Credentials are passed
  through as-is and never stored or logged.
- **Session matching**: requests are matched to sessions via the `x-claude-code-session-id`
  header (fallback: `metadata.user_id`). Claude Code's small background helpers (titles,
  summaries; they run on Haiku) are never rerouted.
- **Fail-open**: if the router, internet, popup or anything else fails, the current model is kept
  and the reason is logged. If Anthropic rejects a routed request (400/404/422), the proxy retries
  it once with the original model.
- **Providers**: recommendation and execution are separate (`proxy/provider.py`). Only the
  Anthropic adapter executes today. OpenAI/Codex, Gemini, OpenRouter… are registered as
  "not configured" placeholders, so they are shown but never faked. Retired Claude names
  (e.g. "Claude 3 Opus") map to the current model of the same family, and the note says so.

## Proxy endpoints

| Endpoint | Purpose |
|---|---|
| `GET /health` | liveness + configuration summary |
| `POST /session/start` | register a session (SessionStart) |
| `GET /session/{id}` | inspect a session's state (debugging) |
| `POST /recommend` | one recommendation for a prompt (UserPromptSubmit) |
| `POST /selection` | record accept / reject |
| `POST /mock/api/route` | local mock of the planned cloud contract |
| everything else | transparent passthrough to `MODELMATCH_UPSTREAM_URL` |

## Install scopes

| Command | Hooks go to | Routing (`ANTHROPIC_BASE_URL`) goes to |
|---|---|---|
| `install.sh --global` | `~/.claude/settings.json` (or `$CLAUDE_CONFIG_DIR`) | same file |
| `install.sh` | `<this folder>/.claude/settings.json` | `<this folder>/.claude/settings.local.json` |
| `install.sh --project DIR` | `DIR/.claude/settings.json` | `DIR/.claude/settings.local.json` |

`scripts/settings_tool.py` does the merging:
- only entries whose command contains `modelmatch_hook.py` are added/removed;
- every changed file is backed up first (`*.bak-<timestamp>`); invalid JSON is never overwritten;
- the hook command ends in `|| true` (a missing script makes Python exit 2, which would *block*
  prompts);
- if `ANTHROPIC_BASE_URL` is already set to something else (settings, or `~/.zshrc` and friends
  for `--global`), routing is not enabled. Chain instead with `MODELMATCH_UPSTREAM_URL`;
- uninstall removes `ANTHROPIC_BASE_URL` only if it still points at this proxy.

Other safeguards in the hook: a run-once guard (global + project installs still ask once per
prompt), scripted runs (`CLAUDE_CODE_ENTRYPOINT=sdk-*`, e.g. `claude -p`) are skipped unless
`MODELMATCH_CONFIRM_UI=auto-accept`, and while paused it still keeps the proxy alive when
routing is on.

## Connecting the real cloud router

Status of the live site (checked 2026-10-02):

| Endpoint | Status |
|---|---|
| `POST /api/route` | does not exist yet (404); needs to be added to the website |
| `POST /api/analyze` | exists; returns a long free-text analysis from an LLM |

**Stopgap:** `MODELMATCH_API_URL=https://workflow-copilot-ten.vercel.app/api/analyze`. The
proxy reads the "SECTION 1: RECOMMENDED MODEL" line and drops the tier (low/max…). It works
(≈4 s per prompt) but knows an old model list (most picks are Gemini/GPT, which are shown, never
applied), costs one LLM call per prompt, and the endpoint has no auth.

**Planned contract:**

```http
POST /api/route
Content-Type: application/json
Authorization: Bearer <MODELMATCH_API_KEY>

{ "prompt": "…", "client": "claude-code" }
```
```json
{ "recommended_model": "claude-sonnet-5-5", "reason": "Coding task; strong and fast.", "confidence": 0.82 }
```

- For `client: "claude-code"`, choose among models Claude Code can run and return API ids
  (`claude-opus-5-5`, `claude-sonnet-5-5`, `claude-haiku-4-5`, `claude-fable-5-1`); display
  names like "Claude Sonnet 5.5" also work.
- One-sentence `reason`; no tiers or modes. Respond in under ~3 s; require an API key.

Rehearse locally with `MODELMATCH_API_URL=http://127.0.0.1:8787/mock/api/route`.

## Known limits

- **No terminal `[Y/n]` inside Claude Code**: hooks run without a controlling terminal, so the
  macOS popup is used. The terminal prompt works where a terminal exists, and is tried first.
- Claude Code's status bar keeps showing the configured model; the note under the prompt and
  `logs/proxy.log` show what answered.
- If your own main model is Haiku, recommendations can't be applied (Haiku requests are treated
  as background helpers).
- Routing needs Claude Code talking to Anthropic directly (subscription or API key), not
  Bedrock / Vertex / Foundry.
- The popup needs macOS; elsewhere the current model is kept.

## Logs

`logs/hook.log` and `logs/proxy.log` record sessions, recommendations, accept/reject, routing and
fallbacks. Prompts appear only as length + fingerprint; keys and auth headers are never logged.

## See also

- [Configuration and every `.env` setting](../README.md#configuration)
- [Commands](../README.md#commands) and [Repository layout](../README.md#repository-layout)
- [Testing](../README.md#testing). Mock router trick: a prompt containing `wc-test: GPT-4o`
  (or any model name) makes the built-in recommender suggest that model.
