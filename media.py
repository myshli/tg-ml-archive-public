"""Save the images and videos of recent posts for the digest pages (Telethon + ffmpeg).

    uv run media.py               # posts from the last 3 days that have no media saved yet
    uv run media.py --days 7      # look further back
    uv run media.py --prune 90    # delete files of posts older than 90 days (pages then link to Telegram)

Photos are kept as Telegram sends them. Videos and GIFs are re-encoded to H.264, at most 720p,
with a still frame as poster. Files go to MEDIA_DIR/<channel>/<msg_id>_<n>.<ext>.
"""

import argparse
import asyncio
import json
import os
import shutil
import subprocess
import tempfile
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv
from telethon import TelegramClient

import db

ROOT = Path(__file__).parent
load_dotenv(ROOT / ".env")

MEDIA_DIR = Path(os.environ.get("MEDIA_DIR", "/var/www/tg-digest/media"))
MAX_BYTES = int(os.environ.get("MEDIA_MAX_MB", "300")) * 1024 * 1024
MAX_PER_POST = 10
_CFG = __import__("tomllib").loads((Path(__file__).parent / "channels.toml").read_text())
NO_MEDIA = set(_CFG.get("digest", {}).get("no_media", []))
FFMPEG = shutil.which("ffmpeg") or "ffmpeg"
FFPROBE = shutil.which("ffprobe") or "ffprobe"


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def kind_of(msg) -> str | None:
    if msg.photo:
        return "photo"
    if msg.gif:
        return "gif"
    if msg.video:  # includes round video messages
        return "video"
    return None  # documents, audio, stickers, web previews: not shown on pages


def probe(path: Path) -> dict:
    out = subprocess.run(
        [FFPROBE, "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height:format=duration",
         "-of", "json", str(path)],
        capture_output=True, text=True,
    )
    try:
        info = json.loads(out.stdout)
        st = (info.get("streams") or [{}])[0]
        return {"width": st.get("width"), "height": st.get("height"),
                "duration": float(info.get("format", {}).get("duration") or 0) or None}
    except (ValueError, IndexError):
        return {}


def encode_video(src: Path, dst: Path, poster: Path, with_audio: bool) -> None:
    scale = "scale=-2:'min(720,ih)':flags=bicubic,format=yuv420p"
    audio = ["-c:a", "aac", "-b:a", "96k"] if with_audio else ["-an"]
    subprocess.run(
        [FFMPEG, "-y", "-v", "error", "-i", str(src), "-vf", scale, "-c:v", "libx264", "-preset", "veryfast",
         "-crf", "28", *audio, "-movflags", "+faststart", "-threads", "1", str(dst)],
        check=True, timeout=1800,
    )
    subprocess.run(
        [FFMPEG, "-y", "-v", "error", "-ss", "0.5", "-i", str(dst), "-frames:v", "1", "-q:v", "4", str(poster)],
        check=False, timeout=120,
    )
    if not poster.exists():  # very short clips: take the first frame
        subprocess.run([FFMPEG, "-y", "-v", "error", "-i", str(dst), "-frames:v", "1", "-q:v", "4", str(poster)],
                       check=False, timeout=120)


async def save_one(client, msg, kind: str, channel: str, base: str, tmp: Path) -> dict | None:
    size = getattr(msg.file, "size", None) or 0
    if size > MAX_BYTES:
        print(f"  skip {channel}/{msg.id}: {size // 2**20} MB", flush=True)
        return None
    out_dir = MEDIA_DIR / channel
    out_dir.mkdir(parents=True, exist_ok=True)
    out_dir.chmod(0o755)
    raw = Path(await client.download_media(msg, file=str(tmp / f"raw_{msg.id}")))
    if kind == "photo":
        dst = out_dir / f"{base}.jpg"
        shutil.move(raw, dst)
        info, poster = probe(dst), None
    else:
        dst, poster_path = out_dir / f"{base}.mp4", out_dir / f"{base}_poster.jpg"
        try:
            encode_video(raw, dst, poster_path, with_audio=(kind == "video"))
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
            print(f"  encode failed {channel}/{msg.id}: {e}", flush=True)
            dst.unlink(missing_ok=True)
            return None
        finally:
            raw.unlink(missing_ok=True)
        info = probe(dst)
        poster = poster_path.relative_to(MEDIA_DIR).as_posix() if poster_path.exists() else None
    # The sync service runs with UMask=0007 (the database must stay private to its group); these files are public
    # and Caddy reads them as another user, so set their mode explicitly.
    for f in (dst, MEDIA_DIR / poster if poster else None):
        if f:
            f.chmod(0o644)
    return {"kind": kind, "path": dst.relative_to(MEDIA_DIR).as_posix(), "poster": poster,
            "bytes": dst.stat().st_size, **info}


