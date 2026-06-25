#!/usr/bin/env python3
"""
Daily TrendBoard updater — v2 (cloud-IP-friendly sources).

Runs inside GitHub Actions. Sources signals from APIs that actually work from
GitHub's IP ranges:
  - Wikipedia top pageviews  (no auth, no rate limit, totally reliable)
  - HN via Algolia search    (no auth, very reliable)
  - GDELT global news        (academic API, no auth, reliable)
  - Reddit OAuth             (OPTIONAL — set REDDIT_CLIENT_ID + REDDIT_CLIENT_SECRET
                              repo secrets to enable; instructions in README of script)

Even if every signal source fails, the script still decays the existing trends
and bumps meta.updated so the site at least shows today's date.

Required env: GITHUB_TOKEN  (auto-injected by Actions)
Optional env: REDDIT_CLIENT_ID, REDDIT_CLIENT_SECRET
Required files at repo root: trends-data.json, decoder-data.json
"""

import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from urllib.parse import quote_plus

import requests

# ---- Config ---------------------------------------------------------------

GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN")
if not GITHUB_TOKEN:
    sys.exit("GITHUB_TOKEN env var not set")

MODELS_ENDPOINT = "https://models.github.ai/inference/chat/completions"
MODEL = "openai/gpt-4o-mini"

TRENDS_PATH = "trends-data.json"
DECODER_PATH = "decoder-data.json"

UA = "trendboard-daily-update/2.0 (github.com/arthurcolab2010-source/trendboard)"

# ---- Date helpers ---------------------------------------------------------

def now_utc():
    return datetime.now(timezone.utc)

def today_label():
    return now_utc().strftime("%B %-d, %Y")

def short_label():
    return now_utc().strftime("%b %-d")

# ---- Signal sources -------------------------------------------------------

def fetch_wikipedia_top():
    """Wikipedia top pageviews — what people searched yesterday."""
    # Today's data isn't complete yet — use yesterday.
    y = now_utc() - timedelta(days=1)
    url = f"https://wikimedia.org/api/rest_v1/metrics/pageviews/top/en.wikipedia/all-access/{y.year}/{y.month:02d}/{y.day:02d}"
    try:
        r = requests.get(url, headers={"User-Agent": UA}, timeout=15)
        if r.status_code != 200:
            print(f"  wikipedia -> HTTP {r.status_code}")
            return []
        articles = r.json()["items"][0]["articles"]
        skip = {"Main_Page", "Search", "-", "Wikipedia:Featured_pictures"}
        out = []
        for a in articles[:80]:
            name = a["article"]
            if name in skip or name.startswith("Special:") or name.startswith("Wikipedia:"):
                continue
            out.append(name.replace("_", " "))
        print(f"  wikipedia: {len(out)} pages")
        return out[:30]
    except Exception as e:
        print(f"  wikipedia failed: {e}")
        return []

def fetch_hn_search(query, limit=15):
    """Hacker News search via Algolia. No auth, no rate limit in practice."""
    try:
        r = requests.get(
            f"https://hn.algolia.com/api/v1/search?query={quote_plus(query)}&hitsPerPage={limit}",
            headers={"User-Agent": UA},
            timeout=15,
        )
        r.raise_for_status()
        hits = [h.get("title", "") for h in r.json().get("hits", []) if h.get("title")]
        print(f"  hn '{query}': {len(hits)} hits")
        return hits
    except Exception as e:
        print(f"  hn '{query}' failed: {e}")
        return []

def fetch_gdelt():
    """GDELT global news — recent articles mentioning trend-adjacent terms."""
    try:
        r = requests.get(
            "https://api.gdeltproject.org/api/v2/doc/doc",
            params={
                "query": '("viral trend" OR "tiktok trend" OR "internet meme" OR "going viral") sourcelang:eng',
                "mode": "ArtList",
                "maxrecords": "25",
                "format": "json",
                "sort": "DateDesc",
                "timespan": "3d",
            },
            headers={"User-Agent": UA},
            timeout=20,
        )
        r.raise_for_status()
        articles = [a.get("title", "") for a in r.json().get("articles", []) if a.get("title")]
        print(f"  gdelt: {len(articles)} articles")
        return articles[:20]
    except Exception as e:
        print(f"  gdelt failed: {e}")
        return []

def get_reddit_token():
    """OPTIONAL — Reddit OAuth if the user has registered a free script app
    and set REDDIT_CLIENT_ID + REDDIT_CLIENT_SECRET as repo secrets."""
    cid = os.environ.get("REDDIT_CLIENT_ID")
    csec = os.environ.get("REDDIT_CLIENT_SECRET")
    if not cid or not csec:
        return None
    try:
        r = requests.post(
            "https://www.reddit.com/api/v1/access_token",
            auth=(cid, csec),
            data={"grant_type": "client_credentials"},
            headers={"User-Agent": UA},
            timeout=15,
        )
        r.raise_for_status()
        return r.json()["access_token"]
    except Exception as e:
        print(f"  reddit oauth failed: {e}")
        return None

