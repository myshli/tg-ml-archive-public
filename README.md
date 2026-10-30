# tg-ml-archive

Archives posts (text and links) from ML news Telegram channels, tags them by topic, and lets Claude search them through an MCP server.

```
systemd timer (hourly) ── ingest.py ──► archive.db (SQLite: posts + FTS5 + sqlite-vec)
                             └─ enrich.py ─┘  (OpenAI: tags + summary + embeddings)
Claude ──HTTPS + token──► mcp_server.py
```

- `ingest.py`: Telethon (your user account) fetches new posts for each channel in `channels.toml`.
- `enrich.py`: OpenAI tags each post (topics, kind, entities, an English one-line summary) and embeds it.
- `mcp_server.py`: tools `search_posts` (hybrid keyword + semantic), `get_post`, `recent`, `list_topics`, `stats`.

## Local setup

```bash
uv sync
cp .env.example .env   # fill in TG_API_ID / TG_API_HASH (my.telegram.org) and OPENAI_API_KEY
uv run ingest.py       # first run asks for phone + code, saves tg.session, backfills history
uv run enrich.py --limit 100   # tag a sample and check quality
uv run enrich.py
```

Check the tags: `sqlite3 archive.db "select summary, topics, kind from posts order by random() limit 20"`. To change the taxonomy, edit `channels.toml`, then run `uv run enrich.py --retag`.

Use it from Claude Code locally (stdio):

```bash
claude mcp add tg-archive -- uv run --directory /path/to/tg-ml-archive mcp_server.py
```

## VPS deployment

The archive lives on a VPS (`DEPLOY_HOST` in `.env`, for example `root@203.0.113.10`; code in `/opt/tg-ml-archive`,
system user `tgarchive`). The deploy scripts read `DEPLOY_HOST` and `DOMAIN` from `.env`.
A systemd timer runs `ingest.py` + `enrich.py` hourly. The database is `/var/lib/tg-archive/archive.db`.

```bash
deploy/push.sh               # after code changes: copy code, uv sync, reinstall the timer
deploy/push.sh --with-data   # first time only: also copies .env, tg.session, archive.db
```

Claude Code starts the MCP server on the VPS over SSH (stdio):

```bash
claude mcp add -s user tg-archive -- ssh -T -o BatchMode=yes <DEPLOY_HOST> runuser -u tgarchive -- /opt/tg-ml-archive/.venv/bin/python /opt/tg-ml-archive/mcp_server.py
```

Useful on the VPS: `systemctl list-timers tg-archive-sync.timer`, `journalctl -u tg-archive-sync -n 50`,
`systemctl start tg-archive-sync` (sync now).

Once the VPS has `tg.session`, don't run `ingest.py` on the Mac with the same session: Telegram can revoke
a session that is used from two places at once. The local `archive.db` is a stale snapshot from then on.

### Public HTTPS endpoint (claude.ai web / phone, and the Mac)

`https://<DOMAIN>/mcp`, set up once with `deploy/go-public.sh`:

- Caddy (TLS) → `127.0.0.1:8765` → `mcp_server.py --http` as user **`tgmcp`** (`deploy/tg-archive-mcp.service`, hardened).
- The database lives in `/var/lib/tg-archive/` (group `tgarchive-db`), shared by both services. `tgmcp` cannot read
  `tg.session` (a full Telegram login) or the ingest `.env`. Its secrets (`MCP_TOKEN`, `OPENAI_API_KEY`) are in
  `/etc/tg-archive-mcp.env`, root-only.
- Clients send `Authorization: Bearer <MCP_TOKEN>`, or `?token=` where headers are impossible (claude.ai connectors).
  Caddy keeps no access log, so the query token is not written to disk.
- Rotate the token: edit `/etc/tg-archive-mcp.env`, `systemctl restart tg-archive-mcp`, update the clients.

The old SSH stdio route (`ssh … runuser -u tgarchive … mcp_server.py`) still works if you add
`ARCHIVE_DB=/var/lib/tg-archive/archive.db`, but each Claude session then keeps its own server process on the VPS.

Resources: sync uses ~4 s CPU per hour; the DB is ~35 MB for 8.6k posts.

## Digest page

`https://<DOMAIN>/` always shows the latest finished 3-day window (fixed windows from 2026-10-17,
00:00 UTC). Old digests are not kept; `/digest/...` links redirect to `/`. `/feed.xml` is an Atom feed with one
entry per new digest.

- `media.py` (in the hourly sync): saves photos, videos and GIFs of new posts, whole albums included, to
  `/var/www/tg-digest/media/`. Videos are re-encoded to H.264 ≤720p with a poster frame (ffmpeg). Posts whose
  topics are all in `[digest] no_media` (`channels.toml`) get no downloads.
- `digest.py` (timer `tg-archive-digest`, daily 01:30 UTC): when a newer window has finished, it
  1. leaves out humor, off-topic and empty posts (under 60 characters without useful links);
  2. groups posts about the same thing across channels and rates their importance (gpt-5-mini);
  3. writes each item in its sources' language, in same-language batches, and rewrites any item that comes back
     in the wrong language (GPT-6 Sol, medium effort);
  4. writes the overview as themed short lists, in Russian;
  5. saves everything to `/var/www/tg-digest/data.json`, renders `templates/` to `index.html` and `feed.xml`, and
     deletes the media of posts from before the new window.
- Section order and the text-only sections are set in `[digest]` in `channels.toml`.
- After template changes: `digest.py --render-only` (no AI calls). New overview only: `digest.py --redo-overview`.
  Rebuild: `digest.py --window 2026-10-17`. Run these as `tgarchive` with `ARCHIVE_DB=/var/lib/tg-archive/archive.db`.
- Cost: all calls use OpenAI's Flex tier (half price; if Flex is refused, the call is repeated on the standard
  tier). About $0.12 per digest with GPT-6 Sol; the real cost is printed and saved in `data.json` under `usage`.
  Settings: `DIGEST_WRITE_MODEL`, `DIGEST_WRITE_EFFORT`, `DIGEST_FLEX=0`, and `DIGEST_THIN_MODEL` /
  `DIGEST_THIN_CHARS` to send thin English items to a cheaper model (off; gpt-5-mini saved ~$0.04 but wrote worse).
  Model test on the same groups (Oct 2026): gpt-5 $0.31, GPT-6 Sol $0.12 (best Russian), GPT-6 Luna $0.007
  (close to gpt-5, slightly rougher). Switch with `DIGEST_WRITE_MODEL=gpt-6-luna`.
- Media on disk: the current and the next window, ~250 MB at most.

## Notes

- `tg.session` is a full login to your Telegram account. Keep it private (it is in `.gitignore`).
- Keyword search uses prefix matching instead of stemming. Semantic search (embeddings) covers paraphrases and works across Russian and English.
- `EMBED_DIM` (default 512) is fixed when the DB is created.
