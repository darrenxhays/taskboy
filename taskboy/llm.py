"""small structured model calls used outside agent sessions.

these go through the claude agent sdk (the claude code cli) so billing follows the
cli's own login — usage credits/subscription — rather than a metered ANTHROPIC_API_KEY.
"""

import json
import logging
import shutil
import tempfile
from typing import Any

from taskboy.redact import redactor

logger = logging.getLogger("taskboy.llm")

_DIAGNOSTIC_SNIPPET_LEN = 300

# sdk raises this after already yielding a usable result frame (issue #80)
_TOLERABLE_ERROR_TEXT = "Claude Code returned an error result: success"

_JSON_FALLBACK_PREAMBLE = "Respond with only a single minified JSON object (no prose, no markdown fences) matching this schema exactly:\n"


class _NoFrameObserved(Exception):
    """the tolerated SDK error (#80) arrived before any usable frame was seen (#93)."""


async def structured_call(model_id: str, prompt: str, schema: dict[str, Any]) -> tuple[dict, dict | None]:
    """one tool-free schema-shaped call; retries once on failure, then raises."""
    use_output_format = True
    last_exc: Exception | None = None
    for attempt in (1, 2):
        try:
            return await _structured_call_once(model_id, prompt, schema, use_output_format=use_output_format)
        except Exception as exc:
            last_exc = exc
            if isinstance(exc, _NoFrameObserved):
                # retrying the identical call fails identically, so the retry drops output_format and parses text
                use_output_format = False
            logger.warning("structured_call attempt %s/2 failed (%s): %s", attempt, type(exc).__name__, redactor.redact(str(exc))[:_DIAGNOSTIC_SNIPPET_LEN])
    assert last_exc is not None
    raise last_exc


async def _structured_call_once(model_id: str, prompt: str, schema: dict[str, Any], *, use_output_format: bool) -> tuple[dict, dict | None]:
    from claude_agent_sdk import ClaudeAgentOptions, SystemMessage, query

    cwd = tempfile.mkdtemp(prefix="taskboy-llm-")
    call_prompt = prompt if use_output_format else f"{prompt}\n\n{_JSON_FALLBACK_PREAMBLE}{json.dumps(schema)}"
    try:
        options = ClaudeAgentOptions(
            model=model_id,
            max_turns=3,  # structured output consumes an extra turn; 1 fails with error_max_turns (phase 0 finding)
            allowed_tools=[],
            setting_sources=[],
            cwd=cwd,
            output_format={"type": "json_schema", "schema": schema} if use_output_format else None,
            effort="low",
        )
        final = None
        seen: list[str] = []
        try:
            async for message in query(prompt=call_prompt, options=options):
                seen.append(type(message).__name__)
                # trailing session_state_changed marker must not clobber the result frame (#80)
                if isinstance(message, SystemMessage) and message.subtype == "session_state_changed":
                    continue
                final = message
        except Exception as exc:
            if str(exc) != _TOLERABLE_ERROR_TEXT:
                raise
            if final is None:
                # surface what was actually seen instead of the same undiagnosable message every time
                diag = f"{exc} (no result frame observed before raise; frames seen: {seen or ['none']})"
                raise _NoFrameObserved(diag) from exc
            logger.warning("tolerating SDK 'error result: success' (%s)", _diagnose(final))
    finally:
        shutil.rmtree(cwd, ignore_errors=True)  # per-call temp cwd must not accumulate on the host
    candidate = getattr(final, "structured_output", None)
    if candidate is None:
        result = getattr(final, "result", None)
        candidate = _extract_json(result) if isinstance(result, str) else result
    if not isinstance(candidate, dict):
        raise ValueError(f"model returned no structured output ({_diagnose(final)})")
    return candidate, extract_usage(final)


def _diagnose(message: Any) -> str:
    """attributes a 'no structured output' failure to the terminal message shape instead of a bare traceback."""
    if message is None:
        return "type=None subtype=None result=None"
    msg_type = type(message).__name__
    subtype = getattr(message, "subtype", None)
    result = getattr(message, "result", None)
    snippet = redactor.redact(str(result))[:_DIAGNOSTIC_SNIPPET_LEN] if result is not None else None
    return f"type={msg_type} subtype={subtype!r} result={snippet!r}"


def _extract_json(text: str) -> dict | None:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        try:
            return json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            return None
    return None


def extract_usage(message) -> dict | None:
    usage = getattr(message, "usage", None) or {}
    cost = getattr(message, "total_cost_usd", None)
    if not usage and cost is None:
        return None
    return {
        "input_tokens": usage.get("input_tokens"),
        "output_tokens": usage.get("output_tokens"),
        "cache_read_tokens": usage.get("cache_read_input_tokens"),
        "cache_write_tokens": usage.get("cache_creation_input_tokens"),
        "cost_usd": cost,
    }
