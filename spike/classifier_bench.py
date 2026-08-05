"""benchmark classifier model candidates (haiku vs sonnet) on cost, tokens, latency, and routing agreement.

uses the real production classifier prompt + schema + routing rules, so results transfer directly.
run: .venv/bin/python spike/classifier_bench.py
"""

import asyncio
import tempfile
import time

from agent_harness.classifier import extract_usage, parse_classification
from agent_harness.config import load_config
from agent_harness.prompts import CLASSIFICATION_SCHEMA, classifier_prompt
from agent_harness.router import route

MODELS = ["haiku", "sonnet", "fable"]
APPROVED = ["example-org/example-repo"]

REQUESTS = [
    ("trivial-question", "In one sentence, what does HTTP status 429 mean?"),
    ("investigation", "Clone example-repo and summarize what the service does and how it's structured"),
    ("bug-fix+jira", "PROJ-482 is still throwing a KeyError in the exposure calculator — find and fix it and open a PR"),
    ("feature", "Add a /api/v1/health endpoint to example-repo that reports db and cache status, with tests, and open a PR"),
    ("pr-review", "Review PR #212 in example-repo and leave comments on anything risky"),
    ("incident", "We're seeing 500s from the service since the 2pm deploy — check Sentry and recent commits and tell me what broke"),
    ("critical", "Fix a cross-service race that can double-charge customers during concurrent retries; preserve existing data and prove the locking design is safe"),
    ("jira-ops", "Create a Jira story for adding retry logic to the feed poller, put it in the PROJ project"),
    ("vague", "hey agent what's up"),
]


async def classify_once(model: str, text: str, semaphore: asyncio.Semaphore) -> dict:
    from claude_agent_sdk import ClaudeAgentOptions, query

    options = ClaudeAgentOptions(model=model, max_turns=3, allowed_tools=[], setting_sources=[], cwd=tempfile.mkdtemp(prefix="bench-"), output_format={"type": "json_schema", "schema": CLASSIFICATION_SCHEMA})
    async with semaphore:
        start = time.monotonic()
        final = None
        async for message in query(prompt=classifier_prompt(text, APPROVED, ["github", "aws", "sentry", "jira"]), options=options):
            final = message
        latency = time.monotonic() - start
    classification = parse_classification(final)
    usage = extract_usage(final) or {}
    return {
        "classification": classification,
        "cost": float(getattr(final, "total_cost_usd", 0) or 0),
        "in_tokens": usage.get("input_tokens") or 0,
        "out_tokens": usage.get("output_tokens") or 0,
        "cache_read": usage.get("cache_read_tokens") or 0,
        "latency": latency,
    }


async def main() -> None:
    raw = load_config("config/config.yaml").raw
    semaphore = asyncio.Semaphore(4)
    jobs = {(model, name): asyncio.create_task(classify_once(model, text, semaphore)) for model in MODELS for name, text in REQUESTS}
    results: dict = {}
    for key, job in jobs.items():
        try:
            results[key] = await job
        except Exception as e:
            results[key] = {"error": f"{type(e).__name__}: {e}"}

    totals = {model: {"cost": 0.0, "in": 0, "out": 0, "latency": 0.0, "errors": 0} for model in MODELS}
    agree_type = {model: 0 for model in MODELS if model != "haiku"}
    agree_tier = {model: 0 for model in MODELS if model != "haiku"}
    print(f"\n{'request':<18} {'model':<7} {'task_type':<19} {'cmplx':<9} {'tier':<7} {'cost':>9} {'in':>6} {'out':>5} {'cache':>7} {'sec':>6}")
    print("-" * 100)
    for name, _ in REQUESTS:
        tiers, types = {}, {}
        for model in MODELS:
            r = results[(model, name)]
            if "error" in r:
                print(f"{name:<18} {model:<7} ERROR {r['error']}")
                totals[model]["errors"] += 1
                continue
            c = r["classification"]
            tier = route(c["task_type"], c["complexity"], None, raw).model_alias
            tiers[model], types[model] = tier, c["task_type"]
            totals[model]["cost"] += r["cost"]
            totals[model]["in"] += r["in_tokens"]
            totals[model]["out"] += r["out_tokens"]
            totals[model]["latency"] += r["latency"]
            print(f"{name:<18} {model:<7} {c['task_type']:<19} {c['complexity']:<9} {tier:<7} {r['cost']:>9.4f} {r['in_tokens']:>6} {r['out_tokens']:>5} {r['cache_read']:>7} {r['latency']:>6.1f}")
        if "haiku" in types:
            for model in agree_type:
                if model in types:
                    agree_type[model] += types["haiku"] == types[model]
                    agree_tier[model] += tiers["haiku"] == tiers[model]
    print("-" * 100)
    n = len(REQUESTS)
    for model in MODELS:
        t = totals[model]
        ok = n - t["errors"]
        print(f"{model}: total ${t['cost']:.4f}  avg ${t['cost'] / max(ok, 1):.4f}/call  tokens in={t['in']} out={t['out']}  avg latency {t['latency'] / max(ok, 1):.1f}s  errors={t['errors']}")
    for model in agree_type:
        print(f"agreement vs haiku ({model}): task_type {agree_type[model]}/{n}, routed tier {agree_tier[model]}/{n}")


if __name__ == "__main__":
    asyncio.run(main())
