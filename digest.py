"""Build the digest page for the latest finished 3-day window: SITE_DIR/index.html, plus an Atom feed.

    uv run digest.py                     # build the latest finished window, if it is not the one shown now
    uv run digest.py --window 2026-10-17 # (re)build a given window (start date, UTC)
    uv run digest.py --render-only       # re-render the page from SITE_DIR/data.json (no AI calls)

Windows are fixed: DIGEST_EPOCH + 3k days, 00:00 UTC. Only the latest digest is kept: after a new page is
written, the media of posts from before its window are deleted.
"""

import argparse
import json
import os
import re
import tomllib
from concurrent.futures import ThreadPoolExecutor
from threading import Lock
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv
from jinja2 import Environment, FileSystemLoader, select_autoescape
import openai
from openai import OpenAI

import db
import media
from enrich import STRIP

ROOT = Path(__file__).parent
load_dotenv(ROOT / ".env")

SITE_DIR = Path(os.environ.get("SITE_DIR", "/var/www/tg-digest"))
MEDIA_DIR = Path(os.environ.get("MEDIA_DIR", SITE_DIR / "media"))
SITE_URL = os.environ.get("SITE_URL") or f"https://{os.environ.get('DOMAIN', 'localhost')}"
EPOCH = date.fromisoformat(os.environ.get("DIGEST_EPOCH", "2026-10-17"))
PERIOD = timedelta(days=3)
# Grouping posts is easy and runs on the small model; writing the text uses the larger one.
CLUSTER_MODEL = os.environ.get("DIGEST_CLUSTER_MODEL", "gpt-5-mini")
CLUSTER_EFFORT = os.environ.get("DIGEST_CLUSTER_EFFORT", "medium")
# GPT-6 Sol: in a side-by-side test it wrote better Russian than gpt-5 at ~40% of the cost (little hidden reasoning).
WRITE_MODEL = os.environ.get("DIGEST_WRITE_MODEL", "gpt-6-sol")
WRITE_EFFORT = os.environ.get("DIGEST_WRITE_EFFORT", "medium")  # low: ~half the cost, slightly thinner
# Optional cheaper model for thin English items (mostly the Reddit bot's two-line posts). Off by default: tested
# with gpt-5-mini it saved ~$0.04 per digest but added filler and an invented detail.
THIN_MODEL = os.environ.get("DIGEST_THIN_MODEL", WRITE_MODEL)
THIN_CHARS = int(os.environ.get("DIGEST_THIN_CHARS", "800"))
# OpenAI Flex tier: half price, slower, sometimes refused when busy -> then the call is repeated on the standard tier.
FLEX = os.environ.get("DIGEST_FLEX", "1") != "0"
# USD per 1M tokens (input, output) at the standard tier, for the cost line. Reasoning counts as output.
PRICES = {"gpt-6-sol": (2.0, 10.0), "gpt-6-luna": (0.10, 0.50), "gpt-5": (1.25, 10.0), "gpt-5-mini": (0.25, 2.0)}
TIER_FACTOR = {"flex": 0.5}
WRITE_BATCH = 6
MIN_CONTENT_CHARS = 60  # shorter posts without useful links ("#news", "В догонку… Твит") are left out
MAX_MEDIA = 6
MAX_SOURCE_CHARS = 2500

