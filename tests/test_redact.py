from agent_harness.models import COMPLETED, QUEUED, RECEIVED, RUNNING
from agent_harness.redact import Redactor, redactor


def test_known_token_families_are_redacted():
    r = Redactor()
    samples = [
        "token ghp_abcdefghijklmnopqrstuvwxyz0123456789",
        "installation ghs_abcdefghijklmnopqrstuvwxyz0123456789",
        "fine grained github_pat_11ABCDEFGHIJKLMNOPQRSTUVWXYZabcdef",
        "slack xoxb-1234567890-abcdefghijklm",
        "app token xapp-1-A0123-456789-abcdef",
        "aws key AKIAIOSFODNN7EXAMPLE",
        "anthropic sk-ant-api03-abcdefghijklmnop",
        "sentry sntrys_eyJpc3MiOiJzZW50cnkabcdefgh",
        "jwt eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0In0.SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJVadQssw5c",
        "header Authorization: Bearer supersecrettoken123",
        "-----BEGIN RSA PRIVATE KEY-----\nMIIEow\n-----END RSA PRIVATE KEY-----",
    ]
    for sample in samples:
        redacted = r.redact(sample)
        assert "[redacted" in redacted, sample
    # bearer keeps the header label but loses the token
    assert "Authorization: Bearer [redacted]" in r.redact("Authorization: Bearer supersecrettoken123")


def test_registered_exact_values_are_redacted_and_unregistered():
    r = Redactor()
    r.register("my-live-secret-value")
    assert r.redact("output contains my-live-secret-value here") == "output contains [redacted] here"
    r.unregister("my-live-secret-value")
    assert "my-live-secret-value" in r.redact("output contains my-live-secret-value here")


def test_tiny_values_are_not_registered():
    r = Redactor()
    r.register("abc")  # would shred normal text
    assert r.redact("abcdef") == "abcdef"


def test_store_redacts_request_text_and_summaries(store):
    redactor.register("live-secret-abc123")
    try:
        task, _ = store.create_task(slack_team_id="T1", slack_channel_id="C1", slack_thread_ts="1.1", slack_message_ts="1.1", slack_user_id="U1", request_text="fix this, token is live-secret-abc123")
        assert "live-secret-abc123" not in task.request_text
        store.transition(task.task_id, RECEIVED, QUEUED, "classified")
        store.transition(task.task_id, QUEUED, RUNNING, "dispatched")
        done = store.transition(task.task_id, RUNNING, COMPLETED, "finished", result_summary="found xoxb-1234567890-abcdefghijklm in logs")
        assert "xoxb-" not in done.result_summary
    finally:
        redactor.unregister("live-secret-abc123")


def test_store_redacts_audit_event_details(store, make_task):
    task = make_task()
    store.add_event(task.task_id, "tool_call", {"input": "curl -H 'Authorization: Bearer supersecrettoken123'"}, tool_name="Bash", is_write=True)
    events = store.events_for(task.task_id)
    assert "supersecrettoken123" not in events[-1]["detail_json"]
