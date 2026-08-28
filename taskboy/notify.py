"""notifier interface (duck-typed) + stdout implementation, used before slack lands and by `inject --no-slack`.

any notifier must provide: ack, started, completed, failed, blocked, issue_blocked, recovered, refused, refuse_intake, answer.
"""

from taskboy.models import Task


class StdoutNotifier:
    async def ack(self, task: Task) -> None:
        print(f"[{task.task_id}] accepted")

    async def started(self, task: Task) -> None:
        print(f"[{task.task_id}] running")

    async def completed(self, task: Task) -> None:
        print(f"[{task.task_id}] completed: {task.result_summary}")

    async def failed(self, task: Task, error: str) -> None:
        print(f"[{task.task_id}] failed: {error}")

    async def blocked(self, task: Task) -> None:
        print(f"[{task.task_id}] blocked: {task.blocked_reason}")

    async def issue_blocked(self, task: Task, issue: dict) -> None:
        print(f"[{task.task_id}] blocked: {task.blocked_reason} (reopened issue #{issue['id']})")

    async def recovered(self, task: Task) -> None:
        print(f"[{task.task_id}] requeued after restart")

    async def refused(self, task: Task, reason: str) -> None:
        print(f"[{task.task_id}] refused: {reason}")

    async def refuse_intake(self, channel_id: str, thread_ts: str, reason: str) -> None:
        print(f"[intake {channel_id}/{thread_ts}] refused: {reason}")

    async def answer(self, channel_id: str, thread_ts: str, text: str) -> None:
        print(f"[answer {channel_id}/{thread_ts}] {text}")

    async def progress(self, task: Task, message: str) -> None:
        print(f"[{task.task_id}] progress: {message}")
