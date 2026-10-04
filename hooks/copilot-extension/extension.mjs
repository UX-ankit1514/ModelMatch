// ModelMatch for GitHub Copilot CLI (a Copilot CLI extension).
//
// scripts/install_copilot.sh copies this file to ~/.copilot/extensions/modelmatch/,
// next to a modelmatch.json that says where the ModelMatch folder is. Copilot
// loads it when experimental mode is on. It registers two hooks:
//
//   onSessionStart         make sure the local proxy is running and register the session
//   onUserPromptSubmitted  ask hooks/modelmatch_copilot_hook.py for ONE recommendation
//                          (it shows the Use / Keep popup) and, if you click Use, run this
//                          prompt on that model
//
// Why the prompt runs "again": Copilot applies a model switch only between turns. So when
// you accept, this first copy of the prompt is answered with a one-line note and no model
// call, the switch is applied, the same prompt is sent again and runs on the recommended
// model, and your previous model is restored as soon as that answer is finished.
//
// Golden rule: never get in Copilot's way. Any error keeps your current model and the
// prompt runs normally. Never write to stdout: it is Copilot's JSON-RPC channel.

import { execFileSync, spawn } from "node:child_process";
import { appendFileSync, mkdirSync, readFileSync } from "node:fs";
import { basename, dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { joinSession } from "@github/copilot-sdk/extension";

const HERE = dirname(fileURLToPath(import.meta.url));
const MIN_SWITCH_VERSION = [1, 0, 44]; // first Copilot CLI where a prompt hook can answer without a model call
const DECIDE_TIMEOUT_MS = 115000; // the decision script gives up by itself after 100 s
const MODEL_LIST_TTL_MS = 10 * 60 * 1000;
const RESEND_FALLBACK_MS = 2000;
const STUCK_AFTER_MS = 15 * 60 * 1000;

const settings = loadSettings();
const promptMode = isPromptMode();
const canSwitch = versionAtLeast(copilotVersion(), MIN_SWITCH_VERSION);

let session;
let modelCache = { at: 0, list: [] };
// A prompt being re-run on another model:
// { prompt, target, targetName, restoreTo, restoreTier, stage, since }
// stage: "awaiting-original" -> "sent" -> "running" -> (restored, cleared)
let pending = null;

function loadSettings() {
  try {
    const data = JSON.parse(readFileSync(join(HERE, "modelmatch.json"), "utf8"));
    if (data && typeof data.repo === "string") return { repo: data.repo, python: data.python || "python3" };
  } catch {
    // not installed by the installer: nothing to do
  }
  return null;
}

function log(level, message) {
  if (!settings) return;
  try {
    const dir = join(settings.repo, "logs");
    mkdirSync(dir, { recursive: true });
    const now = new Date();
    const stamp = new Date(now.getTime() - now.getTimezoneOffset() * 60000).toISOString().replace("T", " ").slice(0, 19);
    appendFileSync(join(dir, "copilot-extension.log"), `${stamp} | ${level.padEnd(7)} | cp-ext  | ${message}\n`);
  } catch {
    // logging is best effort
  }
}

function short(value) {
  return String(value || "-").slice(0, 8);
}

function isPromptMode() {
  const pid = process.env.COPILOT_EXTENSION_PARENT_PID || String(process.ppid);
  try {
    const command = execFileSync("ps", ["-o", "command=", "-p", pid], { encoding: "utf8", timeout: 3000 });
    return command.trim().split(/\s+/).slice(1).some((w) => w === "-p" || w === "--prompt" || w.startsWith("--prompt="));
  } catch {
    return false;
  }
}

function copilotVersion() {
  const candidates = [process.env.COPILOT_CLI_RESOLVED_DIST_DIR, process.env.COPILOT_CLI_DIST_DIR]
    .filter(Boolean)
    .map((path) => basename(path));
  candidates.push(process.env.COPILOT_CLI_BINARY_VERSION || "");
  for (const text of candidates) {
    const match = /^(\d+)\.(\d+)\.(\d+)/.exec(text);
    if (match) return match.slice(1, 4).map(Number);
  }
  return null;
}

function versionAtLeast(version, minimum) {
  if (!version) return true; // unknown: assume a current Copilot
  for (let i = 0; i < 3; i += 1) {
    if (version[i] !== minimum[i]) return version[i] > minimum[i];
  }
  return true;
}

// Ask hooks/modelmatch_copilot_hook.py. Resolves to its JSON answer, or null on any problem.
function askDecision(payload) {
  return new Promise((resolve) => {
    if (!settings) return resolve(null);
    let output = "";
    let finished = false;
    const done = (value) => {
      if (finished) return;
      finished = true;
      clearTimeout(timer);
      resolve(value);
    };
    let child;
    try {
      child = spawn(settings.python, [join(settings.repo, "hooks", "modelmatch_copilot_hook.py"), "--extension"], {
        stdio: ["pipe", "pipe", "ignore"],
      });
    } catch (error) {
      log("ERROR", `could not run the decision script: ${error}`);
      return resolve(null);
    }
    const timer = setTimeout(() => {
      log("WARNING", "decision script took too long | keeping current model");
      child.kill();
      done(null);
    }, DECIDE_TIMEOUT_MS);
    child.stdout.on("data", (chunk) => {
      output += chunk;
    });
    child.on("error", (error) => {
      log("ERROR", `decision script failed to start: ${error}`);
      done(null);
    });
    child.on("close", () => {
      try {
        done(JSON.parse(output || "{}"));
      } catch {
        log("ERROR", "decision script gave an unreadable answer | keeping current model");
        done(null);
      }
    });
    child.stdin.on("error", () => {});
    child.stdin.end(JSON.stringify(payload));
  });
}

async function say(message) {
  if (!message) return;
  try {
    await session.log(message, { level: "info" });
  } catch (error) {
    log("WARNING", `could not show a message: ${error}`);
  }
}

function report(ok, detail, sessionId) {
  askDecision({ event: "switch_result", session_id: sessionId, ok, detail }).catch(() => {});
}

async function currentModel() {
  try {
    return (await session.rpc.model.getCurrent()) || {};
  } catch {
    return {};
  }
}

async function availableModels() {
  if (modelCache.list.length && Date.now() - modelCache.at < MODEL_LIST_TTL_MS) return modelCache.list;
  try {
    const result = await session.rpc.model.list();
    const raw = Array.isArray(result) ? result : result?.list || result?.models || [];
    const list = raw
      .filter((m) => m && m.id && m.policy?.state !== "disabled")
      .map((m) => ({ id: m.id, name: m.name || "", category: m.modelPickerCategory || "" }));
    modelCache = { at: Date.now(), list };
    return list;
  } catch (error) {
    log("WARNING", `model list unavailable: ${error}`);
    return [];
  }
}

async function onSessionStart(input, invocation) {
  // Not awaited: starting the proxy can take a few seconds and must never hold up Copilot.
  (async () => {
    const current = await currentModel();
    const answer = await askDecision({
      event: "session_start",
      session_id: input?.sessionId || invocation?.sessionId,
      cwd: input?.workingDirectory,
      source: input?.source,
      model: current.modelId || null,
      interactive: !promptMode,
      can_switch: canSwitch,
    });
    if (answer?.message) await say(answer.message);
  })().catch((error) => log("ERROR", `session start: ${error}`));
}

async function onUserPromptSubmitted(input, invocation) {
  try {
    const prompt = input?.prompt;
    if (typeof prompt !== "string") return undefined;
    if (pending && pending.stage === "sent" && prompt === pending.prompt) {
      pending.stage = "running"; // the re-sent copy: let it run on the recommended model
      return undefined;
    }
    if (pending && Date.now() - pending.since > STUCK_AFTER_MS) pending = null;
    if (pending) return undefined; // a switched prompt is still running; this one joins it

    const sessionId = input.sessionId || invocation?.sessionId;
    const current = await currentModel();
    const answer = await askDecision({
      event: "prompt",
      session_id: sessionId,
      cwd: input.workingDirectory,
      prompt,
      current_model: current.modelId || null,
      available_models: await availableModels(),
      interactive: !promptMode,
      can_switch: canSwitch,
    });
    if (!answer) return undefined;
    if (answer.action !== "switch" || !answer.model_id) {
      await say(answer.message);
      return undefined;
    }
    return await switchForThisPrompt(prompt, sessionId, current, answer);
  } catch (error) {
    log("ERROR", `prompt hook: ${error} | keeping current model`);
    return undefined;
  }
}

async function switchForThisPrompt(prompt, sessionId, current, answer) {
  let result;
  try {
    result = await session.rpc.model.switchTo({ modelId: answer.model_id });
  } catch (error) {
    report(false, `switch to ${answer.model_id} failed: ${error}`, sessionId);
    await say(`ModelMatch couldn't switch to ${answer.model_name}. Keeping your current model.`);
    return undefined;
  }
  const failed = ["failed", "rejected", "error", "unavailable", "cancelled"].includes(String(result?.status || ""));
  if (result?.confirmation || failed) {
    // e.g. the target's context window needs the conversation compacted first: never do that silently
    report(false, `switch to ${answer.model_id} not applied (${result?.status || "needs confirmation"})`, sessionId);
    await say(`ModelMatch didn't switch to ${answer.model_name} (Copilot asked for a confirmation). ` +
      "Keeping your current model.");
    return undefined;
  }
  pending = {
    prompt,
    target: answer.model_id,
    targetName: answer.model_name,
    restoreTo: current.modelId || null,
    restoreTier: current.autoTier || null,
    stage: "awaiting-original",
    since: Date.now(),
  };
  setTimeout(() => {
    if (pending && pending.stage === "awaiting-original") resend([]);
  }, RESEND_FALLBACK_MS);
  report(true, `${current.modelId || "?"} -> ${answer.model_id} (session ${short(sessionId)})`, sessionId);
  log("INFO", `switching | session=${short(sessionId)} | ${current.modelId || "?"} -> ${answer.model_id} | re-sending prompt`);
  return {
    handled: true,
    responseContent: `${answer.message}\n(Your prompt runs again below on ${answer.model_name}.)`,
  };
}

async function resend(attachments) {
  if (!pending || pending.stage !== "awaiting-original") return;
  pending.stage = "sent";
  try {
    const options = { prompt: pending.prompt, mode: "enqueue" };
    if (Array.isArray(attachments) && attachments.length) options.attachments = attachments;
    await session.send(options);
  } catch (error) {
    log("ERROR", `re-sending the prompt failed: ${error}`);
    const done = pending;
    pending = null;
    await restore(done);
    await say("ModelMatch couldn't re-send your prompt, so please send it again. Your model was put back.");
  }
}

async function restore(done) {
  if (!done || !done.restoreTo) return;
  const now = await currentModel();
  if (now.modelId && now.modelId !== done.target) return; // you picked another model meanwhile: leave it
  try {
    const options = { modelId: done.restoreTo };
    if (done.restoreTo === "auto" && done.restoreTier) options.autoTier = done.restoreTier;
    await session.rpc.model.switchTo(options);
    log("INFO", `restored | ${done.target} -> ${done.restoreTo}`);
  } catch (error) {
    log("ERROR", `could not switch back to ${done.restoreTo}: ${error}`);
    await say(`ModelMatch couldn't switch back to ${done.restoreTo}. Use /model to pick it again.`);
  }
}

function onEvent(event) {
  if (!pending || !event) return;
  const data = event.data || {};
  if (event.type === "user.message") {
    if (pending.stage === "awaiting-original" && data.content === pending.prompt) {
      resend(data.attachments); // the original copy, with any files you attached
    } else if (pending.stage === "sent" && data.content === pending.prompt) {
      pending.stage = "running";
    }
  } else if (event.type === "session.idle" && pending.stage === "running") {
    const done = pending;
    pending = null;
    restore(done);
  }
}

session = await joinSession(
  settings ? { hooks: { onSessionStart, onUserPromptSubmitted } } : {},
);
if (settings) {
  session.on(onEvent);
  log("INFO", `loaded | copilot ${copilotVersion()?.join(".") || "unknown"} | switching ${canSwitch ? "available" : "needs 1.0.44+"}${promptMode ? " | prompt mode" : ""}`);
}
