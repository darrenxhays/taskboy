"""service entrypoint: wire store + orchestrator (+ slack when configured), handle signals, run until stopped."""

import asyncio
import logging
import os
import signal
import sys
import traceback
from typing import Any

from taskboy import repocache, settings, workspace
from taskboy.classifier import Classifier, stub_classify
from taskboy.config import ConfigError, load_config, role_for
from taskboy.notify import StdoutNotifier
from taskboy.orchestrator import Orchestrator
from taskboy.runner import ClaudeRunner, EchoRunner
from taskboy.store import Store

logger = logging.getLogger("taskboy")


def should_start_review_poller(config, broker) -> bool:
    review_requests = (config.raw.get("github") or {}).get("review_requests") or {}
    if config.runner != "claude" or broker is None or not review_requests.get("enabled", False):
        return False
    if role_for(config.roles, "github") is None:
        logger.warning("github review-request polling is enabled but user 'github' has no configured role — poller disabled")
        return False
    return True


async def amain() -> None:
    config = load_config(settings.CONFIG_PATH)
    store = Store(settings.DB_PATH)

    secrets = None
    if config.slack.enabled or config.runner == "claude" or config.dashboard.enabled:
        from taskboy.secrets import load_secrets

        secrets = load_secrets()
        if secrets.claude_oauth_token and not os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"):
            os.environ["CLAUDE_CODE_OAUTH_TOKEN"] = secrets.claude_oauth_token  # the sdk's cli subprocesses read it from the process env

    notifier: Any  # duck-typed interface, see notify.py
    handler = None
    slack_client = None
    if config.slack.enabled:
        from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler

        from taskboy import slack

        assert secrets is not None  # loaded above whenever slack is enabled
        if not (secrets.slack_bot_token and secrets.slack_app_token):
            raise ConfigError("slack.team_id is set but SLACK_BOT_TOKEN / SLACK_APP_TOKEN are not in the environment")
        app, notifier = await slack.build(store, config, secrets.slack_bot_token)
        slack_client = app.client
        handler = AsyncSocketModeHandler(app, secrets.slack_app_token)
        logger.info("slack intake enabled (team=%s)", config.slack.team_id)
    else:
        notifier = StdoutNotifier()
        logger.info("slack intake disabled (slack.team_id not set) — use `taskboy inject`")

    broker = None
    reviewer_broker = None
    if config.runner == "claude":
        from taskboy.broker import CredentialBroker

        if secrets is not None and secrets.github_enabled:
            broker = CredentialBroker(secrets.github_app_id, secrets.github_installation_id, secrets.github_app_private_key, settings.BROKER_SOCKET, settings.GIT_CRED_HELPER)
            await broker.start()
            try:
                await broker.verify((config.raw.get("github") or {}).get("approved_repos") or [])
                logger.info("credential broker started, github app credentials verified (socket=%s)", settings.BROKER_SOCKET)
            except Exception as e:
                logger.warning("github app verification FAILED — git/PR operations will fail until fixed: %s", e)
        else:
            logger.info("github credentials not configured — sessions run without git push auth")
        if config.reviewer.enabled:
            if secrets is not None and secrets.reviewer_github_enabled:
                candidate = CredentialBroker(secrets.reviewer_github_app_id, secrets.reviewer_github_installation_id, secrets.reviewer_github_app_private_key, settings.REVIEWER_BROKER_SOCKET, settings.GIT_CRED_HELPER)
                try:
                    await candidate.start()
                    await candidate.verify((config.raw.get("github") or {}).get("approved_repos") or [])
                except Exception as e:
                    logger.warning("reviewer github app verification FAILED — reviewer GitHub review tasks are disabled: %s", e)
                    await candidate.stop()
                else:
                    reviewer_broker = candidate
                    logger.info("reviewer credential broker started, github app credentials verified (socket=%s)", settings.REVIEWER_BROKER_SOCKET)
            else:
                logger.warning("reviewer is enabled but the reviewer's GitHub identity is not configured — reviewer GitHub review tasks are disabled")
        classify = Classifier(store, config).classify
        run = ClaudeRunner(store, config, settings.WORKSPACES_ROOT, settings.MEMORY_ROOT, progress=notifier.progress, broker=broker, secrets=secrets, slack_client=slack_client, debug=getattr(notifier, "debug", None), reviewer_broker=reviewer_broker).run
        assert secrets is not None
        logger.info(
            "session integrations: github=%s jira=%s confluence=%s sentry=%s aws=%s slack=%s",
            broker is not None,
            secrets.jira_enabled and bool((config.raw.get("jira") or {}).get("site")),
            secrets.jira_enabled and bool((config.raw.get("confluence") or {}).get("site")),
            bool(secrets.sentry_token and (config.raw.get("sentry") or {}).get("organization")),
            bool((config.raw.get("aws") or {}).get("allowed_services")),
            slack_client is not None,
        )
    else:
        classify = stub_classify
        run = EchoRunner().run
    orchestrator = Orchestrator(store, config, classify=classify, run=run, notifier=notifier, memory_root=settings.MEMORY_ROOT)
    dashboard_store = None
    dashboard_server = None
    dashboard_task = None
    if config.dashboard.enabled:
        import uvicorn

        from taskboy.dashboard import create_app

        dashboard_store = Store(settings.DB_PATH)  # own connection; wal keeps it safe alongside the orchestrator's
        dashboard_app = create_app(dashboard_store, config, notifier, secrets, orchestrator=orchestrator, ui_dist=settings.UI_DIST)

        class EmbeddedServer(uvicorn.Server):
            def install_signal_handlers(self) -> None:
                pass  # the orchestrator owns process signals

        dashboard_server = EmbeddedServer(uvicorn.Config(dashboard_app, host=config.dashboard.bind, port=config.dashboard.port, lifespan="off", log_level="warning"))

        async def serve_dashboard() -> None:
            try:
                await dashboard_server.serve()
            except asyncio.CancelledError:
                raise
            except BaseException as e:
                dashboard_store.add_error("dashboard", type(e).__name__, str(e), traceback=traceback.format_exc())
                logger.exception("dashboard stopped after an error")

        dashboard_task = asyncio.create_task(serve_dashboard())
        logger.info("mission control dashboard listening on %s:%s", config.dashboard.bind, config.dashboard.port)
    review_poller = None
    if should_start_review_poller(config, broker):
        from taskboy.review_requests import ReviewRequestPoller

        review_poller = asyncio.ensure_future(ReviewRequestPoller(store, config, broker, notifier, reviewer_broker=reviewer_broker).run())
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, lambda: asyncio.ensure_future(orchestrator.shutdown()))

    audit_bucket = str((config.raw.get("audit") or {}).get("bucket") or "")

    async def housekeeping_loop() -> None:
        from taskboy import audit
        from taskboy.issue_runs import fail_stalled_implementation_run, sync_in_review

        while True:
            try:
                workspace.sweep_once(store, settings.WORKSPACES_ROOT, settings.MEMORY_ROOT, config.raw.get("retention") or {})
                if audit_bucket:
                    await audit.ship_once(store, audit_bucket)
                    await audit.ship_admin_once(store, audit_bucket)
                if broker is not None:
                    await repocache.refresh_all(store, broker, settings.REPOS_ROOT, (config.raw.get("github") or {}).get("approved_repos") or [])
                    await sync_in_review(store, broker)
                stalled_task_id = fail_stalled_implementation_run(store)
                if stalled_task_id is not None:
                    message = f"failed stalled implementation coordinator {stalled_task_id}"
                    logger.warning(message)
                    debug = getattr(notifier, "debug", None)
                    if debug is not None:
                        await debug.system_error("housekeeping", message)
            except Exception as e:
                store.add_error("housekeeping", type(e).__name__, str(e), traceback=traceback.format_exc())
                debug = getattr(notifier, "debug", None)
                if debug is not None:
                    await debug.system_error("housekeeping", str(e))
                logger.exception("housekeeping failed")
            await asyncio.sleep(3600)

    await orchestrator.reconcile()
    housekeeper = asyncio.ensure_future(housekeeping_loop())
    scheduler_task = None
    if config.runner == "claude":
        from taskboy.scheduler import scheduler_loop, seed_default_schedules

        seed_default_schedules(store, self_repo=str((config.raw.get("github") or {}).get("self_repo") or ""))
        scheduler_task = asyncio.ensure_future(scheduler_loop(store, config, notifier))
    if handler is not None:
        asyncio.ensure_future(handler.start_async())
    logger.info("taskboy started (environment=%s, db=%s, audit_shipping=%s)", settings.ENVIRONMENT, settings.DB_PATH, bool(audit_bucket))
    await orchestrator.dispatcher_loop()
    housekeeper.cancel()
    if scheduler_task is not None:
        scheduler_task.cancel()
    if review_poller is not None:
        review_poller.cancel()
    if handler is not None:
        await handler.close_async()
    if broker is not None:
        await broker.stop()
    if reviewer_broker is not None:
        await reviewer_broker.stop()
    if dashboard_server is not None:
        dashboard_server.should_exit = True
    if dashboard_task is not None:
        try:
            await asyncio.wait_for(dashboard_task, timeout=10)
        except TimeoutError:
            dashboard_task.cancel()
    if dashboard_store is not None:
        dashboard_store.close()
    logger.info("taskboy stopped")
    store.close()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    try:
        asyncio.run(amain())
    except ConfigError as e:
        print(f"invalid config: {e}", file=sys.stderr)
        sys.exit(64)  # matches systemd RestartPreventExitStatus=64: don't crash-loop on bad config


if __name__ == "__main__":
    main()
