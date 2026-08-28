from taskboy.prompts import CLASSIFICATION_SCHEMA, TRIAGE_SCHEMA, classifier_prompt, dm_chat_prompt, quick_answer_prompt, task_prompt, triage_prompt


def test_classifier_prompt_includes_thread_context_only_when_present():
    without = classifier_prompt("fix it", [], [])
    with_context = classifier_prompt("fix it", [], [], "<@U1>: earlier decision")
    assert "Conversation in the Slack thread" not in without
    assert "Conversation in the Slack thread" in with_context
    assert "earlier decision" in with_context


def test_classifier_prompt_treats_close_and_delete_as_supported():
    prompt = classifier_prompt("close PR #4 and delete its branch", ["org/taskboy"], ["github"])
    assert 'classify these as "bug_fix", not "unsupported"' in prompt


def test_classifier_prompt_treats_review_comment_response_as_bug_fix():
    prompt = classifier_prompt("address the review comments on your PR #6", ["org/taskboy"], ["github"])
    assert "review comments on a pull request are code changes" in prompt
    assert 'not "pr_review"' in prompt


def test_classifier_prompt_self_repo_line_only_when_configured():
    without = classifier_prompt("fix yourself", ["org/taskboy"], ["github"])
    with_self_repo = classifier_prompt("fix yourself", ["org/taskboy"], ["github"], self_repo="org/taskboy", bot_name="Ruby")
    assert "own source code" not in without
    assert 'The repository "org/taskboy" is Ruby\'s own source code' in with_self_repo
    assert '"you", "your code", "yourself"' in with_self_repo


def test_task_prompt_includes_thread_and_precloned_repositories(make_task):
    task = make_task(thread_context="<@U1>: use the prior approach")
    prompt = task_prompt(task, None, [], github=True, thread_context=task.thread_context, cloned_repos=["org/service-a", "org/service-b"])
    assert "## Conversation in this Slack thread" in prompt
    assert "use the prior approach" in prompt
    assert "./service-a, ./service-b" in prompt


def test_task_prompt_self_repo_rules_only_when_present(make_task):
    task = make_task()
    without = task_prompt(task, None, [], github=True)
    with_self_repo = task_prompt(task, None, [], github=True, self_repo="org/taskboy")
    without_github = task_prompt(task, None, [], self_repo="org/taskboy")
    assert "service you are running as" not in without
    assert "service you are running as" not in without_github
    assert "org/taskboy is your own source code" in with_self_repo
    assert "never attempt to merge it yourself" in with_self_repo


def test_task_prompt_includes_skill_name_args_instructions_and_precedence(make_task):
    prompt = task_prompt(make_task("/review url"), None, [], skill={"name": "review", "args": "url", "instructions": "inspect simplicity"})
    assert "## Skill invocation" in prompt
    assert "Arguments: url" in prompt
    assert "### Skill: /review" in prompt
    assert "inspect simplicity" in prompt
    assert "take precedence over conflicting general guidance" in prompt


def test_task_prompt_includes_conventions_section_only_when_enabled(make_task):
    task = make_task()
    default_prompt = task_prompt(task, None, [])
    conventions_prompt = task_prompt(task, None, [], conventions=True)
    assert "## Engineering conventions" not in default_prompt
    assert "## Engineering conventions" in conventions_prompt
    assert "./CONVENTIONS.md" in conventions_prompt


def test_task_prompt_includes_pr_jira_link_and_progress_rules(make_task):
    prompt = task_prompt(make_task(), None, [], github=True, jira=True)
    assert "Include the pull request link in your `## Reply`" in prompt
    assert "## Jira work rules" in prompt
    assert "transition it to `In Progress` with `mcp__jira__transition_issue` before you begin implementation" in prompt
    assert "include the issue link in your `## Reply`" in prompt
    assert "Do not post a `report_progress` update announcing something you just created" in prompt
    assert "## Jira work rules" not in task_prompt(make_task(), None, [])


def test_task_prompt_includes_github_pre_push_and_review_comment_rules(make_task):
    prompt = task_prompt(make_task(), None, [], github=True, bot_name="Red")

    assert "Docker is available" in prompt
    assert "Before pushing code changes" in prompt
    assert "`make check` (lint, format, mypy, tests)" in prompt
    assert "reply to that comment with mcp__github__reply_to_pr_comment as Red" in prompt
    assert "mcp__github__resolve_pr_thread that you or Reviewer started" in prompt


def test_task_prompt_tells_red_not_to_delegate_but_not_blue(make_task):
    red_prompt = task_prompt(make_task(), None, [], github=True, bot_name="Red", other_bot_name="Blue", is_reviewer=False)
    blue_prompt = task_prompt(make_task(), None, [], github=True, bot_name="Blue", other_bot_name="Red", is_reviewer=True)

    assert "Do not request a Blue review or spawn Blue" in red_prompt
    assert "Do not request a Blue review or spawn Blue" not in blue_prompt


