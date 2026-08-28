import sys
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from taskboy import review_requests
from taskboy.adapters.github_api import GitHubStatusError
from taskboy.config import Role
from taskboy.main import should_start_review_poller
from taskboy.models import BLOCKED, CANCELLED, FAILED, RECEIVED
from taskboy.review_requests import DEBUG_PAGE_AFTER_CONSECUTIVE_FAILURES, ReviewRequestPoller


def pull(sha="abc", reviewers=None, number=7, repo="org/a", author="human", ref="agent/t1-fix"):
    return {
        "number": number,
        "html_url": f"https://github.com/{repo}/pull/{number}",
        "head": {"sha": sha, "ref": ref},
        "user": {"login": author},
        "requested_reviewers": [{"login": login} for login in (reviewers or ["red-app[bot]"])],
    }


def ok(pulls):
    return 200, {}, pulls


def review(id, state, login="blue-app[bot]", commit_id="abc"):
    return {"id": id, "user": {"login": login}, "state": state, "commit_id": commit_id}


def get_router(pulls, reviews=None):
    """dispatches the poller's `_get` by path: the pulls list, or one PR's `/reviews` page."""

    async def get(path, token):
        if "/reviews" in path:
            return ok(reviews or [])
        return ok(pulls)

    return get


def poller(store, config, notifier, repos=None, blue=False, auto_address_agent_prs=False, round_cap=3):
    config.runner = "claude"
    config.raw = {
        "github": {
            "approved_repos": repos or ["org/a"],
            "review_requests": {"enabled": True, "poll_interval_seconds": 60, "notify_channel": "", "auto_address_agent_prs": auto_address_agent_prs, "round_cap": round_cap},
        }
    }
    config.roles["system"] = Role("system", ["github"], ["read_only", "standard"], False, 12.0, None)
    broker = AsyncMock()
    broker.app_slug.return_value = "red-app"
    broker.read_token.return_value = ("token", time.time() + 3600)
    reviewer_broker = None
    if blue:
        config.reviewer.enabled = True
        reviewer_broker = AsyncMock()
        reviewer_broker.app_slug.return_value = "blue-app"
    return ReviewRequestPoller(store, config, broker, notifier, reviewer_broker=reviewer_broker)


def finished_blue_review(store, channel="", key="reviewer:org/a#7@abc", url="https://github.com/org/a/pull/7"):
    """the state a real sweep sees once the reviewer's review is up: the reviewer task for this head exists and is terminal.
    without it, the sweep's own just-created reviewer task counts as active and correctly suppresses the follow-up."""
    task, created = store.create_task(slack_team_id="github", slack_channel_id=channel, slack_thread_ts=key, slack_message_ts=key, slack_user_id="github", request_text=f"/review {url}", persona="reviewer")
    assert created
    store.transition(task.task_id, RECEIVED, CANCELLED, "test: the reviewer finished reviewing")
    return task


@pytest.mark.asyncio
async def test_requested_reviewer_creates_once_per_head_and_new_head_retriggers(store, config, notifier):
    subject = poller(store, config, notifier)
    subject._get = AsyncMock(return_value=ok([pull()]))
    await subject.sweep()
    await subject.sweep()
    tasks = store.tasks_in_state(RECEIVED)
    assert len(tasks) == 1
    assert tasks[0].request_text == "/review https://github.com/org/a/pull/7"
    assert tasks[0].slack_user_id == "github"
    assert tasks[0].slack_team_id == "github"
    assert tasks[0].slack_thread_ts == "org/a#7@abc"

    subject._get.return_value = ok([pull(sha="def")])
    await subject.sweep()
    assert len(store.tasks_in_state(RECEIVED)) == 2
    subject.broker.read_token.assert_awaited_once_with(["org/a"], permissions={"pull_requests": "read", "metadata": "read"})


@pytest.mark.asyncio
async def test_main_requested_reviewer_creates_one_blue_task_when_available(store, config, notifier):
    subject = poller(store, config, notifier, blue=True)
    subject._get = AsyncMock(return_value=ok([pull(reviewers=["red-app[bot]", "blue-app[bot]"])]))

    await subject.sweep()

    tasks = store.tasks_in_state(RECEIVED)
    assert len(tasks) == 1
    assert tasks[0].persona == "reviewer"
    assert tasks[0].slack_thread_ts == "reviewer:org/a#7@abc"


@pytest.mark.asyncio
async def test_main_requested_reviewer_falls_back_when_reviewer_broker_is_unavailable(store, config, notifier, caplog):
    subject = poller(store, config, notifier, blue=True)
    subject.reviewer_broker = None
    subject._get = AsyncMock(return_value=ok([pull()]))

    await subject.sweep()

    task = store.tasks_in_state(RECEIVED)[0]
    assert task.persona is None
    assert task.slack_thread_ts == "org/a#7@abc"
    assert "falling back to the main agent" in caplog.text


@pytest.mark.asyncio
async def test_other_reviewers_do_not_trigger(store, config, notifier):
    subject = poller(store, config, notifier)
    subject._get = AsyncMock(return_value=ok([pull(reviewers=["somebody"])]))
    await subject.sweep()
    assert store.count_tasks(RECEIVED) == 0


