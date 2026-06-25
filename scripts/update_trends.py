#!/usr/bin/env python3
"""
Daily TrendBoard updater.

Runs inside GitHub Actions. Fetches trend signals from free public sources
(Reddit JSON, Know Your Meme RSS, Google Trends RSS), then asks GitHub Models
(also free, authenticated with GITHUB_TOKEN) for editorial picks. Writes
trends-data.json and decoder-data.json next to itself. Cost: $0.

Required env: GITHUB_TOKEN  (auto-injected by Actions).
Required files at repo root: trends-data.json, decoder-data.json
"""

import json
import os
import re
import sys
from datetime import datetime, timezone

import requests

# ---- Config ---------------------------------------------------------------

GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN")
if not GITHUB_TOKEN:
    sys.exit("GITHUB_TOKEN env var not set")

MODELS_ENDPOINT = "https://models.github.ai/inference/chat/completions"
MODEL = "openai/gpt-4o-mini"  # plenty for editorial writing; swap to claude-3-5-haiku if preferred

TRENDS_PATH = "trends-data.json"
DECODER_PATH = "decoder-data.json"

REDDIT_UA = "trendboard-daily-update/1.0 (by /u/arthurcolab2010-source)"

# ---- Date helpers ---------------------------------------------------------

def now_utc():
    return datetime.now(timezone.utc)

def today_label():
    # e.g. "June 15, 2026" — Linux strftime, fine on ubuntu-latest runners
    return now_utc().strftime("%B %-d, %Y")

def short_label():
    return now_utc().strftime("%b %-d")

# ---- Signal fetching (all free, no API keys) -----------------------------

def fetch_reddit_top(subreddit, t="week", limit=12):
    """Public Reddit JSON endpoint. Rate-limited but no auth required."""
    url = f"https://www.reddit.com/r/{subreddit}/top.json?t={t}&limit={limit}"
    try:
        r = requests.get(url, headers={"User-Agent": REDDIT_UA}, timeout=15)
        if r.status_code != 200:
            print(f"  reddit /r/{subreddit} -> HTTP {r.status_code}")
            return []
        return [c["data"] for c in r.json()["data"]["children"]]
    except Exception as e:
        print(f"  reddit /r/{subreddit} failed: {e}")
        return []

def fetch_kym_rss():
    """Know Your Meme RSS feed of popular memes."""
    try:
        r = requests.get(
            "https://knowyourmeme.com/memes/popular/feed",
            headers={"User-Agent": REDDIT_UA},
            timeout=15,
        )
        r.raise_for_status()
        # Crude RSS parsing — pull <item><title> and <description>
        items = re.findall(r"<item>(.*?)</item>", r.text, re.DOTALL)
        out = []
        for item in items[:12]:
            t = re.search(r"<title>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</title>", item, re.DOTALL)
            d = re.search(r"<description>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</description>", item, re.DOTALL)
            if t:
                out.append({
                    "title": t.group(1).strip(),
                    "desc": (d.group(1).strip()[:240] if d else ""),
                })
        return out
    except Exception as e:
        print(f"  kym RSS failed: {e}")
        return []

def fetch_google_trends():
    """Google Trends daily RSS — US trending searches."""
    try:
        r = requests.get(
            "https://trends.google.com/trends/trendingsearches/daily/rss?geo=US",
            timeout=15,
        )
        r.raise_for_status()
        items = re.findall(r"<item>(.*?)</item>", r.text, re.DOTALL)
        out = []
        for item in items[:10]:
            t = re.search(r"<title>(.*?)</title>", item, re.DOTALL)
            if t:
                out.append(t.group(1).strip())
        return out
    except Exception as e:
        print(f"  google trends failed: {e}")
        return []

def fetch_hn_search(query):
    """HN via Algolia search — free, no auth, no rate limit in practice."""
    try:
        r = requests.get(
            f"https://hn.algolia.com/api/v1/search?query={query}&tags=front_page&hitsPerPage=8",
            timeout=15,
        )
        r.raise_for_status()
        return [h.get("title", "") for h in r.json().get("hits", []) if h.get("title")]
    except Exception as e:
        print(f"  hn search '{query}' failed: {e}")
        return []

def build_signal_digest():
    """Aggregate the day's signals into a compact LLM-friendly block."""
    print("Fetching signals...")
    sections = []

    for sub in ["OutOfTheLoop", "memes", "TikTokCringe", "GenZ"]:
        posts = fetch_reddit_top(sub)
        if posts:
            sections.append(f"=== r/{sub} (top this week) ===")
            for p in posts[:10]:
                sections.append(f"- {p.get('title', '')}")
            sections.append("")

    kym = fetch_kym_rss()
    if kym:
        sections.append("=== Know Your Meme — popular ===")
        for it in kym[:10]:
            sections.append(f"- {it['title']}")
        sections.append("")

    gt = fetch_google_trends()
    if gt:
        sections.append("=== Google Trends (US, today) ===")
        for t in gt:
            sections.append(f"- {t}")
        sections.append("")

    hn = fetch_hn_search("meme OR tiktok OR viral")
    if hn:
        sections.append("=== HN front page mentions ===")
        for t in hn:
            sections.append(f"- {t}")

    digest = "\n".join(sections)
    print(f"Signal digest: {len(digest)} chars across {sum(1 for s in sections if s.startswith('==='))} sources")
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
        print(f"  models API returned HTTP {r.status_code}: {r.text[:300]}")
        r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"]