def test_task_prompt_uses_configured_names_instead_of_hardcoded_red_blue(make_task):
    """the two-persona names come from config (bot_name/other_bot_name), not literal "Red"/"Blue" checks."""
    prompt = task_prompt(make_task(), None, [], github=True, bot_name="Crimson", other_bot_name="Cyan", is_reviewer=False)

    assert "as Crimson" in prompt
    assert "Do not request a Cyan review or spawn Cyan" in prompt
    assert "that you or Cyan started" in prompt
    assert "Red" not in prompt
    assert "Blue" not in prompt

    blue_prompt = task_prompt(make_task(), None, [], github=True, bot_name="Cyan", other_bot_name="Crimson", is_reviewer=True)
    assert "Do not request" not in blue_prompt


def test_task_prompt_uses_full_report_reply_rule_for_report_style_task_types(make_task):
    for task_type in ("question", "investigation", "incident_diagnosis"):
        task = make_task()
        task.task_type = task_type
        prompt = task_prompt(task, None, [])
        assert "the deliverable is information, not a code" in prompt, task_type
        assert "TLDR at the top" in prompt, task_type
        assert 'never say "see below"' in prompt, task_type
        assert "`## Reply`: 2–6 human sentences" not in prompt, task_type


def test_task_prompt_keeps_short_reply_rule_for_non_report_task_types(make_task):
    task = make_task()
    task.task_type = "bug_fix"
    prompt = task_prompt(task, None, [])
    assert "`## Reply`: 2–6 human sentences" in prompt
    assert "the deliverable is information, not a code" not in prompt

    unclassified = make_task()
    assert unclassified.task_type is None
    assert "`## Reply`: 2–6 human sentences" in task_prompt(unclassified, None, [])


def test_personality_shapes_reply_but_not_internal_report(make_task):
    task = make_task()
    prompt = task_prompt(task, None, [], personality="Dry, concise, exact.")
    assert "## Personality" in prompt
    assert "Dry, concise, exact." in prompt
    assert "`## Reply`: 2–6 human sentences" in prompt
    assert "internal debug log" in prompt
    assert "## Personality" not in task_prompt(task, None, [])
    quick = quick_answer_prompt("what is 429?", "Red", None, personality="Dry, concise, exact.")
    assert "Personality for the answer text" in quick
    dm = dm_chat_prompt("and then?", "Red", "Red: previous answer", personality="Dry, concise, exact.")
    assert "Recent direct-message conversation" in dm
    assert "Red: previous answer" in dm
    assert "never claim that you started one" in dm


def test_triage_prompt_combines_answer_context_and_classification_scope():
    prompt = triage_prompt(
        "fix your own retry bug",
        "Red",
        "task t20260101-deadbeef is completed",
        ["org/taskboy"],
        ["github", "jira"],
        thread_context="<@U2>: it happens on retries",
        personality="Dry and exact.",
        self_repo="org/taskboy",
    )
    assert "fast triage path" in prompt
    assert "task t20260101-deadbeef is completed" in prompt
    assert "it happens on retries" in prompt
    assert "org/taskboy" in prompt
    assert "own source code" in prompt
    assert 'classify these as "bug_fix", not "unsupported"' in prompt
    assert "review comments on a pull request are code changes" in prompt
    assert 'Greetings and small talk (e.g. "hi", "thanks", "how are you") also get action "answer"' in prompt
    assert "never classify them" in prompt
    assert set(TRIAGE_SCHEMA["required"]) == {"action", "answer"}


def test_triage_schema_anyof_requires_classification_fields_only_on_classify_branch():
    assert set(TRIAGE_SCHEMA["required"]) == {"action", "answer"}  # top-level required is unchanged
    branches = TRIAGE_SCHEMA["anyOf"]
    assert len(branches) == 2

    answer_branch = next(b for b in branches if b["properties"]["action"]["const"] == "answer")
    assert set(answer_branch["required"]) == {"action", "answer"}

    classify_branch = next(b for b in branches if b["properties"]["action"]["const"] == "classify")
    assert set(classify_branch["required"]) == {"action", "answer", *CLASSIFICATION_SCHEMA["required"]}


def test_task_prompt_includes_answered_questions_only_when_present(make_task):
    task = make_task()
    bare = task_prompt(task, None, [])
    assert "## Answers from the requester" not in bare
    assert "ask_questions" in bare  # the tool contract is always taught
    prompt = task_prompt(task, None, [], answered_questions=[{"questions": "1. Which env?", "answer_text": "1. staging"}])
    assert "## Answers from the requester" in prompt
    assert "1. Which env?" in prompt
    assert "1. staging" in prompt
