#!/usr/bin/env bash
# host-side deploy step, invoked by ci via ssm run command: fetch the release tarball,
# reinstall, restart. restarts are safe — running tasks requeue and resume (REL-002/003).
# usage: remote-update.sh s3://bucket/deploy/agent-harness-<sha>.tgz
set -euo pipefail

release_url="$1"
workdir=$(mktemp -d /tmp/agent-harness-deploy.XXXXXX)
trap 'rm -rf "$workdir"' EXIT

echo "== fetching $release_url =="
aws s3 cp "$release_url" "$workdir/app.tgz" --only-show-errors
tar xzf "$workdir/app.tgz" -C "$workdir"

echo "== installing =="
bash "$workdir/deploy/install.sh"

echo "== restarting =="
systemctl restart agent-harness
sleep 5
systemctl is-active agent-harness
echo "deploy ok: $(basename "$release_url")"
