import os
import json
import re
import hashlib
import datetime
import pathlib
import html
import calendar
from zoneinfo import ZoneInfo

import feedparser
import requests
from google import genai
from google.genai import errors
from jinja2 import Environment, FileSystemLoader, select_autoescape
from playwright.sync_api import sync_playwright

ROOT = pathlib.Path(__file__).resolve().parents[1]
OUT = ROOT / "output"
EDITION_ROOT = OUT / "editions"
OUT.mkdir(exist_ok=True)
EDITION_ROOT.mkdir(parents=True, exist_ok=True)

sources = json.loads((ROOT / "sources.json").read_text(encoding="utf-8"))
prefs = json.loads((ROOT / "preferences.json").read_text(encoding="utf-8"))
TIMEZONE = ZoneInfo(prefs.get("timezone", "Asia/Kolkata"))
TEMPLATES = Environment(
    loader=FileSystemLoader(str(ROOT / "app")),
    autoescape=select_autoescape(["html", "xml"]),
)


def clean(s):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", s or "")).strip()


def is_recent(entry, hours=24):
    stamp = entry.get("published_parsed") or entry.get("updated_parsed")
    if not stamp:
        return True
    published = datetime.datetime.fromtimestamp(
        calendar.timegm(stamp), tz=datetime.timezone.utc
    )
    return published >= datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=hours)


def fetch_rss(url, source_name, section, limit=12):
    feed = feedparser.parse(url)
    results = []
    for entry in feed.entries:
        if not is_recent(entry):
            continue
        link = entry.get("link", "")
        title = clean(entry.get("title", ""))
        if not title or not link:
            continue
        image = ""
        for enclosure in entry.get("enclosures", []):
            if enclosure.get("type", "").startswith("image/"):
                image = enclosure.get("href", "")
        if not image:
            media = entry.get("media_content", [])
            if media:
                image = media[0].get("url", "")
        published = entry.get("published", entry.get("updated", ""))
        results.append({
            "title": title,
            "url": link,
            "summary": clean(entry.get("summary", ""))[:1200],
            "source": source_name,
            "section": section,
            "image": image,
            "kind": "article",
            "published": published,
        })
        if len(results) >= limit:
            break
    return results


def fetch_youtube(channel):
    url = f"https://www.youtube.com/feeds/videos.xml?channel_id={channel['channel_id']}"
    feed = feedparser.parse(url)
    results = []
    for entry in feed.entries:
        if not is_recent(entry):
            continue
        title = clean(entry.get("title", ""))
        if re.search(r"\b(live|livestream|study with me|pomodoro stream)\b", title, re.I):
            continue
        video_id = entry.get("yt_videoid", "")
        results.append({
            "title": title,
            "url": entry.get("link", ""),
            "summary": clean(entry.get("summary", ""))[:900],
            "source": channel["name"],
            "section": channel["section"],
            "image": f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg" if video_id else "",
            "kind": "video",
            "published": entry.get("published", ""),
        })
        if len(results) >= 8:
            break
    return results


def fetch_github(repo):
    url = f"https://github.com/{repo['name']}/releases.atom"
    return fetch_rss(url, repo["name"], repo["section"], limit=6)


def collect():
    items = []
    for source in sources["rss"]:
        try:
            items += fetch_rss(source["url"], source["name"], source["section"])
        except Exception as exc:
            print("RSS error", source["name"], exc)
    for channel in sources["youtube_channels"]:
        try:
            items += fetch_youtube(channel)
        except Exception as exc:
            print("YouTube error", channel["name"], exc)
    for repo in sources["github_repos"]:
        try:
            items += fetch_github(repo)
        except Exception as exc:
            print("GitHub error", repo["name"], exc)

    seen = set()
    unique = []
    for item in items:
        key = hashlib.md5((item["url"] or item["title"]).encode()).hexdigest()
        if key not in seen:
            seen.add(key)
            unique.append(item)
    unique.sort(key=lambda item: item.get("published", ""), reverse=True)
    return unique


def fetch_og_image(url):
    try:
        response = requests.get(
            url, timeout=12, headers={"User-Agent": "Mozilla/5.0"}
        )
        match = re.search(
            r'<meta[^>]+property="og:image"[^>]+content="([^"]+)',
            response.text,
            re.I,
        )
        if not match:
            match = re.search(
                r'<meta[^>]+content="([^"]+)"[^>]+property="og:image"',
                response.text,
                re.I,
            )
        return html.unescape(match.group(1)) if match else ""
    except Exception:
        return ""


