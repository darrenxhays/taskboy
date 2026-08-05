"""deployment-level settings from the environment. operator policy lives in config.yaml; credentials arrive via secrets manager in phase 2."""

import os

ENVIRONMENT = os.environ.get("ENVIRONMENT", "local")
DB_PATH = os.environ.get("AGENT_HARNESS_DB_PATH", "local/agent-harness.db")
CONFIG_PATH = os.environ.get("AGENT_HARNESS_CONFIG_PATH", "config/config.yaml")
WORKSPACES_ROOT = os.environ.get("AGENT_HARNESS_WORKSPACES_ROOT", "local/workspaces")
REPOS_ROOT = os.environ.get("AGENT_HARNESS_REPOS_ROOT", "local/repos")
MEMORY_ROOT = os.environ.get("AGENT_HARNESS_MEMORY_ROOT", "local/memory")
SKILLS_ROOT = os.environ.get("AGENT_HARNESS_SKILLS_ROOT", "skills")
UI_DIST = os.environ.get("AGENT_HARNESS_UI_DIST", "ui/dist")
REGION = os.environ.get("AWS_REGION", "us-east-1")
SECRETS_NAME = os.environ.get("AGENT_HARNESS_SECRETS_NAME", f"AGENT_HARNESS_SECRETS_{ENVIRONMENT.upper()}")
BROKER_SOCKET = os.environ.get("AGENT_HARNESS_BROKER_SOCKET", "local/broker.sock")
REVIEWER_BROKER_SOCKET = os.environ.get("AGENT_HARNESS_REVIEWER_BROKER_SOCKET", "local/broker-reviewer.sock")
GIT_CRED_HELPER = os.environ.get("AGENT_HARNESS_GIT_CRED_HELPER", os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "deploy", "git-cred-helper.py"))