def extract_json(text):
    """Pull a JSON object out of a model response, tolerating markdown fences."""
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

    # 1. Decay every active trend, shift its timeline forward by one tick.
    for t in data.get("trends", []):
        t["decay"] = max(0.0, round(t["decay"] - 0.05, 2))
        tl = t.get("tl", {})
        if isinstance(tl.get("d"), list) and isinstance(tl.get("l"), list):
            tl["d"] = (tl["d"][1:] + [round(t["decay"] * 100)])
            tl["l"] = (tl["l"][1:] + [label])

    # 2. Demote sub-0.20 trends to archive and graveyard.
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

    # 3. Ask GitHub Models for 1-3 new trends.
    existing_names = [t["name"] for t in survivors] + [t["name"] for t in archive[:40]]
    all_ids = [t["id"] for t in survivors] + [t["id"] for t in archive]
    max_id = max(all_ids) if all_ids else 100
    next_id = max_id + 1

    system = (
        "You are the curator of TrendBoard, a static site that tracks internet/meme trends. "
        "Given today's signals from public trend sources, pick 1-3 brand-new trends worth adding "
        "to the leaderboard. Output ONLY a valid JSON object — no prose, no markdown fences."
    )
    user = f"""Today is {today}. Free-source signals (Reddit, Know Your Meme, Google Trends, HN):

{signal_digest}

Trends already tracked (do NOT duplicate):
{', '.join(existing_names[:80])}

Pick 1-3 genuinely new trends. Output JSON with EXACTLY this shape:

{{
  "new_trends": [
    {{
      "id": {next_id},
      "name": "<trend name>",
      "cat": "<one of: meme, slang, audio, drama, challenge, phrase>",
      "plat": "<e.g. 'TikTok · Reels' or 'X · Reddit'>",
      "decay": 0.85,
      "fresh": "<one of: 'Brand new', 'Hot right now', 'Climbing', 'Peak viral'>",
      "def": {{
        "normie": "<2 sentences explaining to a parent>",
        "normal": "<2 sentences for someone casually online>",
        "zoomer": "<2 sentences in extremely-online voice — knowing, slightly mean>"
      }},
      "origin": "<short origin with year>",
      "age": {{"z": "<Peak|Active|Fading|Dead|Confused>", "m": "<same>", "main": "<same>"}},
      "tl": {{"l": ["May 24","Jun 1","Jun 8","Jun 15","{label}"], "d": [10,30,55,75,<decay*100>], "pk": 4}},
      "replaced": "Still climbing",
      "vid": "<youtube search query>"
    }}
  ]
}}

Use sequential ids starting at {next_id}. If signals reveal nothing new, return {{"new_trends": []}}."""

    new_trends = []
    try:
        resp = call_model(system, user, max_tokens=3500)
        new_trends = extract_json(resp).get("new_trends", [])
        print(f"  model returned {len(new_trends)} new trend(s)")
    except Exception as e:
        print(f"  model call failed: {e}")
        new_trends = []

    data["trends"] = new_trends + data["trends"]

    # 4. Cap active list at 25 — push overflow into archive.
    if len(data["trends"]) > 25:
        overflow = data["trends"][25:]
        for t in overflow:
            t["fresh"] = "Archived"
            data["archive"].insert(0, t)
        data["trends"] = data["trends"][:25]

    # 5. Recompute meta.
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

    existing_terms = list(data.get("dict", {}).keys())
    new_trend_names = [t.get("name", "") for t in new_trends]

    system = (
        "You are the editor of TrendBoard's slang decoder. Pick 2-5 NEW slang terms, phrases, "
        "or memes worth adding to the dictionary today, based on the signals and any new trends. "
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
    "<term key (lowercase)>": {{
      "type": "<one of: abbr, slang, meme, phrase, acronym, emoji>",
      "def": "<1-2 sentence definition>",
      "origin": "<short origin with year if known>",
      "status": "ok"
    }}
  }}
}}

If nothing new, return {{"new_terms": {{}}}}."""

    added = {}
    try:
        resp = call_model(system, user, max_tokens=1800)
        added = extract_json(resp).get("new_terms", {})
        print(f"  model returned {len(added)} new term(s)")
    except Exception as e:
        print(f"  model call failed: {e}")
        added = {}

    # Insert new terms BEFORE the emoji block so the structure stays clean.
    keys = list(data["dict"].keys())
    emoji_start = next(
        (i for i, k in enumerate(keys) if k and ord(k[0]) > 1000),
        len(keys),
    )
    pre = {k: data["dict"][k] for k in keys[:emoji_start]}
    emo = {k: data["dict"][k] for k in keys[emoji_start:]}
    # Drop any duplicates the model snuck in
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
    if not digest.strip():
        print("\nNo signals fetched at all — bailing out without changes.")
        return
    new_trends, demoted = update_trends(digest)
    update_decoder(digest, new_trends)
    print("\nDone.")

if __name__ == "__main__":
    main()
