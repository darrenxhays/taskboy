import json
from unittest.mock import AsyncMock

import pytest

from taskboy.classifier import FALLBACK_CLASSIFICATION, Classifier, extract_usage, parse_classification, validate_classification
from taskboy.config import ConfigError, Role
from taskboy.prompts import CLASSIFICATION_SCHEMA
from taskboy.router import RoleRefusal

RAW = {
    "classifier": {"tier": "haiku"},
    "models": {
        "haiku": {"id": "claude-haiku-4-5", "fallbacks": ["sonnet"]},
        "sonnet": {"id": "claude-sonnet-4-6", "fallbacks": []},
        "opus": {"id": "claude-opus-4-6", "fallbacks": []},
    },
    "routing": {
        "rules": [
            {"name": "cheap", "match": {"complexity": ["trivial"]}, "tier": "haiku", "profile": "read_only"},
        ],
        "default": {"tier": "sonnet", "profile": "read_only"},
    },
    "profiles": {
        "read_only": {"max_budget_usd": 2.0, "max_turns": 60, "max_runtime_minutes": 30},
        "standard": {"max_budget_usd": 12.0, "max_turns": 400, "max_runtime_minutes": 240},
    },
    "skills": {"tier": "opus", "profile": "standard"},
}

CLASSIFICATION = {
    "task_type": "question",
    "complexity": "trivial",
    "risk": "read_only",
    "expected_duration": "minutes",
    "required_integrations": [],
    "target_repos": [],
    "jira_keys": [],
}


def make_classifier(store, config):
    config.raw = RAW
    return Classifier(store, config)


@pytest.mark.asyncio
async def test_classify_routes_and_audits(store, config, make_task):
    classifier = make_classifier(store, config)
    classifier._call_model = AsyncMock(return_value=(dict(CLASSIFICATION), {"input_tokens": 100, "output_tokens": 20, "cache_read_tokens": None, "cache_write_tokens": None, "cost_usd": 0.001}))
    task = make_task("what does the fire service do?")
    fields = await classifier.classify(task)
    assert fields["model_alias"] == "haiku"
    assert fields["profile"] == "read_only"
    assert fields["max_budget_usd"] == 2.0
    assert json.loads(fields["classification_json"])["task_type"] == "question"
    kinds = [event["kind"] for event in store.events_for(task.task_id)]
    assert "classified" in kinds
    assert "model_routing" in kinds  # MOD-005
    usage = store.conn.execute("SELECT * FROM usage WHERE task_id = ?", (task.task_id,)).fetchone()
    assert usage["source"] == "classifier"
    assert usage["cost_usd"] == 0.001


@pytest.mark.asyncio
async def test_classify_falls_back_after_failure(store, config, make_task):
    classifier = make_classifier(store, config)
    classifier._call_model = AsyncMock(side_effect=RuntimeError("model exploded"))
    task = make_task()
    fields = await classifier.classify(task)
    # fallback classification routes through the default rule, never a hidden guess (MOD-009)
    assert fields["task_type"] == FALLBACK_CLASSIFICATION["task_type"]
    assert fields["model_alias"] == "sonnet"
    events = store.events_for(task.task_id)
    # one failed call is recorded; the single retry now lives inside structured_call, so _call_model is invoked once
    failures = [event for event in events if event["kind"] == "classifier_failed"]
    assert len(failures) == 1
    assert classifier._call_model.await_count == 1
    classified = next(event for event in events if event["kind"] == "classified")
    assert json.loads(classified["detail_json"])["fallback"] is True


@pytest.mark.asyncio
async def test_model_override_flows_through_routing(store, config, make_task):
    classifier = make_classifier(store, config)
    classifier._call_model = AsyncMock(return_value=(dict(CLASSIFICATION), None))
    task = make_task(model_override="opus")
    fields = await classifier.classify(task)
    assert fields["model_alias"] == "opus"
    assert "overridden" in fields["routing_rationale"]


@pytest.mark.asyncio
async def test_classify_refuses_task_when_user_no_longer_has_a_role(store, config, make_task):
    classifier = make_classifier(store, config)
    classifier._call_model = AsyncMock(return_value=(dict(CLASSIFICATION), None))
    task = make_task()
    config.roles = {}

    with pytest.raises(RoleRefusal, match="U1.*no configured role"):
        await classifier.classify(task)
    classifier._call_model.assert_not_awaited()


def test_unknown_classifier_tier_fails_at_construction(store, config):
    config.raw = {**RAW, "classifier": {"tier": "mystery"}}
    with pytest.raises(ConfigError, match="mystery"):
        Classifier(store, config)


