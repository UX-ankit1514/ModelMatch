# ModelMatch for Codex CLI and GitHub Copilot CLI: install and use

The same idea as ModelMatch for Claude Code, now in **Codex CLI** and **GitHub Copilot CLI**:
type as usual, ModelMatch suggests the best model for each prompt, and one click on **Use**
runs that prompt on it. Your model comes back for the next prompt.

The Claude Code version is unchanged and keeps working exactly as before. All three can be
installed side by side. How it was designed and why: [CODEX-COPILOT-PLAN.md](CODEX-COPILOT-PLAN.md).

---

## What you get

| | Codex CLI | GitHub Copilot CLI |
| --- | --- | --- |
| Suggestion for every prompt | ✅ | ✅ |
| Use / Keep popup (macOS) | ✅ | ✅ |
| One click switches the model for that prompt | ✅ | ✅ |
| Models it can switch to | The models in your Codex model list | The models in your Copilot plan (Claude, GPT, Gemini, …) |
| A line under your prompt saying what happened | ✅ | ✅ |
| Your model is back for the next prompt | ✅ | ✅ (switched back as soon as the answer is done) |
| Scripted runs stay quiet | `codex exec` | `copilot -p` |

---

## Before you start

| You need | Check it with |
| --- | --- |
| A Mac (for the popup) | |
| Python 3.9 or newer | `python3 --version` (missing? run `xcode-select --install`) |
| This folder in a permanent place, e.g. `~/modelmatch` | Codex and Copilot run ModelMatch from here. If you move it, install again. |
| **For Codex:** Codex CLI 0.116 or newer, signed in | `codex --version` |
| **For Copilot:** GitHub Copilot CLI **1.0.44 or newer**, signed in | `copilot --version` (update with `copilot update`) |

> [!TIP]
> **Never used Terminal?** Press <kbd>⌘ Command</kbd> + <kbd>Space</kbd>, type **Terminal**, press
> <kbd>Return</kbd>. To run a command from this page, copy it, paste it into Terminal and press <kbd>Return</kbd>.

In every command below, `cd ~/modelmatch` means "go to the ModelMatch folder". If yours
is somewhere else (for example `~/Desktop/Hooks`), use that path instead.

---

## Codex CLI

### Step 1. Install

```bash
cd ~/modelmatch
bash scripts/install_codex.sh
```

It takes about a minute and ends with green ticks and **Done.** It:

1. checks Python and Codex,
2. installs the local helper's packages,
3. adds two hooks to `~/.codex/hooks.json` (your other hooks are kept),
4. starts the local helper ("proxy") and a small background service that keeps it running,
5. points Codex at the helper (`openai_base_url` in `~/.codex/config.toml`, backed up first),
6. shows which of your Codex models it will use for **light**, **standard** and **heavy** prompts.

> [!NOTE]
> **Already using a gateway such as opencodex?** If `~/.codex/config.toml` already has an
> `openai_base_url`, ModelMatch keeps it: requests go Codex → ModelMatch → your gateway.
> Uninstalling puts your original line back.

### Step 2. Trust the hooks in Codex (one time)

Codex never runs a new hook until you approve it.

1. Start Codex: `codex`
2. Codex says new hooks need review. Type **`/hooks`** and press <kbd>Return</kbd>.
3. You'll see two **ModelMatch** hooks (`SessionStart` and `UserPromptSubmit`). Both run
   `python3 ".../hooks/modelmatch_codex_hook.py"`. Trust both.
4. Quit Codex and start it again.

### Step 3. Use it

Type a prompt as usual. A popup appears:

```
┌─────────────────────────────────────────────────────┐
│  ModelMatch                                         │
│  Recommended model: GPT-5.6-Luna                    │
│  Reason: Short, simple request: a fast,             │
│  lightweight model is enough.                       │
│  Use this model for this prompt?                    │
│        [ Keep current ]   [ Use GPT-5.6-Luna ]      │
└─────────────────────────────────────────────────────┘
```

* **Use**: this prompt is answered by GPT-5.6-Luna. A line under the prompt confirms it.
* **Keep current**, or no click within 30 seconds: nothing changes.
* The next prompt starts fresh with its own suggestion.

### Options

