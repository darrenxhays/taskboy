"""audit trail tamper-evidence and off-host shipping (§10).

every task_event carries a hash chained to the previous event (computed in store._insert_event);
verify_chain recomputes it. ship_once exports new events as jsonl to the object-locked s3 bucket,
so even a compromised host cannot rewrite history that has already shipped.
"""

import asyncio
import json
import logging

from agent_harness.store import Store, admin_event_hash, event_hash

logger = logging.getLogger("agent_harness.audit")


def verify_chain(store: Store) -> tuple[bool, int]:
    """recompute the hash chain; returns (intact, events_checked). rows predating the hash column are skipped."""
    prev = ""
    checked = 0
    for row in store.events_after(0, limit=1_000_000):
        if row["hash"] is not None:
            expected = event_hash(prev, row["ts"], row["task_id"], row["kind"], row["tool_name"], row["detail_json"])
            if row["hash"] != expected:
                logger.error("audit chain broken at event id %s", row["id"])
                return False, checked
            checked += 1
        prev = row["hash"] or ""
    return True, checked


async def ship_once(store: Store, bucket: str, prefix: str = "audit") -> int:
    """upload events newer than the last shipped id; the meta cursor makes retries idempotent.

    sqlite stays on the event-loop thread (the connection is not shareable across threads);
    only the s3 upload runs in a worker.
    """
    last_shipped = int(store.meta_get("audit_shipped_id") or 0)
    events = store.events_after(last_shipped)
    if not events:
        return 0
    key = f"{prefix}/events-{events[0]['id']:012d}-{events[-1]['id']:012d}.jsonl"
    await asyncio.to_thread(_put, bucket, key, "\n".join(json.dumps(event) for event in events))
    store.meta_set("audit_shipped_id", str(events[-1]["id"]))
    logger.info("shipped %s audit events to s3://%s/%s", len(events), bucket, key)
    return len(events)


def verify_admin_chain(store: Store) -> tuple[bool, int]:
    prev = ""
    checked = 0
    for row in store.admin_events_after(0, limit=1_000_000):
        expected = admin_event_hash(prev, row["ts"], row["actor"], row["action"], row["target"], row["outcome"], row["detail_json"])
        if row["hash"] != expected:
            logger.error("admin audit chain broken at event id %s", row["id"])
            return False, checked
        prev = row["hash"]
        checked += 1
    return True, checked


async def ship_admin_once(store: Store, bucket: str, prefix: str = "admin-audit") -> int:
    last_shipped = int(store.meta_get("admin_audit_shipped_id") or 0)
    events = store.admin_events_after(last_shipped)
    if not events:
        return 0
    key = f"{prefix}/events-{events[0]['id']:012d}-{events[-1]['id']:012d}.jsonl"
    await asyncio.to_thread(_put, bucket, key, "\n".join(json.dumps(event) for event in events))
    store.meta_set("admin_audit_shipped_id", str(events[-1]["id"]))
    logger.info("shipped %s admin audit events to s3://%s/%s", len(events), bucket, key)
    return len(events)


def _put(bucket: str, key: str, body: str) -> None:
    """the s3 seam — patched in unit tests."""
    import boto3

    boto3.client("s3").put_object(Bucket=bucket, Key=key, Body=body.encode())
