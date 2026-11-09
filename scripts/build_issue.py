import os
import json
import re
import hashlib
import datetime
import pathlib
import html
import calendar
import time
import random
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


# Gemini service errors that are generally temporary and worth retrying.
TRANSIENT_GEMINI_CODES = {500, 502, 503, 504}


def clean(value):
    """Strip HTML and collapse whitespace from feed content."""
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", value or "")).strip()


def is_recent(entry, hours=24):
    """Return True when an entry is within the configured lookback window."""
    stamp = entry.get("published_parsed") or entry.get("updated_parsed")
    if not stamp:
        # Some feeds omit dates. Keep them rather than silently dropping them.
        return True

    published = datetime.datetime.fromtimestamp(
        calendar.timegm(stamp), tz=datetime.timezone.utc
    )
    cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=hours)
    return published >= cutoff


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
                break

        if not image:
            media = entry.get("media_content", [])
            if media:
                image = media[0].get("url", "")

        published = entry.get("published", entry.get("updated", ""))
        results.append(
            {
                "title": title,
                "url": link,
                "summary": clean(entry.get("summary", ""))[:1200],
                "source": source_name,
                "section": section,
                "image": image,
                "kind": "article",
                "published": published,
            }
        )

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
        if not title:
            continue

        if re.search(r"\b(live|livestream|study with me|pomodoro stream)\b", title, re.I):
            continue

        video_id = entry.get("yt_videoid", "")
        results.append(
            {
                "title": title,
                "url": entry.get("link", ""),
                "summary": clean(entry.get("summary", ""))[:900],
                "source": channel["name"],
                "section": channel["section"],
                "image": (
                    f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg" if video_id else ""
                ),
                "kind": "video",
                "published": entry.get("published", ""),
            }
        )

        if len(results) >= 8:
            break

    return results


def fetch_github(repo):
    url = f"https://github.com/{repo['name']}/releases.atom"
    return fetch_rss(url, repo["name"], repo["section"], limit=6)


def collect():
    items = []

    for source in sources.get("rss", []):
        try:
            items += fetch_rss(source["url"], source["name"], source["section"])
        except Exception as exc:
            print("RSS error", source.get("name", source.get("url", "unknown")), exc)

    for channel in sources.get("youtube_channels", []):
        try:
            items += fetch_youtube(channel)
        except Exception as exc:
            print("YouTube error", channel.get("name", "unknown"), exc)

    for repo in sources.get("github_repos", []):
        try:
            items += fetch_github(repo)
        except Exception as exc:
            print("GitHub error", repo.get("name", "unknown"), exc)

    seen = set()
    unique = []
    for item in items:
        canonical = item.get("url") or item.get("title", "")
        key = hashlib.md5(canonical.encode("utf-8")).hexdigest()
        if key not in seen:
            seen.add(key)
            unique.append(item)

    # ISO/RFC publication date strings sort chronologically for the feeds in this project.
    unique.sort(key=lambda item: item.get("published", ""), reverse=True)
    print(f"Collected {len(unique)} unique feed items")
    return unique


def fetch_og_image(url):
    """Try to get an Open Graph image where the feed has no image."""
    if not url:
        return ""

    try:
        response = requests.get(
            url,
            timeout=10,
            headers={"User-Agent": "Mozilla/5.0"},
        )
        response.raise_for_status()

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
    except requests.RequestException as exc:
        print(f"Image lookup skipped for {url}: {exc}")
        return ""