CONFIG = tomllib.loads((ROOT / "channels.toml").read_text())
TOPICS = list(CONFIG["topics"])
SECTION_ORDER = CONFIG.get("digest", {}).get("section_order", TOPICS)
NO_MEDIA = set(CONFIG.get("digest", {}).get("no_media", []))
SECTION_LABELS = {
    "image": "Images", "video-gen": "Video generation", "video-editing": "Video editing & VFX",
    "character-animation": "Characters, avatars & lipsync", "3d": "3D & splats", "world-models": "World models",
    "gamedev": "Game dev", "music-audio": "Music & audio", "speech-voice": "Speech & voice",
    "upscale-restoration": "Upscale & restoration", "comfyui-workflows": "ComfyUI & workflows",
    "lora-finetuning": "LoRA & fine-tuning", "local-optimization": "Running locally",
    "llm": "Language models", "multimodal-understanding": "Vision-language & documents", "agents": "Agents",
    "coding-tools": "Coding tools", "perception-cv": "Computer vision", "robotics": "Robotics",
    "hardware": "Hardware", "industry": "Industry", "society-ethics": "Society & ethics",
    "community": "Community", "offtopic": "Other",
}
AVAILABILITY_LABELS = {
    "open-weights": "open weights", "code-only": "code", "commercial-api": "paid / API",
    "demo-only": "demo", "not-released": "not released", "unknown": None, "n-a": None,
}
# Most usable first: a merged item shows the best availability among its posts.
AVAILABILITY_ORDER = ["open-weights", "code-only", "demo-only", "commercial-api", "not-released", "unknown", "n-a"]
CYRILLIC = re.compile("[а-яА-ЯёЁ]")
# Telegram posts themselves and the channels' "VK | MAX" footer links are not useful as item links.
SELF_LINK = re.compile(r"^https?://(t\.me|max\.ru|vk\.ru|vk\.com)/", re.I)
FOOTER = re.compile(r"\n\s*(VK\s*\|\s*)?MAX\s*$")  # Нейронавт ends posts with "MAX" / "VK | MAX"
HASHTAG = re.compile(r"#\w+")

client = OpenAI()
USAGE: dict[tuple[str, str], list[int]] = {}  # (model, tier) -> [input tokens, output tokens, reasoning tokens]
_usage_lock = Lock()


# ---------- windows and posts ----------

def window_for(start: date) -> tuple[datetime, datetime]:
    s = datetime(start.year, start.month, start.day, tzinfo=timezone.utc)
    return s, s + PERIOD


def finished_windows(now: datetime) -> list[date]:
    out, d = [], EPOCH
    while window_for(d)[1] <= now:
        out.append(d)
        d += PERIOD
    return out


def load_posts(conn, start: datetime, end: datetime) -> list[dict]:
    rows = conn.execute(
        """SELECT * FROM posts WHERE date >= ? AND date < ? AND enriched_at IS NOT NULL
           AND coalesce(kind, '') != 'humor' ORDER BY date""",
        (start.isoformat(), end.isoformat()),
    ).fetchall()
    posts = []
    for r in rows:
        p = dict(r)
        for k in ("urls", "topics", "entities", "hashtags"):
            p[k] = json.loads(p[k] or "[]")
        if p["topics"] == ["offtopic"]:
            continue
        text = STRIP[p["channel"]].sub("", p["text"]) if p["channel"] in STRIP else p["text"]
        p["clean_text"] = FOOTER.sub("", text.rstrip()).rstrip()
        if len(HASHTAG.sub("", p["clean_text"]).strip()) < MIN_CONTENT_CHARS and \
                not any(not SELF_LINK.match(u) for u in p["urls"]):
            continue
        p["media"] = [dict(m) for m in conn.execute(
            "SELECT kind, path, poster, width, height FROM media WHERE post_id = ? ORDER BY idx", (p["id"],))]
        posts.append(p)
    return posts


# ---------- AI steps ----------

def ask(system: str, user: str, schema: dict, name: str, model: str, effort: str) -> dict:
    kwargs = dict(
        model=model,
        reasoning_effort=effort,
        messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
        response_format={"type": "json_schema", "json_schema": {"name": name, "strict": True, "schema": schema}},
    )
    resp = None
    if FLEX:
        try:
            resp = client.with_options(timeout=900, max_retries=1).chat.completions.create(
                service_tier="flex", **kwargs)
        except (openai.RateLimitError, openai.APITimeoutError, openai.APIConnectionError,
                openai.InternalServerError) as e:
            print(f"  flex unavailable ({type(e).__name__}), using the standard tier", flush=True)
    if resp is None:
        resp = client.chat.completions.create(**kwargs)
    tier = "flex" if getattr(resp, "service_tier", None) == "flex" else "standard"
    u = resp.usage
    reasoning = getattr(u.completion_tokens_details, "reasoning_tokens", 0) or 0
    with _usage_lock:
        acc = USAGE.setdefault((model, tier), [0, 0, 0])
        acc[0] += u.prompt_tokens
        acc[1] += u.completion_tokens
        acc[2] += reasoning
    return json.loads(resp.choices[0].message.content)


