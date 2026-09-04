from pathlib import Path

import pytest
import yaml

from taskboy.config import ConfigError, Role
from taskboy.router import RoleRefusal, route, route_skill

RAW = {
    "models": {
        "haiku": {"id": "claude-haiku-4-5", "fallbacks": ["sonnet"]},
        "sonnet": {"id": "claude-sonnet-5", "fallbacks": ["opus"]},
        "opus": {"id": "claude-opus-5", "fallbacks": []},
        "fable": {"id": "claude-fable-5-1", "fallbacks": ["opus"]},
    },
    "routing": {
        "rules": [
            {"name": "cheap-questions", "match": {"task_type": ["question", "investigation"], "complexity": ["trivial"]}, "tier": "haiku", "profile": "read_only"},
            {"name": "critical-work", "match": {"complexity": ["critical"]}, "tier": "fable", "profile": "deep"},
            {"name": "complex-work", "match": {"complexity": ["complex"]}, "tier": "opus", "profile": "deep"},
            {"name": "standard-eng", "match": {"task_type": ["bug_fix", "feature"]}, "tier": "sonnet", "profile": "standard"},
        ],
        "default": {"tier": "sonnet", "profile": "read_only"},
    },
    "profiles": {
        "read_only": {"max_budget_usd": 2.0, "max_turns": 60, "max_runtime_minutes": 30},
        "standard": {"max_budget_usd": 12.0, "max_turns": 400, "max_runtime_minutes": 240},
        "deep": {"max_budget_usd": 20.0, "max_turns": 400, "max_runtime_minutes": 240},
    },
}


def test_first_matching_rule_wins_and_fields_are_anded():
    decision = route("question", "trivial", None, RAW)
    assert decision.model_alias == "haiku"
    assert decision.profile == "read_only"
    assert decision.rationale == "rule: cheap-questions"
    # complexity mismatch: falls through cheap-questions to complex-work? no — not complex either -> default
    decision = route("question", "standard", None, RAW)
    assert decision.rationale == "rule: default"
    assert decision.model_alias == "sonnet"


def test_complexity_alone_can_route_to_opus():
    decision = route("bug_fix", "complex", None, RAW)
    assert decision.model_alias == "opus"
    assert decision.profile == "deep"
    assert decision.max_budget_usd == 20.0


def test_critical_routes_to_fable_and_deep():
    decision = route("bug_fix", "critical", None, RAW)
    assert decision.model_alias == "fable"
    assert decision.model_id == "claude-fable-5-1"
    assert decision.fallback_chain == ["fable", "opus"]
    assert decision.profile == "deep"
    assert decision.max_budget_usd == 20.0


def test_fallback_chain_is_preferred_first_and_configured_only():
    decision = route("question", "trivial", None, RAW)
    assert decision.fallback_chain == ["haiku", "sonnet", "opus"]
    decision = route("bug_fix", "complex", None, RAW)
    assert decision.fallback_chain == ["opus"]


def test_override_picks_from_catalog_and_is_audited():
    decision = route("question", "trivial", "opus", RAW)
    assert decision.model_alias == "opus"
    assert "overridden to opus" in decision.rationale
    fable = route("question", "trivial", "fable", RAW)
    assert fable.fallback_chain == ["fable", "opus"]
    with pytest.raises(ConfigError, match="not in the configured model catalog"):
        route("question", "trivial", "gpt-9", RAW)


def test_role_enforces_profile_override_and_budget_with_rationale():
    developer = Role("developer", ["U1"], ["read_only", "standard", "deep"], False, 12.0, None)
    decision = route("feature", "complex", None, RAW, role=developer)
    assert decision.max_budget_usd == 12.0
    assert "budget capped at $12 by role developer" in decision.rationale
    assert decision.rationale.endswith("role: developer")
    with pytest.raises(RoleRefusal, match="model overrides"):
        route("question", "trivial", "opus", RAW, role=developer)

    readonly = Role("readonly", ["U2"], ["read_only"], False, 2.0, None)
    with pytest.raises(RoleRefusal, match="deep execution profile"):
        route("feature", "complex", None, RAW, role=readonly)