@pytest.mark.asyncio
async def test_blue_reviews_red_authored_pr(store, config, notifier):
    subject = poller(store, config, notifier, blue=True)
    subject._get = AsyncMock(return_value=ok([pull(reviewers=["somebody"], author="red-app[bot]")]))
    await subject.sweep()
    tasks = store.tasks_in_state(RECEIVED)
    assert len(tasks) == 1
    assert tasks[0].persona == "reviewer"
    assert tasks[0].request_text == "/review https://github.com/org/a/pull/7"
    assert tasks[0].slack_thread_ts == "reviewer:org/a#7@abc"


@pytest.mark.asyncio
async def test_blue_task_skipped_when_red_is_already_working_the_pr(store, config, notifier):
    subject = poller(store, config, notifier, blue=True)
    subject._get = AsyncMock(return_value=ok([pull(reviewers=["red-app[bot]", "blue-app[bot]"])]))
    store.create_task(
        slack_team_id="T1",
        slack_channel_id="C1",
        slack_thread_ts="1",
        slack_message_ts="1",
        slack_user_id="U1",
        request_text="address the comments on https://github.com/org/a/pull/7",
    )

    await subject.sweep()

    assert store.count_tasks(RECEIVED) == 1  # only the pre-existing red task; no blue task created


@pytest.mark.asyncio
async def test_blue_task_still_created_once_the_red_task_referencing_the_pr_is_terminal(store, config, notifier):
    subject = poller(store, config, notifier, blue=True)
    subject._get = AsyncMock(return_value=ok([pull(reviewers=["red-app[bot]", "blue-app[bot]"])]))
    task, _ = store.create_task(
        slack_team_id="T1",
        slack_channel_id="C1",
        slack_thread_ts="1",
        slack_message_ts="1",
        slack_user_id="U1",
        request_text="address the comments on https://github.com/org/a/pull/7",
    )
    store.transition(task.task_id, RECEIVED, CANCELLED, "done")

    await subject.sweep()

    tasks = [t for t in store.tasks_in_state(RECEIVED) if t.persona == "reviewer"]
    assert len(tasks) == 1


@pytest.mark.asyncio
async def test_reviewer_requested_reviewer_creates_blue_task(store, config, notifier):
    subject = poller(store, config, notifier, blue=True)
    subject._get = AsyncMock(return_value=ok([pull(reviewers=["blue-app[bot]"])]))
    await subject.sweep()
    task = store.tasks_in_state(RECEIVED)[0]
    assert task.persona == "reviewer"
    assert task.request_text.startswith("/review ")


@pytest.mark.asyncio
async def test_blue_auto_review_can_be_disabled_and_requires_broker(store, config, notifier):
    subject = poller(store, config, notifier, blue=True)
    config.reviewer.review_agent_prs = False
    subject._get = AsyncMock(return_value=ok([pull(reviewers=["somebody"], author="red-app[bot]")]))
    await subject.sweep()
    assert store.count_tasks(RECEIVED) == 0

    subject.reviewer_broker = None
    subject.reviewer_bot_login = None
    subject._get.return_value = ok([pull(reviewers=["blue-app[bot]"])])
    await subject.sweep()
    assert store.count_tasks(RECEIVED) == 0


@pytest.mark.asyncio
async def test_red_request_and_red_authorship_create_only_one_blue_task(store, config, notifier):
    subject = poller(store, config, notifier, blue=True)
    subject._get = AsyncMock(return_value=ok([pull(author="red-app[bot]")]))
    await subject.sweep()
    tasks = store.tasks_in_state(RECEIVED)
    assert len(tasks) == 1
    assert tasks[0].persona == "reviewer"
    assert tasks[0].slack_thread_ts == "reviewer:org/a#7@abc"


@pytest.mark.asyncio
async def test_auto_address_disabled_by_default_even_with_unresolved_blue_review(store, config, notifier):
    subject = poller(store, config, notifier, blue=True)  # auto_address_agent_prs defaults False
    subject._get = get_router([pull(reviewers=["somebody"], author="red-app[bot]")], [review(1, "CHANGES_REQUESTED")])
    await subject.sweep()
    assert not any(t.persona is None and "address review comments" in t.request_text for t in store.tasks_in_state(RECEIVED))


@pytest.mark.asyncio
async def test_auto_address_creates_red_task_when_blue_requests_changes_on_current_head(store, config, notifier):
    subject = poller(store, config, notifier, blue=True, auto_address_agent_prs=True)
    finished_blue_review(store)
    subject._get = get_router([pull(reviewers=["somebody"], author="red-app[bot]")], [review(41, "CHANGES_REQUESTED")])

    await subject.sweep()

    followups = [t for t in store.tasks_in_state(RECEIVED) if t.request_text.startswith("address review comments on")]
    assert len(followups) == 1
    assert followups[0].request_text == "address review comments on https://github.com/org/a/pull/7 — push the fix to the existing branch `agent/t1-fix`, not a new one"
    assert followups[0].persona is None  # runs as red, not blue
    assert followups[0].slack_thread_ts == "org/a#7@abc:review:41"


@pytest.mark.asyncio
async def test_auto_address_skips_when_blue_approves(store, config, notifier):
    subject = poller(store, config, notifier, blue=True, auto_address_agent_prs=True)
    finished_blue_review(store)
    subject._get = get_router([pull(reviewers=["somebody"], author="red-app[bot]")], [review(1, "CHANGES_REQUESTED"), review(2, "APPROVED")])

    await subject.sweep()

    assert not any(t.request_text.startswith("address review comments on") for t in store.tasks_in_state(RECEIVED))