def usage_report() -> dict:
    out, total = {}, 0.0
    for (model, tier), (inp, outp, reas) in USAGE.items():
        pin, pout = PRICES.get(model, (0, 0))
        cost = (inp * pin + outp * pout) / 1e6 * TIER_FACTOR.get(tier, 1.0)
        total += cost
        out[f"{model}/{tier}"] = {"input": inp, "output": outp, "reasoning": reas, "cost_usd": round(cost, 4)}
    out["total_usd"] = round(total, 4)
    return out


def obj(props: dict) -> dict:
    return {"type": "object", "additionalProperties": False, "required": list(props), "properties": props}


CLUSTER_SYSTEM = """You prepare a news digest from Telegram channel posts about generative AI.
Group the posts into news items. Posts belong to one item only when they are about the same concrete thing:
the same model, tool, paper, release or event (often from different channels, or a post and its follow-up).
Posts that merely share a topic stay separate. Every post id must appear in exactly one item.
For each item give:
- section: the single best section from the allowed list
- importance: 1-5. 5 = major release or news most readers of the field would care about; 3 = a solid useful
  tool/model/workflow; 1 = minor, niche, or low-information (e.g. a vague post with no details)."""

WRITE_SYSTEM = """You are the editor of a news digest about generative AI for artists and creators. For each item
you get one or more source posts (from Telegram channels; some are AI-written summaries of Reddit threads) and the
links they contain. Write each item for a reader who has not seen the sources.

LANGUAGE: write every title and body in {lang}, whatever language the sources are in. Keep product names and
established English terms as they are (workflow, LoRA, checkpoint, ComfyUI, node, VRAM, fine-tune).{style}

For each item return:
- title: the name of the model/tool/paper and what it is, at most ~90 characters. No emoji, no clickbait.
- body: a summary whose length matches how much the sources say. A thin source (a title and a few bullet points)
  gets 1-2 sentences. A detailed source gets up to 3 short paragraphs (separate paragraphs with a blank line).
  Never write more than the sources contain; combine overlapping sources without repeating yourself.
- links: up to 5 of the most useful links, taken ONLY from the item's link list (project page, code, weights,
  paper, demo, original thread). label = a short name: "GitHub", "Hugging Face", "Paper", "Demo", "Site",
  "Reddit thread", "Civitai". Skip duplicates.

Rules for the body:
- Use only facts from the sources. Keep every number they give (sizes, VRAM, speed, resolution, prices, scores).
- Keep attributions: say who claims something when the source does. The channel author's own opinions or advice
  are the author's, not the company's ("автор советует…", "the author suggests…").
- Do not soften or strengthen claims (a source's "keeps" stays "keeps", "SOTA" stays a claim of SOTA).
- Do not add caveats, advice, usage tips or evaluations that are not in the sources.
- Do not write about the sources themselves ("the post says", "no details are given", "the source does not list").
- No labels like "What it is:" or "How to use:", no marketing words, no filler.
Return one entry per item, with its item number."""

STYLE = {
    "Russian": " Write natural Russian, the way a Russian tech editor writes, not a translation: short sentences, "
               "no bureaucratic phrasing (\"осуществляется\", \"является\", \"данный\").",
    "English": " Write plain, concise English.",
}

