"""Tag and embed posts that need it (OpenAI). Safe to re-run: only touches un-enriched rows.

    uv run enrich.py              # tag + embed new posts
    uv run enrich.py --retag      # re-tag everything (after changing the taxonomy)
    uv run enrich.py --sample 100 # tag 100 random posts to check quality
    uv run enrich.py --retag-where "text LIKE '%workflow%'"   # re-tag a subset after adding a category
"""

import argparse
import json
import os
import re
import tomllib
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from openai import OpenAI

import db

ROOT = Path(__file__).parent
load_dotenv(ROOT / ".env")

TAG_MODEL = os.environ.get("TAG_MODEL", "gpt-5-mini")
EMBED_MODEL = os.environ.get("EMBED_MODEL", "text-embedding-3-small")
TAG_EFFORT = os.environ.get("TAG_EFFORT", "low")  # low: ~same tags as default, half the output tokens
TAG_BATCH = 20
TAG_WORKERS = int(os.environ.get("TAG_WORKERS", "8"))
EMBED_BATCH = 100
MAX_CHARS = 3000  # per post sent to the tagger / embedder

CONFIG = tomllib.loads((ROOT / "channels.toml").read_text())
TOPICS, KINDS, AVAILABILITY = CONFIG["topics"], CONFIG["kinds"], CONFIG["availability"]
ABOUT = CONFIG.get("about", {})
STRIP = {ch: re.compile(rx) for ch, rx in CONFIG.get("strip", {}).items()}


def _describe(d: dict) -> str:
    return "\n".join(f"  - {k}: {v}" for k, v in d.items())


SYSTEM = f"""You tag posts from Telegram channels about generative AI, mostly for artists and creators.
Each post is labelled with its channel. The channels:
{_describe(ABOUT)}
For each post return:
- summary: one concise English sentence saying what the post is about. Name the model, paper or tool.
  For a digest, list the main items briefly.
- topics: 1-3 labels (up to 5 for a digest) from this list only:
{_describe(TOPICS)}
- kind: exactly one of:
{_describe(KINDS)}
- availability: exactly one of:
{_describe(AVAILABILITY)}
  Judge availability from concrete evidence (Hugging Face / GitHub / Civitai links, "weights", "code"), not from
  words like "open source" in a title.
- entities: named models, papers, products, companies, people, datasets mentioned (original spelling, max 10).
The author's hashtags in the text are a strong hint for topics. A post that is only "#humor" or a joke is kind
"humor"; give it the topic it jokes about, or "offtopic". Return one item per input post, keyed by its ref number."""

SCHEMA = {
    "name": "tags",
    "strict": True,
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "required": ["posts"],
        "properties": {
            "posts": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["ref", "summary", "topics", "kind", "availability", "entities"],
                    "properties": {
                        "ref": {"type": "integer"},
                        "summary": {"type": "string"},
                        "topics": {"type": "array", "items": {"type": "string", "enum": list(TOPICS)}},
                        "kind": {"type": "string", "enum": list(KINDS)},
                        "availability": {"type": "string", "enum": list(AVAILABILITY)},
                        "entities": {"type": "array", "items": {"type": "string"}},
                    },
                },
            }
        },
    },
}


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def post_blob(row) -> str:
    urls = json.loads(row["urls"])
    text = row["text"]
    if rx := STRIP.get(row["channel"]):
        text = rx.sub("", text).rstrip()
    parts = [text[:MAX_CHARS]]
    if urls:
        parts.append("Links: " + " ".join(urls[:10]))
    if row["forwarded_from"]:
        parts.append(f"Forwarded from: {row['forwarded_from']}")
    return "\n".join(parts)


