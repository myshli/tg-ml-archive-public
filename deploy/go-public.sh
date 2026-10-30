#!/usr/bin/env bash
# One-time switch to a public MCP endpoint with the user split. Run from the
# repo root on the Mac:  deploy/go-public.sh
#
# What it does on the VPS, in order:
#   1. stops the hourly sync and the per-session stdio MCP servers
#   2. backs up archive.db to /root, then moves it to /var/lib/tg-archive
#      (group tgarchive-db), so tgmcp can open it without seeing tg.session
#   3. creates user tgmcp and /etc/tg-archive-mcp.env (new random MCP_TOKEN +
#      the OpenAI key copied from the ingest .env; root-only, never printed)
#   4. deploys code and units with deploy/push.sh (starts tg-archive-mcp,
#      re-enables the sync timer)
#   5. installs Caddy for https://$DOMAIN
#   6. checks: 401 without the token, 200 with it, and one sync run
#
# Safe to re-run: every step checks before it changes anything.
set -euo pipefail

cd "$(dirname "$0")/.."
source deploy/env.sh  # HOST and DOMAIN from DEPLOY_HOST and DOMAIN
if [ -z "$DOMAIN" ]; then echo "DOMAIN is not set: add it to .env" >&2; exit 1; fi

echo "== 1-2. stop, back up, move the database"
ssh "$HOST" 'set -e
  systemctl stop tg-archive-sync.timer
  while systemctl is-active --quiet tg-archive-sync.service; do sleep 5; done
  pkill -u tgarchive -f "mcp_server.py$" || true
  id tgmcp >/dev/null 2>&1 || useradd --system --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin tgmcp
  getent group tgarchive-db >/dev/null || groupadd --system tgarchive-db
  usermod -aG tgarchive-db tgarchive && usermod -aG tgarchive-db tgmcp
  install -d -o tgarchive -g tgarchive-db -m 2770 /var/lib/tg-archive
  if [ -f /opt/tg-ml-archive/archive.db ] && [ ! -f /var/lib/tg-archive/archive.db ]; then
    cd /opt/tg-ml-archive
    .venv/bin/python -c "import sqlite3; c = sqlite3.connect(\"archive.db\"); c.execute(\"pragma wal_checkpoint(TRUNCATE)\"); c.close()"
    cp -p archive.db /root/tg-archive-db-backup-$(date -u +%Y%m%dT%H%M%SZ).db
    mv archive.db /var/lib/tg-archive/archive.db
    rm -f archive.db-wal archive.db-shm
  fi
  chown tgarchive:tgarchive-db /var/lib/tg-archive/archive.db
  chmod 660 /var/lib/tg-archive/archive.db
  ls -l /var/lib/tg-archive'

echo "== 3. secrets for the MCP service"
ssh "$HOST" 'set -e
  if [ ! -f /etc/tg-archive-mcp.env ]; then
    umask 077
    { echo "MCP_TOKEN=$(openssl rand -hex 32)"
      grep "^OPENAI_API_KEY=" /opt/tg-ml-archive/.env
      grep "^EMBED_DIM=" /opt/tg-ml-archive/.env || true
    } > /etc/tg-archive-mcp.env
  fi
  chmod 600 /etc/tg-archive-mcp.env
  echo "keys in /etc/tg-archive-mcp.env: $(cut -d= -f1 /etc/tg-archive-mcp.env | tr "\n" " ")"'

echo "== 4. deploy code and units"
deploy/push.sh

echo "== 5. Caddy"
# Caddy is shared with other projects on the server: each project owns one
# file in /etc/caddy/sites/ and the main Caddyfile only imports them. Never write the main
# file unless it is still Caddy's stock one.
ssh "$HOST" 'set -e
  command -v caddy >/dev/null || DEBIAN_FRONTEND=noninteractive apt-get install -y -q caddy >/dev/null
  [ -f /etc/caddy/Caddyfile.dist ] || cp /etc/caddy/Caddyfile /etc/caddy/Caddyfile.dist
  IMPORT="import /etc/caddy/sites/*.caddy"
  install -d -m 755 /etc/caddy/sites
  if ! grep -qxF "$IMPORT" /etc/caddy/Caddyfile; then
    if cmp -s /etc/caddy/Caddyfile /etc/caddy/Caddyfile.dist; then
      printf "%s\n" "# Only imports. Each project owns one file in /etc/caddy/sites/." "$IMPORT" > /etc/caddy/Caddyfile
    else
      echo "/etc/caddy/Caddyfile has sites but no \"$IMPORT\" line: move them to /etc/caddy/sites/ first" >&2
      exit 1
    fi
  fi'
ssh "$HOST" "sed 's/^__DOMAIN__/$DOMAIN/' > /etc/caddy/sites/tg-ml-archive.caddy" < deploy/tg-ml-archive.caddy
ssh "$HOST" 'caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile >/dev/null && systemctl enable caddy >/dev/null && systemctl reload-or-restart caddy && systemctl is-active caddy'

echo "== 6. checks"
ssh "$HOST" 'set -e
  sleep 2
  systemctl is-active tg-archive-mcp
  T=$(grep "^MCP_TOKEN=" /etc/tg-archive-mcp.env | cut -d= -f2)
  INIT="{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"initialize\",\"params\":{\"protocolVersion\":\"2025-03-26\",\"capabilities\":{},\"clientInfo\":{\"name\":\"check\",\"version\":\"0\"}}}"
  H=(-H "Content-Type: application/json" -H "Accept: application/json, text/event-stream")
  echo "without token: $(curl -s -o /dev/null -w "%{http_code}" -X POST "${H[@]}" -d "$INIT" http://127.0.0.1:8765/mcp) (want 401)"
  echo "with token:    $(curl -s -o /dev/null -w "%{http_code}" -X POST "${H[@]}" -H "Authorization: Bearer $T" -d "$INIT" http://127.0.0.1:8765/mcp) (want 200)"
  echo "tgmcp reads tg.session? $(runuser -u tgmcp -- cat /opt/tg-ml-archive/tg.session >/dev/null 2>&1 && echo YES-BAD || echo no)"
  echo "tgmcp reads .env?       $(runuser -u tgmcp -- cat /opt/tg-ml-archive/.env >/dev/null 2>&1 && echo YES-BAD || echo no)"
  systemctl start tg-archive-sync.service && echo "sync run: $(systemctl show -p Result --value tg-archive-sync.service)"
  ls -l /var/lib/tg-archive'

cat <<EOF

Done on the server. Next:
  1. At your DNS provider, add an A record: $DOMAIN -> ${HOST#*@}
  2. When it resolves, Caddy fetches the certificate by itself. Test:
       curl -s -o /dev/null -w '%{http_code}\n' https://$DOMAIN/mcp     (want 401)
EOF
