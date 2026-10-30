"""SQLite storage: posts table, FTS5 keyword index, sqlite-vec vector index, hybrid search."""

import json
import os
import re
import sqlite3
import struct
from pathlib import Path

import sqlite_vec
from dotenv import load_dotenv

_ENV = Path(__file__).parent / ".env"
if os.access(_ENV, os.R_OK):  # the public MCP user cannot read it; its env comes from systemd
    load_dotenv(_ENV)

DB_PATH = Path(os.environ.get("ARCHIVE_DB", Path(__file__).parent / "archive.db"))
EMBED_DIM = int(os.environ.get("EMBED_DIM", "512"))

SCHEMA = f"""
CREATE TABLE IF NOT EXISTS posts (
    id            INTEGER PRIMARY KEY,
    channel       TEXT NOT NULL,
    msg_id        INTEGER NOT NULL,
    date          TEXT NOT NULL,          -- ISO 8601 UTC
    edit_date     TEXT,
    text          TEXT NOT NULL,
    urls          TEXT NOT NULL DEFAULT '[]',   -- JSON list
    forwarded_from TEXT,
    views         INTEGER,
    has_media     INTEGER NOT NULL DEFAULT 0,
    permalink     TEXT NOT NULL,
    hashtags      TEXT NOT NULL DEFAULT '[]',   -- JSON list, the author's own #tags, lowercased
    -- enrichment (NULL until enrich.py runs)
    summary       TEXT,
    topics        TEXT,                   -- JSON list
    kind          TEXT,
    entities      TEXT,                   -- JSON list
    availability  TEXT,
    enriched_at   TEXT,
    embedded_at   TEXT,
    UNIQUE (channel, msg_id)
);
CREATE INDEX IF NOT EXISTS posts_date ON posts(date);
CREATE INDEX IF NOT EXISTS posts_channel ON posts(channel, msg_id);

CREATE TABLE IF NOT EXISTS channels (
    username  TEXT PRIMARY KEY,
    title     TEXT,
    last_id   INTEGER NOT NULL DEFAULT 0
);

CREATE VIRTUAL TABLE IF NOT EXISTS posts_fts USING fts5(
    text, summary, entities, content='posts', content_rowid='id',
    tokenize='unicode61 remove_diacritics 2'
);
CREATE TRIGGER IF NOT EXISTS posts_ai AFTER INSERT ON posts BEGIN
    INSERT INTO posts_fts(rowid, text, summary, entities)
    VALUES (new.id, new.text, coalesce(new.summary, ''), coalesce(new.entities, ''));
END;
CREATE TRIGGER IF NOT EXISTS posts_ad AFTER DELETE ON posts BEGIN
    INSERT INTO posts_fts(posts_fts, rowid, text, summary, entities)
    VALUES ('delete', old.id, old.text, coalesce(old.summary, ''), coalesce(old.entities, ''));
END;
CREATE TRIGGER IF NOT EXISTS posts_au AFTER UPDATE OF text, summary, entities ON posts BEGIN
    INSERT INTO posts_fts(posts_fts, rowid, text, summary, entities)
    VALUES ('delete', old.id, old.text, coalesce(old.summary, ''), coalesce(old.entities, ''));
    INSERT INTO posts_fts(rowid, text, summary, entities)
    VALUES (new.id, new.text, coalesce(new.summary, ''), coalesce(new.entities, ''));
END;

-- Images and videos saved by media.py (paths are relative to MEDIA_DIR). An album's files all
-- belong to the post that carries the album's text.
CREATE TABLE IF NOT EXISTS media (
    id          INTEGER PRIMARY KEY,
    post_id     INTEGER NOT NULL REFERENCES posts(id) ON DELETE CASCADE,
    idx         INTEGER NOT NULL,           -- order inside the post / album
    src_msg_id  INTEGER NOT NULL,           -- the Telegram message the file came from
    kind        TEXT NOT NULL,              -- photo | video | gif
    path        TEXT,                       -- NULL once pruned
    poster      TEXT,                       -- still frame for videos
    width       INTEGER,
    height      INTEGER,
    duration    REAL,
    bytes       INTEGER,
    saved_at    TEXT NOT NULL,
    UNIQUE (post_id, idx)
);

CREATE VIRTUAL TABLE IF NOT EXISTS vec_posts USING vec0(
    post_id INTEGER PRIMARY KEY,
    embedding float[{EMBED_DIM}] distance_metric=cosine
);
"""


HASHTAG_RE = re.compile(r"#(\w+)")


URL_RE = re.compile(r"https?://\S+")


def extract_hashtags(text: str) -> list[str]:
    # Drop URLs first so #fragments in links don't become tags.
    return sorted({h.lower() for h in HASHTAG_RE.findall(URL_RE.sub(" ", text)) if not h.isdigit()})