@pytest.mark.asyncio
async def test_auto_address_ignores_blue_comment_only_reviews(store, config, notifier):
    # reply_to_pr_comment posts standalone replies as COMMENTED reviews too, so counting them here would burn
    # round-cap rounds on blue's own thread replies and could resurrect a finished loop
    subject = poller(store, config, notifier, blue=True, auto_address_agent_prs=True, round_cap=2)
    finished_blue_review(store)
    rounds = [review(1, "CHANGES_REQUESTED"), review(2, "COMMENTED"), review(3, "COMMENTED"), review(4, "APPROVED")]
    subject._get = get_router([pull(reviewers=["somebody"], author="red-app[bot]")], rounds)

    await subject.sweep()

    # the latest real review is the APPROVED one — the two COMMENTED replies in between must not reopen the loop
    assert not any(t.request_text.startswith("address review comments on") for t in store.tasks_in_state(RECEIVED))
    assert not any(call[0] == "answer" for call in notifier.calls)  # nor should they burn round-cap rounds


@pytest.mark.asyncio
async def test_auto_address_ignores_review_left_on_a_previous_push(store, config, notifier):
    subject = poller(store, config, notifier, blue=True, auto_address_agent_prs=True)
    finished_blue_review(store)
    subject._get = get_router([pull(reviewers=["somebody"], author="red-app[bot]")], [review(1, "CHANGES_REQUESTED", commit_id="stale-sha")])

    await subject.sweep()

    assert not any(t.request_text.startswith("address review comments on") for t in store.tasks_in_state(RECEIVED))


@pytest.mark.asyncio
async def test_auto_address_is_scoped_to_red_authored_prs(store, config, notifier):
    subject = poller(store, config, notifier, blue=True, auto_address_agent_prs=True)
    subject._get = get_router([pull(reviewers=["somebody"], author="a-human")], [review(1, "CHANGES_REQUESTED")])

    await subject.sweep()

    assert not any(t.request_text.startswith("address review comments on") for t in store.tasks_in_state(RECEIVED))


@pytest.mark.asyncio
async def test_auto_address_is_idempotent_per_review(store, config, notifier):
    subject = poller(store, config, notifier, blue=True, auto_address_agent_prs=True)
    finished_blue_review(store)
    subject._get = get_router([pull(reviewers=["somebody"], author="red-app[bot]")], [review(41, "CHANGES_REQUESTED")])

    await subject.sweep()
    await subject.sweep()

    followups = [t for t in store.tasks_in_state(RECEIVED) if t.request_text.startswith("address review comments on")]
    assert len(followups) == 1


@pytest.mark.asyncio
async def test_auto_address_round_cap_ignores_pre_existing_review_history(store, config, notifier):
    # a PR can carry CHANGES_REQUESTED reviews from before the flag was enabled or from human /review runs; those
    # must not count toward the cap — only rounds this poller actually created
    subject = poller(store, config, notifier, blue=True, auto_address_agent_prs=True, round_cap=2)
    finished_blue_review(store)
    rounds = [review(1, "CHANGES_REQUESTED"), review(2, "CHANGES_REQUESTED"), review(3, "CHANGES_REQUESTED")]
    subject._get = get_router([pull(reviewers=["somebody"], author="red-app[bot]")], rounds)

    await subject.sweep()

    followups = [t for t in store.tasks_in_state(RECEIVED) if t.request_text.startswith("address review comments on")]
    assert len(followups) == 1  # this poller's own round counter starts at zero regardless of prior review history


@pytest.mark.asyncio
async def test_auto_address_round_cap_escalates_once_instead_of_spawning_further_rounds(store, config, notifier):
    subject = poller(store, config, notifier, blue=True, auto_address_agent_prs=True, round_cap=2)
    subject.notify_channel = "C_ESCALATE"
    finished_blue_review(store, channel="C_ESCALATE")
    subject.store.meta_set("review_followup_round:org/a#7", "2")  # this poller already spent both of its rounds
    subject._get = get_router([pull(reviewers=["somebody"], author="red-app[bot]")], [review(3, "CHANGES_REQUESTED")])

    await subject.sweep()

    followups = [t for t in store.tasks_in_state(RECEIVED) if t.request_text.startswith("address review comments on")]
    assert len(followups) == 0  # round 3 exceeds the cap, so no new red task is created
    escalations = [call for call in notifier.calls if call[0] == "answer"]
    assert len(escalations) == 1
    assert escalations[0][1] == "C_ESCALATE"
    assert "round cap" in escalations[0][3]

    await subject.sweep()  # a repeat sweep against the same review must not escalate again
    assert len([call for call in notifier.calls if call[0] == "answer"]) == 1


@pytest.mark.asyncio
async def test_auto_address_round_cap_escalation_falls_back_to_debug_notifier_when_no_channel_configured(store, config, notifier):
    notifier.debug = AsyncMock()
    subject = poller(store, config, notifier, blue=True, auto_address_agent_prs=True, round_cap=2)
    finished_blue_review(store)
    # notify_channel left at its default "" — turning the flag on must not leave the round cap silent
    subject.store.meta_set("review_followup_round:org/a#7", "2")
    subject._get = get_router([pull(reviewers=["somebody"], author="red-app[bot]")], [review(3, "CHANGES_REQUESTED")])

    await subject.sweep()

    notifier.debug.system_error.assert_awaited_once()
    assert not any(call[0] == "answer" for call in notifier.calls)