def test_parse_classification_shapes():
    class WithStructured:
        structured_output = dict(CLASSIFICATION)

    class WithJsonResult:
        result = json.dumps(CLASSIFICATION)

    class Empty:
        result = None

    class JsonInProse:
        result = f"Here is the classification:\n{json.dumps(CLASSIFICATION)}\nDone."

    class Chatty:
        result = "Hey! Not much, just hanging out. What can I help with?"

    assert parse_classification(WithStructured())["task_type"] == "question"
    assert parse_classification(WithJsonResult())["complexity"] == "trivial"
    assert parse_classification(JsonInProse())["task_type"] == "question"
    with pytest.raises(ValueError):
        parse_classification(Chatty())
    with pytest.raises(ValueError):
        parse_classification(Empty())
    incomplete = {"task_type": "question"}

    class Incomplete:
        structured_output = incomplete

    with pytest.raises(ValueError, match="missing"):
        parse_classification(Incomplete())


def test_extract_usage_shapes():
    class WithUsage:
        usage = {"input_tokens": 10, "output_tokens": 5}
        total_cost_usd = 0.01

    class Bare:
        pass

    assert extract_usage(WithUsage())["cost_usd"] == 0.01
    assert extract_usage(Bare()) is None


def test_schema_required_fields_match_fallback():
    assert set(CLASSIFICATION_SCHEMA["required"]) == set(FALLBACK_CLASSIFICATION)


def test_validate_classification_fills_missing_non_critical_field():
    partial = {key: value for key, value in CLASSIFICATION.items() if key != "jira_keys"}
    result = validate_classification(partial)
    assert result["jira_keys"] == FALLBACK_CLASSIFICATION["jira_keys"]
    for key, value in CLASSIFICATION.items():
        if key != "jira_keys":
            assert result[key] == value


@pytest.mark.parametrize("missing_key", ["task_type", "complexity"])
def test_validate_classification_still_raises_without_task_type_or_complexity(missing_key):
    candidate = {key: value for key, value in CLASSIFICATION.items() if key != missing_key}
    with pytest.raises(ValueError, match="missing"):
        validate_classification(candidate)


def test_validate_classification_rejects_non_dict():
    with pytest.raises(ValueError, match="no structured output"):
        validate_classification(None)


@pytest.mark.asyncio
async def test_classifier_prompt_uses_thread_context_and_role_scoped_repos(store, config, make_task):
    config.raw = {**RAW, "github": {"approved_repos": ["org/a", "org/b"], "self_repo": "org/a"}}
    config.roles["admin"] = Role("admin", ["U1"], ["read_only"], True, None, ["org/a"])
    classifier = Classifier(store, config)
    classifier._call_model = AsyncMock(return_value=(dict(CLASSIFICATION), None))
    task = make_task(thread_context="<@U2>: earlier context")
    await classifier.classify(task)
    prompt = classifier._call_model.call_args.args[0]
    assert "org/a" in prompt
    assert "org/b" not in prompt
    assert "earlier context" in prompt
    assert 'The repository "org/a" is Agent\'s own source code' in prompt


@pytest.mark.asyncio
async def test_classifier_prompt_omits_self_repo_outside_role_scope(store, config, make_task):
    config.raw = {**RAW, "github": {"approved_repos": ["org/a", "org/taskboy"], "self_repo": "org/taskboy"}}
    config.roles["admin"] = Role("admin", ["U1"], ["read_only"], True, None, ["org/a"])
    classifier = Classifier(store, config)
    classifier._call_model = AsyncMock(return_value=(dict(CLASSIFICATION), None))
    await classifier.classify(make_task())
    prompt = classifier._call_model.call_args.args[0]
    assert "org/taskboy" not in prompt
    assert "own source code" not in prompt


@pytest.mark.asyncio
@pytest.mark.parametrize("reviewer_enabled", [False, True])
async def test_skill_classifies_without_model_call_and_routes_from_skill_config(store, config, make_task, tmp_path, monkeypatch, reviewer_enabled):
    path = tmp_path / "review"
    path.mkdir()
    (path / "SKILL.md").write_text("---\nname: review\ndescription: review\n---\nbody\n")
    monkeypatch.setattr("taskboy.settings.SKILLS_ROOT", str(tmp_path))
    config.raw = {**RAW, "github": {"approved_repos": ["org/core", "org/risk-nextgen", "org/other"]}}
    config.reviewer.enabled = reviewer_enabled
    classifier = Classifier(store, config)
    classifier._call_model = AsyncMock(side_effect=AssertionError("model should not run"))
    task = make_task("/review https://github.com/org/core/pull/1 risk-nextgen")
    fields = await classifier.classify(task)
    classification = json.loads(fields["classification_json"])
    assert fields["task_type"] == "skill"
    assert fields["model_alias"] == "opus"
    assert fields["profile"] == "standard"
    assert fields.get("persona") == ("reviewer" if reviewer_enabled else None)
    assert classification["skill"] == "review"
    assert classification["skill_args"].startswith("https://github.com")
    assert classification["target_repos"] == ["org/core", "org/risk-nextgen"]
    classifier._call_model.assert_not_awaited()
    assert store.conn.execute("SELECT COUNT(*) FROM usage WHERE task_id = ?", (task.task_id,)).fetchone()[0] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("preclassified", [False, True])
