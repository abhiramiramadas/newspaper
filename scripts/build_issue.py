
import os, json, re, hashlib, datetime, pathlib, html, calendar
import feedparser, requests
from google import genai
from google.genai import errors
from jinja2 import Template
from playwright.sync_api import sync_playwright

ROOT = pathlib.Path(__file__).resolve().parents[1]
OUT = ROOT / "output"
OUT.mkdir(exist_ok=True)
sources = json.loads((ROOT / "sources.json").read_text(encoding="utf-8"))
prefs = json.loads((ROOT / "preferences.json").read_text(encoding="utf-8"))

def clean(s):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", s or "")).strip()

def is_recent(entry, hours=24):
    stamp = entry.get("published_parsed") or entry.get("updated_parsed")
    if not stamp: return True
    published = datetime.datetime.fromtimestamp(calendar.timegm(stamp), tz=datetime.timezone.utc)
    return published >= datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=hours)

def fetch_rss(url, source_name, section, limit=12):
    feed = feedparser.parse(url)
    out = []
    for e in feed.entries:
        if not is_recent(e): continue
        link = e.get("link", "")
        title = clean(e.get("title", ""))
        if not title or not link: continue
        image = ""
        for enc in e.get("enclosures", []):
            if enc.get("type", "").startswith("image/"): image = enc.get("href", "")
        if not image:
            media = e.get("media_content", [])
            if media: image = media[0].get("url", "")
        published = e.get("published", e.get("updated", ""))
        out.append({"title": title, "url": link, "summary": clean(e.get("summary", ""))[:1200],
                    "source": source_name, "section": section, "image": image, "kind": "article",
                    "published": published})
        if len(out) >= limit: break
    return out

def fetch_youtube(channel):
    url = f"https://www.youtube.com/feeds/videos.xml?channel_id={channel['channel_id']}"
    feed = feedparser.parse(url)
    out = []

    for e in feed.entries:
        if not is_recent(e):
            continue

        title = clean(e.get("title", ""))

        if re.search(r"\b(live|livestream|study with me|pomodoro stream)\b", title, re.I):
            continue

        video_id = e.get("yt_videoid", "")

        out.append({
            "title": title,
            "url": e.get("link", ""),
            "summary": clean(e.get("summary", ""))[:900],
            "source": channel["name"],
            "section": channel["section"],
            "image": f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg" if video_id else "",
            "kind": "video",
            "published": e.get("published", "")
        })

        if len(out) >= 8:
            break

    return out

def fetch_github(repo):
    url = f"https://github.com/{repo['name']}/releases.atom"
    return fetch_rss(url, repo["name"], repo["section"], limit=6)

def collect():
    items=[]
    for s in sources["rss"]:
        try: items += fetch_rss(s["url"], s["name"], s["section"])
        except Exception as e: print("RSS error", s["name"], e)
    for c in sources["youtube_channels"]:
        try: items += fetch_youtube(c)
        except Exception as e: print("YouTube error", c["name"], e)
    for r in sources["github_repos"]:
        try: items += fetch_github(r)
        except Exception as e: print("GitHub error", r["name"], e)
    seen=set(); unique=[]
    for x in items:
        key=hashlib.md5((x["url"] or x["title"]).encode()).hexdigest()
        if key not in seen: seen.add(key); unique.append(x)
    unique.sort(key=lambda x: x.get("published", ""), reverse=True)
    return unique

def fetch_og_image(url):
    try:
        response = requests.get(url, timeout=12, headers={"User-Agent":"Mozilla/5.0"})
        match = re.search(r'<meta[^>]+property="og:image"[^>]+content="([^"]+)', response.text, re.I)
        if not match:
            match = re.search(r'<meta[^>]+content="([^"]+)"[^>]+property="og:image"', response.text, re.I)
        return html.unescape(match.group(1)) if match else ""
    except Exception:
        return ""

def curate(items):
    key=os.environ.get("GEMINI_API_KEY")
    if not key: raise RuntimeError("Missing GEMINI_API_KEY environment variable")
    client=genai.Client(api_key=key)
    model=os.environ.get("GEMINI_MODEL","gemini-3.8-flash")
    payload=[{k:x[k] for k in ("title","url","summary","source","section","kind")} for x in items[:100]]
    prompt=f"""
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
{json.dumps(payload,ensure_ascii=False)}
"""
    fallback_models = [
        m.strip()
        for m in os.environ.get(
            "GEMINI_FALLBACK_MODELS",
            "gemini-3.7-flash,gemini-3.6-flash"
        ).split(",")
        if m.strip()
    ]

    models_to_try = list(dict.fromkeys([model, *fallback_models]))
    response = None
    last_error = None

    for candidate_model in models_to_try:
        try:
            print(f"Generating edition with {candidate_model}")
            response = client.models.generate_content(
                model=candidate_model,
                contents=prompt
            )
            print(f"Generation succeeded with {candidate_model}")
            break
        except errors.APIError as exc:
            if exc.code not in (500, 502, 503, 504):
                raise
            last_error = exc
            print(
                f"{candidate_model} unavailable ({exc.code}). "
                "Trying next model."
            )

    if response is None:
        raise RuntimeError(
            f"All Gemini models were unavailable. Last error: {last_error}"
        ) from last_error

    raw = response.text.strip()
    raw=re.sub(r"^```(?:json)?|```$", "", raw, flags=re.M).strip()
    data=json.loads(raw)
    chosen=[]
    for story in data.get("stories",[]):
        i=story.get("index")
        if isinstance(i,int) and 0 <= i < len(payload):
            item=items[i].copy(); item.update(story); chosen.append(item)
    for story in chosen:
        if not story.get("image"):
            story["image"] = fetch_og_image(story.get("url", ""))
    data["stories"]=chosen
    return data

def main():
    items=collect()
    if not items: raise RuntimeError("No feed items found. Check sources.json and feed URLs.")
    edition=curate(items)
    now=datetime.datetime.now(datetime.timezone.utc).astimezone()
    edition["date"]=now.strftime("%A, %d %B %Y")
    edition["edition_number"]=now.strftime("%Y%m%d")
    edition["masthead"]=prefs["masthead"]
    edition["reading_time"]="8–10 MIN READ"
    edition["caught_up"]="You're caught up. Close the tabs. Go live your day."
    template=Template((ROOT/"app/template.html").read_text(encoding="utf-8"))
    page=template.render(edition=edition)
    (OUT/"index.html").write_text(page,encoding="utf-8")
    with sync_playwright() as p:
        browser=p.chromium.launch()
        page_obj=browser.new_page(viewport={"width":1240,"height":1754}, device_scale_factor=1)
        page_obj.set_content(page, wait_until="networkidle", timeout=90000)
        page_obj.pdf(path=str(OUT/"daily-abby.pdf"), format="A4", print_background=True,
                     margin={"top":"10mm","right":"10mm","bottom":"10mm","left":"10mm"})
        browser.close()
    print("Built", OUT/"index.html", "and", OUT/"daily-abby.pdf")

if __name__=="__main__": main()
