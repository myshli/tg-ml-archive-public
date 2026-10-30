# tg-ml-archive

tg-ml-archive copies the posts of public Telegram channels into a SQLite database. An OpenAI model tags each post by topic and writes a one-line English summary. An MCP server lets Claude search the archive by keywords and by meaning. A digest page shows the news of the last 3 days.

```
systemd timer (hourly) ── ingest.py ──► archive.db (SQLite: posts + FTS5 + sqlite-vec)
                             ├─ enrich.py ─┘  (OpenAI: tags + summary + embeddings)
                             └─ media.py ──► /var/www/tg-digest/media (pictures and videos for the digest)
systemd timer (daily) ──── digest.py ──► /var/www/tg-digest/index.html + feed.xml
Claude ──HTTPS + token──► Caddy ──► mcp_server.py --http
```

| File | What it does |
|---|---|
| `ingest.py` | Gets new posts of each channel in `channels.toml` with Telethon. It logs in as your Telegram user account. |
| `enrich.py` | Tags each post with OpenAI (topics, kind, availability, entities, summary) and stores an embedding. |
| `media.py` | Downloads the pictures and videos of new posts for the digest page. |
| `digest.py` | Builds the digest page for the latest finished 3-day window. |
| `mcp_server.py` | MCP tools: `search_posts`, `get_post`, `recent`, `list_topics`, `stats`. |
| `channels.toml` | The channel list, the channel descriptions, the topic list, and the digest settings. |
| `deploy/` | Scripts and systemd units for the VPS. |

## Setup guide

This guide is written so that a person or an AI agent can do it from start to end. Do the parts in this order:

1. Collect the accounts and values (Part A).
2. Install the project on your computer and fill the archive (Part B).
3. Deploy to the VPS (Part C).
4. Make the MCP server and the digest public over HTTPS (Part D).
5. Connect Claude (Part E).

If you only want local search on your computer, do Part A, Part B, and step E1.

### Rules for AI agents

- Do not print, copy into chat, or commit `.env`, `tg.session`, or `/etc/tg-archive-mcp.env`.
- `tg.session` gives full access to the Telegram account. Treat it as a password.
- Only the owner of the Telegram account can type the phone number and the login code. Ask the owner to run step B4 in a terminal.
- After the VPS has `tg.session`, do not run `ingest.py` on the local computer. Telegram can revoke a session that runs in two places at the same time.

### Part A. Collect the accounts and values

You need these items before you start:

| Item | Where to get it | Goes into |
|---|---|---|
| Telegram API ID and API hash | https://my.telegram.org → API development tools | `TG_API_ID`, `TG_API_HASH` in `.env` |
| A Telegram account with a phone number | Your own account. The code reads channels as this user. | Login in step B4 |
| OpenAI API key | https://platform.openai.com/api-keys | `OPENAI_API_KEY` in `.env` |
| A VPS with Ubuntu and root SSH key login | Any provider. Tested on Ubuntu 24.04 with 2 GB of memory. | `DEPLOY_HOST` in `.env`, for example `root@203.0.113.10` |
| A domain or subdomain that you control | Your DNS provider | `DOMAIN` in `.env`, for example `archive.example.com` |
| The public channels to archive | The `@username` of each channel | `channels.toml` |

