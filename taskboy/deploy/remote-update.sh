#!/usr/bin/env bash
# host-side deploy step, invoked by the shell repo's ci via ssm run command:
# pip-upgrade the venv from pypi, optionally sync operator config, refresh the
# packaged host files, restart. restarts are safe — running tasks requeue and
# resume (REL-002/003). host-level bootstrap changes (users, docker, iptables)
# still need install.sh.
# usage: remote-update.sh <pip-spec> [config-bundle-s3-url]
#   pip-spec:             e.g. "taskboy==X.Y.Z" (the shell repo's pin)
#   config-bundle-s3-url: optional s3 url of a tarball with config/ and skills/
#                         directories; config/ syncs to /etc/taskboy and
#                         skills/ to /opt/taskboy/skills (copy-over, no deletes)
set -euo pipefail

pip_spec="$1"
config_url="${2:-}"
workdir=$(mktemp -d /tmp/taskboy-deploy.XXXXXX)
trap 'rm -rf "$workdir"' EXIT

echo "== installing $pip_spec =="
/opt/taskboy/.venv/bin/pip install -q --upgrade "$pip_spec"
# ci runs this as root; the service user must own the venv so the off-peak cli_update pip upgrade keeps working
chown -R taskboy:taskboy /opt/taskboy/.venv

if [ -n "$config_url" ]; then
  echo "== syncing config from $config_url =="
  aws s3 cp "$config_url" "$workdir/config.tgz" --only-show-errors
  mkdir "$workdir/bundle"
  tar xzf "$workdir/config.tgz" -C "$workdir/bundle"
  if [ -d "$workdir/bundle/config" ]; then
    find "$workdir/bundle/config" -type f -exec chmod 640 {} +
    cp -R "$workdir/bundle/config/." /etc/taskboy/
    find /etc/taskboy -type f -exec chown "root:taskboy" {} + -exec chmod 640 {} +
  fi
  if [ -d "$workdir/bundle/skills" ]; then
    cp -R "$workdir/bundle/skills/." /opt/taskboy/skills/
    chown -R "taskboy:taskboy" /opt/taskboy/skills
  fi
fi

echo "== refreshing packaged host files =="
/opt/taskboy/.venv/bin/taskboy assets deploy "$workdir" > /dev/null
install -m 644 "$workdir/deploy/taskboy.service" /etc/systemd/system/taskboy.service
install -m 644 "$workdir/deploy/taskboy-restart.service" /etc/systemd/system/taskboy-restart.service
install -m 644 "$workdir/deploy/taskboy-restart.path" /etc/systemd/system/taskboy-restart.path
# the running copy keeps its inode; the new script takes effect next invocation
install -m 755 "$workdir/deploy/install.sh" /opt/taskboy/deploy/install.sh
install -m 755 "$workdir/deploy/remote-update.sh" /opt/taskboy/deploy/remote-update.sh
systemctl daemon-reload

echo "== restarting =="
systemctl restart taskboy
sleep 5
systemctl is-active taskboy
echo "deploy ok: $pip_spec"