| I want… | Run |
| --- | --- |
| Every Codex session, with switching *(recommended)* | `bash scripts/install_codex.sh` |
| Suggestions only, never switch | `bash scripts/install_codex.sh --no-routing` |
| Hooks for one project only | `bash scripts/install_codex.sh --project ~/path/to/project` (switching is a Codex-wide setting, so it stays as it is) |
| Choose the light / standard / heavy models myself | add to `.env`: `MODELMATCH_CODEX_LIGHT_MODEL=gpt-5.6-luna` (also `_STANDARD_` and `_HEAVY_`), then `python3 scripts/agents_service.py restart` |

---

## GitHub Copilot CLI

### Step 1. Install

```bash
cd ~/modelmatch
bash scripts/install_copilot.sh
```

It:

1. checks Python and Copilot CLI (1.0.44+ is needed to switch models),
2. installs the local helper's packages,
3. installs the **ModelMatch extension** in `~/.copilot/extensions/modelmatch/`,
4. turns on Copilot's **experimental mode** (`"experimental": true` in `~/.copilot/settings.json`), which
   Copilot needs to load extensions. Uninstalling turns it off again, but only if the installer turned it on.
5. starts the local helper.

### Step 2. Restart Copilot

Quit Copilot and start it again with `copilot`. You'll see a line: **"ModelMatch is on…"**.
Type `/extensions` if you want to see **modelmatch** in the list.

### Step 3. Use it

Type a prompt. The same popup appears, showing a model from **your Copilot plan**.

* **Keep current**: nothing changes. A line in the timeline says what was suggested.
* **Use**: you'll see this in the timeline:
  1. your prompt,
  2. a one-line note, *"ModelMatch: running this prompt on Claude Haiku 4.5. … (Your prompt runs again below on Claude Haiku 4.5.)"* This note costs nothing: no model is called for it.
  3. your prompt again, answered by Claude Haiku 4.5,
  4. then your previous model is switched back automatically.

> [!NOTE]
> **Why does the prompt appear twice?** Copilot only changes models between prompts, never in the
> middle of one. So ModelMatch answers the first copy with that one-line note, switches the
> model, sends your prompt again, and switches back when the answer is done. Files you attached are
> sent again with it.

### Option: suggestions only, without the extension

If you'd rather not turn on Copilot's experimental mode:

```bash
bash scripts/install_copilot.sh --hooks-only
```

You get a macOS notification with the suggested model for each prompt, and you switch yourself with
`/model` if you agree. Nothing is switched automatically.

---

## Everyday commands

Run these from the ModelMatch folder.

| Command | What it does |
| --- | --- |
| `bash scripts/doctor_codex.sh` / `bash scripts/doctor_copilot.sh` | Health check, with the exact fix for anything wrong |
| `bash scripts/pause.sh` / `bash scripts/resume.sh` | Pause / resume ModelMatch in **all** tools (Claude Code, Codex, Copilot), from the next prompt |
| `MODELMATCH_CODEX_DISABLED=1` or `MODELMATCH_COPILOT_DISABLED=1` in `.env` | Pause one tool only |
| `git pull` then run the install command again | Update to the newest version |
| `bash scripts/uninstall_codex.sh` | Remove it from Codex (your previous `openai_base_url` comes back) |
| `bash scripts/uninstall_copilot.sh` | Remove it from Copilot |
| `python3 hooks/modelmatch_codex_hook.py --status` | Is the local helper running? (`--start-proxy`, `--stop-proxy`) |
| `.venv/bin/python -m pytest` | Run all the tests |

After installing, updating or uninstalling, **restart Codex / Copilot**.

---

## The line under your prompt

| Message | What it means |
| --- | --- |
| *ModelMatch: using GPT-5.6-Luna for this prompt.* (Codex) | You clicked **Use**. |
| *ModelMatch: running this prompt on Claude Haiku 4.5.* (Copilot) | You clicked **Use**; the prompt runs again below on that model. |
| *ModelMatch: kept your current model (recommended …)* | You clicked **Keep current**. |
| *… no answer within 30s, kept your current model …* | The popup timed out. Nothing changed. |
| *… which is already your current model.* | You're already on the best model, so no popup. |
| *… isn't in your Codex model list / your Copilot plan.* | The best fit isn't available to you. Shown as advice, nothing switched. |
| *… (ModelMatch named X; this is the closest model …)* | The exact model isn't available; the newest model of the same family is used. |
| *… (Recommendation only: model routing is off for Codex.)* | Installed with `--no-routing`, or another gateway is in charge. |
| *ModelMatch is unavailable right now …* | Something failed behind the scenes. Your model answers as usual. |

