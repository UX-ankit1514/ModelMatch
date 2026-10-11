#!/usr/bin/env python3
"""A plain-English summary of what ModelMatch V2 has been doing.

    .venv/bin/python scripts/modelmatch_report.py            # last 7 days, all tools
    .venv/bin/python scripts/modelmatch_report.py --days 30
    .venv/bin/python scripts/modelmatch_report.py --json     # the raw numbers

Reads V2's own databases (state/modelmatch.db for Claude Code, state/agents-modelmatch.db
for Codex / Copilot). Works whether or not the proxies are running. Shows only counts and
rates: no prompts are ever stored, so none can be shown.
"""

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from proxy.agents.config import get_agent_settings  # noqa: E402
from proxy.config import get_settings  # noqa: E402
from proxy.v2.metrics import summarize  # noqa: E402
from proxy.v2.store import Store  # noqa: E402


def percent(value):
    return "-" if value is None else "{:.0f}%".format(value * 100)


def describe(name, data):
    lines = ["", name, "-" * len(name)]
    if not data["prompts"]:
        return lines + ["No prompts recorded in this period."]
    router, act, out = data["router"], data["interventions"], data["outcomes"]
    lines.append("Prompts analysed: {}".format(data["prompts"]))
    if router["latency_ms_p50"] is not None:
        lines.append("Task analyst speed: typically {:.1f} s (slowest 5%: {:.1f} s); failed {} time(s).".format(
            router["latency_ms_p50"] / 1000, (router["latency_ms_p95"] or 0) / 1000, router["failures"]))
    lines.append("Stayed quiet: {}   Asked you: {}   Switched automatically: {}".format(
        percent(act["silent_keep_rate"]), percent(act["popup_rate"]), percent(act["auto_route_rate"])))
    if act["accept_rate"] is not None:
        lines.append("When asked, you took the recommendation {} of the time ({} popups unanswered).".format(
            percent(act["accept_rate"]), act["unanswered_popups"]))
    if out["closed"]:
        lines.append("Afterwards: retries {}, escalations to a stronger model {}, manual model changes {}.".format(
            percent(out["retry_rate"]), percent(out["escalation_rate"]), percent(out["override_rate"])))
        if out["routed_decisions"]:
            lines.append("Routing regret (a switch that was followed by a retry, escalation, switch-back, refusal "
                         "or error): {} of {} switched prompts.".format(percent(out["routing_regret_rate"]),
                                                                      out["routed_decisions"]))
    usage = data["measured_usage_by_model"]
    if usage:
        lines.append("Measured usage (from the API's own numbers):")
        for model, numbers in sorted(usage.items(), key=lambda kv: -kv[1]["requests"]):
            lines.append("  {:<26} {:>5} requests | {:>9,} new input | {:>11,} read from cache | {:>9,} written to "
                         "cache | {:>8,} output".format(model, numbers["requests"], numbers["input_tokens"],
                                                        numbers["cache_read_tokens"], numbers["cache_write_tokens"],
                                                        numbers["output_tokens"]))
    return lines


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--days", type=float, default=7.0)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    sources = [("Claude Code", get_settings().db_file, "claude-code"),
               ("Codex CLI", get_agent_settings().base.db_file, "codex"),
               ("GitHub Copilot CLI", get_agent_settings().base.db_file, "copilot")]
    report = {}
    for name, path, client in sources:
        if path is None or not Path(path).exists():
            continue
        report[name] = summarize(Store(Path(path)), args.days, client)
    if args.json:
        print(json.dumps(report, indent=2))
        return 0
    if not report:
        print("No ModelMatch V2 data yet. Turn V2 on with MODELMATCH_ROUTER_V2=1 in .env and restart the proxy.")
        return 0
    print("ModelMatch V2, last {:g} days".format(args.days))
    for name, data in report.items():
        print("\n".join(describe(name, data)))
    print("\nNo savings are claimed from these numbers; token counts are measured, not estimated.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
