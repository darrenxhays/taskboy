"""phase 0 auth spike: validates the max-subscription assumption from spec §13 before any build-out.

setup (on your workstation):
    claude setup-token            # mints a long-lived oauth token from your max subscription
    export CLAUDE_CODE_OAUTH_TOKEN=...
    pip install claude-agent-sdk
    python spike/auth_spike.py            # all checks
    python spike/auth_spike.py 1 3        # just checks 1 and 3

then repeat on a throwaway ec2 box with ONLY the token in the environment.

go criteria: every check passes with catchable, distinguishable errors.
no-go: document findings and get explicit approval for the ANTHROPIC_API_KEY fallback.
"""

import asyncio
import os
import sys
import tempfile

from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient, query

MODELS = ["haiku", "sonnet", "opus"]


def show(message):
    name = type(message).__name__
    session_id = getattr(message, "session_id", None) or (getattr(message, "data", {}) or {}).get("session_id")
    print(f"    {name} session={session_id} cost={getattr(message, 'total_cost_usd', None)} turns={getattr(message, 'num_turns', None)}")
    return message


async def check_1_headless_structured_output():
    """single haiku call with a json schema — proves headless auth works at all, no browser/keychain prompts."""
    schema = {"type": "object", "properties": {"answer": {"type": "string"}, "confidence": {"type": "string", "enum": ["low", "medium", "high"]}}, "required": ["answer", "confidence"]}
    # structured output consumes an extra turn, so max_turns must be > 1 (first run failed on this)
    options = ClaudeAgentOptions(model="haiku", max_turns=3, allowed_tools=[], setting_sources=[], cwd=tempfile.mkdtemp(), output_format={"type": "json_schema", "schema": schema})
    async for message in query(prompt="What is 2+2? Answer briefly.", options=options):
        final = show(message)
    structured = getattr(final, "structured_output", None)
    print(f"    structured_output: {structured!r}")
    print(f"    result: {getattr(final, 'result', None)!r}")
    if not structured and not getattr(final, "result", None):
        raise RuntimeError("no structured output returned")


async def check_2_concurrent_sessions(n=5):
    """n concurrent multi-turn sessions with tool use — watches for cross-session auth failures and the rate-limit error shape."""

    async def one(i):
        workdir = tempfile.mkdtemp(prefix=f"spike2-{i}-")
        options = ClaudeAgentOptions(model="sonnet", max_turns=15, allowed_tools=["Bash", "Write", "Read"], permission_mode="acceptEdits", setting_sources=[], cwd=workdir)
        prompt = "Create a file called numbers.txt containing the numbers 1-20 one per line, then use bash to sum them and report the total."
        try:
            async for message in query(prompt=prompt, options=options):
                final = message
            print(f"  session {i}: ok — {type(final).__name__} cost={getattr(final, 'total_cost_usd', None)}")
        except Exception as e:
            print(f"  session {i}: FAILED with {type(e).__name__}: {e}")
            raise

    results = await asyncio.gather(*(one(i) for i in range(n)), return_exceptions=True)
    failures = [r for r in results if isinstance(r, Exception)]
    if failures:
        raise failures[0]


async def check_3_model_selection():
    """per-session model selection across the catalog; capture the exact error when a model is refused (defines MOD-009 detection)."""
    for alias in MODELS:
        options = ClaudeAgentOptions(model=alias, max_turns=1, allowed_tools=[], setting_sources=[], cwd=tempfile.mkdtemp())
        try:
            async for message in query(prompt="Reply with the single word: ok", options=options):
                final = message
            print(f"  model {alias}: ok — {getattr(final, 'result', '')!r}")
            show(final)
        except Exception as e:
            print(f"  model {alias}: REFUSED with {type(e).__name__}: {e}")


async def check_4_kill_and_resume():
    """interrupt a session mid-task, then resume by session id — restart reconciliation depends on this."""
    workdir = tempfile.mkdtemp(prefix="spike4-")
    options = ClaudeAgentOptions(model="sonnet", max_turns=10, allowed_tools=["Write"], permission_mode="acceptEdits", setting_sources=[], cwd=workdir)
    session_id = None
    async with ClaudeSDKClient(options=options) as client:
        await client.query("Remember this codeword: PELICAN. Then write it to codeword.txt.")
        async for message in client.receive_response():
            session_id = getattr(message, "session_id", None) or (getattr(message, "data", {}) or {}).get("session_id") or session_id
    print(f"  first session ended, session_id={session_id}")
    if not session_id:
        raise RuntimeError("no session id captured — resume cannot work")
    resume_options = ClaudeAgentOptions(model="sonnet", max_turns=5, allowed_tools=[], setting_sources=[], cwd=workdir, resume=session_id)
    async for message in query(prompt="What was the codeword? Reply with just the word.", options=resume_options):
        final = message
    result = str(getattr(final, "result", ""))
    print(f"  resumed answer: {result!r}")
    if "PELICAN" not in result.upper():
        raise RuntimeError("resumed session lost its context")


async def check_5_cost_reporting():
    """does subscription auth populate cost/usage? if cost is zero/None, budgets must fall back to token counts."""
    options = ClaudeAgentOptions(model="haiku", max_turns=1, allowed_tools=[], setting_sources=[], cwd=tempfile.mkdtemp())
    async for message in query(prompt="Reply with the single word: ok", options=options):
        final = message
    cost = getattr(final, "total_cost_usd", None)
    usage = getattr(final, "usage", None)
    print(f"  total_cost_usd={cost!r}")
    print(f"  usage={usage!r}")
    if not cost and not usage:
        print("  WARNING: neither cost nor usage reported — budget enforcement needs a different signal")


CHECKS = {
    "1": ("headless structured output", check_1_headless_structured_output),
    "2": ("5 concurrent sessions", check_2_concurrent_sessions),
    "3": ("per-session model selection", check_3_model_selection),
    "4": ("kill and resume", check_4_kill_and_resume),
    "5": ("cost reporting", check_5_cost_reporting),
}


async def main():
    if not os.environ.get("CLAUDE_CODE_OAUTH_TOKEN") and not os.environ.get("ANTHROPIC_API_KEY"):
        print("set CLAUDE_CODE_OAUTH_TOKEN (from `claude setup-token`) before running")
        sys.exit(2)
    selected = sys.argv[1:] or list(CHECKS)
    passed, failed = [], []
    for key in selected:
        name, check = CHECKS[key]
        print(f"\n== check {key}: {name} ==")
        try:
            await check()
            passed.append(key)
            print(f"== check {key}: PASS ==")
        except Exception as e:
            failed.append(key)
            print(f"== check {key}: FAIL — {type(e).__name__}: {e} ==")
    print(f"\npassed: {passed}  failed: {failed}")
    print("GO" if not failed else "NO-GO: document findings; api-key fallback needs explicit approval (spec §13)")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    asyncio.run(main())
