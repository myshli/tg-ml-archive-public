#!/usr/bin/env bash
# Copy the code to the VPS and install dependencies. Run from the repo root:
#   deploy/push.sh                 # code only (safe to re-run after changes)
#   deploy/push.sh --with-data     # first time: also .env, tg.session and archive.db
set -euo pipefail

cd "$(dirname "$0")/.."
source deploy/env.sh  # HOST from DEPLOY_HOST
DIR=/opt/tg-ml-archive
APP_USER=tgarchive
MCP_USER=tgmcp            # runs the public MCP server; cannot read tg.session or .env
DATA=/var/lib/tg-archive  # only the database lives here, shared through group tgarchive-db
SITE=/var/www/tg-digest   # public digest pages + media, served by Caddy

ssh "$HOST" "id $APP_USER >/dev/null 2>&1 || useradd --system --create-home --home-dir $DIR --shell /usr/sbin/nologin $APP_USER
  id $MCP_USER >/dev/null 2>&1 || useradd --system --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin $MCP_USER
  getent group tgarchive-db >/dev/null || groupadd --system tgarchive-db
  usermod -aG tgarchive-db $APP_USER && usermod -aG tgarchive-db $MCP_USER
  install -d -o $APP_USER -g tgarchive-db -m 2770 $DATA
  install -d -o $APP_USER -g $APP_USER -m 755 $SITE $SITE/media
  command -v ffmpeg >/dev/null || DEBIAN_FRONTEND=noninteractive apt-get install -y -q ffmpeg >/dev/null
  [ -x /usr/local/bin/uv ] || curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR=/usr/local/bin UV_NO_MODIFY_PATH=1 sh"

git ls-files -z | rsync -az --from0 --files-from=- ./ "$HOST:$DIR/"

if [[ "${1:-}" == "--with-data" ]]; then
  # Fold the WAL into the main file so a single copy is consistent.
  sqlite3 archive.db "PRAGMA wal_checkpoint(TRUNCATE);"
  rsync -az .env tg.session "$HOST:$DIR/"
  rsync -az archive.db "$HOST:$DATA/"
  ssh "$HOST" "chmod 600 $DIR/.env $DIR/tg.session && chown $APP_USER:tgarchive-db $DATA/archive.db && chmod 660 $DATA/archive.db"
fi

# 755: tgmcp must list the code directory (Python's importer does);
# .env and tg.session stay 600.
ssh "$HOST" "chown -R $APP_USER:$APP_USER $DIR && chmod 755 $DIR
  cd $DIR && runuser -u $APP_USER -- /usr/local/bin/uv sync --frozen --no-dev -q
  cp deploy/tg-archive-sync.service deploy/tg-archive-sync.timer deploy/tg-archive-digest.service deploy/tg-archive-digest.timer /etc/systemd/system/
  systemctl daemon-reload && systemctl enable --now tg-archive-sync.timer tg-archive-digest.timer >/dev/null
  if [ -f /etc/tg-archive-mcp.env ]; then
    cp deploy/tg-archive-mcp.service /etc/systemd/system/ && systemctl daemon-reload
    systemctl enable tg-archive-mcp >/dev/null && systemctl restart tg-archive-mcp
  fi"

echo "deployed to $HOST:$DIR"