def fetch_reddit_authed(token, subreddit, t="week", limit=12):
    try:
        r = requests.get(
            f"https://oauth.reddit.com/r/{subreddit}/top",
            headers={"Authorization": f"Bearer {token}", "User-Agent": UA},
            params={"t": t, "limit": limit},
            timeout=15,
        )
        if r.status_code != 200:
            print(f"  reddit /r/{subreddit} -> HTTP {r.status_code}")
            return []
        posts = [c["data"] for c in r.json()["data"]["children"]]
        print(f"  reddit /r/{subreddit}: {len(posts)} posts")
        return posts
    except Exception as e:
        print(f"  reddit /r/{subreddit} failed: {e}")
        return []

def build_signal_digest():
    print("Fetching signals...")
    sections = []
    n_sources = 0

    wiki = fetch_wikipedia_top()
    if wiki:
        n_sources += 1
        sections.append("=== Wikipedia top pageviews (yesterday) ===")
        sections.extend(f"- {w}" for w in wiki)
        sections.append("")

    for q in ["meme", "tiktok", "viral trend", "gen z slang"]:
        hits = fetch_hn_search(q, limit=8)
        if hits:
            n_sources += 1
            sections.append(f"=== HN search: \"{q}\" ===")
            sections.extend(f"- {h}" for h in hits)
            sections.append("")

    gdelt = fetch_gdelt()
    if gdelt:
        n_sources += 1
        sections.append("=== GDELT news mentions ===")
        sections.extend(f"- {a}" for a in gdelt)
        sections.append("")

    # Optional Reddit
    token = get_reddit_token()
    if token:
        for sub in ["OutOfTheLoop", "memes", "TikTokCringe", "GenZ"]:
            posts = fetch_reddit_authed(token, sub)
            if posts:
                n_sources += 1
                sections.append(f"=== r/{sub} (top this week, authed) ===")
                sections.extend(f"- {p.get('title','')}" for p in posts[:10])
                sections.append("")
    else:
        print("  reddit: skipped (no REDDIT_CLIENT_ID / REDDIT_CLIENT_SECRET secrets set)")

    digest = "\n".join(sections)
    print(f"Signal digest: {len(digest)} chars across {n_sources} sources")
    return digest

# ---- GitHub Models -------------------------------------------------------

def call_model(system_prompt, user_prompt, max_tokens=4000):
    payload = {
        "model": MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": 0.7,
        "max_tokens": max_tokens,
    }
    headers = {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
        "Content-Type": "application/json",
    }
    r = requests.post(MODELS_ENDPOINT, headers=headers, json=payload, timeout=120)
    if r.status_code != 200:
        print(f"  models API HTTP {r.status_code}: {r.text[:300]}")
        r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"]

def extract_json(text):
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if m:
        text = m.group(1)
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("no JSON object found in model response")
    return json.loads(text[start : end + 1])

# ---- File ops ------------------------------------------------------------

def read_json(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)

def write_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=1, ensure_ascii=False)
        f.write("\n")

# ---- Update logic --------------------------------------------------------