OVERVIEW_SYSTEM = """You write the overview at the top of a 3-day digest about generative AI for creators, in Russian.
It is a quick scan of the period, as short lists. You get the digest's items: id, importance, section, title.
- Group the most notable items (about 12-20, importance first) into 3-6 themes.
- theme: a short heading of 1-4 words, e.g. "3D и сплаты", "Видео", "Языковые модели и агенты".
- Order themes by the reader's interests, most interesting first: {order}.
- Each bullet is one item: name = the model/tool/paper name exactly as in its title (e.g. "UniForge3D",
  "Grok Voice 3.0"); what = what it is in at most ~10 words, without repeating the name, starting with a
  lowercase letter (unless it starts with a name) and with no full stop at the end. Keep numbers exact;
  no adjectives like "мощный" or "впечатляющий". Use the item's id.
- summary: one plain sentence (for an RSS feed) naming the period's 2-3 biggest releases."""


def cluster(posts: list[dict]) -> list[dict]:
    lines = [
        f"P{p['id']} [{p['channel']}] {p['date'][:16]} | topics: {','.join(p['topics'])} | {p['summary']}"
        f" | entities: {', '.join(p['entities'][:8])}"
        for p in posts
    ]
    schema = obj({"items": {"type": "array", "items": obj({
        "posts": {"type": "array", "items": {"type": "integer"}},
        "section": {"type": "string", "enum": TOPICS},
        "importance": {"type": "integer"},
    })}})
    out = ask(CLUSTER_SYSTEM, "Allowed sections: " + ", ".join(TOPICS) + "\n\n" + "\n".join(lines), schema,
              "clusters", CLUSTER_MODEL, CLUSTER_EFFORT)
    by_id = {p["id"]: p for p in posts}
    seen, items = set(), []
    for it in out["items"]:
        ids = [i for i in it["posts"] if i in by_id and i not in seen]
        if ids:
            seen.update(ids)
            items.append({"posts": ids, "section": it["section"], "importance": max(1, min(5, it["importance"]))})
    for p in posts:  # anything the model dropped becomes its own item
        if p["id"] not in seen:
            items.append({"posts": [p["id"]], "section": p["topics"][0] if p["topics"] else "community",
                          "importance": 2})
    return items


GITHUB_REPO = re.compile(r"^https?://(?:www\.)?github\.com/([\w.-]+)/([\w.-]+)", re.I)
# Big hub repos that many unrelated posts link to: a shared link to them says nothing.
HUB_REPOS = {"comfyui/comfyui", "comfyanonymous/comfyui", "comfy-org/comfyui", "huggingface/diffusers",
             "huggingface/transformers", "ggerganov/llama.cpp", "ggml-org/llama.cpp", "ollama/ollama"}
MAX_REPO_ITEMS = 3  # a repo linked from more items than this is a hub too


def repos(p: dict) -> set[str]:
    out = set()
    for u in p["urls"]:
        m = GITHUB_REPO.match(u)
        if m:
            repo = f"{m[1]}/{m[2].removesuffix('.git')}".lower()
            if repo not in HUB_REPOS:
                out.add(repo)
    return out


def merge_by_repo(items: list[dict], by_id: dict[int, dict]) -> list[dict]:
    """Merge items whose posts link the same GitHub repo. The clustering model sometimes keeps a post and its
    follow-up from days later as two items (for example two Reddit threads about one project)."""
    item_repos = [set().union(*(repos(by_id[i]) for i in it["posts"])) for it in items]
    count: dict[str, int] = {}
    for rs in item_repos:
        for r in rs:
            count[r] = count.get(r, 0) + 1
    parent = list(range(len(items)))

    def root(n: int) -> int:
        while parent[n] != n:
            parent[n] = parent[parent[n]]
            n = parent[n]
        return n

    first: dict[str, int] = {}
    for n, rs in enumerate(item_repos):
        for r in rs:
            if count[r] > MAX_REPO_ITEMS:
                continue
            if r in first:
                parent[root(n)] = root(first[r])
            else:
                first[r] = n
    groups: dict[int, list[dict]] = {}
    for n, it in enumerate(items):
        groups.setdefault(root(n), []).append(it)
    out = []
    for g in groups.values():
        if len(g) == 1:
            out.append(g[0])
            continue
        main = max(g, key=lambda it: it["importance"])
        ids = sorted({i for it in g for i in it["posts"]}, key=lambda i: by_id[i]["date"])
        print(f"merged {len(g)} items by GitHub repo: posts {ids}", flush=True)
        out.append({"posts": ids, "section": main["section"], "importance": main["importance"]})
    return out