def source_feed_digest(items):
    """Create a transparent feed-based edition when Gemini is unavailable.

    This does not claim that AI selected, ranked, or summarised the stories.
    It uses the titles and descriptions supplied by the source feeds.
    """
    stories = []

    for item in items[: prefs.get("max_stories", 12)]:
        original_summary = clean(item.get("summary", ""))
        source_name = item.get("source", "Source feed")
        section = item.get("section") or "Briefing"

        if original_summary:
            dek = original_summary[:220]
            summary = original_summary[:900]
        else:
            dek = f"A recent update from {source_name}. Open the original source for details."
            summary = (
                "The source feed did not provide a description for this item. "
                "Open the original link to read the full story."
            )

        stories.append(
            {
                **item,
                "section": section,
                "headline": item.get("title") or "Untitled update",
                "dek": dek,
                "why_it_matters": (
                    "Automatic AI curation was unavailable for this edition; "
                    "this item is shown from its source feed."
                ),
                "summary": summary,
                "image_query": "",
            }
        )

    return {
        "edition_note": (
            "Gemini was temporarily unavailable. This edition is an "
            "unranked digest of collected source-feed items."
        ),
        "lead_index": 0,
        "stories": stories,
        "curation_mode": "source-feed digest (AI unavailable)",
    }


def curate(items):
    """Ask Gemini to curate the issue, with retries and model fallbacks."""
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError("Missing GEMINI_API_KEY environment variable")

    client = genai.Client(api_key=api_key)
    primary_model = os.environ.get("GEMINI_MODEL", "gemini-3.8-flash").strip()

    # All of these IDs are listed as stable text-capable Gemini API models
    # in Google's model documentation at the time this code was prepared.
    default_fallbacks = (
        "gemini-3.7-flash,"
        "gemini-3.6-flash,"
        "gemini-3.5-flash,"
        "gemini-3.5-flash-lite,"
        "gemini-3.1-flash-lite"
    )
    fallback_models = [
        name.strip()
        for name in os.environ.get("GEMINI_FALLBACK_MODELS", default_fallbacks).split(",")
        if name.strip()
    ]
    models_to_try = list(dict.fromkeys([primary_model, *fallback_models]))

    payload = [
        {
            key: item.get(key, "")
            for key in ("title", "url", "summary", "source", "section", "kind")
        }
        for item in items[:100]
    ]

    prompt = f"""
You are the editor of {prefs['masthead']}, a finite personal newspaper.
Reader profile: {json.dumps(prefs['editorial_profile'], ensure_ascii=False)}
Interest weights (relative weights, not percentages that must sum to 100):
{json.dumps(prefs['interest_weights'], ensure_ascii=False)}

Choose up to {prefs.get('max_stories', 12)} items that are genuinely useful,
diverse, recent and non-duplicative.
Prioritise AI, Claude, Gemini, AI agents and developer tools. Balance these with
books, literature, writing, psychology, world news, science, business, coding,
study/productivity, design, culture and curiosity.
Exclude shopping promotions, affiliate links, generic viral posts, stale content
and low-value announcements.
Do not invent facts or claim a source says something absent from its title or summary.
Return ONLY valid JSON in this exact shape:
{{"edition_note":"...","lead_index":0,"stories":[{{"index":0,
"section":"...","headline":"...","dek":"one sentence",
"why_it_matters":"one concise sentence","summary":"60-100 words",
"image_query":"short image description"}}]}}

Items:
{json.dumps(payload, ensure_ascii=False)}
"""

    response = None
    last_error = None
    attempts_per_model = int(os.environ.get("GEMINI_ATTEMPTS_PER_MODEL", "2"))
    attempts_per_model = max(1, min(attempts_per_model, 4))

    for candidate_model in models_to_try:
        for attempt in range(1, attempts_per_model + 1):
            try:
                print(
                    f"Generating edition with {candidate_model} "
                    f"(attempt {attempt}/{attempts_per_model})"
                )
                response = client.models.generate_content(
                    model=candidate_model,
                    contents=prompt,
                )

                if response is None or not getattr(response, "text", None):
                    raise RuntimeError(
                        f"Gemini model {candidate_model} returned an empty response."
                    )

                print(f"Gemini generation succeeded with {candidate_model}")
                break

            except errors.APIError as exc:
                # A 429 can be model-specific rate limiting or project quota.
                # Try the next configured model once; if all models are limited,
                # the source-feed digest below still lets the archive publish.
                if exc.code == 429:
                    last_error = exc
                    print(
                        f"{candidate_model} returned 429 (rate limit/quota); "
                        "trying the next configured model."
                    )
                    break

                if exc.code not in TRANSIENT_GEMINI_CODES:
                    raise

                last_error = exc
                if attempt < attempts_per_model:
                    base_delay = min(30, 5 * (2 ** (attempt - 1)))
                    delay = base_delay + random.uniform(0, 1.5)
                    print(
                        f"{candidate_model} returned {exc.code}; "
                        f"retrying in {delay:.1f} seconds."
                    )
                    time.sleep(delay)
                else:
                    print(
                        f"{candidate_model} unavailable after "
                        f"{attempts_per_model} attempts; trying next model."
                    )

        if response is not None and getattr(response, "text", None):
            break
        response = None

    if response is None:
        print(
            "WARNING: All configured Gemini models were unavailable. "
            "Creating a source-feed digest instead."
        )
        if last_error:
            print(f"Last Gemini service error: {last_error}")
        return source_feed_digest(items)

    raw = response.text.strip()
    raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.M).strip()

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            "Gemini responded, but the response was not valid JSON. "
            "The workflow stopped rather than publishing malformed AI content."
        ) from exc

    chosen = []
    for story in data.get("stories", []):
        index = story.get("index")
        if isinstance(index, int) and 0 <= index < len(payload):
            item = items[index].copy()
            item.update(story)
            chosen.append(item)

    if not chosen:
        print("Gemini returned no usable story selections; using source-feed digest.")
        return source_feed_digest(items)

    for story in chosen:
        if not story.get("image"):
            story["image"] = fetch_og_image(story.get("url", ""))

    data["stories"] = chosen
    data["curation_mode"] = f"AI-curated ({primary_model} with fallback support)"
    return data


