# ModelMatch for Codex CLI and GitHub Copilot CLI: research and plan

This is the structure plan behind the Codex and Copilot versions of ModelMatch. For
installing and everyday use, read [CODEX-COPILOT-GUIDE.md](CODEX-COPILOT-GUIDE.md).

Nothing in the Claude Code version was changed. The new parts live in new files next to it and
reuse its code (router, session store, popup) by importing it.

---

## 1. What the Claude Code version does (the target behaviour)

| Step | Claude Code version |
| --- | --- |
| Session starts | `SessionStart` hook starts the local proxy if needed and registers the session |
| You type a prompt | `UserPromptSubmit` hook asks the proxy for **one** recommended model |
| You decide | macOS popup: **Use** / **Keep current** (30 s timeout keeps your model) |
| Switch | Claude Code sends its API calls through the proxy (`ANTHROPIC_BASE_URL`); for the accepted prompt only, the proxy swaps the model and adapts the request |
| Feedback | One line under the prompt (`systemMessage`): "ModelMatch: using Claude Haiku 4.5 for this prompt." |
| Safety | Every failure keeps your model; the hook never blocks a prompt; a rejected switch is retried on your model |

The goal: the same experience in Codex CLI and GitHub Copilot CLI.

---

## 2. What was researched, and how

Versions tested on 2026-10-03: **Codex CLI 0.159.2** (installed here) and **GitHub Copilot CLI
1.0.91** (downloaded into a scratch folder, run offline against a fake local model server, so no
account was needed).

