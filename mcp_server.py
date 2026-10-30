"""MCP server exposing the archive to Claude.

    uv run mcp_server.py            # stdio (local Claude Code / Desktop)
    uv run mcp_server.py --http     # streamable HTTP on 127.0.0.1:8765/mcp, needs MCP_TOKEN
"""

import argparse
import hmac
import json
import logging
import os
import tomllib
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv
from mcp.server.mcpserver import MCPServer

import db

ROOT = Path(__file__).parent
if os.access(ROOT / ".env", os.R_OK):  # see db.py
    load_dotenv(ROOT / ".env")

for _name in ("httpx", "httpx2"):  # one INFO line per OpenAI call is noise
    logging.getLogger(_name).setLevel(logging.WARNING)

CONFIG = tomllib.loads((ROOT / "channels.toml").read_text())

mcp = MCPServer(
    name="tg-ml-archive",
    instructions=(
        "Archive of posts from ML news Telegram channels (mostly Russian). "
        "Use search_posts for any question about what was posted; queries work in English or Russian. "
        "Run several searches with different phrasings or filters for broad questions. "
        "Always cite posts with their permalink. Dates are UTC ISO 8601."
    ),
)

_openai = None


def embed_query(q: str) -> list[float] | None:
    global _openai
    if not os.environ.get("OPENAI_API_KEY"):
        return None
    from openai import OpenAI

    _openai = _openai or OpenAI()
    resp = _openai.embeddings.create(
        model=os.environ.get("EMBED_MODEL", "text-embedding-3-small"), input=[q], dimensions=db.EMBED_DIM
    )
    return resp.data[0].embedding


@mcp.tool()
def search_posts(
    query: str,
    channels: list[str] | None = None,
    since: str | None = None,
    until: str | None = None,
    topics: list[str] | None = None,
    kind: str | None = None,
    hashtags: list[str] | None = None,
    availability: list[str] | None = None,
    limit: int = 15,
) -> list[dict]:
    """Hybrid keyword + semantic search over all archived posts.

    query: natural-language or keyword query (any language; exact names like "Qwen3" or arXiv ids work too).
    channels: restrict to these channel usernames.
    since / until: ISO dates, e.g. "2026-09-01" (until is exclusive).
    topics: any-of filter, values from list_topics().
    kind: one post type from list_topics() (e.g. model-release, tool-release, paper, tutorial, humor).
    hashtags: any-of filter on the author's own hashtags, e.g. ["text2video", "gaussian"] (very precise).
    availability: any-of filter, e.g. ["open-weights", "code-only"] for things you can run locally.
    Returns posts with date, channel, summary, topics, links, text snippet and permalink.
    """
    conn = db.connect()
    try:
        return db.hybrid_search(
            conn, query, embed_query(query), channels, since, until, topics, kind, min(limit, 50),
            hashtags, availability,
        )
    finally:
        conn.close()


@mcp.tool()
def get_post(channel: str, msg_id: int) -> dict:
    """Full text and metadata of one post (use after search_posts returns a snippet)."""
    conn = db.connect()
    try:
        row = conn.execute(
            "SELECT * FROM posts WHERE channel = ? AND msg_id = ?", (channel.lstrip("@").lower(), msg_id)
        ).fetchone()
        return db.row_to_dict(row, full_text=True) if row else {"error": "not found"}
    finally:
        conn.close()


@mcp.tool()
def recent(days: int = 7, topics: list[str] | None = None, channels: list[str] | None = None, limit: int = 40) -> list[dict]:
    """Posts from the last N days, newest first, optionally filtered by topics/channels.
    Good for "what happened this week in agents"."""
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    filt, args = db._filters(channels, since, None, topics, None)
    conn = db.connect()
    try:
        rows = conn.execute(
            f"SELECT * FROM posts p WHERE 1=1 {filt} ORDER BY p.date DESC LIMIT ?", [*args, min(limit, 100)]
        ).fetchall()
        return [db.row_to_dict(r, snippet_len=250) for r in rows]
    finally:
        conn.close()


@mcp.tool()
def list_topics(top_hashtags: int = 150) -> dict:
    """Topics, post kinds and availability values (with descriptions and post counts),
    plus the author's most used hashtags. Call this to pick filters for search_posts."""
    conn = db.connect()
    try:
        def counts(sql: str) -> dict:
            return dict(conn.execute(sql).fetchall())

        topic_n = counts("SELECT value, count(*) FROM posts, json_each(posts.topics) GROUP BY value")
        kind_n = counts("SELECT kind, count(*) FROM posts WHERE kind IS NOT NULL GROUP BY kind")
        avail_n = counts("SELECT availability, count(*) FROM posts WHERE availability IS NOT NULL GROUP BY 1")
        tags = conn.execute(
            "SELECT value, count(*) FROM posts, json_each(posts.hashtags) GROUP BY value ORDER BY 2 DESC LIMIT ?",
            (top_hashtags,),
        ).fetchall()
        return {
            "topics": {t: {"about": d, "posts": topic_n.get(t, 0)} for t, d in CONFIG["topics"].items()},
            "kinds": {k: {"about": d, "posts": kind_n.get(k, 0)} for k, d in CONFIG["kinds"].items()},
            "availability": {a: {"about": d, "posts": avail_n.get(a, 0)} for a, d in CONFIG["availability"].items()},
            "hashtags": dict(tags),
        }
    finally:
        conn.close()


@mcp.tool()
def stats() -> list[dict]:
    """Per-channel post counts, date range and enrichment progress."""
    conn = db.connect()
    try:
        rows = conn.execute(
            """SELECT p.channel, c.title, count(*) AS posts, min(p.date) AS first, max(p.date) AS last,
                      sum(p.enriched_at IS NOT NULL) AS tagged, sum(p.embedded_at IS NOT NULL) AS embedded
               FROM posts p LEFT JOIN channels c ON c.username = p.channel GROUP BY p.channel"""
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


class BearerAuth:
    """Tiny ASGI guard: requires `Authorization: Bearer <MCP_TOKEN>` or `?token=<MCP_TOKEN>`
    (the query form is for clients that can't set headers, e.g. Claude Desktop custom connectors)."""

    def __init__(self, app, token: str):
        self.app, self.token = app, token.encode()

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            headers = dict(scope["headers"])
            auth = headers.get(b"authorization", b"")
            supplied = auth[7:] if auth.lower().startswith(b"bearer ") else b""
            if not supplied:
                from urllib.parse import parse_qs

                supplied = parse_qs(scope.get("query_string", b"").decode()).get("token", [""])[0].encode()
            if not hmac.compare_digest(supplied, self.token):
                await send({"type": "http.response.start", "status": 401, "headers": [(b"content-type", b"application/json")]})
                await send({"type": "http.response.body", "body": json.dumps({"error": "unauthorized"}).encode()})
                return
        await self.app(scope, receive, send)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--http", action="store_true")
    ap.add_argument("--host", default=os.environ.get("MCP_HOST", "127.0.0.1"))
    ap.add_argument("--port", type=int, default=int(os.environ.get("MCP_PORT", "8765")))
    args = ap.parse_args()

    if not args.http:
        mcp.run()
        return

    import uvicorn
    from mcp.server.transport_security import TransportSecuritySettings

    token = os.environ.get("MCP_TOKEN")
    if not token:
        raise SystemExit("MCP_TOKEN is required for --http")
    # Behind Caddy the Host header is the public domain; the bearer token is the protection here.
    app = mcp.streamable_http_app(
        stateless_http=True,
        json_response=True,
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )
    uvicorn.run(BearerAuth(app, token), host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
