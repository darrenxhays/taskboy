---
name: reviewandmonitor
description: Review a GitHub PR for simplicity right now, then keep watching it — re-reviewing on every change for up to 3 hours. Invoked as `@{{agent_name}} /reviewandmonitor {pr_url}`.
requires: [review, monitor]
---

# reviewandmonitor

Review a pull request now, then keep watching it. A thin chain of the `/review` procedure followed by the `/monitor` procedure on the same PR (both included below).

The first GitHub PR URL in the arguments is the PR. If it is missing, call `report_blocked` asking for it, then stop.

Two ordered steps, both in this session:

1. Run the included `/review` procedure on the PR end to end — it posts the review comments.
2. Then run the included `/monitor` procedure on the same PR — establish its baseline **after** the review so the loop starts clean.

The order matters: reviewing first fills the gap left by monitor's no-review-at-baseline rule.

## Final Report

The initial review's comment count, then the monitoring summary (ticks, re-reviews). Nothing else.