---

## Settings (`.env`)

ModelMatch for Codex and Copilot reads the same `.env` as the Claude Code version (router URL,
key, popup timeout, …). These settings are new:

| Setting | Default | Controls |
| --- | :-: | --- |
| `MODELMATCH_AGENTS_PORT` | 8788 | Port of the local helper for Codex and Copilot (Claude Code's stays on 8787) |
| `MODELMATCH_CODEX_UPSTREAM_URL` | *automatic* | Where Codex's requests go after ModelMatch. Set by the installer when you already had a gateway. |
| `MODELMATCH_CODEX_LIGHT_MODEL` / `_STANDARD_` / `_HEAVY_` | *from Codex's model list* | Pin the model used for each size of prompt |
| `MODELMATCH_COPILOT_LIGHT_MODEL` / `_STANDARD_` / `_HEAVY_` | *from your Copilot plan* | The same for Copilot (use Copilot's model ids, e.g. `claude-haiku-4.5`) |
| `MODELMATCH_CODEX_DISABLED` / `MODELMATCH_COPILOT_DISABLED` | 0 | `1` pauses that tool only |
| `MODELMATCH_NOTIFY` | 1 | `0` turns off the notifications of the `--hooks-only` Copilot install |

After changing `.env`, restart the helper: `python3 scripts/agents_service.py restart`.

When the real cloud router is connected (`MODELMATCH_API_URL`), Codex and Copilot ask it
with `"client": "codex"` or `"client": "copilot"`, so it can answer with a model that tool runs.

---

## Troubleshooting

<details>
<summary><b>Codex shows connection errors after installing.</b></summary>

<br>

The local helper isn't running. Start it: `python3 hooks/modelmatch_codex_hook.py --start-proxy`.
Still stuck? Turn switching off. Codex then talks to its models directly again:
`bash scripts/install_codex.sh --no-routing`. Then run `bash scripts/doctor_codex.sh`.
</details>

<details>
<summary><b>The installer stopped with "proxy did not start" or "Operation not permitted".</b></summary>

<br>

macOS doesn't let background services read files in **Desktop**, **Documents** or **Downloads**. The
installer handles this by giving the background service its own copy in
`~/Library/Application Support/ModelMatch/agents`. If you see this error you have an older
version of the installer: update to the newest files and run `bash scripts/install_codex.sh` again.
After changing `.env` or updating ModelMatch, run `python3 scripts/agents_service.py restart`
so the service picks up the change (`doctor_codex.sh` tells you when it's out of date).
</details>

<details>
<summary><b>No popup in Codex.</b></summary>

<br>

Codex only runs hooks you trusted: type `/hooks` in Codex and trust the two ModelMatch hooks.
Then restart Codex. The popup may also be behind another window (check Mission Control).
</details>

<details>
<summary><b>No popup in Copilot.</b></summary>

<br>

Check that experimental mode is on (`/experimental on` in Copilot), that `/extensions` lists
**modelmatch**, and that `copilot --version` is 1.0.44 or newer. `bash scripts/doctor_copilot.sh`
checks all of this.
</details>

<details>
<summary><b>The model shown in Codex's / Copilot's status bar didn't change.</b></summary>

<br>

In Codex that's expected: Codex shows the model you picked, while the helper switches the requests
for the prompt you accepted. The line under the prompt and `logs/agents-proxy.log` show what answered.
In Copilot the model really switches for that prompt and back afterwards.
</details>

<details>
<summary><b>It suggested a model but didn't switch.</b></summary>

<br>

The model isn't in your Codex model list or your Copilot plan, so it's shown as advice only.
Switching is never faked.
</details>

<details>
<summary><b>Where are the logs?</b></summary>

<br>

In the ModelMatch folder: `logs/codex-hook.log`, `logs/copilot-hook.log`,
`logs/copilot-extension.log` and `logs/agents-proxy.log`. They record suggestions, choices and errors,
never your prompt text, passwords or keys.
</details>

---

## Limits

* macOS only for the popup; elsewhere your model is always kept.
* Codex: switching needs Codex's built-in OpenAI provider (a custom `model_provider` gets suggestions
  only). The model shown in Codex's status bar doesn't change.
* Copilot: switching relies on Copilot's extension feature, which Copilot still labels experimental.
  If a future Copilot version changes it, ModelMatch keeps your model and tells you.
* Messages you type while a switched Copilot prompt is still running are answered by the same model.
