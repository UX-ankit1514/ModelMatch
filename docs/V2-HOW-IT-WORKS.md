# ModelMatch V2: how the routing brain works

V1 proved the experience: a hook catches your prompt, the local proxy recommends one model, a
popup asks *Use / Keep*, and the proxy switches the model. Its recommender was a list of keywords.

V2 replaces that brain with three systems that feed one decision engine:

| System | Question it answers | Where |
|---|---|---|
| **1. Semantic task analyst** | What does this task really need? | [`proxy/v2/analyzer.py`](../proxy/v2/analyzer.py) |
| **2. Outcome-based learning** | What has actually worked for *you* on this kind of task? | [`profiles.py`](../proxy/v2/profiles.py), [`outcomes.py`](../proxy/v2/outcomes.py) |
| **3. Switch economics** | Is switching worth it *in this session*, given what's already cached? | [`cache.py`](../proxy/v2/cache.py), [`economics.py`](../proxy/v2/economics.py) |
| **Decision engine** | Which model, and should we interrupt you at all? | [`engine.py`](../proxy/v2/engine.py) |

Switch it on with `MODELMATCH_ROUTER_V2=1` in `.env`, then restart the proxy. With it off,
ModelMatch behaves exactly like V1.

> **Principle:** automation when the answer is obvious, your decision when it matters. The popup
> isn't the product. Knowing *when* a popup is needed is.

---

## The flow of one prompt

```
You type a prompt
   │
   ▼
Hook ──► local proxy ──► task analyst (Curvet): type, complexity, risk, capability tier, same task?
                │
                ├─ your history for this kind of task (outcomes, not clicks)
                ├─ the session: which model is running, how much context, is its cache warm?
                ├─ stay-vs-switch cost for this task
                ▼
          decision engine ──► SILENT (keep going)  │  ASK (two-button popup)  │  AUTO (switch + note)
                │
                ▼
     the model runs ──► usage measured from the response ──► next prompt judges the outcome
```

## System 1: the task analyst

A small, fast model behind the **Curvet AI gateway** reads the prompt and returns strict JSON:
the task type (architecture, frontend_ui, complex_debugging…), complexity, reasoning need, scope,
risk, expected output size and tool steps, and the **capability tier** the task needs
(`light` / `standard` / `heavy`). The tier is a *need*, not a model name. Which model provides it
is decided per tool: Claude Code maps it to Haiku 4.5 / Sonnet 5.5 / Opus 5.5 (configurable), and
Codex and Copilot map it into their own model lists.

- **Benchmark (2026-10-08, 9 example prompts):** `gpt-5.4-mini` got every tier right with a
  median of about 2.2 s. It's the default, with `claude-haiku-4-5` as a backup that starts if the
  first answer takes longer than 3 s. A cheap code model under-rated a refactor and an ambiguous
  task, so it wasn't picked.
- **Semantic, not keywords:** "duplicate payments after webhook retries, find the race condition"
  comes back as `complex_debugging`, `high`, `heavy`. "Change the button padding" comes back as
  `small_edit`, `low`, `light`.
- **Fail open:** timeouts, 401/403/429/5xx, unreachable network, invalid JSON, an unknown tier or
  very low confidence all mean *keep your current model*. Your workflow never waits on the router
  for more than `MODELMATCH_API_TIMEOUT` (6 s).
- **What leaves your Mac:** the prompt, trimmed to 8,000 characters, with obvious secrets (API
  keys, tokens, private keys, `password=…`) replaced by `[REDACTED]`. For continuity, the analyst
  also gets the previous task's type and a few-word label, never the previous prompt. Your
  Claude, Codex or Copilot sign-in is never used for routing.

## System 2: learning from outcomes, not clicks

Every decision is stored as metadata in `state/modelmatch.db` (`state/agents-modelmatch.db` for
Codex and Copilot). When your **next** prompt arrives, the previous decision is judged:

| Outcome | Signal |
|---|---|
| `no_negative_signal` | The task continued or moved on normally |
| `retry` | Same task, "still broken / try again", or the same prompt again |
| `escalated` | Same task, and you then needed a stronger model |
| `switched_back` | ModelMatch moved the model and you asked to go back |
| `refused` / `error` | The model refused, its requests failed, or the routed model was rejected |
| `manual_override` | You changed the tool's model yourself |