def render_pdf(html_path, pdf_path):
    """Render one HTML edition as an A4 PDF."""
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        try:
            page = browser.new_page(
                viewport={"width": 1240, "height": 1754},
                device_scale_factor=1,
            )
            page.goto(
                html_path.resolve().as_uri(),
                wait_until="networkidle",
                timeout=90000,
            )
            page.pdf(
                path=str(pdf_path),
                format="A4",
                print_background=True,
                margin={
                    "top": "10mm",
                    "right": "10mm",
                    "bottom": "10mm",
                    "left": "10mm",
                },
            )
        finally:
            browser.close()


def build_archive_page():
    """Build the archive homepage from every saved edition metadata file."""
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

    editions.sort(
        key=lambda edition: edition.get("date_iso", ""),
        reverse=True,
    )
    years = sorted(
        {
            edition.get("date_iso", "")[:4]
            for edition in editions
            if edition.get("date_iso")
        },
        reverse=True,
    )

    archive = {
        "masthead": prefs.get("masthead", "THE DAILY ABBY"),
        "editions": editions,
        "edition_count": len(editions),
        "years": years,
        "updated": datetime.datetime.now(TIMEZONE).strftime(
            "%d %b %Y · %I:%M %p"
        ),
    }

    template = TEMPLATES.get_template("archive_template.html")
    page = template.render(archive=archive)
    (OUT / "index.html").write_text(page, encoding="utf-8")
    print(f"Built archive index with {len(editions)} edition(s)")


def main():
    items = collect()
    if not items:
        # Keep the current archive homepage buildable, but fail the run so that
        # a no-feed day is visible in Actions rather than silently pretending
        # a new newspaper was generated.
        build_archive_page()
        raise RuntimeError(
            "No feed items were collected. Existing archive was preserved; "
            "no new dated edition was created."
        )

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
        "lead_headline": lead.get(
            "headline", "A new edition of The Daily Abby"
        ),
        "lead_dek": lead.get("dek", "Your personal daily newspaper."),
        "curation_mode": edition.get("curation_mode", "AI-curated"),
    }

    (edition_dir / "edition.json").write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    build_archive_page()
    print(f"Built edition: {issue_html_path}")
    print(f"Built PDF: {issue_pdf_path}")
    print(f"Curation mode: {metadata['curation_mode']}")


if __name__ == "__main__":
    main()
