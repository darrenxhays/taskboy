import asyncio
import itertools
import sys
from types import SimpleNamespace

import pytest

from taskboy.config import KNOWN_SERVICES, Config, Role, SlackConfig
from taskboy.store import Store


@pytest.fixture
def fake_aiohttp(monkeypatch):
    def install(status, *, headers=None, body="", payload=None, content=b""):
        class FakeResponse:
            def __init__(self):
                self.status = status
                self.headers = headers or {}

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return None

            async def text(self):
                return body

            async def json(self):
                return payload

            async def read(self):
                return content

        response = FakeResponse()

        class FakeSession:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return None

            def request(self, *args, **kwargs):
                return response

            def get(self, *args, **kwargs):
                return response

        module = SimpleNamespace(BasicAuth=lambda username, password: (username, password), ClientSession=lambda *args, **kwargs: FakeSession())
        monkeypatch.setitem(sys.modules, "aiohttp", module)

    return install


def make_config(**overrides) -> Config:
    fields = dict(
        max_concurrency=2,
        queue_max=10,
        max_retries=2,
        progress_min_interval_seconds=0,
        runner="echo",
        slack=SlackConfig(team_id="T1", allowed_channels=["C1"], enabled=True),
        roles={"admin": Role(name="admin", members=["U1"], allowed_profiles=["read_only", "standard", "deep"], model_override=True, max_budget_usd=None, repos=None)},
        raw={"github": {"approved_repos": ["example-org/taskboy"], "self_repo": "example-org/taskboy"}, "issues": {"notify_channel": "", "uploads_bucket": ""}},
        services={name: True for name in KNOWN_SERVICES},  # tests exercise every integration path unless a test disables one
    )
    fields.update(overrides)
    return Config(**fields)


@pytest.fixture
def store(tmp_path):
    s = Store(str(tmp_path / "test.db"))
    yield s
    s.close()


@pytest.fixture
def config():
    return make_config()


@pytest.fixture
def make_task(store):
    counter = itertools.count()

    def _make(text="do the thing", **overrides):
        n = str(next(counter))
        task, created = store.create_task(slack_team_id="T1", slack_channel_id="C1", slack_thread_ts=n, slack_message_ts=n, slack_user_id="U1", request_text=text, **overrides)
        assert created
        return task

    return _make


class RecordingNotifier:
    def __init__(self):
        self.calls = []

    async def ack(self, task):
        self.calls.append(("ack", task.task_id))

    async def started(self, task):
        self.calls.append(("started", task.task_id))

    async def completed(self, task):
        self.calls.append(("completed", task.task_id))

    async def failed(self, task, error):
        self.calls.append(("failed", task.task_id, error))

    async def blocked(self, task):
        self.calls.append(("blocked", task.task_id))

    async def questions(self, task, questions):
        self.calls.append(("questions", task.task_id, questions))

    async def issue_blocked(self, task, issue):
        self.calls.append(("issue_blocked", task.task_id, issue["id"]))

    async def recovered(self, task):
        self.calls.append(("recovered", task.task_id))

    async def refused(self, task, reason):
        self.calls.append(("refused", task.task_id, reason))

    async def refuse_intake(self, channel_id, thread_ts, reason):
        self.calls.append(("refuse_intake", channel_id, thread_ts, reason))

    async def answer(self, channel_id, thread_ts, text):
        self.calls.append(("answer", channel_id, thread_ts, text))

    async def progress(self, task, message):
        self.calls.append(("progress", task.task_id, message))

    def kinds(self):
        return [call[0] for call in self.calls]


@pytest.fixture
def notifier():
    return RecordingNotifier()


@pytest.fixture
def wait_until():
    async def _wait(predicate, timeout=5.0):
        deadline = asyncio.get_event_loop().time() + timeout
        while not predicate():
            if asyncio.get_event_loop().time() > deadline:
                raise AssertionError("condition not met within timeout")
            await asyncio.sleep(0.01)

    return _wait