def item_language(posts: list[dict]) -> str:
    return "Russian" if any(CYRILLIC.search(p["clean_text"]) for p in posts) else "English"


def in_language(text: str, lang: str) -> bool:
    letters = [c for c in text if c.isalpha()]
    share = sum(1 for c in letters if CYRILLIC.match(c)) / max(1, len(letters))
    return share > 0.3 if lang == "Russian" else share < 0.05


CONTENT_NOISE = re.compile(r"(Quick Description|Key Features:|🔗 View Original Post|🔗 Watch Video|https?://\S+|#\w+)")


def content_chars(posts: list[dict]) -> int:
    """How much the sources actually say: text without links, hashtags and the Reddit bot's template words."""
    return len(CONTENT_NOISE.sub("", "\n".join(p["clean_text"] for p in posts)).strip())


def write_batch(lang: str, batch: list[tuple[int, dict, list[dict]]], model: str | None = None) -> dict[int, dict]:
    blocks = []
    for n, item, ps in batch:
        links = sorted({u for p in ps for u in p["urls"] if not SELF_LINK.match(u)})
        src = "\n\n".join(f"--- source ({p['channel']}, {p['date'][:10]}):\n{p['clean_text'][:MAX_SOURCE_CHARS]}"
                          for p in ps)
        blocks.append(f"### item {n}\nLinks:\n" + "\n".join(links) + f"\n{src}")
    schema = obj({"items": {"type": "array", "items": obj({
        "item": {"type": "integer"}, "title": {"type": "string"}, "body": {"type": "string"},
        "links": {"type": "array", "items": obj({"label": {"type": "string"}, "url": {"type": "string"}})},
    })}})
    system = WRITE_SYSTEM.format(lang=lang, style=STYLE[lang])
    out = ask(system, "\n\n".join(blocks), schema, "written", model or WRITE_MODEL, WRITE_EFFORT)
    ids = {n for n, _, _ in batch}
    return {it["item"]: it for it in out["items"] if it["item"] in ids}


def write_items(work: list[tuple[int, dict, list[dict]]]) -> dict[int, dict]:
    """Write all items in same-language batches (thin English items on the small model); retry one by one, on the
    main model, the items that came back missing or in the wrong language."""
    batches = []
    for lang in ("Russian", "English"):
        same = [w for w in work if w[1]["lang"] == lang]
        thin = [w for w in same if lang == "English" and content_chars(w[2]) < THIN_CHARS]
        rich = [w for w in same if w not in thin]
        for group, model in ((rich, WRITE_MODEL), (thin, THIN_MODEL)):
            batches += [(lang, group[i:i + WRITE_BATCH], model) for i in range(0, len(group), WRITE_BATCH)]
    written: dict[int, dict] = {}
    with ThreadPoolExecutor(4) as pool:
        for res in pool.map(lambda b: write_batch(*b), batches):
            written.update(res)

    def ok(n: int, lang: str) -> bool:
        w = written.get(n)
        return bool(w) and in_language(w["title"] + " " + w["body"], lang)

    retry = [w for w in work if not ok(w[0], w[1]["lang"])]
    if retry:
        print(f"  rewriting {len(retry)} items one by one", flush=True)
        with ThreadPoolExecutor(4) as pool:
            for res in pool.map(lambda w: write_batch(w[1]["lang"], [w]), retry):
                written.update(res)
    still = [w[0] for w in work if not ok(w[0], w[1]["lang"])]
    if still:
        print(f"  still missing or in the wrong language: items {still}", flush=True)
    return written


