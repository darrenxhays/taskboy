"""mission control: the operator web dashboard, served from the orchestrator process."""

from agent_harness.dashboard.app import create_app

__all__ = ["create_app"]
