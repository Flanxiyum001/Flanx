"""Persistent usage counters for Flanx: LLM tokens, estimated cost, sandbox quota.

State lives in a small JSON file (default ``flanx_usage.json``) so counters survive
bot restarts. On ephemeral hosts the file resets on redeploy unless you point
``FLANX_USAGE_FILE`` at a persistent path (e.g. a Railway volume).

Cost is computed from USD-per-1M-token prices read from the environment. Both default
to ``0`` because Flanx runs on a free token pool — set them if you ever move to a paid
endpoint so the counter reports real spend.
"""

from __future__ import annotations

import json
import os
import threading
from copy import deepcopy
from datetime import datetime, timezone

USAGE_FILE = os.getenv("FLANX_USAGE_FILE", "flanx_usage.json")
SANDBOX_MONTHLY_QUOTA = 100_000  # OnlineCompiler.io free-plan runs per calendar month

PRICE_PER_M_INPUT = float(os.getenv("AI_INPUT_PRICE_PER_M", "0") or 0)
PRICE_PER_M_OUTPUT = float(os.getenv("AI_OUTPUT_PRICE_PER_M", "0") or 0)

_lock = threading.Lock()


def _empty() -> dict:
    return {
        "llm": {
            "calls": 0,
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
            "cost_usd": 0.0,
            "by_model": {},
        },
        "sandbox": {},  # month keys ("YYYY-MM") map to {runs, passed, failed}
    }


def _month_key() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m")


def _load() -> dict:
    try:
        with open(USAGE_FILE, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        base = _empty()
        base["llm"].update(data.get("llm", {}))
        base["sandbox"].update(data.get("sandbox", {}))
        return base
    except (FileNotFoundError, ValueError, OSError):
        return _empty()


def _save(data: dict) -> None:
    tmp = f"{USAGE_FILE}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, sort_keys=True)
    os.replace(tmp, USAGE_FILE)  # atomic: never leaves a half-written counter file


def _field(obj, key: str, default=0):
    """Read a field from an OpenAI-style usage object or a plain dict."""
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def record_llm_usage(model: str, usage) -> dict:
    """Add one chat-completion's token usage (and its estimated cost) to the tally."""
    prompt = int(_field(usage, "prompt_tokens", 0) or 0)
    completion = int(_field(usage, "completion_tokens", 0) or 0)
    total = int(_field(usage, "total_tokens", 0) or (prompt + completion))
    cost = prompt / 1_000_000 * PRICE_PER_M_INPUT + completion / 1_000_000 * PRICE_PER_M_OUTPUT

    with _lock:
        data = _load()
        llm = data["llm"]
        llm["calls"] = int(llm.get("calls", 0)) + 1
        llm["prompt_tokens"] = int(llm.get("prompt_tokens", 0)) + prompt
        llm["completion_tokens"] = int(llm.get("completion_tokens", 0)) + completion
        llm["total_tokens"] = int(llm.get("total_tokens", 0)) + total
        llm["cost_usd"] = round(float(llm.get("cost_usd", 0.0)) + cost, 6)

        by_model = llm.setdefault("by_model", {})
        entry = by_model.setdefault(
            model, {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
        )
        entry["calls"] += 1
        entry["prompt_tokens"] += prompt
        entry["completion_tokens"] += completion
        entry["total_tokens"] += total
        _save(data)
    return data


def record_sandbox_run(result) -> dict:
    """Count one sandbox execution against this month's free quota."""
    passed = bool(getattr(result, "passed", False))
    with _lock:
        data = _load()
        month = data["sandbox"].setdefault(_month_key(), {"runs": 0, "passed": 0, "failed": 0})
        month["runs"] += 1
        month["passed" if passed else "failed"] += 1
        _save(data)
    return data


def get_usage() -> dict:
    with _lock:
        return deepcopy(_load())


def format_usage() -> str:
    """Human-readable report for Discord (English, under the 2000-char cap)."""
    data = get_usage()
    llm = data["llm"]
    month = data["sandbox"].get(_month_key(), {"runs": 0, "passed": 0, "failed": 0})
    used = int(month["runs"])
    left = max(0, SANDBOX_MONTHLY_QUOTA - used)
    pass_rate = (100 * int(month["passed"]) / used) if used else 0.0

    month_label = datetime.now(timezone.utc).strftime("%b %Y")
    lines = [
        "📊 **Flanx usage**",
        (
            f"**LLM:** {llm['calls']:,} calls · {llm['total_tokens']:,} tokens "
            f"({llm['prompt_tokens']:,} in / {llm['completion_tokens']:,} out) · "
            f"est. cost **${llm['cost_usd']:.4f}**"
        ),
    ]

    by_model = llm.get("by_model", {})
    for name in sorted(by_model, key=lambda k: by_model[k]["total_tokens"], reverse=True)[:5]:
        e = by_model[name]
        lines.append(f"  • `{name}`: {e['calls']:,} calls · {e['total_tokens']:,} tokens")
    if len(by_model) > 5:
        lines.append(f"  • …and {len(by_model) - 5} more model(s)")

    lines.append(
        f"**Sandbox ({month_label}):** {used:,} / {SANDBOX_MONTHLY_QUOTA:,} runs used "
        f"({pass_rate:.0f}% passed) · {left:,} left on the free plan"
    )
    lines.append("_Cost shows $0 on a free token pool; set AI_INPUT_PRICE_PER_M / AI_OUTPUT_PRICE_PER_M for paid endpoints._")
    return "\n".join(lines)
