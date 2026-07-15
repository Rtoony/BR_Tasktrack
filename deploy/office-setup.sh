#!/usr/bin/env bash
# office-setup.sh — one console/office visit on BRPLVM, then remote maintenance.
#
# Run ON BRPLVM as patheal (sudo required for --apply):
#   bash deploy/office-setup.sh            # non-mutating preflight
#   bash deploy/office-setup.sh --apply    # enable SSH, deploy, mint, verify
#
# Apply order deliberately enables Tailscale SSH first. If a later git merge,
# token, or service check fails, the machine is still repairable from home.
#
# The token value never enters shell history, process arguments, xtrace output,
# or terminal output. The env file is root-owned mode 0600.
set +x
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/tasktrack}"
ENV_FILE="${ENV_FILE:-/etc/tasktrack/tasktrack.env}"
SERVICE="${SERVICE:-tasktrack}"
BRANCH="${BRANCH:-feat/agent-api-restore}"
BASE_URL="${BASE_URL:-https://127.0.0.1:8444}"
APPLY=0

case "${1:-}" in
  "") ;;
  --apply) APPLY=1 ;;
  -h|--help)
    grep '^#' "$0" | sed 's/^# \{0,1\}//'
    exit 0
    ;;
  *)
    echo "usage: $0 [--apply]" >&2
    exit 2
    ;;
esac
[ "$#" -le 1 ] || { echo "usage: $0 [--apply]" >&2; exit 2; }

MODE="DRY RUN (no changes)"
[ "$APPLY" = 1 ] && MODE="APPLY"
echo "=== BRPLVM office setup — $MODE ==="

for command_name in curl git openssl stat systemctl tailscale; do
  command -v "$command_name" >/dev/null || {
    echo "❌ required command missing: $command_name" >&2
    exit 1
  }
done
command -v sudo >/dev/null || {
  echo "❌ sudo is required" >&2
  exit 1
}

if [ "$APPLY" = 1 ]; then
  sudo -v
fi

# ── preflight the existing production checkout ──────────────────────────────
DEPLOY_READY=1
OWNER=""
if [ -d "$APP_DIR/.git" ]; then
  OWNER="$(stat -c %U "$APP_DIR/.git")"
  echo "── app checkout: $APP_DIR (git, owner=$OWNER) ──"
  echo "current: $(sudo -u "$OWNER" git -C "$APP_DIR" rev-parse --short HEAD)"
  WORKTREE_STATUS="$(sudo -u "$OWNER" git -C "$APP_DIR" status --porcelain)"
  if [ -n "$WORKTREE_STATUS" ]; then
    echo "❌ $APP_DIR has uncommitted/untracked changes:" >&2
    printf '%s\n' "$WORKTREE_STATUS" | sed 's/^/   /' >&2
    echo "   Preserve or commit them before deploying." >&2
    DEPLOY_READY=0
  else
    echo "✅ production checkout clean"
  fi
else
  echo "❌ $APP_DIR is not a git checkout." >&2
  echo "   Tailscale SSH can still be enabled, then deployment can be repaired remotely." >&2
  DEPLOY_READY=0
fi

TOKEN_STATE="unknown until sudo apply"
if [ "$APPLY" = 0 ]; then
  if sudo grep -q '^TASKTRACK_TOKEN_HERMES=..*' "$ENV_FILE" 2>/dev/null; then
    TOKEN_LEN="$(sudo grep -m1 -oP '^TASKTRACK_TOKEN_HERMES=\K.*' "$ENV_FILE" | wc -c)"
    TOKEN_LEN="$((TOKEN_LEN > 0 ? TOKEN_LEN - 1 : 0))"
    TOKEN_STATE="present (len=$TOKEN_LEN)"
  else
    TOKEN_STATE="missing/empty (would mint 64 characters)"
  fi
fi
echo "Hermes token: $TOKEN_STATE"

if [ "$APPLY" = 0 ]; then
  echo
  echo "Plan:"
  echo "  1. Enable Tailscale SSH without changing other Tailscale preferences."
  echo "  2. Fetch and merge origin/$BRANCH into the clean production checkout."
  echo "  3. Secure $ENV_FILE and ensure exactly one Hermes token."
  echo "  4. Restart $SERVICE; require healthz=200 and authed digest=200."
  [ "$DEPLOY_READY" = 1 ] || {
    echo
    echo "DRY RUN FAILED preflight: fix the checkout issue above before --apply." >&2
    exit 1
  }
  echo "DRY RUN complete — no changes made."
  exit 0
fi

# ── 1. establish the permanent remote transport first ───────────────────────
echo "── enabling Tailscale SSH ──"
sudo tailscale set --ssh
echo "✅ Tailscale SSH enabled without changing DNS/routes preferences"
tailscale status | head -3 || true