@pytest.mark.asyncio
async def test_round_cap_does_not_escalate_when_blue_approved_the_final_round(store, config, notifier):
    # the cap check runs after the APPROVED check: a loop that finished successfully on its last round
    # must not page a human with a "hit its round cap" escalation
    subject = poller(store, config, notifier, blue=True, auto_address_agent_prs=True, round_cap=2)
    subject.notify_channel = "C_ESCALATE"
    finished_blue_review(store, channel="C_ESCALATE")
    subject.store.meta_set("review_followup_round:org/a#7", "2")  # all rounds spent
    subject._get = get_router([pull(reviewers=["somebody"], author="red-app[bot]")], [review(3, "APPROVED")])

    await subject.sweep()

    assert not any(call[0] == "answer" for call in notifier.calls)


@pytest.mark.asyncio
async def test_capped_pr_stops_fetching_reviews_once_the_escalation_landed(store, config, notifier):
    # after the one-shot cap escalation, sweeping a still-open capped pr must not keep paying a paged
    # reviews GET per sweep — etag caching is off by design while the flag is on
    subject = poller(store, config, notifier, blue=True, auto_address_agent_prs=True, round_cap=2)
    subject.notify_channel = "C_ESCALATE"
    finished_blue_review(store, channel="C_ESCALATE")
    subject.store.meta_set("review_followup_round:org/a#7", "2")
    calls = []

    async def get(path, token):
        calls.append(path)
        if "/reviews" in path:
            return ok([review(3, "CHANGES_REQUESTED")])
        return ok([pull(reviewers=["somebody"], author="red-app[bot]")])

    subject._get = get

    await subject.sweep()  # fetches reviews once, hits the cap, escalates
    reviews_fetches = len([c for c in calls if "/reviews" in c])
    assert reviews_fetches > 0

    await subject.sweep()

    assert len([c for c in calls if "/reviews" in c]) == reviews_fetches  # no further reviews paging
    assert len([call for call in notifier.calls if call[0] == "answer"]) == 1


@pytest.mark.asyncio
async def test_escalate_once_retries_when_the_notification_fails(store, config, notifier):
    # marking the key sent before the send lands would permanently suppress the only human-facing ping
    subject = poller(store, config, notifier, blue=True, auto_address_agent_prs=True)
    subject.notify_channel = "C_ESCALATE"
    real_answer = notifier.answer
    state = {"failed": False}

    async def flaky_answer(channel_id, thread_ts, text):
        if not state["failed"]:
            state["failed"] = True
            raise RuntimeError("slack down")
        await real_answer(channel_id, thread_ts, text)

    notifier.answer = flaky_answer

    with pytest.raises(RuntimeError):
        await subject._escalate_once("escalation-key", "log line", "human ping")
    assert store.meta_get("escalation-key") != "1"  # the ping never landed, so the key is not consumed

    await subject._escalate_once("escalation-key", "log line", "human ping")
    assert len([call for call in notifier.calls if call[0] == "answer"]) == 1

    await subject._escalate_once("escalation-key", "log line", "human ping")  # and still only once
    assert len([call for call in notifier.calls if call[0] == "answer"]) == 1


@pytest.mark.asyncio
async def test_auto_address_agent_prs_requires_blue_review_agent_prs(store, config, notifier):
    # without blue.review_agent_prs, no blue task is ever created for red's new push and the loop dead-ends after
    # round 1 with no escalation — refuse the combination at config load instead
    config.reviewer.review_agent_prs = False
    subject = poller(store, config, notifier, blue=True, auto_address_agent_prs=True)
    assert subject.auto_address_agent_prs is False


@pytest.mark.asyncio
async def test_auto_address_agent_prs_requires_an_enabled_blue(store, config, notifier):
    # without a blue broker (or with blue disabled) the loop can never spawn a review, so the flag
    # would silently do nothing — force it off loudly at load instead
    subject = poller(store, config, notifier, blue=False, auto_address_agent_prs=True)
    assert subject.auto_address_agent_prs is False


@pytest.mark.asyncio
async def test_maybe_follow_up_agent_skips_when_a_red_task_already_references_the_pr(store, config, notifier, make_task):
    # the follow-up spawner has its own has_active_main_task_referencing guard, independent of the branch above
    # that happens to also check it — exercise it directly regardless of how it's reached
    make_task("some earlier red task still working on https://github.com/org/a/pull/7")
    subject = poller(store, config, notifier, blue=True, auto_address_agent_prs=True)
    subject._get = get_router([], [review(41, "CHANGES_REQUESTED")])

    await subject._maybe_follow_up_agent("org/a", pull(reviewers=["somebody"], author="red-app[bot]"), 7, "abc", "token")

    assert not any(t.request_text.startswith("address review comments on") for t in store.tasks_in_state(RECEIVED))