| Source | Used for |
| --- | --- |
| [Codex hooks docs](https://developers.openai.com/codex/hooks) | Codex events, input fields, output fields, trust, exit codes, timeouts |
| [Copilot hooks reference](https://docs.github.com/en/copilot/reference/hooks-reference), [Using hooks with Copilot CLI](https://docs.github.com/en/copilot/how-tos/copilot-cli/customize-copilot/use-hooks) | Copilot events, file locations, payloads |
| [Copilot BYOK docs](https://docs.github.com/en/copilot/how-tos/copilot-cli/customize-copilot/use-byok-models) | Custom model providers (base URL) |
| Copilot CLI's bundled SDK types, `copilot help environment`, changelog | Extensions, `session.rpc.model.switchTo`, "handled" prompts (1.0.44+), what extensions can see |
| Live experiments (fake model servers) | What each tool really sends and does, listed below |

### Experiments and what they proved

| Experiment | Result |
| --- | --- |
| Copilot command hooks dump their input | `userPromptSubmitted` gets `sessionId`, `timestamp`, `cwd`, `prompt`. **No model, no terminal.** Copilot sets `COPILOT_CLI=1`. In `-p` mode the prompt hook runs *before* `sessionStart`. |
| Copilot extension calls `rpc.model.switchTo` inside the prompt hook | Returns `deferred: true`: **the current prompt still runs on the old model**; the switch lands after the turn. |
| Extension: answer the prompt as "handled", queue the switch, re-send the same prompt | Works: no model call for the first copy, the re-sent prompt runs on the new model, then the old model is restored immediately (no turn active). |
| Extension calls `session.log("...")` | Shows a line in Copilot's timeline: the Copilot equivalent of `systemMessage`. |
| Copilot `--experimental` | Saved as `"experimental": true` in `~/.copilot/settings.json`; with it, user-level extensions load (also in `-p`). |
| Codex pointed at a fake server (`openai_base_url`) | Codex tries a **WebSocket** first. Refusing with **HTTP 426** makes it fall back to HTTPS in 0.4 s; 404/501 cost about 7 s of retries. |
| Codex request contents | `POST /v1/responses` with `model`, `reasoning.effort`, `text.verbosity`, `service_tier`; headers `session-id` and `x-codex-turn-metadata` (JSON with `session_id`, `turn_id`, `request_kind`: `prewarm` / `turn` / compaction / memory). |

---

## 3. Capabilities and constraints

| | Claude Code (existing) | Codex CLI | GitHub Copilot CLI |
| --- | --- | --- | --- |
| Session-start hook | `SessionStart` | `SessionStart` (same idea; matcher on `startup`/`resume`/`clear`/`compact`) | `sessionStart` (command hook) or `onSessionStart` (extension) |
| Prompt hook | `UserPromptSubmit` | `UserPromptSubmit` (gets `session_id`, `turn_id`, `model`, `prompt`) | `userPromptSubmitted` (command) or `onUserPromptSubmitted` (extension) |
| Where hooks are configured | `.claude/settings.json` | `~/.codex/hooks.json` or `<repo>/.codex/hooks.json` | `~/.copilot/hooks/*.json`, `.github/hooks/*.json`, extensions in `~/.copilot/extensions/` |
| Hooks must be approved | No | **Yes**: once, with `/hooks` (untrusted hooks are skipped silently) | Extensions need experimental mode (`/experimental on`) |
| Show a line to the user | `systemMessage` | `systemMessage` (shown as a warning-style line) | Command hooks: no. Extension: `session.log()` |
| Terminal `[Y/n]` in the hook | No | No | No (verified) |
| Can a hook change the model? | No | No | No, but an **extension** can switch Copilot's model (applies between turns) |
| How the switch is done | Local proxy (`ANTHROPIC_BASE_URL`) | **Local proxy** (`openai_base_url` in `~/.codex/config.toml`, user level only) | **Copilot's own model switch**, from the extension; no proxy in the path |
| Which models can be used | Claude models | The models in Codex's own list | The models in your Copilot plan (Claude, GPT, Gemini, ...) |
| Block / break risk | Exit 2 blocks | Exit 2 blocks the prompt | Command hooks fail open |
| Scripted runs | `claude -p` (skipped) | `codex exec` (skipped) | `copilot -p` (skipped) |

---

## 4. Design

### 4.1 Shared pieces

```
                         ┌─────────────────────────────┐
 Claude Code ──hooks──►  │ Claude proxy  :8787 (as is) │ ──► Anthropic API
                         └─────────────────────────────┘
 Codex CLI   ──hooks──►  ┌─────────────────────────────┐
 Codex CLI   ──model──►  │ Agents proxy  :8788 (new)   │ ──► OpenAI / ChatGPT backend / your gateway
 Copilot ext ──asks──►   │ /recommend /selection /v1/* │
                         └─────────────────────────────┘
```

* **Agents proxy** (`proxy/agents/`): a sibling of the Claude proxy on port 8788. It reuses the
  Claude proxy's router (built-in recommender or the cloud router (the analyzer website)), session store
  and settings, and adds what Codex and Copilot need: their model lists, and Codex's API passthrough.
* **Recommendation, then matching.** The router names any model. The agents proxy matches it to a
  model the tool can actually run: same model; else the newest model of the same family (and the
  note says so); else, for the built-in recommender's three sizes, the tool's light / standard /
  heavy model. Anything else is shown as advice and never applied.
* **Size picks (light / standard / heavy).** Codex: read from Codex's own model list and its
  descriptions ("Fast and affordable model…", "Frontier intelligence…"). Copilot: from Copilot's model
  list (its picker category, else the model family). Each can be pinned in `.env`.
* **Popup.** The exact same macOS popup as the Claude version (imported, not copied).
* **Pause / resume.** The existing `pause.sh` / `resume.sh` pause all three tools.

### 4.2 Codex CLI

```
You type ─► UserPromptSubmit hook ─► agents proxy /recommend ─► popup ─► /selection (session + turn id)
Codex ─► POST /v1/responses (session-id, turn_id) ─► agents proxy ─► swaps model for THAT turn only ─► upstream
```

* Hooks: `~/.codex/hooks.json` (or a project's `.codex/hooks.json`). You trust them once with `/hooks`.
* Routing: `openai_base_url = "http://127.0.0.1:8788/v1"` in `~/.codex/config.toml`. A previous
  `openai_base_url` (e.g. a gateway such as opencodex) is kept as the proxy's upstream and restored
  on uninstall. Without one, the proxy sends ChatGPT sign-ins to `https://chatgpt.com/backend-api/codex`
  and API keys to `https://api.openai.com/v1`.
* Precise switching: a route applies only to requests whose `turn_id` matches the accepted prompt,
  and only to `request_kind: turn` (never prewarm, compaction or memory requests, never subagents).
* Adapting the request: the reasoning effort is lowered to what the target supports, `text.verbosity`
  and fast mode (`service_tier`) are dropped when the target doesn't support them (from Codex's model list).
* Transport: WebSocket upgrades get HTTP 426, so Codex uses HTTPS straight away.
* Compressed requests (ChatGPT sign-in sends zstd): decompressed with the `zstandard` package; if that
  fails the request passes through untouched (your model is kept).
* Always on: once routing is on, *every* Codex surface (CLI, IDE, app) talks to the proxy, so a macOS
  LaunchAgent keeps it running from login. The hook still restarts it if needed.
* The LaunchAgent runs from its own copy (`~/Library/Application Support/ModelMatch/agents`: a venv,
  the proxy code and a copy of `.env`), because macOS privacy protection ("Operation not permitted")
  stops background services from reading `~/Desktop`, `~/Documents` and `~/Downloads`, where this folder
  often lives. Found when the first real install on Desktop crash-looped; `restart` / `sync` refresh the copy.
* Fail-open: a rejected routed request (400/404/422) is retried once on your model.

### 4.3 GitHub Copilot CLI

```
You type ─► extension onUserPromptSubmitted ─► copilot hook script (recommend + popup + selection)
   Use  ─► queue model switch ─► answer this copy locally ("re-running on X") ─► re-send the prompt
        ─► it runs on X ─► session idle ─► switch back to your model
   Keep ─► nothing changes; a line in the timeline says why
```

* Extension: `~/.copilot/extensions/modelmatch/extension.mjs` (needs experimental mode, which the
  installer turns on in `~/.copilot/settings.json`; uninstall turns it off only if it turned it on).
* Copilot 1.0.44 or newer is required for switching (first version where a prompt hook can answer
  without a model call). Older versions get recommendations only.
* Optional `--hooks-only` install: plain command hooks, no experimental mode; recommendations appear as
  macOS notifications; nothing is switched.

---

## 5. File structure (all new)

```
proxy/agents/                     agents proxy (Codex + Copilot)
  config.py                       settings (port 8788, upstream, size picks)
  codex_config.py                 reads ~/.codex/config.toml and Codex's model list (standard library)
  catalog.py                      matching a recommendation to the tool's models; size picks
  router.py                       the router, telling the cloud router which tool is asking
  responses.py                    Codex requests: session/turn ids, decompression, adapting to the model
  app.py                          the FastAPI app
hooks/
  agents_common.py                shared hook code (config, proxy start/stop, recommend, popup)
  modelmatch_codex_hook.py  Codex hook (SessionStart + UserPromptSubmit)
  modelmatch_copilot_hook.py Copilot decision script (extension) + command-hook fallback
  copilot-extension/extension.mjs Copilot extension (hooks + model switch)
scripts/
  install_codex.sh  uninstall_codex.sh  doctor_codex.sh
  install_copilot.sh uninstall_copilot.sh doctor_copilot.sh
  codex_settings_tool.py          safe edits of ~/.codex/hooks.json and config.toml
  copilot_settings_tool.py        installs the extension / hooks file, experimental flag
  agents_service.py               the macOS LaunchAgent that keeps the agents proxy running
tests/
  test_agents_catalog.py test_agents_app.py test_codex_hook.py test_copilot_hook.py
  test_codex_settings_tool.py test_copilot_settings_tool.py test_agents_service.py
docs/
  CODEX-COPILOT-PLAN.md (this file)  CODEX-COPILOT-GUIDE.md
```

All of these sit inside folders that `scripts/package.sh` already ships, so the shareable zip
includes them without changing the packager.

---

## 6. Risks and limits

| Risk | Handling |
| --- | --- |
| Codex hooks not yet trusted | Codex skips them (no recommendations) but keeps working; the LaunchAgent keeps the proxy up, so routing never breaks Codex. The guide's step "trust with /hooks" is required. |
| Codex app / IDE use the same config | They pass through the proxy untouched (no hook = no route). |
| Proxy down while routing is on | LaunchAgent restarts it; `doctor_codex.sh` spots it; `install_codex.sh --no-routing` turns routing off instantly. |
| A model rejects a converted request | Retried once on your model, and logged. |
| Copilot extension API is "experimental" | Version-checked; any error keeps your model. `--hooks-only` is the fallback. |
| Copilot: the prompt appears twice in the timeline when switching | Inherent to switching mid-prompt in Copilot; the first copy is answered with one explanatory line and no model call. |
| Copilot: files attached to the prompt | Re-sent with the prompt (taken from Copilot's own message event). |
| Copilot reads `.claude/settings.json` hooks in a repo | In this repo folder Copilot also runs the Claude hook; it only prints a message Copilot ignores. Harmless. |
| Popup | macOS only, like the Claude version. Elsewhere your model is kept. |

---

## 7. Test plan

* Unit: model matching and size picks, Codex config parsing and editing, request adaptation.
* Proxy: endpoints, per-turn Codex routing, untouched passthrough, retry on rejection, zstd/gzip
  bodies, WebSocket 426, upstream choice, no prompts or keys in logs.
* Hooks end to end against a real proxy process: accept / keep / timeout / fail-open, duplicates,
  scripted runs, pause.
* Installers against fake home folders (never the real `~/.codex` or `~/.copilot`).
* Copilot extension: syntax check, plus a manual end-to-end run with the real Copilot binary offline.