def make_overview(items: list[dict]) -> tuple[str, list[dict]]:
    """Themed short lists for the top of the page, plus a one-sentence summary for the feed."""
    listing = "\n".join(f"{x['id']} | {x['importance']} | {x['section']} | {x['title']}" for x in items)
    schema = obj({
        "summary": {"type": "string"},
        "themes": {"type": "array", "items": obj({
            "theme": {"type": "string"},
            "bullets": {"type": "array", "items": obj({
                "id": {"type": "string"}, "name": {"type": "string"}, "what": {"type": "string"}})},
        })},
    })
    system = OVERVIEW_SYSTEM.format(order=", ".join(SECTION_ORDER))
    out = ask(system, listing, schema, "overview", WRITE_MODEL, WRITE_EFFORT)
    ids = {x["id"] for x in items}
    titles = {x["id"]: x["title"] for x in items}
    for t in out["themes"]:  # enforce "— lowercase phrase" without a full stop, whatever the model returned
        for b in t["bullets"]:
            w = b["what"].strip().rstrip(".")
            first = w.split()[0] if w.split() else ""
            # lowercase "Создаёт…", but keep names ("Mago…", "NVIDIA…"): those appear in the item's title
            if len(first) > 1 and first[0].isupper() and first[1:].islower() and first not in titles.get(b["id"], ""):
                w = w[0].lower() + w[1:]
            b["what"] = w
    themes = [{"theme": t["theme"], "bullets": [b for b in t["bullets"] if b["id"] in ids]} for t in out["themes"]]
    return out["summary"], [t for t in themes if t["bullets"]]


def build_data(conn, start: date) -> dict:
    ws, we = window_for(start)
    posts = load_posts(conn, ws, we)
    pending = conn.execute(
        "SELECT count(*) FROM posts WHERE date >= ? AND date < ? AND enriched_at IS NULL",
        (ws.isoformat(), we.isoformat()),
    ).fetchone()[0]
    print(f"{start}: {len(posts)} posts ({pending} not tagged yet)", flush=True)
    items = merge_by_repo(cluster(posts), {p["id"]: p for p in posts}) if posts else []
    return compose(conn, start, posts, items)


def compose(conn, start: date, posts: list[dict], items: list[dict]) -> dict:
    """Write the grouped items and the overview, and assemble the page data."""
    by_id = {p["id"]: p for p in posts}
    for it in items:
        it["lang"] = item_language([by_id[i] for i in it["posts"]])
    work = [(n, it, [by_id[i] for i in it["posts"]]) for n, it in enumerate(items)]
    written = write_items(work)

    out_items = []
    for n, it, ps in work:
        w = written.get(n)
        if not w:  # could not be written even one by one: leave it out rather than show the raw tag line
            continue
        allowed = {u for p in ps for u in p["urls"]}
        links, seen_urls = [], set()
        for link in w["links"]:
            if link["url"] in allowed and link["url"] not in seen_urls and not SELF_LINK.match(link["url"]):
                seen_urls.add(link["url"])
                links.append(link)
        avail = min((p["availability"] or "n-a" for p in ps), key=AVAILABILITY_ORDER.index)
        media = [m for p in ps for m in p["media"]][:MAX_MEDIA]
        out_items.append({
            "id": f"i{n}",
            "title": w["title"], "paragraphs": [x.strip() for x in re.split(r"\n+", w["body"]) if x.strip()],  # any line break
            "links": links, "section": it["section"], "importance": it["importance"], "lang": it["lang"],
            "availability": AVAILABILITY_LABELS.get(avail),
            "date": min(p["date"] for p in ps),
            "sources": [{"channel": p["channel"], "url": p["permalink"], "date": p["date"]} for p in ps],
            "media": media,
            "had_media": any(p["has_media"] for p in ps),
        })

    out_items = [x for x in out_items if x["importance"] > 1]  # 1 = vague or promotional posts
    out_items.sort(key=lambda x: (-x["importance"], x["date"]))
    overview, themes = make_overview(out_items) if out_items else ("", [])
    channels = {}
    for p in posts:
        channels[p["channel"]] = channels.get(p["channel"], 0) + 1
    return {
        "start": start.isoformat(), "end": (start + PERIOD).isoformat(),
        "built_at": datetime.now(timezone.utc).isoformat(),
        "models": {"cluster": f"{CLUSTER_MODEL}/{CLUSTER_EFFORT}", "write": f"{WRITE_MODEL}/{WRITE_EFFORT}"},
        "usage": usage_report(),
        "overview": overview, "overview_themes": themes, "items": out_items, "channels": channels, "post_count": len(posts),
        "channel_titles": {r["username"]: (r["title"] or r["username"]).split(" | ")[0]
                           for r in conn.execute("SELECT username, title FROM channels")},
    }