These are behavioural signals, not proof of quality.

Profiles are rebuilt from that evidence **per task type and per model family**, weighted by
recency (21-day half-life). There's no fixed calibration week: the more you use it, the more
confident it gets.

- **Escalation raises the bar.** If Sonnet repeatedly needed rescuing on your `complex_debugging`
  tasks, the next one is recommended Opus (`PAST_SONNET_ESCALATION`).
- **Proven success can lower it, one step, never for high-complexity work.** If Sonnet has a clean
  record on your medium architecture questions, ModelMatch may suggest it.
- **Preference changes *when* we ask, never *what* we recommend.** Choosing Opus for small UI
  edits a hundred times doesn't teach "small UI = Opus". Those decisions are recorded as
  *over-provisioning* (the analyst said a lighter tier was enough and nothing went wrong). After
  you've declined three times in a row, the popup becomes a one-line note, and it asks again every
  sixth time in case you've changed your mind.
- Developers are never labelled junior or senior. Evidence is per task category.

## System 3: switch economics (the cache)

Each model keeps its own prompt cache. A warm model re-reads the conversation at the cheap
cache-read price, while a model new to the conversation must write all of it at full price. The
proxy measures the real numbers from every response (input, cache read, cache write 5m/1h,
output), plus the cache lifetime requested and your billing mode. It never reads the text.