@pytest.mark.asyncio
async def test_maybe_follow_up_agent_waits_while_blues_review_task_is_still_running(store, config, notifier):
    # blue's task posts its review before the task itself finishes; a sweep landing in that window must not
    # spawn red onto the same branch while blue is still running
    subject = poller(store, config, notifier, blue=True, auto_address_agent_prs=True)
    reviewer_task, _ = store.create_task(slack_team_id="github", slack_channel_id="", slack_thread_ts="reviewer:org/a#7@abc", slack_message_ts="reviewer:org/a#7@abc", slack_user_id="github", request_text="/review https://github.com/org/a/pull/7", persona="reviewer")
    subject._get = get_router([pull(reviewers=["somebody"], author="red-app[bot]")], [review(41, "CHANGES_REQUESTED")])

    await subject.sweep()
    assert not any(t.request_text.startswith("address review comments on") for t in store.tasks_in_state(RECEIVED))

    store.transition(reviewer_task.task_id, RECEIVED, CANCELLED, "test: blue finished reviewing")
    await subject.sweep()  # blue's task is terminal now, so the same review spawns the follow-up

    followups = [t for t in store.tasks_in_state(RECEIVED) if t.request_text.startswith("address review comments on")]
    assert len(followups) == 1


@pytest.mark.asyncio
async def test_auto_address_retries_after_a_queue_full_refusal_instead_of_escalating_as_stalled(store, config, notifier):
    # a queue-full refusal creates no task row, so the key survives and later sweeps retry until the queue drains
    subject = poller(store, config, notifier, blue=True, auto_address_agent_prs=True)
    subject.notify_channel = "C_ESCALATE"
    finished_blue_review(store, channel="C_ESCALATE")
    subject.config.queue_max = 0  # the queue is saturated
    subject._get = get_router([pull(reviewers=["somebody"], author="red-app[bot]")], [review(41, "CHANGES_REQUESTED")])

    await subject.sweep()
    await subject.sweep()  # refused both sweeps — no task rows minted, no stall escalation
    assert not store.tasks_in_state(RECEIVED)
    assert not any(call[0] == "answer" for call in notifier.calls)

    subject.config.queue_max = 10  # queue pressure clears
    await subject.sweep()

    followups = [t for t in store.tasks_in_state(RECEIVED) if t.request_text.startswith("address review comments on")]
    assert len(followups) == 1
    assert followups[0].slack_thread_ts == "org/a#7@abc:review:41"  # the original key, never consumed by the refusals
    assert not any(call[0] == "answer" for call in notifier.calls)


@pytest.mark.asyncio
async def test_blue_review_task_queue_full_is_retried_once_the_queue_clears(store, config, notifier):
    # a queue_full refusal must not consume reviewer_key, or the push is never reviewed and the loop
    # dead-ends silently on the blue half
    subject = poller(store, config, notifier, blue=True)
    subject.config.queue_max = 0
    subject._get = AsyncMock(return_value=ok([pull(reviewers=["blue-app[bot]"])]))

    await subject.sweep()  # refused — no task row, the key survives
    assert not store.tasks_in_state(RECEIVED)

    subject.config.queue_max = 10
    await subject.sweep()

    tasks = [t for t in store.tasks_in_state(RECEIVED) if t.persona == "reviewer"]
    assert len(tasks) == 1
    assert tasks[0].slack_thread_ts == "reviewer:org/a#7@abc"


@pytest.mark.asyncio
async def test_escalates_once_when_blues_review_task_dies_without_posting_a_review(store, config, notifier):
    # blue's task can end (failed/cancelled) without ever posting a review; reviewer_key is consumed, so
    # without an escalation the loop would return "blue hasn't weighed in yet" forever
    subject = poller(store, config, notifier, blue=True, auto_address_agent_prs=True)
    subject.notify_channel = "C_ESCALATE"
    subject._get = get_router([pull(reviewers=["somebody"], author="red-app[bot]")], [])

    await subject.sweep()  # creates blue's review task for this head
    reviewer_tasks = [t for t in store.tasks_in_state(RECEIVED) if t.persona == "reviewer"]
    assert len(reviewer_tasks) == 1
    assert not any(call[0] == "answer" for call in notifier.calls)  # blue is still in flight — not a stall

    store.transition(reviewer_tasks[0].task_id, RECEIVED, FAILED, "test: blue died before posting a review")
    await subject.sweep()

    escalations = [call for call in notifier.calls if call[0] == "answer"]
    assert len(escalations) == 1
    assert "without posting a review" in escalations[0][3]

    await subject.sweep()  # a repeat sweep must not escalate again
    assert len([call for call in notifier.calls if call[0] == "answer"]) == 1


@pytest.mark.asyncio
async def test_escalates_when_blues_review_task_parks_blocked_without_posting_a_review(store, config, notifier):
    # BLOCKED is neither active nor terminal — a blue task parked on ask_questions must escalate too,
    # not leave reviewer_key consumed with no review and no ping
    subject = poller(store, config, notifier, blue=True, auto_address_agent_prs=True)
    subject.notify_channel = "C_ESCALATE"
    subject._get = get_router([pull(reviewers=["somebody"], author="red-app[bot]")], [])

    await subject.sweep()
    reviewer_tasks = [t for t in store.tasks_in_state(RECEIVED) if t.persona == "reviewer"]
    assert len(reviewer_tasks) == 1
    store.transition(reviewer_tasks[0].task_id, RECEIVED, BLOCKED, "test: blue parked on a clarifying question")

    await subject.sweep()

    escalations = [call for call in notifier.calls if call[0] == "answer"]
    assert len(escalations) == 1
    assert "blocked" in escalations[0][3]


