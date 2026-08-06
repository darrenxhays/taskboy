"""shared output and error handling for in-process MCP adapters."""

from collections.abc import Callable

from taskboy.redact import redactor

OUTPUT_LIMIT = 4000
TRUNCATION_MARKER = "\n…(output truncated at 4000 chars — narrow the request or use git/the source system for full content)"


def _text(text: str) -> dict:
    value = redactor.redact(text)
    if len(value) > OUTPUT_LIMIT:
        value = value[: OUTPUT_LIMIT - len(TRUNCATION_MARKER)] + TRUNCATION_MARKER
    return {"content": [{"type": "text", "text": value}]}


def _error(message: str) -> dict:
    return {"content": [{"type": "text", "text": f"error: {redactor.redact(message)}"}], "isError": True}


def wrap(fn: Callable, logger):
    async def call(args: dict) -> dict:
        try:
            return await fn(args)
        except Exception as e:
            logger.exception("adapter tool failed")
            return _error(str(e))

    return call