async def test_pr_review_task_type_without_skill_does_not_get_blue_persona(store, config, make_task, preclassified):
    # A model-labeled task_type "pr_review" with no skill == "review" can be a misclassified
    # "address the review comments" request, which writes code and must stay on Red.
    config.reviewer.enabled = True
    classifier = make_classifier(store, config)
    review = {**CLASSIFICATION, "task_type": "pr_review", "complexity": "standard"}
    classifier._call_model = AsyncMock(return_value=(review, None))
    task = make_task(pre_classification=review if preclassified else None)

    fields = await classifier.classify(task)

    assert "persona" not in fields
    if preclassified:
        classifier._call_model.assert_not_awaited()
    else:
        classifier._call_model.assert_awaited_once()


@pytest.mark.asyncio
async def test_addressing_review_comments_stays_on_red(store, config, make_task):
    # Regression for operator feedback: "address the review comments on <pr url>" must not be
    # routed to Blue even if the classifier mislabels it as task_type "pr_review".
    config.reviewer.enabled = True
    classifier = make_classifier(store, config)
    review = {**CLASSIFICATION, "task_type": "pr_review", "complexity": "standard"}
    classifier._call_model = AsyncMock(return_value=(review, None))
    task = make_task("address the review comments on https://github.com/org/repo/pull/6")

    fields = await classifier.classify(task)

    assert "persona" not in fields


@pytest.mark.asyncio
async def test_blue_persona_is_not_stamped_when_disabled_or_not_a_review(store, config, make_task):
    review = {**CLASSIFICATION, "task_type": "pr_review", "complexity": "standard"}
    classifier = make_classifier(store, config)
    classifier._call_model = AsyncMock(return_value=(review, None))
    assert "persona" not in await classifier.classify(make_task())

    config.reviewer.enabled = True
    classifier._call_model = AsyncMock(return_value=(dict(CLASSIFICATION), None))
    assert "persona" not in await classifier.classify(make_task())


@pytest.mark.asyncio
async def test_unknown_skill_falls_through_to_model_classifier(store, config, make_task, tmp_path, monkeypatch):
    monkeypatch.setattr("taskboy.settings.SKILLS_ROOT", str(tmp_path))
    classifier = make_classifier(store, config)
    classifier._call_model = AsyncMock(return_value=(dict(CLASSIFICATION), None))
    await classifier.classify(make_task("/unknown args"))
    classifier._call_model.assert_awaited_once()


@pytest.mark.asyncio
async def test_preclassified_task_routes_and_audits_without_model_call(store, config, make_task):
    classifier = make_classifier(store, config)
    classifier._call_model = AsyncMock(side_effect=AssertionError("model should not run"))
    task = make_task(pre_classification=dict(CLASSIFICATION))

    fields = await classifier.classify(task)

    assert fields["model_alias"] == "haiku"
    classifier._call_model.assert_not_awaited()
    details = [json.loads(event["detail_json"]) for event in store.events_for(task.task_id) if event["kind"] == "classified"]
    assert details == [{**CLASSIFICATION, "fallback": False, "source": "triage"}]
    assert any(event["kind"] == "model_routing" for event in store.events_for(task.task_id))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "text",
    [
        "address all PR comments on your PRs in the taskboy repo",
        "Check all of your PRs in the taskboy repo, address review comments",
        "address the review comments on https://github.com/org/repo/pull/6",
        "review comments on PR #5, please resolve them",
    ],
)
async def test_address_review_comments_guard_forces_bug_fix(store, config, make_task, text):
    # Regression for issue #55: two 1-star feedback tasks on 2026-08-03 came from exactly this
    # misclassification on plural/no-URL phrasings, despite prompt guidance telling the model to
    # classify these as bug_fix. The override must be deterministic, not left to the model.
    classifier = make_classifier(store, config)
    review = {**CLASSIFICATION, "task_type": "pr_review", "complexity": "standard"}
    classifier._call_model = AsyncMock(return_value=(review, None))
    task = make_task(text)

    fields = await classifier.classify(task)

    assert fields["task_type"] == "bug_fix"
    assert fields["risk"] == "writes_code"
    classified = next(event for event in store.events_for(task.task_id) if event["kind"] == "classified")
    detail = json.loads(classified["detail_json"])
    assert detail["guard"] == "review-comments-override"
    assert detail["task_type"] == "bug_fix"