def tag(client: OpenAI, conn, retag: bool, limit: int | None, sample: int | None = None,
        retag_where: str | None = None) -> int:
    if retag_where:
        rows = conn.execute(f"SELECT * FROM posts WHERE {retag_where} ORDER BY date").fetchall()
    elif sample:
        rows = conn.execute("SELECT * FROM posts ORDER BY random() LIMIT ?", (sample,)).fetchall()
    else:
        where = "1=1" if retag else "enriched_at IS NULL"
        rows = conn.execute(
            f"SELECT * FROM posts WHERE {where} ORDER BY date" + (f" LIMIT {int(limit)}" if limit else "")
        ).fetchall()
    def call(batch):
        user = "\n\n".join(f"### ref {r['id']} (channel: {r['channel']})\n{post_blob(r)}" for r in batch)
        resp = client.chat.completions.create(
            model=TAG_MODEL,
            reasoning_effort=TAG_EFFORT,
            messages=[{"role": "system", "content": SYSTEM}, {"role": "user", "content": user}],
            response_format={"type": "json_schema", "json_schema": SCHEMA},
        )
        return batch, json.loads(resp.choices[0].message.content)["posts"]

    batches = [rows[i : i + TAG_BATCH] for i in range(0, len(rows), TAG_BATCH)]
    done = finished = 0
    # API calls run in threads; all SQLite writes stay on this thread.
    with ThreadPoolExecutor(TAG_WORKERS) as pool:
        for fut in as_completed(pool.submit(call, b) for b in batches):
            finished += 1
            try:
                batch, items = fut.result()
            except Exception as e:  # leave the batch un-enriched; the next run retries it
                print(f"  batch failed: {e}", flush=True)
                continue
            ids = {r["id"] for r in batch}
            for it in items:
                if it["ref"] not in ids:
                    continue
                conn.execute(
                    """UPDATE posts SET summary = ?, topics = ?, kind = ?, availability = ?, entities = ?,
                       enriched_at = ?, embedded_at = NULL WHERE id = ?""",
                    (
                        it["summary"],
                        json.dumps(it["topics"]),
                        it["kind"],
                        it["availability"],
                        json.dumps(it["entities"], ensure_ascii=False),
                        now(),
                        it["ref"],
                    ),
                )
                done += 1
            conn.commit()
            print(f"  tagged batch {finished}/{len(batches)}", flush=True)
    return done


def embed_texts(client: OpenAI, texts: list[str]) -> list[list[float]]:
    resp = client.embeddings.create(model=EMBED_MODEL, input=texts, dimensions=db.EMBED_DIM)
    return [d.embedding for d in resp.data]


def embed(client: OpenAI, conn, limit: int | None) -> int:
    # Embed after tagging so the English summary + entities help cross-language recall.
    rows = conn.execute(
        "SELECT * FROM posts WHERE embedded_at IS NULL AND enriched_at IS NOT NULL ORDER BY date"
        + (f" LIMIT {int(limit)}" if limit else "")
    ).fetchall()
    for i in range(0, len(rows), EMBED_BATCH):
        batch = rows[i : i + EMBED_BATCH]
        texts = [
            f"{r['summary']}\nEntities: {', '.join(json.loads(r['entities'] or '[]'))}\n\n{post_blob(r)}"
            for r in batch
        ]
        for r, vec in zip(batch, embed_texts(client, texts)):
            conn.execute("DELETE FROM vec_posts WHERE post_id = ?", (r["id"],))
            conn.execute(
                "INSERT INTO vec_posts (post_id, embedding) VALUES (?, ?)", (r["id"], db.pack_vec(vec))
            )
            conn.execute("UPDATE posts SET embedded_at = ? WHERE id = ?", (now(), r["id"]))
        conn.commit()
        print(f"  embedded {min(i + EMBED_BATCH, len(rows))}/{len(rows)}", flush=True)
    return len(rows)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--retag", action="store_true")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--retag-where", help="re-tag only posts matching this SQL condition on the posts table")
    ap.add_argument("--sample", type=int, help="tag N random posts only (to check tag quality), no embedding")
    args = ap.parse_args()

    client = OpenAI()
    conn = db.connect()
    print(f"tagged {tag(client, conn, args.retag, args.limit, args.sample, args.retag_where)} posts")
    if args.sample:
        return
    print(f"embedded {embed(client, conn, args.limit)} posts")


if __name__ == "__main__":
    main()
