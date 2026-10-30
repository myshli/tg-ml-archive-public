"""Fetch new posts from the channels in channels.toml into archive.db (Telethon, user account).

First run asks for your phone number + login code and saves tg.session; later runs are silent.
    uv run ingest.py                 # new posts for all channels (full history on first run)
    uv run ingest.py --refresh-days 3   # also re-read the last 3 days to pick up edits/views
"""

import argparse
import asyncio
import json
import os
import re
import tomllib
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv
from telethon import TelegramClient
from telethon.tl.types import MessageEntityTextUrl, MessageEntityUrl

import db

ROOT = Path(__file__).parent
load_dotenv(ROOT / ".env")

URL_RE = re.compile(r"https?://[^\s<>()\"']+")


def extract_urls(msg) -> list[str]:
    urls: list[str] = []
    for ent, inner in msg.get_entities_text() or []:
        if isinstance(ent, MessageEntityTextUrl):
            urls.append(ent.url)
        elif isinstance(ent, MessageEntityUrl):
            urls.append(inner if inner.startswith("http") else "https://" + inner)
    urls += URL_RE.findall(msg.message or "")
    if msg.web_preview and getattr(msg.web_preview, "url", None):
        urls.append(msg.web_preview.url)
    seen, out = set(), []
    for u in urls:
        u = u.rstrip(".,;:!?»")
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out


def forwarded_from(msg) -> str | None:
    fwd = msg.fwd_from
    if not fwd:
        return None
    if fwd.from_name:
        return fwd.from_name
    chat = getattr(msg.forward, "chat", None) if msg.forward else None
    return getattr(chat, "username", None) or getattr(chat, "title", None) or "unknown"


UPSERT = """
INSERT INTO posts (channel, msg_id, date, edit_date, text, urls, hashtags, forwarded_from, views, has_media, permalink)
VALUES (:channel, :msg_id, :date, :edit_date, :text, :urls, :hashtags, :forwarded_from, :views, :has_media, :permalink)
ON CONFLICT (channel, msg_id) DO UPDATE SET
    views = excluded.views,
    edit_date = excluded.edit_date,
    urls = excluded.urls,
    hashtags = excluded.hashtags,
    -- an edited text needs fresh tags + embedding
    enriched_at = CASE WHEN posts.text != excluded.text THEN NULL ELSE posts.enriched_at END,
    embedded_at = CASE WHEN posts.text != excluded.text THEN NULL ELSE posts.embedded_at END,
    text = excluded.text
"""


def to_row(channel: str, msg) -> dict | None:
    text = msg.message or ""
    urls = extract_urls(msg)
    if not text.strip() and not urls:
        return None  # pure media / service message
    return {
        "channel": channel,
        "msg_id": msg.id,
        "date": msg.date.astimezone(timezone.utc).isoformat(),
        "edit_date": msg.edit_date.astimezone(timezone.utc).isoformat() if msg.edit_date else None,
        "text": text,
        "urls": json.dumps(urls, ensure_ascii=False),
        "hashtags": json.dumps(db.extract_hashtags(text), ensure_ascii=False),
        "forwarded_from": forwarded_from(msg),
        "views": msg.views,
        "has_media": int(msg.media is not None and not msg.web_preview),
        "permalink": f"https://t.me/{channel}/{msg.id}",
    }


async def ingest_channel(client, conn, username: str, refresh_days: int) -> int:
    entity = await client.get_entity(username)
    channel = (entity.username or username).lower()
    conn.execute(
        "INSERT INTO channels (username, title) VALUES (?, ?) "
        "ON CONFLICT (username) DO UPDATE SET title = excluded.title",
        (channel, entity.title),
    )
    last_id = conn.execute("SELECT last_id FROM channels WHERE username = ?", (channel,)).fetchone()[0]

    n, max_id = 0, last_id
    async for msg in client.iter_messages(entity, min_id=last_id, reverse=True):
        max_id = max(max_id, msg.id)
        if row := to_row(channel, msg):
            conn.execute(UPSERT, row)
            n += 1
        if n and n % 200 == 0:
            conn.execute("UPDATE channels SET last_id = ? WHERE username = ?", (max_id, channel))
            conn.commit()
            print(f"  {channel}: {n} posts…", flush=True)
    conn.execute("UPDATE channels SET last_id = ? WHERE username = ?", (max_id, channel))

    if refresh_days and last_id:
        cutoff = datetime.now(timezone.utc) - timedelta(days=refresh_days)
        async for msg in client.iter_messages(entity):
            if msg.date < cutoff:
                break
            if row := to_row(channel, msg):
                conn.execute(UPSERT, row)
    conn.commit()
    return n


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--refresh-days", type=int, default=0)
    ap.add_argument("--only", help="ingest just this channel username")
    args = ap.parse_args()

    channels = tomllib.loads((ROOT / "channels.toml").read_text())["channels"]
    if args.only:
        channels = [args.only]

    conn = db.connect()
    session = os.environ.get("TG_SESSION", str(ROOT / "tg"))
    async with TelegramClient(session, int(os.environ["TG_API_ID"]), os.environ["TG_API_HASH"]) as client:
        for username in channels:
            n = await ingest_channel(client, conn, username.lstrip("@"), args.refresh_days)
            print(f"{username}: +{n} new posts")


if __name__ == "__main__":
    asyncio.run(main())