def curate(items):
    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        raise RuntimeError("Missing GEMINI_API_KEY environment variable")

    client = genai.Client(api_key=key)
    model = os.environ.get("GEMINI_MODEL", "gemini-3.8-flash")
    payload = [
        {key: item[key] for key in ("title", "url", "summary", "source", "section", "kind")}
        for item in items[:100]
    ]
    prompt = f"""
You are the editor of {prefs['masthead']}, a finite personal newspaper.
Reader profile: {json.dumps(prefs['editorial_profile'])}
Interest weights (relative weights, not percentages that must sum to 100): {json.dumps(prefs['interest_weights'])}
Choose up to {prefs['max_stories']} items that are genuinely useful, diverse, recent and non-duplicative.
Prioritise AI/Claude/Gemini news, then balance books/literature/writing, psychology, world/science/business,
coding/developer tools, study/productivity, design/culture and curiosity.
Exclude shopping promotions, affiliate links, generic viral posts, stale content, and low-value announcements.
Do not invent facts or claim a source says something absent from its title/summary.
Return ONLY valid JSON: {{"edition_note":"...","lead_index":integer,"stories":[{{"index":integer,"section":"...","headline":"...","dek":"one sentence","why_it_matters":"one concise sentence","summary":"60-100 words","image_query":"short image search description"}}]}}
Items:
{json.dumps(payload, ensure_ascii=False)}
"""

    fallback_models = [
        model_name.strip()
        for model_name in os.environ.get(
            "GEMINI_FALLBACK_MODELS", "gemini-3.7-flash,gemini-3.6-flash"
        ).split(",")
        if model_name.strip()
    ]
    models_to_try = list(dict.fromkeys([model, *fallback_models]))
    response = None
    last_error = None

    for candidate_model in models_to_try:
        try:
            print(f"Generating edition with {candidate_model}")
            response = client.models.generate_content(
                model=candidate_model,
                contents=prompt,
            )
            print(f"Generation succeeded with {candidate_model}")
            break
        except errors.APIError as exc:
            if exc.code not in (500, 502, 503, 504):
                raise
            last_error = exc
            print(
                f"{candidate_model} unavailable ({exc.code}). Trying next model."
            )

    if response is None:
        raise RuntimeError(
            f"All configured Gemini models were unavailable. Last error: {last_error}"
        ) from last_error

    if not response.text:
        raise RuntimeError("Gemini returned an empty response.")
    raw = response.text.strip()
    raw = re.sub(r"^```(?:json)?|```$", "", raw, flags=re.M).strip()
    data = json.loads(raw)

    chosen = []
    for story in data.get("stories", []):
        index = story.get("index")
        if isinstance(index, int) and 0 <= index < len(payload):
            item = items[index].copy()
            item.update(story)
            chosen.append(item)
    for story in chosen:
        if not story.get("image"):
            story["image"] = fetch_og_image(story.get("url", ""))
    data["stories"] = chosen
    return data


def render_pdf(html_path, pdf_path):
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page(
            viewport={"width": 1240, "height": 1754},
            device_scale_factor=1,
        )
        page.goto(html_path.resolve().as_uri(), wait_until="networkidle", timeout=90000)
        page.pdf(
            path=str(pdf_path),
            format="A4",
            print_background=True,
            margin={"top": "10mm", "right": "10mm", "bottom": "10mm", "left": "10mm"},
        )
        browser.close()


def build_archive_page():
    editions = []
    for metadata_path in EDITION_ROOT.glob("*/edition.json"):
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            issue_dir = metadata_path.parent
            relative = issue_dir.relative_to(OUT).as_posix()
            metadata["edition_url"] = f"{relative}/"
            metadata["pdf_url"] = f"{relative}/daily-abby.pdf"
            editions.append(metadata)
        except (OSError, json.JSONDecodeError) as exc:
            print(f"Skipping unreadable edition metadata {metadata_path}: {exc}")

    editions.sort(key=lambda edition: edition.get("date_iso", ""), reverse=True)
    years = sorted(
        {edition.get("date_iso", "")[:4] for edition in editions if edition.get("date_iso")},
        reverse=True,
    )
    archive = {
        "masthead": prefs.get("masthead", "THE DAILY ABBY"),
        "editions": editions,
        "edition_count": len(editions),
        "years": years,
        "updated": datetime.datetime.now(TIMEZONE).strftime("%d %b %Y · %I:%M %p"),
    }
    template = TEMPLATES.get_template("archive_template.html")
    page = template.render(archive=archive)
    (OUT / "index.html").write_text(page, encoding="utf-8")
    print(f"Built archive index with {len(editions)} edition(s)")


def main():
    items = collect()
    if not items:
        raise RuntimeError("No feed items found. Check sources.json and feed URLs.")

    edition = curate(items)
    now = datetime.datetime.now(TIMEZONE)
    date_iso = now.date().isoformat()
    edition_dir = EDITION_ROOT / date_iso
    edition_dir.mkdir(parents=True, exist_ok=True)

    edition["date"] = now.strftime("%A, %d %B %Y")
    edition["date_iso"] = date_iso
    edition["edition_number"] = now.strftime("%Y%m%d")
    edition["masthead"] = prefs["masthead"]
    edition["reading_time"] = "8–10 MIN READ"
    edition["caught_up"] = "You're caught up. Close the tabs. Go live your day."

    issue_template = TEMPLATES.get_template("template.html")
    issue_html = issue_template.render(edition=edition)
    issue_html_path = edition_dir / "index.html"
    issue_pdf_path = edition_dir / "daily-abby.pdf"
    issue_html_path.write_text(issue_html, encoding="utf-8")
    render_pdf(issue_html_path, issue_pdf_path)

    lead = edition["stories"][0] if edition.get("stories") else {}
    metadata = {
        "date_iso": date_iso,
        "date_label": edition["date"],
        "edition_number": edition["edition_number"],
        "masthead": edition["masthead"],
        "story_count": len(edition.get("stories", [])),
        "lead_section": lead.get("section", "Daily edition"),
        "lead_headline": lead.get("headline", "A new edition of The Daily Abby"),
        "lead_dek": lead.get("dek", "Your personal daily newspaper."),
    }
    (edition_dir / "edition.json").write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    build_archive_page()
    print(f"Built edition: {issue_html_path}")
    print(f"Built PDF: {issue_pdf_path}")


if __name__ == "__main__":
    main()
