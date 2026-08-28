import hashlib
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urldefrag, urlparse

from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from sources import SOURCES


BASE = Path(__file__).resolve().parent.parent
OUT = BASE / "data" / "events.json"

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 Chrome/131 Safari/537.36"
)


# =========================================================
# HELPERS
# =========================================================

def clean(x):
    return re.sub(r"\s+", " ", str(x or "")).strip()


def now_utc():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def abs_url(raw, base):
    if not raw:
        return None

    raw = clean(raw)

    if raw.lower().startswith(
        ("javascript:", "mailto:", "tel:", "#")
    ):
        return None

    u = urljoin(base, raw)
    p = urlparse(u)

    if p.scheme not in ("http", "https") or not p.netloc:
        return None

    u, _ = urldefrag(u)

    return u


# =========================================================
# DATE PARSING
# =========================================================

def parse_date(text):
    text = clean(text)

    pats = [
        (
            r"\b(20\d{2})[-/](\d{1,2})[-/](\d{1,2})\b",
            lambda m: (
                int(m.group(1)),
                int(m.group(2)),
                int(m.group(3)),
            ),
        ),
        (
            r"\b(\d{1,2})[-/](\d{1,2})[-/](20\d{2})\b",
            lambda m: (
                int(m.group(3)),
                int(m.group(2)),
                int(m.group(1)),
            ),
        ),
    ]

    for pat, fn in pats:
        m = re.search(pat, text)

        if m:
            try:
                y, mo, d = fn(m)
                return datetime(y, mo, d).isoformat()
            except ValueError:
                pass

    months = (
        "January|February|March|April|May|June|July|August|"
        "September|October|November|December|Jan|Feb|Mar|Apr|"
        "May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec"
    )

    m = re.search(
        rf"\b(?:\d{{1,2}}\s+(?:{months})\s+20\d{{2}}|"
        rf"(?:{months})\s+\d{{1,2}},?\s+20\d{{2}})\b",
        text,
        re.I,
    )

    if m:
        for fmt in (
            "%d %B %Y",
            "%d %b %Y",
            "%B %d %Y",
            "%b %d %Y",
        ):
            try:
                return datetime.strptime(
                    m.group(0).replace(",", ""),
                    fmt,
                ).isoformat()
            except ValueError:
                pass

    return None


# =========================================================
# EVENT TYPE
# =========================================================

def event_type(text, fallback):
    t = clean(text).lower()

    for word, typ in [
        ("conference", "conference"),
        ("summit", "conference"),
        ("workshop", "workshop"),
        ("masterclass", "workshop"),
        ("seminar", "seminar"),
        ("meetup", "meetup"),
        ("webinar", "webinar"),
    ]:
        if word in t:
            return typ

    return fallback


# =========================================================
# CATEGORY
# =========================================================

def category(text, fallback):
    t = " " + clean(text).lower() + " "

    category_words = {
        "AI & Machine Learning": [
            " ai ",
            "artificial intelligence",
            "machine learning",
            "generative ai",
            "genai",
        ],
        "Cloud Computing": [
            "cloud",
            "aws",
            "azure",
            "google cloud",
            "gcp",
        ],
        "Cybersecurity": [
            "cyber",
            "security",
            "zero trust",
            "infosec",
        ],
        "Digital Transformation": [
            "digital transformation",
            "automation",
            "modernization",
        ],
    }

    for c, words in category_words.items():
        if any(w in t for w in words):
            return c

    return fallback


# =========================================================
# EVENT DETECTION
# =========================================================

def eventish(url, text):
    return bool(
        re.search(
            r"(event|webinar|conference|summit|seminar|"
            r"workshop|register|registration|meetup|session)",
            f"{url} {text}",
            re.I,
        )
    )


# =========================================================
# STABLE EVENT ID
# =========================================================