def _migrate(conn: sqlite3.Connection) -> None:
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(posts)")}
    if "availability" not in cols:
        conn.execute("ALTER TABLE posts ADD COLUMN availability TEXT")
    if "media_at" not in cols:  # set by media.py once a post's files are saved (or found to be none)
        conn.execute("ALTER TABLE posts ADD COLUMN media_at TEXT")
    if "hashtags" not in cols:
        conn.execute("ALTER TABLE posts ADD COLUMN hashtags TEXT NOT NULL DEFAULT '[]'")
        rows = conn.execute("SELECT id, text FROM posts").fetchall()
        conn.executemany(
            "UPDATE posts SET hashtags = ? WHERE id = ?",
            [(json.dumps(extract_hashtags(r["text"]), ensure_ascii=False), r["id"]) for r in rows],
        )
    conn.commit()


def connect(path: Path = DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.executescript(SCHEMA)
    _migrate(conn)
    return conn


def pack_vec(vec: list[float]) -> bytes:
    return struct.pack(f"{len(vec)}f", *vec)


def row_to_dict(row: sqlite3.Row, full_text: bool = False, snippet_len: int = 400) -> dict:
    d = dict(row)
    for key in ("urls", "topics", "entities", "hashtags"):
        if d.get(key):
            d[key] = json.loads(d[key])
    if not full_text and len(d["text"]) > snippet_len:
        d["text"] = d["text"][:snippet_len] + "…"
    for key in ("id", "enriched_at", "embedded_at", "edit_date"):
        d.pop(key, None)
    return d


def _fts_query(q: str) -> str:
    # Quote each term so user input can't break FTS5 syntax; OR them for recall, BM25 ranks.
    # Prefix match (term*) stands in for stemming: "агент" finds "агентов", "agent" finds "agents".
    terms = [t.replace('"', "") for t in q.split() if t.strip('"')]
    return " OR ".join(f'"{t}"*' for t in terms)


def _filters(channels, since, until, topics, kind, hashtags=None, availability=None) -> tuple[str, list]:
    where, args = [], []
    if channels:
        where.append(f"p.channel IN ({','.join('?' * len(channels))})")
        args += [c.lstrip("@").lower() for c in channels]
    if since:
        where.append("p.date >= ?")
        args.append(since)
    if until:
        where.append("p.date < ?")
        args.append(until)
    if kind:
        where.append("p.kind = ?")
        args.append(kind)
    if topics:
        where.append(
            f"EXISTS (SELECT 1 FROM json_each(p.topics) WHERE value IN ({','.join('?' * len(topics))}))"
        )
        args += topics
    if hashtags:
        where.append(
            f"EXISTS (SELECT 1 FROM json_each(p.hashtags) WHERE value IN ({','.join('?' * len(hashtags))}))"
        )
        args += [h.lstrip("#").lower() for h in hashtags]
    if availability:
        where.append(f"p.availability IN ({','.join('?' * len(availability))})")
        args += availability
    return (" AND " + " AND ".join(where)) if where else "", args


def hybrid_search(
    conn: sqlite3.Connection,
    query: str,
    query_vec: list[float] | None,
    channels: list[str] | None = None,
    since: str | None = None,
    until: str | None = None,
    topics: list[str] | None = None,
    kind: str | None = None,
    limit: int = 15,
    hashtags: list[str] | None = None,
    availability: list[str] | None = None,
) -> list[dict]:
    """FTS5 + vector kNN merged with reciprocal-rank fusion, then filtered."""
    filt, fargs = _filters(channels, since, until, topics, kind, hashtags, availability)
    pool = max(limit * 4, 60)
    k = 60  # RRF constant
    scores: dict[int, float] = {}

    fts = _fts_query(query)
    if fts:
        rows = conn.execute(
            f"""SELECT p.id FROM posts_fts f JOIN posts p ON p.id = f.rowid
                WHERE posts_fts MATCH ? {filt} ORDER BY bm25(posts_fts) LIMIT ?""",
            [fts, *fargs, pool],
        ).fetchall()
        for rank, r in enumerate(rows):
            scores[r["id"]] = scores.get(r["id"], 0) + 1 / (k + rank)

    if query_vec is not None:
        # vec0 kNN can't take arbitrary filters, so over-fetch then filter in the join.
        rows = conn.execute(
            f"""WITH knn AS (
                    SELECT post_id, distance FROM vec_posts
                    WHERE embedding MATCH ? AND k = ?
                )
                SELECT p.id FROM knn JOIN posts p ON p.id = knn.post_id
                WHERE 1=1 {filt} ORDER BY knn.distance""",
            [pack_vec(query_vec), pool * (4 if filt else 1), *fargs],
        ).fetchall()
        for rank, r in enumerate(rows):
            scores[r["id"]] = scores.get(r["id"], 0) + 1 / (k + rank)

    top = sorted(scores, key=scores.get, reverse=True)[:limit]
    if not top:
        return []
    rows = conn.execute(
        f"SELECT * FROM posts WHERE id IN ({','.join('?' * len(top))})", top
    ).fetchall()
    by_id = {r["id"]: r for r in rows}
    return [row_to_dict(by_id[i]) for i in top]
