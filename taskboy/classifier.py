"""task classification: one small structured-output model call, then config-driven routing (MOD-001/002).

failures never guess silently: one retry, then the fixed fallback classification, always audited (MOD-005/009).
"""

import json
import logging
import re

from taskboy import settings, skills
from taskboy.config import Config, ConfigError, Role, role_for
from taskboy.llm import _extract_json, extract_usage, structured_call  # noqa: F401  (extract_usage re-exported for callers/tests)
from taskboy.models import EFFORT_LEVELS, Task
from taskboy.prompts import CLASSIFICATION_SCHEMA, classifier_prompt, trim_context
from taskboy.router import RoleRefusal, RoutingDecision, route, route_skill
from taskboy.store import Store

logger = logging.getLogger("taskboy.classifier")

FALLBACK_CLASSIFICATION = {
    "task_type": "investigation",
    "complexity": "standard",
    "risk": "read_only",
    "expected_duration": "under_hour",
    "required_integrations": [],
    "target_repos": [],
    "jira_keys": [],
}

# "effort" is optional in CLASSIFICATION_SCHEMA (never a hard failure), but a stray/unrecognized value from a
# stored triage classification or a lenient model call should be dropped rather than reach the router (MOD-009)
VALID_EFFORT_VALUES = set(EFFORT_LEVELS) | {"auto"}

# issue #55: prompt guidance (classification_guidance() in prompts.py) tells the model that
# "address/fix/respond to/resolve review comments on a PR" is bug_fix, not pr_review — but a
# haiku-tier classifier following prose isn't reliable on plural/no-URL phrasings, so this is
# re-checked deterministically after the model call, before routing, and always audited.
# matches either order — "address ... comments" and "comments ... resolve them" both trigger,
# since same-intent requests routinely put the verb after the noun (issue #55 follow-up).
_REVIEW_COMMENTS_VERB_RE = re.compile(
    r"\b(address|fix|respond to|resolve)\b.{0,40}\b(review )?comments\b" r"|\b(review )?comments\b.{0,40}\b(address|fix|respond to|resolve)\b",
    re.IGNORECASE | re.DOTALL,
)
_PR_REFERENCE_RE = re.compile(r"\bPRs?\b|\bpull requests?\b|/pull/\d+", re.IGNORECASE)


def _apply_review_comments_guard(classification: dict, request_text: str) -> tuple[dict, str | None]:
    """returns the (possibly overridden) classification and a guard name to audit, or None if untouched."""
    if classification.get("task_type") != "pr_review":
        return classification, None
    if not (_REVIEW_COMMENTS_VERB_RE.search(request_text) and _PR_REFERENCE_RE.search(request_text)):
        return classification, None
    overridden = {**classification, "task_type": "bug_fix", "risk": "writes_code"}
    return overridden, "review-comments-override"


async def stub_classify(task: Task) -> dict:
    """dev/echo-mode classifier: no model call, fixed fields."""
    return {"task_type": "investigation", "complexity": "standard", "routing_rationale": "stub classifier"}