def stable_event_id(e):
    """
    Create an ID that does NOT change every time the scraper runs.

    Primary identity:
        source + event URL

    Fallback:
        source + title + date
    """

    source_id = clean(e.get("source_id")).lower()
    event_url = clean(e.get("event_url")).lower()

    if event_url:
        identity = f"{source_id}|{event_url}"
    else:
        title = clean(e.get("title")).lower()
        date = clean(e.get("date"))[:10]
        identity = f"{source_id}|{title}|{date}"

    return hashlib.sha1(
        identity.encode("utf-8")
    ).hexdigest()


# =========================================================
# EXTRACT EVENTS
# =========================================================

def extract(html, final_url, source):

    soup = BeautifulSoup(html, "lxml")

    cards = soup.select(
        "article, .event-item, .event, .event-card, "
        "[class*='event-card'], li"
    )

    out = []

    def add(card):

        title_node = card.select_one(
            "h1,h2,h3,h4,.event-title,.title,[class*='title']"
        )

        title = clean(
            title_node.get_text(" ", strip=True)
            if title_node
            else ""
        )

        if not title:
            title = clean(
                card.get_text(" ", strip=True)
            )[:180]

        date_node = card.select_one(
            "time,.event-date,.date,[class*='date']"
        )

        date_text = clean(
            date_node.get_text(" ", strip=True)
            if date_node
            else ""
        )

        if date_node and date_node.get("datetime"):
            date_text += " " + date_node["datetime"]

        desc_node = card.select_one(
            ".event-description,.description,"
            "[class*='description'],p"
        )

        desc = clean(
            desc_node.get_text(" ", strip=True)
            if desc_node
            else ""
        )

        event_url = None
        reg = None

        for a in card.select("a[href]"):

            u = abs_url(
                a.get("href"),
                final_url,
            )

            txt = clean(
                a.get_text(" ", strip=True)
            )

            if not u:
                continue

            if not event_url and eventish(u, txt):
                event_url = u

            if not reg and re.search(
                r"register|registration|sign up|book",
                txt,
                re.I,
            ):
                reg = u

            if not event_url:
                event_url = u

        if not title or not event_url:
            return

        combined = f"{title} {desc} {date_text}"

        e = {
            "source_id": source["id"],
            "source_name": source["name"],
            "title": title,
            "description": desc[:800],
            "date": parse_date(combined),
            "type": event_type(
                combined,
                source["type"],
            ),
            "category": category(
                combined,
                source["category"],
            ),
            "location": (
                "Online"
                if re.search(
                    r"\bonline|virtual|webinar\b",
                    combined,
                    re.I,
                )
                else ""
            ),
            "event_url": event_url,
            "registration_url": reg or event_url,
            "source_url": final_url,
            "scraped_at": now_utc(),
        }

        e["id"] = stable_event_id(e)

        out.append(e)

    for c in cards[:500]:
        add(c)

    # -----------------------------------------------------
    # FALLBACK
    # -----------------------------------------------------

    if not out:

        seen = set()

        for a in soup.select("a[href]"):

            u = abs_url(
                a.get("href"),
                final_url,
            )

            txt = clean(
                a.get_text(" ", strip=True)
            )

            if (
                not u
                or not txt
                or len(txt) < 8
                or len(txt) > 240
                or u in seen
                or not eventish(u, txt)
            ):
                continue

            seen.add(u)

            parent = clean(
                a.parent.get_text(
                    " ",
                    strip=True,
                )
                if a.parent
                else txt
            )

            combined = f"{txt} {parent}"

            e = {
                "source_id": source["id"],
                "source_name": source["name"],
                "title": txt,
                "description": parent[:800],
                "date": parse_date(parent),
                "type": event_type(
                    combined,
                    source["type"],
                ),
                "category": category(
                    combined,
                    source["category"],
                ),
                "location": (
                    "Online"
                    if re.search(
                        r"\bonline|virtual|webinar\b",
                        combined,
                        re.I,
                    )
                    else ""
                ),
                "event_url": u,
                "registration_url": u,
                "source_url": final_url,
                "scraped_at": now_utc(),
            }

            e["id"] = stable_event_id(e)

            out.append(e)

    return out