The local computer needs `git`, `ssh`, `rsync`, `sqlite3`, and [uv](https://docs.astral.sh/uv/). macOS and Linux both work.

### Part B. Install locally and fill the archive

B1. Clone the repo and install the dependencies:

```bash
git clone <repo-url> tg-ml-archive
cd tg-ml-archive
uv sync
```

B2. Make the `.env` file:

```bash
cp .env.example .env
```

Then open `.env` and set these keys: `TG_API_ID`, `TG_API_HASH`, `OPENAI_API_KEY`, `DEPLOY_HOST`, `DOMAIN`.
Leave `MCP_TOKEN` empty. Step D1 makes the token on the server.

B3. Edit `channels.toml`:

1. Put your channels in the `channels` list (public usernames, with or without `@`).
2. In `[about]`, write one description for each channel. Use the username in lowercase as the key. The tagger reads this text, so describe what the channel posts and how its posts look.
3. In `[strip]`, remove the entries of channels that you do not archive. Add a regular expression only if a channel repeats an ad footer.
4. Change `[topics]`, `[kinds]`, and `[availability]` only if you need a different taxonomy.

B4. Get the posts (the account owner must do this step):

```bash
uv run ingest.py
```

On the first run, Telethon asks for the phone number and the login code. Then it saves `tg.session` and downloads the full history of each channel. A large channel can take some minutes.

Expected result: `ingest.py` prints the number of new posts for each channel.

B5. Tag a small sample, then examine the tags:

```bash
uv run enrich.py --sample 20
sqlite3 archive.db "select summary, topics, kind from posts where enriched_at is not null order by random() limit 20"
```

If the tags are wrong, improve `[about]` and `[topics]` in `channels.toml`. Then run the sample again.

B6. Tag and embed all posts:

```bash
uv run enrich.py
```

This step sends every post to OpenAI once. The cost depends on the number of posts and on `TAG_MODEL` (default `gpt-5-mini`).

B7. Do a test of the local search:

```bash
claude mcp add tg-archive -- uv run --directory "$PWD" mcp_server.py
```

Then ask Claude a question about the channels. Claude must call the `tg-archive` tools.

### Part C. Deploy to the VPS

C1. Make sure that SSH key login to the VPS works without a password prompt:

```bash
ssh -o BatchMode=yes "$(grep '^DEPLOY_HOST=' .env | cut -d= -f2)" true && echo ok
```

C2. Copy the code, the secrets, and the database to the VPS:

```bash
deploy/push.sh --with-data
```

The script does these steps:
- It makes the system users `tgarchive` and `tgmcp`, and the group `tgarchive-db`.
- It installs `ffmpeg` and `uv` on the VPS if they are not there.
- It copies the files that git tracks to `/opt/tg-ml-archive`.
- It copies `.env` and `tg.session` to `/opt/tg-ml-archive` (mode 600), and `archive.db` to `/var/lib/tg-archive/`.
- It installs and starts the hourly sync timer and the daily digest timer.

Run `--with-data` only one time. Later runs of `--with-data` overwrite the server database with your old local copy.

C3. Make sure that the sync works on the VPS:

```bash
ssh <DEPLOY_HOST> 'systemctl start tg-archive-sync && systemctl show -p Result --value tg-archive-sync'
```

Expected result: `success`. To read the log, run `journalctl -u tg-archive-sync -n 50` on the VPS.

From now on, the VPS is the only place that runs `ingest.py`. Your local `archive.db` does not get new posts.

### Part D. Public HTTPS endpoint and digest page

D1. Run the setup script one time:

```bash
deploy/go-public.sh
```

The script does these steps on the VPS:
1. It stops the sync for a short time. If the database is still in `/opt/tg-ml-archive`, it makes a backup in `/root` and moves the database to `/var/lib/tg-archive/`.
2. It makes `/etc/tg-archive-mcp.env` (root only) with a new random `MCP_TOKEN` and the OpenAI key. The script does not print the token.
3. It deploys the code again and starts `tg-archive-mcp.service` as user `tgmcp`. This user cannot read `tg.session` or `.env`.
4. It installs Caddy and writes `/etc/caddy/sites/tg-ml-archive.caddy` for `DOMAIN`. The main Caddyfile only imports `/etc/caddy/sites/*.caddy`.
5. It does a test of the MCP server: 401 without the token, 200 with the token.

You can run the script again. Each step checks the state before it changes something.

D2. At your DNS provider, add an A record: `DOMAIN` → the IP address of the VPS.

D3. If the VPS has a firewall, open TCP ports 80 and 443. Caddy needs them to get the TLS certificate.

D4. When the DNS record works, do a test of the endpoint:

```bash
curl -s -o /dev/null -w '%{http_code}\n' "https://<DOMAIN>/mcp"
```

Expected result: `401`. This shows that HTTPS works and that the server refuses requests without the token.

D5. The first digest page comes after the first 3-day window is finished. The windows start on `DIGEST_EPOCH` (default `2026-10-17`) at 00:00 UTC. To build a page now for a past window, run this on the VPS:

```bash
cd /opt/tg-ml-archive && runuser -u tgarchive -- env ARCHIVE_DB=/var/lib/tg-archive/archive.db .venv/bin/python digest.py --window 2026-10-17
```

Change the date to the first day of a finished window. Then open `https://<DOMAIN>/`.

### Part E. Connect Claude

E1. **Claude Code on the local computer, local database:** see step B7. This uses your local `archive.db`, which stops getting new posts after Part C.

E2. **Claude Code, VPS database (recommended):** get the token from the VPS into a shell variable, then add the server:

```bash
TOKEN=$(ssh <DEPLOY_HOST> "grep '^MCP_TOKEN=' /etc/tg-archive-mcp.env | cut -d= -f2")
claude mcp add --transport http -s user tg-archive "https://<DOMAIN>/mcp" --header "Authorization: Bearer $TOKEN"
unset TOKEN
```

E3. **claude.ai (web, desktop, and phone):** go to Settings → Connectors → Add custom connector. Use this URL:

```
https://<DOMAIN>/mcp?token=<MCP_TOKEN>
```

claude.ai connectors cannot send headers, so the token goes in the URL. Caddy keeps no access log, so the token is not written to disk on the server.

E4. Do a test: ask Claude "Show the archive stats". Claude must call the `stats` tool and show the post count of each channel.

## Daily operation

| Task | Command (run on the VPS unless noted) |
|---|---|
| Deploy code changes (on the local computer) | `deploy/push.sh` |
| Show the timers | `systemctl list-timers 'tg-archive-*'` |
| Run a sync now | `systemctl start tg-archive-sync` |
| Read the sync log | `journalctl -u tg-archive-sync -n 50` |
| Read the MCP server log | `journalctl -u tg-archive-mcp -n 50` |
| Add a channel | Edit `channels.toml` on the local computer, then run `deploy/push.sh`. The next sync gets its full history. |
| Change the topics | Edit `channels.toml`, deploy, then run `enrich.py --retag` on the VPS as `tgarchive`. |
| Rotate the MCP token | Edit `/etc/tg-archive-mcp.env`, run `systemctl restart tg-archive-mcp`, then update the clients (E2, E3). |

Run Python commands on the VPS as the user `tgarchive`, with the database path:

```bash
cd /opt/tg-ml-archive && runuser -u tgarchive -- env ARCHIVE_DB=/var/lib/tg-archive/archive.db .venv/bin/python <script>.py <flags>
```

## Configuration

All settings are in `.env`. `.env.example` lists them with their defaults.

| Key | Default | Use |
|---|---|---|
| `TG_API_ID`, `TG_API_HASH` | (required) | Telegram API credentials |
| `TG_SESSION` | `./tg` | Session file path without the `.session` extension |
| `OPENAI_API_KEY` | (required) | Tagging, embeddings, and the digest |
| `TAG_MODEL`, `TAG_EFFORT`, `TAG_WORKERS` | `gpt-5-mini`, `low`, `8` | The tagger |
| `EMBED_MODEL`, `EMBED_DIM` | `text-embedding-3-small`, `512` | Embeddings. Set `EMBED_DIM` before the first run. A change later needs a new database. |
| `ARCHIVE_DB` | `./archive.db` | Database path. The systemd units set `/var/lib/tg-archive/archive.db`. |
| `MCP_TOKEN`, `MCP_HOST`, `MCP_PORT` | (required for `--http`), `127.0.0.1`, `8765` | The HTTP MCP server |
| `DEPLOY_HOST` | (required for deploy) | SSH target of the VPS, for example `root@203.0.113.10` |
| `DOMAIN` | (required for Part D) | Public domain of the MCP endpoint and the digest page |
| `SITE_URL` | `https://$DOMAIN` | Base URL in the Atom feed |
| `DIGEST_EPOCH` | `2026-10-17` | First day of the first digest window |
| `DIGEST_WRITE_MODEL`, `DIGEST_WRITE_EFFORT` | `gpt-6-sol`, `medium` | The model that writes the digest text |
| `DIGEST_FLEX` | `1` | `0` turns off the OpenAI Flex tier |

## Digest page

`https://<DOMAIN>/` shows the latest finished 3-day window. The server keeps no old digests, and `/digest/...` links go to `/`. `/feed.xml` is an Atom feed with one entry for each new digest.

- `media.py` runs in the hourly sync. It saves the pictures, videos, and GIFs of new posts (full albums too) to `/var/www/tg-digest/media/`. ffmpeg converts videos to H.264 at 720p or less, with a poster frame. Posts that have only topics from `[digest] no_media` get no downloads.
- `digest.py` runs daily at 01:30 UTC (timer `tg-archive-digest`). When a new window is finished, it does these steps:
  1. It removes humor, off-topic posts, and empty posts (less than 60 characters and no useful links).
  2. It puts posts about the same thing into one item and gives each item an importance (gpt-5-mini). Then it merges items whose posts link the same GitHub repo.
  3. It writes each item in the language of its sources, and writes again each item that comes back in the wrong language.
  4. It writes the overview as short lists by theme, in Russian. To change the language, edit `OVERVIEW_SYSTEM` in `digest.py`.
  5. It saves the result to `/var/www/tg-digest/data.json`, renders `templates/` to `index.html` and `feed.xml`, and deletes the media of posts from before the new window.
- `[digest]` in `channels.toml` sets the section order and the sections without media.
- After a template change, run `digest.py --render-only`. This makes no AI calls. To write only a new overview, run `digest.py --redo-overview`.
- Cost: all calls use the OpenAI Flex tier at half price. If OpenAI refuses a Flex call, the code sends it again on the standard tier. One digest costs about $0.12 with `gpt-6-sol`. `data.json` stores the real cost under `usage`.
- Media on disk: the current window and the next window, 250 MB at most.

## Security model

- `tg.session` and `.env` are readable only by `tgarchive` (mode 600).
- The public MCP server runs as `tgmcp`. systemd blocks its access to `tg.session`, `.env`, `/root`, and `/home`. Its secrets are in `/etc/tg-archive-mcp.env`, which only root can read.
- The database is in `/var/lib/tg-archive/`. The group `tgarchive-db` gives both users access.
- Clients send `Authorization: Bearer <MCP_TOKEN>`, or `?token=` when they cannot send headers.
- Caddy keeps no access log, so tokens in URLs are not written to disk.

## Notes

- Keyword search uses prefix matching, not stemming. Semantic search (embeddings) finds paraphrases and works across Russian and English.
- The sync uses about 4 s of CPU per hour. The database is about 73 MB for 17,000 posts.

## License

MIT. See `LICENSE`.