class Classifier:
    def __init__(self, store: Store, config: Config):
        self.store = store
        self.config = config
        tier = (config.raw.get("classifier") or {}).get("tier", "haiku")
        models = config.raw.get("models") or {}
        if tier not in models:
            raise ConfigError(f"classifier.tier {tier!r} is not in the model catalog")
        self.model_alias = tier
        self.model_id = str(models[tier]["id"])

    async def classify(self, task: Task) -> dict:
        return await self._classify(task)

    async def _classify(self, task: Task) -> dict:
        """returns the task fields set when the task moves received -> queued."""
        role = role_for(self.config.roles, task.slack_user_id)
        if role is None:
            raise RoleRefusal(f"user {task.slack_user_id!r} has no configured role")
        invocation = skills.parse_invocation(task.request_text)
        if invocation and invocation[0] in skills.available(settings.SKILLS_ROOT):
            name, args = invocation
            skill_config = self.config.raw.get("skills") or {}
            loaded = skills.load(settings.SKILLS_ROOT, name)
            profile = str(loaded.profile or skill_config.get("profile", "standard"))
            approved_repos = (self.config.raw.get("github") or {}).get("approved_repos") or []
            if role.repos is not None:
                approved_repos = [repo for repo in approved_repos if repo in role.repos]
            target_repos = []
            for repo in approved_repos:
                short_name = repo.split("/", 1)[-1]
                if re.search(rf"(?<![a-z0-9_-]){re.escape(short_name)}(?![a-z0-9_-])", args, flags=re.IGNORECASE):
                    target_repos.append(repo)
            classification: dict = {
                "task_type": "skill",
                "complexity": "standard",
                "risk": "read_only" if profile == "read_only" else "writes_code_and_jira",
                "expected_duration": "hours",
                "required_integrations": [],
                "target_repos": target_repos,
                "jira_keys": [],
                "skill": name,
                "skill_args": args,
                "skill_internal_tools": loaded.internal_tools,
            }
            decision = route_skill(task.model_override, self.config.raw, role=role, skill_tier=loaded.model, skill_profile=loaded.profile)
            self.store.add_event(task.task_id, "classified", {**classification, "fallback": False})
            self.store.add_event(task.task_id, "model_routing", {"model_alias": decision.model_alias, "model_id": decision.model_id, "profile": decision.profile, "rationale": decision.rationale})
            return _task_fields(classification, decision, _review_persona(classification, self.config.reviewer.enabled))
        if task.classification_json:
            try:
                stored = json.loads(task.classification_json)
                classification = validate_classification(stored)
            except (TypeError, ValueError, json.JSONDecodeError):
                pass
            else:
                classification, guard = _apply_review_comments_guard(classification, task.request_text)
                decision = route(classification["task_type"], classification["complexity"], task.model_override, self.config.raw, role=role, classifier_effort=classification.get("effort"))
                detail = {**classification, "fallback": False, "source": "triage"}
                if guard:
                    detail["guard"] = guard
                self.store.add_event(task.task_id, "classified", detail)
                self.store.add_event(task.task_id, "model_routing", {"model_alias": decision.model_alias, "model_id": decision.model_id, "profile": decision.profile, "rationale": decision.rationale})
                return _task_fields(classification, decision, _review_persona(classification, self.config.reviewer.enabled))
        classification, usage, fell_back = await self._classify_once(task, role)
        classification, guard = _apply_review_comments_guard(classification, task.request_text)
        decision = route(classification["task_type"], classification["complexity"], task.model_override, self.config.raw, role=role, classifier_effort=classification.get("effort"))
        detail = {**classification, "fallback": fell_back}
        if guard:
            detail["guard"] = guard
        self.store.add_event(task.task_id, "classified", detail)
        self.store.add_event(task.task_id, "model_routing", {"model_alias": decision.model_alias, "model_id": decision.model_id, "profile": decision.profile, "rationale": decision.rationale})
        if usage:
            self.store.add_usage(task.task_id, "classifier", self.model_id, **usage)
        return _task_fields(classification, decision, _review_persona(classification, self.config.reviewer.enabled))

    async def _classify_once(self, task: Task, role: Role | None = None) -> tuple[dict, dict | None, bool]:
        github = self.config.raw.get("github") or {}
        approved_repos = github.get("approved_repos") or []
        if role is not None and role.repos is not None:
            approved_repos = [repo for repo in approved_repos if repo in role.repos]
        self_repo = str(github.get("self_repo") or "")
        prompt = classifier_prompt(
            task.request_text,
            approved_repos,
            ["github", "aws", "sentry", "jira", "confluence"],
            trim_context(task.thread_context),
            self_repo=self_repo if self_repo in approved_repos else None,
            bot_name=self.config.agent_name,
        )
        # structured_call already retries once internally, so one call here bounds the worst case at two model calls before fallback
        try:
            classification, usage = await self._call_model(prompt)
            return classification, usage, False
        except Exception as e:
            logger.warning("classifier call failed for %s: %s", task.task_id, e)
            self.store.add_event(task.task_id, "classifier_failed", {"what": "classifier", "error": str(e)})
            self.store.add_error("classifier", type(e).__name__, str(e), task_id=task.task_id)
        return dict(FALLBACK_CLASSIFICATION), None, True

    async def _call_model(self, prompt: str) -> tuple[dict, dict | None]:
        """the per-call API seam — patched in unit tests."""
        classification, usage = await structured_call(self.model_id, prompt, CLASSIFICATION_SCHEMA)
        return validate_classification(classification), usage


def _review_persona(classification: dict, reviewer_enabled: bool) -> str | None:
    """only a genuine /review skill invocation runs as the reviewer: a model-labeled pr_review can be a
    misclassified "address the review comments" request, which writes code and must stay with the main agent."""
    if reviewer_enabled and classification.get("skill") == "review":
        return "reviewer"
    return None


def _task_fields(classification: dict, decision: RoutingDecision, persona: str | None = None) -> dict:
    fields = {
        "classification_json": json.dumps(classification),
        "task_type": classification["task_type"],
        "complexity": classification["complexity"],
        "risk": classification["risk"],
        "model_alias": decision.model_alias,
        "model_id": decision.model_id,
        "profile": decision.profile,
        "routing_rationale": decision.rationale,
        "max_budget_usd": decision.max_budget_usd,
        "max_turns": decision.max_turns,
        "max_runtime_minutes": decision.max_runtime_minutes,
        "effort": decision.effort,
    }
    if persona is not None:
        fields["persona"] = persona
    return fields


def validate_classification(candidate) -> dict:
    """lenient: only task_type/complexity are fatal (no safe default); other missing fields fill from FALLBACK_CLASSIFICATION."""
    if not isinstance(candidate, dict):
        raise ValueError("classifier returned no structured output")
    missing = [key for key in CLASSIFICATION_SCHEMA["required"] if key not in candidate]
    if "task_type" in missing or "complexity" in missing:
        raise ValueError(f"classification missing fields: {missing}")
    if missing:
        logger.info("classification filled missing fields %s with defaults", missing)
    filled = dict(candidate)
    for key in missing:
        filled[key] = FALLBACK_CLASSIFICATION[key]
    if "effort" in filled and filled["effort"] not in VALID_EFFORT_VALUES:
        filled.pop("effort")
    return filled


def parse_classification(message) -> dict:
    """tolerant of sdk result shapes: structured_output attr, json in result, or json embedded in prose."""
    candidate = getattr(message, "structured_output", None)
    if candidate is None:
        result = getattr(message, "result", None)
        candidate = _extract_json(result) if isinstance(result, str) else result
    return validate_classification(candidate)