[ "$DEPLOY_READY" = 1 ] || {
  echo "❌ deployment preflight failed; app/token/service were not changed." >&2
  echo "   Tailscale SSH is enabled, so finish the repair remotely from home." >&2
  exit 1
}

# ── 2. deploy the additive agent API branch ─────────────────────────────────
echo "── deploying origin/$BRANCH ──"
sudo -u "$OWNER" git -C "$APP_DIR" fetch origin "$BRANCH"
echo "incoming commits:"
sudo -u "$OWNER" git -C "$APP_DIR" log --oneline HEAD..FETCH_HEAD | sed 's/^/   /'

ROLLBACK_REF="backup/pre-agent-api-$(date +%Y%m%d-%H%M%S)"
sudo -u "$OWNER" git -C "$APP_DIR" branch "$ROLLBACK_REF" HEAD
echo "✅ rollback ref created: $ROLLBACK_REF"

sudo -u "$OWNER" git -C "$APP_DIR" merge --no-edit FETCH_HEAD
echo "✅ merged origin/$BRANCH at $(sudo -u "$OWNER" git -C "$APP_DIR" rev-parse --short HEAD)"

# ── 3. secure the env file and ensure exactly one token line ────────────────
echo "── securing token configuration ──"
sudo install -d -m 0755 "$(dirname "$ENV_FILE")"
if ! sudo test -e "$ENV_FILE"; then
  sudo install -m 0600 -o root -g root /dev/null "$ENV_FILE"
fi
sudo chown root:root "$ENV_FILE"
sudo chmod 0600 "$ENV_FILE"

TOKEN_LINES="$(sudo grep -c '^TASKTRACK_TOKEN_HERMES=' "$ENV_FILE" 2>/dev/null || true)"
[ "$TOKEN_LINES" -le 1 ] || {
  echo "❌ found $TOKEN_LINES TASKTRACK_TOKEN_HERMES lines; refusing ambiguous config" >&2
  exit 1
}

if sudo grep -q '^TASKTRACK_TOKEN_HERMES=..*' "$ENV_FILE"; then
  TOKEN_LEN="$(sudo grep -m1 -oP '^TASKTRACK_TOKEN_HERMES=\K.*' "$ENV_FILE" | wc -c)"
  TOKEN_LEN="$((TOKEN_LEN > 0 ? TOKEN_LEN - 1 : 0))"
  echo "✅ TASKTRACK_TOKEN_HERMES already present (len=$TOKEN_LEN)"
else
  sudo sed -i '/^TASKTRACK_TOKEN_HERMES=$/d' "$ENV_FILE"
  NEW_TOKEN="$(openssl rand -hex 32)"
  printf 'TASKTRACK_TOKEN_HERMES=%s\n' "$NEW_TOKEN" | sudo tee -a "$ENV_FILE" >/dev/null
  unset NEW_TOKEN
  echo "✅ minted TASKTRACK_TOKEN_HERMES (len=64; value not displayed)"
fi
echo "✅ $ENV_FILE owner=$(sudo stat -c '%U:%G' "$ENV_FILE") mode=$(sudo stat -c '%a' "$ENV_FILE")"

# ── 4. restart and require both health and authenticated API success ────────
echo "── restarting and verifying $SERVICE ──"
sudo systemctl restart "$SERVICE"
sleep 3

HC="$(curl -sk -o /dev/null -w '%{http_code}' --max-time 10 "$BASE_URL/healthz" || true)"
echo "healthz → ${HC:-000}"
[ "$HC" = 200 ] || {
  echo "❌ app unhealthy; inspect: sudo journalctl -u $SERVICE -n 50" >&2
  exit 1
}

TOKEN="$(sudo grep -m1 -oP '^TASKTRACK_TOKEN_HERMES=\K.*' "$ENV_FILE")"
DC="$(printf 'X-Token: %s\n' "$TOKEN" | curl -sk -o /dev/null -H @- \
  -w '%{http_code}' --max-time 10 "$BASE_URL/api/v1/digest" || true)"
unset TOKEN
echo "authed /api/v1/digest → ${DC:-000}"
[ "$DC" = 200 ] || {
  echo "❌ agent API verification failed (HTTP ${DC:-000})." >&2
  echo "   Tailscale SSH remains enabled for remote repair." >&2
  echo "   Code rollback ref: $ROLLBACK_REF" >&2
  exit 1
}
echo "✅ agent API live with the Hermes token"

cat <<'EOF'

=== NEXT (from home) ===
1. Ensure the tailnet policy allows BOTH layers:
   "grants": [{"src": ["autogroup:member"], "dst": ["tag:brplvm"],
               "ip": ["tcp:22"]}]
   "ssh": [{"action": "accept", "src": ["autogroup:member"],
            "dst": ["tag:brplvm"], "users": ["root"]}]
2. On the AI-PC:
   bash ~/scripts/brplvm-fetch-token-2026-07-14.sh --apply

The home command transfers the value directly into Vaultwarden and verifies it.
EOF