# ---------- rendering ----------

env = Environment(loader=FileSystemLoader(ROOT / "templates"), autoescape=select_autoescape(["html", "xml"]))


def human_range(start: date) -> str:
    end = start + PERIOD - timedelta(days=1)
    if start.month == end.month:
        return f"{start:%b} {start.day}–{end.day}, {end.year}"
    return f"{start:%b} {start.day} – {end:%b} {end.day}, {end.year}"


def media_exists(m: dict) -> bool:
    return bool(m.get("path")) and (MEDIA_DIR / m["path"]).exists()


def render() -> None:
    """Render SITE_DIR/data.json (the latest digest) to SITE_DIR/index.html and feed.xml."""
    f = SITE_DIR / "data.json"
    if not f.exists():
        return
    d = json.loads(f.read_text())
    start = date.fromisoformat(d["start"])
    for it in d["items"]:
        if it["section"] in NO_MEDIA:  # text-only sections
            it["media"], it["media_note"] = [], False
            continue
        it["media"] = [m for m in it["media"] if media_exists(m)]  # missing file: point to Telegram instead
        it["media_note"] = it.get("had_media") and not it["media"]
    sections: dict[str, list] = {}
    for it in d["items"]:
        sections.setdefault(it["section"], []).append(it)
    rank = {s: i for i, s in enumerate(SECTION_ORDER)}
    ordered = sorted(sections.items(), key=lambda kv: rank.get(kv[0], len(rank)))
    title = human_range(start)
    write_atomic(SITE_DIR / "index.html", env.get_template("digest.html").render(
        d=d, title=title, sections=ordered, labels=SECTION_LABELS,
        ver=int(datetime.now(timezone.utc).timestamp())))  # new media URLs per render: drops stale browser caches
    write_atomic(SITE_DIR / "feed.xml", env.get_template("feed.xml").render(d=d, title=title, site=SITE_URL))


def write_atomic(path: Path, text: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text)
    tmp.replace(path)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--window", help="start date of the window to (re)build, YYYY-MM-DD (default: latest finished)")
    ap.add_argument("--render-only", action="store_true")
    ap.add_argument("--redo-overview", action="store_true", help="rewrite only the overview of the current page")
    args = ap.parse_args()

    SITE_DIR.mkdir(parents=True, exist_ok=True)
    if args.redo_overview:
        f = SITE_DIR / "data.json"
        d = json.loads(f.read_text())
        d["overview"], d["overview_themes"] = make_overview(d["items"])
        write_atomic(f, json.dumps(d, ensure_ascii=False, indent=1))
        print(f"overview rewritten; OpenAI ${usage_report()['total_usd']}", flush=True)
    elif not args.render_only:
        windows = finished_windows(datetime.now(timezone.utc))
        target = date.fromisoformat(args.window) if args.window else (windows[-1] if windows else None)
        current = SITE_DIR / "data.json"
        built = json.loads(current.read_text())["start"] if current.exists() else None
        if target and (args.window or built != target.isoformat()):
            conn = db.connect()
            data = build_data(conn, target)
            write_atomic(current, json.dumps(data, ensure_ascii=False, indent=1))
            print(f"{target}: {len(data['items'])} items from {data['post_count']} posts; "
                  f"OpenAI ${data['usage']['total_usd']}", flush=True)
            render()
            # Only the latest digest is kept: drop media of older posts once the new page is live.
            print(f"removed {media.prune_before(conn, window_for(target)[0])} old media files", flush=True)
            return
    render()


if __name__ == "__main__":
    main()