async def save_channel(client, conn, channel: str, since: datetime) -> tuple[int, int]:
    posts = conn.execute(
        "SELECT id, msg_id, topics FROM posts WHERE channel = ? AND date >= ? AND media_at IS NULL ORDER BY msg_id",
        (channel, since.isoformat()),
    ).fetchall()
    if not posts:
        return 0, 0
    # One pass over the window's messages: albums are several messages sharing grouped_id,
    # and only one of them carries the text (that one is the post).
    lo = posts[0]["msg_id"] - MAX_PER_POST
    msgs, groups = {}, defaultdict(list)
    async for m in client.iter_messages(channel, min_id=max(lo - 1, 0), reverse=True):
        msgs[m.id] = m
        if m.grouped_id:
            groups[m.grouped_id].append(m)

    n_posts = n_files = 0
    with tempfile.TemporaryDirectory(prefix="tgmedia_") as td:
        for p in posts:
            msg = msgs.get(p["msg_id"])
            topics = set(json.loads(p["topics"] or "[]"))
            if topics and topics <= NO_MEDIA:  # the digest shows these sections as text only
                msg = None
            if msg is None:  # deleted since ingest, or text-only
                conn.execute("UPDATE posts SET media_at = ? WHERE id = ?", (now(), p["id"]))
                continue
            parts = sorted(groups[msg.grouped_id], key=lambda m: m.id) if msg.grouped_id else [msg]
            idx = 0
            for part in parts[:MAX_PER_POST]:
                if not (kind := kind_of(part)):
                    continue
                saved = await save_one(client, part, kind, channel, f"{msg.id}_{idx}", Path(td))
                if saved:
                    conn.execute(
                        """INSERT OR REPLACE INTO media (post_id, idx, src_msg_id, kind, path, poster, width, height,
                           duration, bytes, saved_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                        (p["id"], idx, part.id, saved["kind"], saved["path"], saved["poster"], saved.get("width"),
                         saved.get("height"), saved.get("duration"), saved["bytes"], now()),
                    )
                    idx += 1
                    n_files += 1
            conn.execute("UPDATE posts SET has_media = ?, media_at = ? WHERE id = ?", (int(idx > 0), now(), p["id"]))
            conn.commit()
            n_posts += 1
    return n_posts, n_files


def prune(conn, days: int) -> int:
    return prune_before(conn, datetime.now(timezone.utc) - timedelta(days=days))


def prune_before(conn, before: datetime) -> int:
    """Delete the files of posts dated before `before`; returns how many media rows were cleared."""
    cutoff = before.isoformat()
    rows = conn.execute(
        """SELECT m.id, m.path, m.poster FROM media m JOIN posts p ON p.id = m.post_id
           WHERE p.date < ? AND m.path IS NOT NULL""", (cutoff,),
    ).fetchall()
    for r in rows:
        for rel in (r["path"], r["poster"]):
            if rel:
                (MEDIA_DIR / rel).unlink(missing_ok=True)
        conn.execute("UPDATE media SET path = NULL, poster = NULL WHERE id = ?", (r["id"],))
    conn.commit()
    return len(rows)


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=3)
    ap.add_argument("--prune", type=int, metavar="DAYS", help="only delete files of posts older than DAYS")
    args = ap.parse_args()

    conn = db.connect()
    if args.prune:
        print(f"pruned {prune(conn, args.prune)} files")
        return

    import tomllib
    channels = [c.lstrip("@").lower() for c in tomllib.loads((ROOT / "channels.toml").read_text())["channels"]]
    since = datetime.now(timezone.utc) - timedelta(days=args.days)
    session = os.environ.get("TG_SESSION", str(ROOT / "tg"))
    async with TelegramClient(session, int(os.environ["TG_API_ID"]), os.environ["TG_API_HASH"]) as client:
        for ch in channels:
            n_posts, n_files = await save_channel(client, conn, ch, since)
            print(f"{ch}: {n_files} files from {n_posts} posts")


if __name__ == "__main__":
    asyncio.run(main())