def test_unknown_tier_or_profile_fails_loudly():
    broken = {**RAW, "routing": {"rules": [], "default": {"tier": "mystery", "profile": "read_only"}}}
    with pytest.raises(ConfigError, match="mystery"):
        route(None, None, None, broken)
    broken = {**RAW, "routing": {"rules": [], "default": {"tier": "sonnet", "profile": "mystery"}}}
    with pytest.raises(ConfigError, match="mystery"):
        route(None, None, None, broken)


def test_fallback_cycles_terminate():
    cyclic = {
        **RAW,
        "models": {
            "haiku": {"id": "h", "fallbacks": ["sonnet"]},
            "sonnet": {"id": "s", "fallbacks": ["haiku"]},
        },
        "routing": {"rules": [], "default": {"tier": "haiku", "profile": "read_only"}},
    }
    decision = route(None, None, None, cyclic)
    assert decision.fallback_chain == ["haiku", "sonnet"]


def test_example_config_routes():
    raw = yaml.safe_load((Path(__file__).parents[1] / "taskboy" / "templates" / "config.example.yaml").read_text())
    trivial = route("question", "trivial", None, raw)
    complex_work = route("feature", "complex", None, raw)
    assert trivial.model_alias == "haiku"
    assert complex_work.model_alias == "opus"
    assert complex_work.profile == "deep"
    critical = route("bug_fix", "critical", None, raw)
    assert critical.fallback_chain == ["fable", "opus"]
    assert trivial.max_budget_usd < complex_work.max_budget_usd


def test_route_skill_defaults_and_config_override():
    decision = route_skill(None, RAW)
    assert decision.model_alias == "opus"
    assert decision.profile == "standard"
    assert decision.rationale == "skill"
    configured = {**RAW, "skills": {"tier": "sonnet", "profile": "read_only"}}
    assert route_skill(None, configured).model_alias == "sonnet"
    assert route_skill(None, configured).profile == "read_only"


def test_route_skill_enforces_override_profile_and_budget():
    admin = Role("admin", ["U1"], ["read_only", "standard", "deep"], True, None, None)
    decision = route_skill("haiku", RAW, role=admin)
    assert decision.model_alias == "haiku"
    assert "overridden to haiku" in decision.rationale
    developer = Role("developer", ["U2"], ["standard"], False, 5.0, None)
    capped = route_skill(None, RAW, role=developer)
    assert capped.max_budget_usd == 5.0
    assert "budget capped" in capped.rationale
    with pytest.raises(RoleRefusal, match="model overrides"):
        route_skill("haiku", RAW, role=developer)
    readonly = Role("readonly", ["U3"], ["read_only"], False, 2.0, None)
    with pytest.raises(RoleRefusal, match="standard execution profile"):
        route_skill(None, RAW, role=readonly)


def test_route_skill_rejects_missing_catalog_entries():
    with pytest.raises(ConfigError, match="mystery"):
        route_skill(None, {**RAW, "skills": {"tier": "mystery", "profile": "standard"}})


def test_route_skill_uses_config_default_when_skill_declares_nothing():
    decision = route_skill(None, {**RAW, "skills": {"tier": "opus", "profile": "standard"}})
    assert decision.model_alias == "opus" and decision.profile == "standard" and decision.rationale == "skill"


def test_route_skill_honors_skill_declared_model_and_profile():
    decision = route_skill(None, {**RAW, "skills": {"tier": "opus", "profile": "standard"}}, skill_tier="fable", skill_profile="deep")
    assert decision.model_alias == "fable" and decision.profile == "deep"
    assert decision.rationale == "skill:fable"


def test_route_carries_classifier_effort_through():
    decision = route("question", "trivial", None, RAW, classifier_effort="high")
    assert decision.effort == "high"


def test_route_auto_effort_collapses_to_none():
    decision = route("question", "trivial", None, RAW, classifier_effort="auto")
    assert decision.effort is None


def test_route_no_effort_opinion_is_none():
    decision = route("question", "trivial", None, RAW)
    assert decision.effort is None


def test_route_skill_never_sets_an_effort():
    assert route_skill(None, RAW).effort is None


def test_user_override_still_wins_over_skill_declared_model():
    role = Role(name="admin", members=["U1"], allowed_profiles=["read_only", "standard", "deep"], model_override=True, max_budget_usd=None, repos=None)
    decision = route_skill("opus", RAW, role=role, skill_tier="fable", skill_profile="deep")
    assert decision.model_alias == "opus"
    assert "overridden to opus" in decision.rationale
