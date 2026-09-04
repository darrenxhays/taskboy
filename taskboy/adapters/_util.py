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


def permission_hint(system: str, scope: str) -> str:
    """the one sentence every access failure ends with, so the model requests instead of reporting blocked (§8.4)."""
    return f"this is an access problem an operator can fix: call request_permission with kind='access' and target='{system}:{scope}', quoting this error as the reason, then stop working."


def permission_error(system: str, scope: str, detail: str) -> dict:
    return _error(f"{detail} — {permission_hint(system, scope)}")


class AccessDenied(RuntimeError):
    """an upstream system, credential step, or local scope/config check refused for a permission or configuration
    reason an operator can fix (401/403 from an http seam, a denied or failed AssumeRole, an unconfigured
    environment or config value). wrap() turns it into a permission_error the model can act on."""

    def __init__(self, system: str, scope: str, detail: str):
        super().__init__(detail)
        self.system = system
        self.scope = scope
        self.detail = detail


def wrap(fn: Callable, logger):
    adapter = getattr(fn, "__self__", None)
    task = getattr(adapter, "task", None)
    task_id = getattr(task, "task_id", None) or "-"
    tool_name = getattr(fn, "__name__", "tool")

    async def call(args: dict) -> dict:
        try:
            return await fn(args)
        except AccessDenied as e:
            logger.warning("adapter access denied task=%s tool=%s system=%s scope=%s — %s", task_id, tool_name, e.system, e.scope, e.detail)
            return permission_error(e.system, e.scope, e.detail)
        except Exception as e:
            logger.exception("adapter tool failed task=%s tool=%s", task_id, tool_name)
            return _error(str(e))

    return call