For a task of *k* model calls (from the analyst's expected tool steps), the engine compares:

```
stay   = first call: context x (cache read if warm, cache write if cold)
         + (k-1) calls reading the context from cache
         + k x (new tokens + output)
switch = the same with the candidate's prices and cache state (normally cold)
```

Example (API prices, 95k-token warm Opus 5.5 session, small frontend edit): staying costs about
$0.07, switching to a cold Sonnet 5.5 about $0.27. So **V2 stays on Opus**, and if you usually use
Sonnet for frontend work, it asks:

```
Recommended: Stay on Opus 5.5

You normally use Sonnet 5.5 for frontend work, but this Opus 5.5 session already has a
large reusable context. Switching now is likely to use more resources.

            [ Use Sonnet 5.5 anyway ]   [ Stay on Opus 5.5 ]
```

Honesty rules:

- Dollars appear only for **API-key billing with a measured context**.
- Subscriptions get words ("likely uses more of your plan usage"), because plan usage isn't
  billed per token.
- Models with no known price fall back to token counts (e.g. Codex), or *unknown*.
- Copilot's traffic never passes through ModelMatch, so its cache is reported as **UNKNOWN**.
- Prices (as of 2026-10-05) live in [`pricing.py`](../proxy/v2/pricing.py). Override them with
  `MODELMATCH_PRICING_FILE`.
- Near a plan limit (rate-limit headers ≥ 80%), smaller savings become worth suggesting.

## The decision engine

Priority order:

1. **Your explicit instruction** ("use opus for this") always wins, applied with a note.
2. **Restrictions:** `MODELMATCH_DENIED_MODELS` / `MODELMATCH_ALLOWED_MODELS` and Claude Code's
   `availableModels`.
3. **Availability:** the model must exist in the tool and be switchable.
4. **Capability floor:** the analyst's tier, adjusted by your outcomes.
5. **Session economics:** never leave a warm, capable model when that costs more.
6. **Outcomes, then preference, then resource savings.**

| Situation | What you see |
|---|---|
| Current model already fits | Nothing (silent) |
| Lighter model barely helps (same tier, or under 25% saving) | Nothing |
| Clear saving, confident, evidence on your side, `MODELMATCH_AUTO_ROUTE=1` | Switches, with a note on how to override |
| Clear saving, still calibrating (default) | Popup: *Use Sonnet 5.5* / *Keep Opus 5.5* |
| Task says lighter, but the warm session says stay, and you usually pick lighter | Popup: *Stay on Opus 5.5* / *Use Sonnet 5.5 anyway* |
| Task needs a stronger model | Popup |
| Task crosses two kinds of work, or the analyst is unsure | Popup |
| Analyst very unsure, or unreachable | Nothing (keeps your model; an outage is mentioned once) |

**Task continuity (leases):** "Design authentication architecture", then "How should token
rotation work?" is the same task, so there's no popup and the model stays. "Now implement the
React login form" is a new phase, so it gets re-evaluated. The analyst's `same_task_probability`
is combined with a work-group rule (design → frontend → backend…), because language models call
any work on the same feature a "continuation". A model you accept lasts for the task, not just one
prompt, so there's no flapping. A lease ends on a new task, `/clear`, `/compact`, a fresh session,
two idle hours, or your own model change.

Every decision carries **reason codes** (`TASK_LOW_COMPLEXITY`, `CURRENT_CACHE_WARM`,
`SWITCH_USAGE_PENALTY`, `PAST_SONNET_ESCALATION`, `EXPLICIT_MODEL_REQUEST`…). The short popup text
is generated from them in [`explain.py`](../proxy/v2/explain.py).

## Per tool

| | Claude Code | Codex CLI | Copilot CLI |
|---|---|---|---|
| Models | Haiku / Sonnet / Opus (`MODELMATCH_CLAUDE_*_MODEL`) | your Codex model list | your Copilot plan |
| Switching | proxy rewrites the model | proxy rewrites the model per turn | extension switches, re-applied per follow-up |
| Usage and cache measured | ✅ input, cache read/write, output, TTL | ✅ input, cached input, output | ❌ unknown (traffic doesn't pass through) |
| Prices | ✅ (API list) | tokens only | unknown |

## Data and privacy

**Stored** (`state/*.db`, mode 600, kept 180 days): task type and complexity, analyst model and
latency, models (configured, running, recommended, chosen), the intervention, reason codes, the
outcome, and token counts.

**Never stored:** the prompt, the task's wording, request or response bodies, headers, sign-in
tokens or keys. The few-word task label lives in memory only.

## Measuring it

```bash
.venv/bin/python scripts/modelmatch_report.py          # plain-English summary, last 7 days
curl -s http://127.0.0.1:8787/v2/metrics | python3 -m json.tool
```

The report covers router latency (p50/p95), failure rate, how often it stayed quiet, asked or
switched automatically, accept and reject rates, retry, escalation, override and switch-back
rates, routing regret, and measured usage per model. Savings are not claimed until the data
supports them.

## Settings

| Setting | Default | Meaning |
|---|---|---|
| `MODELMATCH_ROUTER_V2` | 0 | 1 = the V2 decision engine |
| `MODELMATCH_API_URL` | | `https://curvet.in/api/v1/chat/completions` for the Curvet analyst |
| `MODELMATCH_API_KEY` / `MODELMATCH_API_SECRET` | | Curvet App key / App secret (`.env` only, never committed) |
| `MODELMATCH_ROUTER_MODEL` / `_FALLBACK_MODEL` | `gpt-5.4-mini` / `claude-haiku-4-5-20251001` | Analyst models on Curvet |
| `MODELMATCH_API_TIMEOUT` | 6 | Seconds before keeping your model |
| `MODELMATCH_PERSONALIZATION` | 1 | Learn from your outcomes |
| `MODELMATCH_CACHE_AWARE` | 1 | Stay-vs-switch economics |
| `MODELMATCH_AUTO_ROUTE` | 0 | Allow switching without asking when the evidence is strong |
| `MODELMATCH_DENIED_MODELS` / `_ALLOWED_MODELS` | | Comma-separated ids or families |
| `MODELMATCH_CLAUDE_{LIGHT,STANDARD,HEAVY}_MODEL` | Haiku 4.5 / Sonnet 5.5 / Opus 5.5 | Which Claude model provides each tier |
| `MODELMATCH_DB_FILE` / `MODELMATCH_AGENTS_DB_FILE` | `state/modelmatch.db` / `state/agents-modelmatch.db` | V2's memory |

## Known limits

- The analyst adds about 2 s to each prompt (one Curvet call). A hosted `/api/route` could replace
  the direct call later. Set `MODELMATCH_API_URL` to it; the same JSON schema is accepted.
- Outcome signals are behavioural evidence (retries, escalations, overrides, refusals), not a
  quality score. Whether tests passed isn't detected yet.
- Copilot's usage, cache and prices are unknown, so its decisions rely on the task and your
  history only.
- Subscription plan usage is described relatively. Anthropic doesn't publish how cache reads count
  against plan limits.
