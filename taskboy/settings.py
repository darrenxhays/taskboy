"""deployment-level settings from the environment. operator policy lives in config.yaml; credentials arrive via secrets manager in phase 2."""

import os

from taskboy import assets

ENVIRONMENT = os.environ.get("ENVIRONMENT", "local")
DB_PATH = os.environ.get("TASKBOY_DB_PATH", "local/taskboy.db")
CONFIG_PATH = os.environ.get("TASKBOY_CONFIG_PATH", "config/config.yaml")
WORKSPACES_ROOT = os.environ.get("TASKBOY_WORKSPACES_ROOT", "local/workspaces")
REPOS_ROOT = os.environ.get("TASKBOY_REPOS_ROOT", "local/repos")
MEMORY_ROOT = os.environ.get("TASKBOY_MEMORY_ROOT", "local/memory")
SKILLS_ROOT = os.environ.get("TASKBOY_SKILLS_ROOT", "skills")
UI_DIST = os.environ.get("TASKBOY_UI_DIST", str(assets.UI_DIST))
REGION = os.environ.get("AWS_REGION", "us-east-1")
SECRETS_NAME = os.environ.get("TASKBOY_SECRETS_NAME", f"TASKBOY_SECRETS_{ENVIRONMENT.upper()}")
# absolute: the cred helper runs with cwd = the workspace, not the service's WorkingDirectory
BROKER_SOCKET = os.path.abspath(os.environ.get("TASKBOY_BROKER_SOCKET", "local/broker.sock"))
REVIEWER_BROKER_SOCKET = os.path.abspath(os.environ.get("TASKBOY_REVIEWER_BROKER_SOCKET", "local/broker-reviewer.sock"))
GIT_CRED_HELPER = os.path.abspath(os.environ.get("TASKBOY_GIT_CRED_HELPER", str(assets.GIT_CRED_HELPER)))