# =========================================================
# LOAD PREVIOUS DATA
# =========================================================

previous_events = {}

if OUT.exists():

    try:

        previous_payload = json.loads(
            OUT.read_text(
                encoding="utf-8"
            )
        )

        old_events = previous_payload.get(
            "events",
            []
        )

        for old in old_events:

            old_id = old.get("id")

            if old_id:
                previous_events[old_id] = old

        print(
            f"[HISTORY] Loaded {len(previous_events)} previous events."
        )

    except Exception as ex:

        print(
            f"[HISTORY] Could not read previous events: {repr(ex)}"
        )

        previous_events = {}


# =========================================================
# MAIN SCRAPER
# =========================================================

all_events = []
results = []

scrape_started = now_utc()


with sync_playwright() as p:

    browser = p.chromium.launch(
        headless=True
    )

    for s in SOURCES:

        page = None

        try:

            page = browser.new_page(
                user_agent=UA,
                viewport={
                    "width": 1440,
                    "height": 1000,
                },
            )

            page.goto(
                s["url"],
                wait_until="domcontentloaded",
                timeout=60000,
            )

            page.wait_for_timeout(250)

            # Scroll once to trigger lazy-loaded event cards.
            page.evaluate(
                "window.scrollTo(0, document.body.scrollHeight)"
            )

            page.wait_for_timeout(1500)

            html = page.content()

            final = page.url

            items = extract(
                html,
                final,
                s,
            )

            all_events.extend(items)

            results.append(
                {
                    "source": s["name"],
                    "success": True,
                    "count": len(items),
                }
            )

            print(
                f"[OK] {s['name']}: {len(items)}"
            )

        except Exception as ex:

            results.append(
                {
                    "source": s["name"],
                    "success": False,
                    "count": 0,
                    "error": repr(ex),
                }
            )

            print(
                f"[FAIL] {s['name']}: {repr(ex)}"
            )

        finally:

            if page:
                page.close()

    browser.close()


# =========================================================
# DEDUPLICATE
# =========================================================

seen = set()

dedup = []

for e in all_events:

    # Ensure stable ID exists.
    e["id"] = stable_event_id(e)

    if e["id"] in seen:
        continue

    seen.add(e["id"])

    dedup.append(e)


# =========================================================
# NEW EVENT DETECTION
# =========================================================

new_count = 0

today = now_utc()


for e in dedup:

    event_id = e["id"]

    if event_id in previous_events:

        old = previous_events[event_id]

        # Preserve original first-seen date.
        e["first_seen"] = old.get(
            "first_seen",
            old.get(
                "scraped_at",
                scrape_started,
            ),
        )

        # This event existed before.
        e["is_new"] = False

    else:

        # Genuinely new event.
        e["first_seen"] = today

        e["is_new"] = True

        new_count += 1


# =========================================================
# SORT
# =========================================================

dedup.sort(
    key=lambda e: (
        e.get("date") or "9999-12-31",
        e.get("title") or "",
    )
)


# =========================================================
# WRITE OUTPUT
# =========================================================

payload = {

    "generated_at": today,

    "comparison": {
        "previous_event_count": len(
            previous_events
        ),
        "current_event_count": len(
            dedup
        ),
        "new_event_count": new_count,
        "scrape_started": scrape_started,
    },

    "events": dedup,

    "results": results,
}


# Automatically create data/ if it doesn't exist.
OUT.parent.mkdir(
    parents=True,
    exist_ok=True,
)


OUT.write_text(
    json.dumps(
        payload,
        ensure_ascii=False,
        indent=2,
    ),
    encoding="utf-8",
)


# =========================================================
# SUMMARY
# =========================================================

print(
    f"Saved {len(dedup)} events."
)

print(
    f"Previous events: {len(previous_events)}"
)

print(
    f"New events detected: {new_count}"
)