@pytest.mark.asyncio
async def test_auto_address_escalates_once_when_red_follow_up_stalls_without_pushing(store, config, notifier):
    subject = poller(store, config, notifier, blue=True, auto_address_agent_prs=True)
    subject.notify_channel = "C_ESCALATE"
    finished_blue_review(store, channel="C_ESCALATE")
    subject._get = get_router([pull(reviewers=["somebody"], author="red-app[bot]")], [review(41, "CHANGES_REQUESTED")])

    await subject.sweep()
    followups = [t for t in store.tasks_in_state(RECEIVED) if t.request_text.startswith("address review comments on")]
    assert len(followups) == 1
    store.transition(followups[0].task_id, RECEIVED, CANCELLED, "test: red replied without pushing a fix")

    await subject.sweep()  # same head_sha, same review id — the key dedups, but the task behind it is now terminal

    escalations = [call for call in notifier.calls if call[0] == "answer"]
    assert len(escalations) == 1
    assert escalations[0][1] == "C_ESCALATE"
    assert "stalled" in escalations[0][3]

    await subject.sweep()  # a repeat sweep against the same stalled review must not escalate again
    assert len([call for call in notifier.calls if call[0] == "answer"]) == 1


@pytest.mark.asyncio
async def test_auto_address_escalates_when_red_follow_up_parks_on_ask_questions(store, config, notifier):
    # BLOCKED is neither terminal nor "active" — a follow-up parked on ask_questions must still count as a
    # stall, not dedup silently forever like an in-flight task would.
    subject = poller(store, config, notifier, blue=True, auto_address_agent_prs=True)
    subject.notify_channel = "C_ESCALATE"
    finished_blue_review(store, channel="C_ESCALATE")
    subject._get = get_router([pull(reviewers=["somebody"], author="red-app[bot]")], [review(41, "CHANGES_REQUESTED")])

    await subject.sweep()
    followups = [t for t in store.tasks_in_state(RECEIVED) if t.request_text.startswith("address review comments on")]
    assert len(followups) == 1
    store.transition(followups[0].task_id, RECEIVED, BLOCKED, "test: red parked on a clarifying question")

    await subject.sweep()  # same head_sha, same review id — the key dedups, but the task is parked, not active

    escalations = [call for call in notifier.calls if call[0] == "answer"]
    assert len(escalations) == 1
    assert escalations[0][1] == "C_ESCALATE"
    assert "stalled" in escalations[0][3]

    await subject.sweep()  # a repeat sweep against the same stalled review must not escalate again
    assert len([call for call in notifier.calls if call[0] == "answer"]) == 1


@pytest.mark.asyncio
async def test_per_repo_failure_does_not_stop_other_repos(store, config, notifier):
    subject = poller(store, config, notifier, ["org/a", "org/b"])

    async def get(path, token):
        if "org/a" in path:
            raise RuntimeError("github down")
        return ok([pull(repo="org/b")])

    subject._get = get
    await subject.sweep()
    tasks = store.tasks_in_state(RECEIVED)
    assert len(tasks) == 1
    assert "org/b/pull/7" in tasks[0].request_text


@pytest.mark.asyncio
async def test_401_clears_cached_token_and_stops_sweep_early(store, config, notifier):
    notifier.debug = AsyncMock()
    subject = poller(store, config, notifier, ["org/a", "org/b"])
    subject._get = AsyncMock(side_effect=GitHubStatusError(401, "github api GET x failed: 401 — Bad credentials"))

    await subject.sweep()

    assert subject.token is None
    assert subject.token_expires_at == 0.0
    assert subject._get.await_count == 2  # org/a's first attempt plus the post-remint retry; org/b never fetched
    assert len(store.recent_errors()) == 1
    assert "org/a" not in subject.consecutive_failures
    notifier.debug.system_error.assert_awaited_once()

    # the next sweep re-mints the token instead of reusing the cleared one
    subject.broker.read_token.reset_mock()
    subject._get.side_effect = None
    subject._get.return_value = ok([pull()])
    await subject.sweep()
    subject.broker.read_token.assert_awaited_once()


@pytest.mark.asyncio
async def test_transient_error_is_retried_once_and_succeeds(store, config, notifier, monkeypatch):
    monkeypatch.setattr(review_requests, "TRANSIENT_RETRY_DELAY_SECONDS", 0)
    notifier.debug = AsyncMock()
    subject = poller(store, config, notifier)
    subject._get = AsyncMock(side_effect=[GitHubStatusError(503, "github api GET x failed: 503 — unavailable"), ok([pull()])])

    await subject.sweep()

    assert subject._get.await_count == 2
    assert store.recent_errors() == []
    assert len(store.tasks_in_state(RECEIVED)) == 1
    notifier.debug.system_error.assert_not_awaited()


@pytest.mark.asyncio
async def test_transient_error_persisting_after_retry_does_not_page_before_threshold(store, config, notifier, monkeypatch):
    monkeypatch.setattr(review_requests, "TRANSIENT_RETRY_DELAY_SECONDS", 0)
    notifier.debug = AsyncMock()
    subject = poller(store, config, notifier)
    subject._get = AsyncMock(side_effect=GitHubStatusError(503, "github api GET x failed: 503 — unavailable"))

    await subject.sweep()

    assert subject._get.await_count == 2
    assert len(store.recent_errors()) == 1
    notifier.debug.system_error.assert_not_awaited()


