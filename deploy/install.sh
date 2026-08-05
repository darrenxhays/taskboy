#!/usr/bin/env bash
# host bootstrap for the agent-harness host (amazon linux 2023). idempotent; run as root.
# usage: sudo ./deploy/install.sh   (from a checkout of the repo)
set -euo pipefail

# fixed install layout — not operator knobs
SLUG="agent-harness"
SVC_USER="agentharness"
OPT_DIR="/opt/$SLUG"
ETC_DIR="/etc/$SLUG"
VAR_DIR="/var/lib/$SLUG"
RUN_DIR="/run/$SLUG"

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"

echo "== packages =="
dnf install -y -q docker git python3.12 iptables-nft

echo "== service user + directories =="
id -u "$SVC_USER" &>/dev/null || useradd --system --home-dir "$VAR_DIR" --shell /sbin/nologin "$SVC_USER"
install -d -o "$SVC_USER" -g "$SVC_USER" -m 700 "$VAR_DIR" "$VAR_DIR/workspaces" "$VAR_DIR/memory" "$VAR_DIR/repos"
install -d -o "$SVC_USER" -g "$SVC_USER" -m 755 "$RUN_DIR"
install -d -m 755 "$ETC_DIR" "$OPT_DIR"
echo "d $RUN_DIR 0755 $SVC_USER $SVC_USER" > "/etc/tmpfiles.d/$SLUG.conf"

echo "== code + venv =="
rsync -a --delete --exclude .venv --exclude local --exclude .git "$REPO_DIR/" "$OPT_DIR/"
python3.12 -m venv "$OPT_DIR/.venv"
"$OPT_DIR/.venv/bin/pip" install -q --upgrade pip
"$OPT_DIR/.venv/bin/pip" install -q "$OPT_DIR"
chown -R "$SVC_USER:$SVC_USER" "$OPT_DIR"
# note: the claude cli is bundled inside the claude-agent-sdk package — no separate install

echo "== config =="
[ -f "$ETC_DIR/env" ] || install -m 640 -g "$SVC_USER" "$REPO_DIR/deploy/env.example" "$ETC_DIR/env"
# operator config is version-controlled: every deploy ships the repo's config dir verbatim,
# minus the example files (config.yaml, personalities, conventions, task_started_messages.yaml, ...)
rsync -a --exclude 'config.example.yaml' --exclude '*.example.md' "$REPO_DIR/config/" "$ETC_DIR/"
# first install without a repo config.yaml: seed one from the example
[ -f "$ETC_DIR/config.yaml" ] || install -m 640 -g "$SVC_USER" "$REPO_DIR/config/config.example.yaml" "$ETC_DIR/config.yaml"
# rsync keeps repo ownership/modes; enforce root:$SVC_USER 640 on every shipped file (env included)
find "$ETC_DIR" -type f -exec chown "root:$SVC_USER" {} + -exec chmod 640 {} +

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
install -m 644 "$REPO_DIR/deploy/$SLUG.service" "/etc/systemd/system/$SLUG.service"
# off-peak cli auto-update restart path: the service runs unprivileged (NoNewPrivileges=yes) so it can't
# sudo/systemctl-restart itself; instead it touches $RUN_DIR/restart-requested and this root-owned
# path unit restarts it (config cli_update:, agent_harness/scheduler.py). replaces the old sudoers drop-in.
install -m 644 "$REPO_DIR/deploy/$SLUG-restart.service" "/etc/systemd/system/$SLUG-restart.service"
install -m 644 "$REPO_DIR/deploy/$SLUG-restart.path" "/etc/systemd/system/$SLUG-restart.path"
rm -f "/etc/sudoers.d/$SVC_USER-cli-update"  # remove the old, NoNewPrivileges-incompatible sudo rule if present
systemctl daemon-reload
systemctl enable "$SLUG"
systemctl enable --now "$SLUG-restart.path"

echo "== done. next steps =="
echo "  1) put secrets in aws secrets manager (AGENT_HARNESS_SECRETS_<ENV>), or export them in $ETC_DIR/env for a no-aws install"
echo "  2) edit $ETC_DIR/config.yaml"
echo "  3) systemctl start $SLUG && journalctl -fu $SLUG"