def update_trends(signal_digest):
    print("\n=== Updating trends-data.json ===")
    data = read_json(TRENDS_PATH)
    today = today_label()
    label = short_label()

    # 1. Decay every active trend (always runs, even without signals).
    for t in data.get("trends", []):
        t["decay"] = max(0.0, round(t["decay"] - 0.05, 2))
        tl = t.get("tl", {})
        if isinstance(tl.get("d"), list) and isinstance(tl.get("l"), list):
            tl["d"] = tl["d"][1:] + [round(t["decay"] * 100)]
            tl["l"] = tl["l"][1:] + [label]

    # 2. Demote sub-0.20 to archive + graveyard.
    archive = data.get("archive", [])
    graveyard = data.get("graveyard", [])
    survivors = []
    demoted = []
    for t in data.get("trends", []):
        if t["decay"] < 0.20:
            t["fresh"] = "Archived"
            archive.insert(0, t)
            graveyard.insert(0, t["name"])
            demoted.append(t["name"])
        else:
            survivors.append(t)
    graveyard = graveyard[:12]
    data["trends"] = survivors
    data["archive"] = archive
    data["graveyard"] = graveyard

    # 3. Ask the model for new trends — only if we have signals.
    new_trends = []
    if signal_digest.strip():
        existing_names = [t["name"] for t in survivors] + [t["name"] for t in archive[:40]]
        all_ids = [t["id"] for t in survivors] + [t["id"] for t in archive]
        next_id = (max(all_ids) if all_ids else 100) + 1

        system = (
            "You are the curator of TrendBoard, a site that tracks internet/meme trends. "
            "Given today's free-source signals, pick 1-3 brand-new trends genuinely worth adding "
            "to the leaderboard. Output ONLY valid JSON — no prose, no markdown fences."
        )
        user = f"""Today is {today}. Signals from Wikipedia, HN, GDELT, (optionally) Reddit:

{signal_digest}

Trends already tracked (do NOT duplicate):
{', '.join(existing_names[:80])}

Pick 1-3 genuinely new trends — not generic news, not stuff that's been around for years. Output EXACTLY:

{{
  "new_trends": [
    {{
      "id": {next_id},
      "name": "<trend name>",
      "cat": "<meme | slang | audio | drama | challenge | phrase>",
      "plat": "<e.g. 'TikTok · Reels' or 'X · Reddit'>",
      "decay": 0.85,
      "fresh": "<'Brand new' | 'Hot right now' | 'Climbing' | 'Peak viral'>",
      "def": {{
        "normie": "<2 sentences for a parent>",
        "normal": "<2 sentences for someone casually online>",
        "zoomer": "<2 sentences, extremely online voice, knowing, slightly mean>"
      }},
      "origin": "<short origin with year>",
      "age": {{"z": "<Peak|Active|Fading|Dead|Confused>", "m": "<same>", "main": "<same>"}},
      "tl": {{"l": ["May 24","Jun 1","Jun 8","Jun 15","{label}"], "d": [10,30,55,75,<decay*100>], "pk": 4}},
      "replaced": "Still climbing",
      "vid": "<youtube search query>"
    }}
  ]
}}

Use sequential ids starting at {next_id}. If nothing genuinely new, return {{"new_trends": []}}."""

        try:
            resp = call_model(system, user, max_tokens=3500)
            new_trends = extract_json(resp).get("new_trends", [])
            print(f"  model returned {len(new_trends)} new trend(s)")
        except Exception as e:
            print(f"  model call failed: {e}")
    else:
        print("  no signals — skipping model call, just decaying.")

    data["trends"] = new_trends + data["trends"]

    # 4. Cap active list at 25 — overflow to archive.
    if len(data["trends"]) > 25:
        overflow = data["trends"][25:]
        for t in overflow:
            t["fresh"] = "Archived"
            data["archive"].insert(0, t)
        data["trends"] = data["trends"][:25]

    # 5. Recompute meta — always runs so the site at least shows today.
    peak_count = sum(1 for t in data["trends"] if t["decay"] >= 0.80)
    prev_tracking = data.get("meta", {}).get("tracking", 0)
    data["meta"] = {
        "tracking": prev_tracking + len(new_trends),
        "peak": peak_count,
        "died": len(data["graveyard"]),
        "archived": len(data["archive"]),
        "updated": today,
    }

    write_json(TRENDS_PATH, data)
    print(f"  added: {[t.get('name') for t in new_trends]}")
    print(f"  demoted to archive: {demoted}")
    return new_trends, demoted

def update_decoder(signal_digest, new_trends):
    print("\n=== Updating decoder-data.json ===")
    data = read_json(DECODER_PATH)
    today = today_label()

    added = {}
    if signal_digest.strip():
        existing_terms = list(data.get("dict", {}).keys())
        new_trend_names = [t.get("name", "") for t in new_trends]

        system = (
            "You are the editor of TrendBoard's slang decoder. Pick 2-5 NEW slang terms, phrases, "
            "or memes worth adding to the dictionary, based on signals + any new trends. "
            "Output ONLY valid JSON."
        )
        user = f"""Today is {today}. Signals:

{signal_digest}

New trends added to the leaderboard today: {', '.join(new_trend_names) or '(none)'}

Terms already in the dictionary (do NOT duplicate):
{', '.join(existing_terms[:120])}

Pick 2-5 new terms. Output JSON:

{{
  "new_terms": {{
    "<lowercase term>": {{
      "type": "<abbr | slang | meme | phrase | acronym | emoji>",
      "def": "<1-2 sentence definition>",
      "origin": "<short origin>",
      "status": "ok"
    }}
  }}
}}

If nothing new, return {{"new_terms": {{}}}}."""

        try:
            resp = call_model(system, user, max_tokens=1800)
            added = extract_json(resp).get("new_terms", {})
            print(f"  model returned {len(added)} new term(s)")
        except Exception as e:
            print(f"  model call failed: {e}")
    else:
        print("  no signals — skipping model call.")

    # Insert new terms BEFORE the emoji block.
    keys = list(data["dict"].keys())
    emoji_start = next(
        (i for i, k in enumerate(keys) if k and ord(k[0]) > 1000),
        len(keys),
    )
    pre = {k: data["dict"][k] for k in keys[:emoji_start]}
    emo = {k: data["dict"][k] for k in keys[emoji_start:]}
    for k in list(added.keys()):
        if k in pre or k in emo:
            del added[k]
    pre.update(added)
    data["dict"] = {**pre, **emo}

    data["meta"] = {
        "updated": today,
        "termCount": len(data["dict"]),
    }

    write_json(DECODER_PATH, data)
    print(f"  added terms: {list(added.keys())}")

# ---- Main ----------------------------------------------------------------

def main():
    print(f"=== TrendBoard daily update — {today_label()} (UTC) ===\n")
    digest = build_signal_digest()
    # We NEVER bail early. Even with zero signals, decay + bump meta.updated so
    # the live site always reflects today's date.
    new_trends, demoted = update_trends(digest)
    update_decoder(digest, new_trends)
    print("\nDone.")

if __name__ == "__main__":
    main()