@pytest.mark.asyncio
async def test_address_review_comments_guard_audits_stored_triage_path(store, config, make_task):
    classifier = make_classifier(store, config)
    review = {**CLASSIFICATION, "task_type": "pr_review", "complexity": "standard"}
    classifier._call_model = AsyncMock(side_effect=AssertionError("model should not run"))
    task = make_task("address all PR comments on your PRs in the taskboy repo", pre_classification=review)

    fields = await classifier.classify(task)

    assert fields["task_type"] == "bug_fix"
    classifier._call_model.assert_not_awaited()
    classified = next(event for event in store.events_for(task.task_id) if event["kind"] == "classified")
    detail = json.loads(classified["detail_json"])
    assert detail["guard"] == "review-comments-override"
    assert detail["source"] == "triage"


@pytest.mark.asyncio
async def test_genuine_pr_review_request_is_not_overridden(store, config, make_task):
    # Acceptance check: a real "review this PR" request must classify pr_review untouched — the
    # guard only fires on an address/fix/respond-to/resolve verb, which "review" is not.
    classifier = make_classifier(store, config)
    review = {**CLASSIFICATION, "task_type": "pr_review", "complexity": "standard"}
    classifier._call_model = AsyncMock(return_value=(review, None))
    task = make_task("review this PR: https://github.com/org/repo/pull/1")

    fields = await classifier.classify(task)

    assert fields["task_type"] == "pr_review"
    classified = next(event for event in store.events_for(task.task_id) if event["kind"] == "classified")
    detail = json.loads(classified["detail_json"])
    assert "guard" not in detail


@pytest.mark.asyncio
async def test_invalid_stored_classification_falls_back_to_model(store, config, make_task):
    classifier = make_classifier(store, config)
    classifier._call_model = AsyncMock(return_value=(dict(CLASSIFICATION), None))
    task = make_task(pre_classification={"task_type": "question"})

    await classifier.classify(task)

    classifier._call_model.assert_awaited_once()


# -- classifier-selected effort (issue #67) ----------------------------------


@pytest.mark.asyncio
async def test_classifier_effort_is_persisted_on_the_task(store, config, make_task):
    classifier = make_classifier(store, config)
    classifier._call_model = AsyncMock(return_value=({**CLASSIFICATION, "effort": "high"}, None))
    fields = await classifier.classify(make_task())
    assert fields["effort"] == "high"


@pytest.mark.asyncio
async def test_classifier_auto_effort_persists_as_none(store, config, make_task):
    classifier = make_classifier(store, config)
    classifier._call_model = AsyncMock(return_value=({**CLASSIFICATION, "effort": "auto"}, None))
    fields = await classifier.classify(make_task())
    assert fields["effort"] is None


@pytest.mark.asyncio
async def test_classifier_missing_effort_persists_as_none(store, config, make_task):
    classifier = make_classifier(store, config)
    classifier._call_model = AsyncMock(return_value=(dict(CLASSIFICATION), None))
    fields = await classifier.classify(make_task())
    assert fields["effort"] is None


@pytest.mark.asyncio
async def test_skill_classification_never_sets_an_effort(store, config, make_task, tmp_path, monkeypatch):
    path = tmp_path / "review"
    path.mkdir()
    (path / "SKILL.md").write_text("---\nname: review\ndescription: review\n---\nbody\n")
    monkeypatch.setattr("taskboy.settings.SKILLS_ROOT", str(tmp_path))
    config.raw = {**RAW, "github": {"approved_repos": ["org/core"]}}
    classifier = Classifier(store, config)
    classifier._call_model = AsyncMock(side_effect=AssertionError("model should not run"))
    fields = await classifier.classify(make_task("/review https://github.com/org/core/pull/1"))
    assert fields["effort"] is None


def test_validate_classification_drops_unrecognized_effort():
    result = validate_classification({**CLASSIFICATION, "effort": "extreme"})
    assert "effort" not in result


def test_validate_classification_keeps_recognized_effort():
    result = validate_classification({**CLASSIFICATION, "effort": "xhigh"})
    assert result["effort"] == "xhigh"
