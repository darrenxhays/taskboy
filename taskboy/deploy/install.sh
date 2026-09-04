#!/usr/bin/env bash
# host bootstrap for the taskboy host (amazon linux 2023). idempotent; run as root.
# usage: sudo ./install.sh [pip-spec]
#   pip-spec: what to install into the venv — "taskboy==X.Y.Z" (recommended: your shell
#   repo's pinned version), a wheel path, or a checkout directory. defaults to the latest
#   taskboy release on PyPI.
# this script ships inside the package; to bootstrap a fresh host without a checkout:
#   python3.12 -m venv /tmp/bootstrap && /tmp/bootstrap/bin/pip install "taskboy==X.Y.Z"
#   /tmp/bootstrap/bin/taskboy assets deploy /tmp && sudo bash /tmp/deploy/install.sh "taskboy==X.Y.Z"
# routine releases do NOT re-run this script; ci invokes remote-update.sh via ssm.
# re-run this only for host-level changes (users, docker, iptables, units).
set -euo pipefail

# fixed install layout — not operator knobs
SLUG="taskboy"
SVC_USER="taskboy"
OPT_DIR="/opt/$SLUG"
ETC_DIR="/etc/$SLUG"
VAR_DIR="/var/lib/$SLUG"
RUN_DIR="/run/$SLUG"

PKG="${1:-taskboy}"

echo "== packages =="
dnf install -y -q docker git python3.12 iptables-nft

echo "== service user + directories =="
id -u "$SVC_USER" &>/dev/null || useradd --system --home-dir "$VAR_DIR" --shell /sbin/nologin "$SVC_USER"
install -d -o "$SVC_USER" -g "$SVC_USER" -m 700 "$VAR_DIR" "$VAR_DIR/workspaces" "$VAR_DIR/memory" "$VAR_DIR/repos"
install -d -o "$SVC_USER" -g "$SVC_USER" -m 755 "$RUN_DIR"
# group-writable so the dashboard, running as the service user, can write temp files next to the live config
install -d -g "$SVC_USER" -m 775 "$ETC_DIR"
install -d -m 755 "$OPT_DIR"
echo "d $RUN_DIR 0755 $SVC_USER $SVC_USER" > "/etc/tmpfiles.d/$SLUG.conf"

echo "== code + venv =="
# the application is a pip-installed package; no source checkout lives on the host
python3.12 -m venv "$OPT_DIR/.venv"
"$OPT_DIR/.venv/bin/pip" install -q --upgrade pip
"$OPT_DIR/.venv/bin/pip" install -q --upgrade "$PKG"
install -d -o "$SVC_USER" -g "$SVC_USER" "$OPT_DIR/skills"
chown -R "$SVC_USER:$SVC_USER" "$OPT_DIR/.venv"
# note: the claude cli is bundled inside the claude-agent-sdk package — no separate install

echo "== packaged host files =="
assets_dir=$(mktemp -d)
trap 'rm -rf "$assets_dir"' EXIT
"$OPT_DIR/.venv/bin/taskboy" assets deploy "$assets_dir" > /dev/null
"$OPT_DIR/.venv/bin/taskboy" assets templates "$assets_dir" > /dev/null
# host copies of the deploy scripts — ci invokes remote-update.sh via ssm on every release
install -d "$OPT_DIR/deploy"
install -m 755 "$assets_dir/deploy/install.sh" "$OPT_DIR/deploy/install.sh"
install -m 755 "$assets_dir/deploy/remote-update.sh" "$OPT_DIR/deploy/remote-update.sh"

echo "== config =="
[ -f "$ETC_DIR/env" ] || install -m 640 -g "$SVC_USER" "$assets_dir/deploy/env.example" "$ETC_DIR/env"
# first install: seed operator config from the packaged example; /etc/taskboy is the
# live copy from then on. your shell repo's deploy workflow ships config updates via
# remote-update.sh; releases never overwrite what's here.
[ -f "$ETC_DIR/config.yaml" ] || install -m 640 -g "$SVC_USER" "$assets_dir/templates/config.example.yaml" "$ETC_DIR/config.yaml"
[ -f "$ETC_DIR/task_started_messages.yaml" ] || install -m 640 -g "$SVC_USER" "$assets_dir/templates/task_started_messages.yaml" "$ETC_DIR/task_started_messages.yaml"

echo "== task isolation prep =="
# slot users for sub-agent process separation (activated when the runner moves to systemd-run scopes)
for i in 0 1 2 3 4 5; do
  id -u "$SVC_USER-t$i" &>/dev/null || useradd --system --no-create-home --shell /sbin/nologin "$SVC_USER-t$i"
done

echo "== docker =="
systemctl enable --now docker
usermod -aG docker "$SVC_USER"
for i in 0 1 2 3 4 5; do
  usermod -aG docker "$SVC_USER-t$i"
done
install -d -m 755 /usr/libexec/docker/cli-plugins
if [ ! -x /usr/libexec/docker/cli-plugins/docker-compose ]; then
  compose_tmp="$(mktemp)"
  curl -fsSL "https://github.com/docker/compose/releases/download/v2.27.0/docker-compose-linux-$(uname -m)" -o "$compose_tmp"
  install -m 755 "$compose_tmp" /usr/libexec/docker/cli-plugins/docker-compose
  rm -f "$compose_tmp"
fi
# block the instance metadata service for task slot users; the orchestrator keeps access (§8.3)
for i in 0 1 2 3 4 5; do
  iptables -C OUTPUT -d 169.254.169.254 -m owner --uid-owner "$SVC_USER-t$i" -j REJECT 2>/dev/null ||
    iptables -A OUTPUT -d 169.254.169.254 -m owner --uid-owner "$SVC_USER-t$i" -j REJECT
done

echo "== systemd unit =="
install -m 644 "$assets_dir/deploy/$SLUG.service" "/etc/systemd/system/$SLUG.service"
# off-peak cli auto-update restart path: the service runs unprivileged (NoNewPrivileges=yes) so it can't
# sudo/systemctl-restart itself; instead it touches $RUN_DIR/restart-requested and this root-owned
# path unit restarts it (config cli_update:, taskboy/scheduler.py). replaces the old sudoers drop-in.
install -m 644 "$assets_dir/deploy/$SLUG-restart.service" "/etc/systemd/system/$SLUG-restart.service"
install -m 644 "$assets_dir/deploy/$SLUG-restart.path" "/etc/systemd/system/$SLUG-restart.path"
rm -f "/etc/sudoers.d/$SVC_USER-cli-update"  # remove the old, NoNewPrivileges-incompatible sudo rule if present
systemctl daemon-reload
systemctl enable "$SLUG"
systemctl enable --now "$SLUG-restart.path"

echo "== done. next steps =="
echo "  1) put secrets in aws secrets manager (TASKBOY_SECRETS_<ENV>), or export them in $ETC_DIR/env for a no-aws install"
echo "  2) edit $ETC_DIR/config.yaml (or ship your shell repo's config via its deploy workflow)"
echo "  3) systemctl start $SLUG && journalctl -fu $SLUG"