@pytest.mark.asyncio
async def test_transient_error_pages_after_consecutive_failure_threshold(store, config, notifier, monkeypatch):
    monkeypatch.setattr(review_requests, "TRANSIENT_RETRY_DELAY_SECONDS", 0)
    notifier.debug = AsyncMock()
    subject = poller(store, config, notifier)
    subject._get = AsyncMock(side_effect=GitHubStatusError(503, "github api GET x failed: 503 — unavailable"))

    for _ in range(DEBUG_PAGE_AFTER_CONSECUTIVE_FAILURES - 1):
        await subject.sweep()
    notifier.debug.system_error.assert_not_awaited()

    await subject.sweep()
    notifier.debug.system_error.assert_awaited_once()

    # a further consecutive failure does not page again immediately
    await subject.sweep()
    notifier.debug.system_error.assert_awaited_once()


@pytest.mark.asyncio
async def test_non_transient_error_pages_immediately(store, config, notifier):
    notifier.debug = AsyncMock()
    subject = poller(store, config, notifier)
    subject._get = AsyncMock(side_effect=GitHubStatusError(422, "github api GET x failed: 422 — bad request"))

    await subject.sweep()

    assert subject._get.await_count == 1
    notifier.debug.system_error.assert_awaited_once()


@pytest.mark.asyncio
async def test_secondary_rate_limit_with_retry_after_is_treated_as_transient(store, config, notifier, monkeypatch):
    monkeypatch.setattr(review_requests, "TRANSIENT_RETRY_DELAY_SECONDS", 0)
    notifier.debug = AsyncMock()
    subject = poller(store, config, notifier)
    subject._get = AsyncMock(side_effect=[GitHubStatusError(403, "github api GET x failed: 403 — abuse detection", retry_after="30"), ok([pull()])])

    await subject.sweep()

    assert subject._get.await_count == 2
    notifier.debug.system_error.assert_not_awaited()


@pytest.mark.asyncio
async def test_plain_403_without_retry_after_pages_immediately(store, config, notifier):
    notifier.debug = AsyncMock()
    subject = poller(store, config, notifier)
    subject._get = AsyncMock(side_effect=GitHubStatusError(403, "github api GET x failed: 403 — forbidden"))

    await subject.sweep()

    assert subject._get.await_count == 1
    notifier.debug.system_error.assert_awaited_once()


@pytest.mark.asyncio
async def test_consecutive_failure_count_resets_after_success(store, config, notifier, monkeypatch):
    monkeypatch.setattr(review_requests, "TRANSIENT_RETRY_DELAY_SECONDS", 0)
    notifier.debug = AsyncMock()
    subject = poller(store, config, notifier)
    subject._get = AsyncMock(side_effect=GitHubStatusError(503, "github api GET x failed: 503 — unavailable"))

    for _ in range(DEBUG_PAGE_AFTER_CONSECUTIVE_FAILURES - 1):
        await subject.sweep()
    assert subject.consecutive_failures["org/a"] == DEBUG_PAGE_AFTER_CONSECUTIVE_FAILURES - 1

    subject._get.side_effect = None
    subject._get.return_value = ok([pull()])
    await subject.sweep()
    assert "org/a" not in subject.consecutive_failures

    subject._get.side_effect = GitHubStatusError(503, "github api GET x failed: 503 — unavailable")
    subject._get.return_value = None
    await subject.sweep()
    notifier.debug.system_error.assert_not_awaited()


@pytest.mark.asyncio
async def test_401_remints_once_and_recovers_within_the_same_sweep(store, config, notifier):
    notifier.debug = AsyncMock()
    subject = poller(store, config, notifier, ["org/a", "org/b"])
    subject.broker.read_token.side_effect = [("stale-token", time.time() + 3600), ("fresh-token", time.time() + 3600)]

    async def get(path, token):
        if token == "stale-token":
            raise GitHubStatusError(401, "github api GET x failed: 401 — Bad credentials")
        return ok([pull(repo="org/b" if "org/b" in path else "org/a")])

    subject._get = get
    await subject.sweep()

    assert subject.broker.read_token.await_count == 2
    assert store.recent_errors() == []
    notifier.debug.system_error.assert_not_awaited()
    tasks = store.tasks_in_state(RECEIVED)
    assert len(tasks) == 2


@pytest.mark.asyncio
async def test_401_retry_also_fails_records_one_error_and_aborts_sweep(store, config, notifier):
    notifier.debug = AsyncMock()
    subject = poller(store, config, notifier, ["org/a", "org/b"])
    subject.broker.read_token.side_effect = [("stale-token", time.time() + 3600), ("still-bad-token", time.time() + 3600)]
    subject._get = AsyncMock(side_effect=GitHubStatusError(401, "github api GET x failed: 401 — Bad credentials"))

    await subject.sweep()

    assert subject.broker.read_token.await_count == 2
    assert len(store.recent_errors()) == 1
    notifier.debug.system_error.assert_awaited_once()
    assert subject._get.await_count == 2  # org/a's first attempt plus the post-remint retry; org/b never fetched
    assert subject.token is None


@pytest.mark.asyncio
async def test_token_remints_when_broker_expiry_is_within_refresh_margin(store, config, notifier):
    subject = poller(store, config, notifier)
    subject.broker.read_token.side_effect = [("token-a", time.time() + 60), ("token-b", time.time() + 3600)]

    assert await subject._token() == "token-a"
    assert await subject._token() == "token-b"
    assert subject.broker.read_token.await_count == 2


@pytest.mark.asyncio
async def test_disabled_sweep_does_nothing(store, config, notifier):
    subject = poller(store, config, notifier)
    subject.enabled = False
    subject._get = AsyncMock()
    await subject.sweep()
    subject._get.assert_not_awaited()
    subject.broker.app_slug.assert_not_awaited()


