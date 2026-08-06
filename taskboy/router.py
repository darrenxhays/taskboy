"""model routing: pure function from (classification fields, override, config) to a routing decision.

rules live in config.yaml, never in code (MOD-002/006/007). first matching rule wins;
fields within a rule are ANDed, values within a field are ORed. no code path can pick
a model outside the configured catalog (MOD-009).
"""

from dataclasses import dataclass

from taskboy.config import ConfigError, Role


class RoleRefusal(Exception):
    """a valid request selected permissions the caller's role does not grant."""


@dataclass
class RoutingDecision:
    model_alias: str
    model_id: str
    fallback_chain: list[str]  # aliases to try in order if the model is unavailable, preferred first
    profile: str
    max_budget_usd: float
    max_turns: int
    max_runtime_minutes: int
    rationale: str  # matched rule name + override note, recorded on the task (MOD-005)
    effort: str | None = None  # the classifier's pick, carried through to be persisted (issue #67); "auto" collapses to None


def route(task_type: str | None, complexity: str | None, model_override: str | None, raw_config: dict, *, role: Role | None = None, classifier_effort: str | None = None) -> RoutingDecision:
    models = raw_config.get("models") or {}
    routing = raw_config.get("routing") or {}
    profiles = raw_config.get("profiles") or {}
    if not models or not routing or not profiles:
        raise ConfigError("config needs models, routing, and profiles sections to route tasks")

    rule_name, tier, profile_name = _match_rule(task_type, complexity, routing)
    rationale = f"rule: {rule_name}"
    if model_override:
        if role is not None and not role.model_override:
            raise RoleRefusal(f"your {role.name} role does not allow model overrides")
        if model_override not in models:
            # an override may only pick from the catalog — never an unapproved model (MOD-008/009)
            raise ConfigError(f"model override {model_override!r} is not in the configured model catalog")
        tier = model_override
        rationale = f"rule: {rule_name}, overridden to {model_override}"

    if tier not in models:
        raise ConfigError(f"routing selected tier {tier!r} which is not in the model catalog")
    if profile_name not in profiles:
        raise ConfigError(f"routing selected profile {profile_name!r} which is not configured")
    if role is not None and profile_name not in role.allowed_profiles:
        raise RoleRefusal(f"your {role.name} role does not allow the {profile_name} execution profile required by this request")

    profile = profiles[profile_name]
    max_budget_usd = float(profile["max_budget_usd"])
    if role is not None:
        if role.max_budget_usd is not None and role.max_budget_usd < max_budget_usd:
            max_budget_usd = role.max_budget_usd
            rationale += f", budget capped at ${max_budget_usd:g} by role {role.name}"
        rationale += f", role: {role.name}"
    return RoutingDecision(
        model_alias=tier,
        model_id=str(models[tier]["id"]),
        fallback_chain=fallback_chain(tier, models),
        profile=profile_name,
        max_budget_usd=max_budget_usd,
        max_turns=int(profile["max_turns"]),
        max_runtime_minutes=int(profile["max_runtime_minutes"]),
        rationale=rationale,
        effort=classifier_effort if classifier_effort not in (None, "auto") else None,
    )


def route_skill(model_override: str | None, raw_config: dict, *, role: Role | None = None, skill_tier: str | None = None, skill_profile: str | None = None) -> RoutingDecision:
    models = raw_config.get("models") or {}
    profiles = raw_config.get("profiles") or {}
    if not models or not profiles:
        raise ConfigError("config needs models and profiles sections to route skills")
    skill_config = raw_config.get("skills") or {}
    # a skill may declare its own model/profile in frontmatter (e.g. discovery runs on fable); the config
    # default fills in otherwise. a user's explicit model_override still wins over both, below.
    tier = str(skill_tier or skill_config.get("tier", "opus"))
    profile_name = str(skill_profile or skill_config.get("profile", "standard"))
    rationale = f"skill:{skill_tier}" if skill_tier else "skill"
    if model_override:
        if role is not None and not role.model_override:
            raise RoleRefusal(f"your {role.name} role does not allow model overrides")
        if model_override not in models:
            raise ConfigError(f"model override {model_override!r} is not in the configured model catalog")
        tier = model_override
        rationale = f"skill, overridden to {model_override}"
    if tier not in models:
        raise ConfigError(f"skills selected tier {tier!r} which is not in the model catalog")
    if profile_name not in profiles:
        raise ConfigError(f"skills selected profile {profile_name!r} which is not configured")
    if role is not None and profile_name not in role.allowed_profiles:
        raise RoleRefusal(f"your {role.name} role does not allow the {profile_name} execution profile required by this request")
    profile = profiles[profile_name]
    max_budget_usd = float(profile["max_budget_usd"])
    if role is not None:
        if role.max_budget_usd is not None and role.max_budget_usd < max_budget_usd:
            max_budget_usd = role.max_budget_usd
            rationale += f", budget capped at ${max_budget_usd:g} by role {role.name}"
        rationale += f", role: {role.name}"
    return RoutingDecision(
        model_alias=tier,
        model_id=str(models[tier]["id"]),
        fallback_chain=fallback_chain(tier, models),
        profile=profile_name,
        max_budget_usd=max_budget_usd,
        max_turns=int(profile["max_turns"]),
        max_runtime_minutes=int(profile["max_runtime_minutes"]),
        rationale=rationale,
    )


def _match_rule(task_type: str | None, complexity: str | None, routing: dict) -> tuple[str, str, str]:
    fields = {"task_type": task_type, "complexity": complexity}
    for rule in routing.get("rules") or []:
        match = rule.get("match") or {}
        if all(fields.get(field) in allowed for field, allowed in match.items()):
            return str(rule["name"]), str(rule["tier"]), str(rule["profile"])
    default = routing.get("default")
    if not default:
        raise ConfigError("routing has no matching rule and no default")
    return "default", str(default["tier"]), str(default["profile"])


def fallback_chain(tier: str, models: dict) -> list[str]:
    """preferred alias first, then the configured fallbacks in order; cycles stop rather than loop (MOD-009)."""
    if tier not in models:
        raise ConfigError(f"model alias {tier!r} is not in the model catalog")
    chain = [tier]
    current = tier
    while True:
        next_aliases = models[current].get("fallbacks") or []
        if not next_aliases:
            return chain
        nxt = next_aliases[0]
        if nxt not in models:
            raise ConfigError(f"model {current!r} lists unknown fallback {nxt!r}")
        if nxt in chain:
            return chain  # cycle — stop rather than loop
        chain.append(nxt)
        current = nxt