@pytest.mark.asyncio
async def test_etag_is_cached_and_304_skips_processing(store, config, notifier):
    subject = poller(store, config, notifier)
    path = "/repos/org/a/pulls?state=open&per_page=50"
    subject._get = AsyncMock(side_effect=[(200, {"ETag": '"v1"'}, [pull()]), (304, {}, None)])

    await subject.sweep()
    await subject.sweep()

    assert subject.etags[path] == '"v1"'
    assert len(store.tasks_in_state(RECEIVED)) == 1
    assert store.recent_errors() == []


@pytest.mark.asyncio
async def test_auto_address_skips_the_etag_short_circuit_so_stalled_follow_ups_are_re_examined(store, config, notifier):
    # a follow-up task can terminate (reply-only, disagree) without ever touching the PR, so the pulls-list body
    # never changes. if the poller trusted a cached etag here it would get a 304 and never re-check the PR's
    # reviews, so the stall would never escalate.
    subject = poller(store, config, notifier, blue=True, auto_address_agent_prs=True)
    subject.notify_channel = "C_ESCALATE"
    finished_blue_review(store, channel="C_ESCALATE")
    path = "/repos/org/a/pulls?state=open&per_page=50"
    pr = pull(reviewers=["somebody"], author="red-app[bot]")
    reviews = [review(41, "CHANGES_REQUESTED")]

    async def get(request_path, token):
        if "/reviews" in request_path:
            return ok(reviews)
        return 200, {"ETag": '"v1"'}, [pr]  # github still sends an etag even though the poller shouldn't keep it

    subject._get = get

    await subject.sweep()
    assert path not in subject.etags  # never cached while the auto-follow-up loop is on, even though github sent one

    followups = [t for t in store.tasks_in_state(RECEIVED) if t.request_text.startswith("address review comments on")]
    assert len(followups) == 1
    store.transition(followups[0].task_id, RECEIVED, CANCELLED, "test: red replied without pushing a fix")

    await subject.sweep()  # a real 304 here (etag cached) would skip the per-pr loop and miss the stall entirely

    escalations = [call for call in notifier.calls if call[0] == "answer"]
    assert len(escalations) == 1
    assert "stalled" in escalations[0][3]


@pytest.mark.asyncio
async def test_get_reviews_pages_past_the_first_page(store, config, notifier):
    # github returns reviews oldest-first; a PR with >100 reviews must not stop after page 1, or
    # reviewer_reviews[-1] would be a stale review that never matches the current head_sha
    subject = poller(store, config, notifier, blue=True, auto_address_agent_prs=True, round_cap=200)
    finished_blue_review(store)
    first_page = [review(n, "CHANGES_REQUESTED") for n in range(1, 101)]
    second_page = [review(101, "CHANGES_REQUESTED")]
    calls = []

    async def get(path, token):
        calls.append(path)
        if "/reviews" not in path:
            return ok([pull(reviewers=["somebody"], author="red-app[bot]")])
        return ok(second_page if "page=2" in path else first_page)

    subject._get = get

    await subject.sweep()

    assert any("page=2" in c for c in calls)  # the short second page was fetched
    followups = [t for t in store.tasks_in_state(RECEIVED) if t.request_text.startswith("address review comments on")]
    assert len(followups) == 1
    assert followups[0].slack_thread_ts == "org/a#7@abc:review:101"  # keyed off the true latest review, not page 1's


@pytest.mark.asyncio
async def test_get_sends_if_none_match(monkeypatch, store, config, notifier):
    captured = {}

    class Response:
        status = 200
        headers = {"ETag": '"v2"'}

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def json(self):
            return []

    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        def get(self, url, headers):
            captured.update(headers)
            return Response()

    monkeypatch.setitem(sys.modules, "aiohttp", SimpleNamespace(ClientSession=Session))
    subject = poller(store, config, notifier)
    path = "/repos/org/a/pulls?state=open&per_page=50"
    subject.etags[path] = '"v1"'

    status, headers, pulls = await subject._get(path, "token")

    assert (status, headers["ETag"], pulls) == (200, '"v2"', [])
    assert captured["If-None-Match"] == '"v1"'


@pytest.mark.asyncio
async def test_get_raises_status_error_with_retry_after_on_failure(monkeypatch, store, config, notifier):
    class Response:
        status = 403
        headers = {"Retry-After": "12"}

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def text(self):
            return "abuse detection triggered"

    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        def get(self, url, headers):
            return Response()

    monkeypatch.setitem(sys.modules, "aiohttp", SimpleNamespace(ClientSession=Session))
    subject = poller(store, config, notifier)

    with pytest.raises(GitHubStatusError) as exc_info:
        await subject._get("/repos/org/a/pulls?state=open&per_page=50", "token")

    assert exc_info.value.status == 403
    assert exc_info.value.retry_after == "12"


def test_poller_start_guard(config, caplog):
    broker = object()
    config.runner = "claude"
    config.raw = {"github": {"review_requests": {"enabled": True}}}
    assert should_start_review_poller(config, broker) is False
    assert "no configured role" in caplog.text
    config.roles["system"] = Role("system", ["github"], ["standard"], False, 12.0, None)
    assert should_start_review_poller(config, broker) is True
    assert should_start_review_poller(config, None) is False
    config.raw["github"]["review_requests"]["enabled"] = False
    assert should_start_review_poller(config, broker) is False
