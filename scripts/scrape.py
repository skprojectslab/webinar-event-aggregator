import hashlib
import json
import re
import sys
from difflib import SequenceMatcher
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
    Create an ID that remains stable for the same source event.

    Primary identity:
        source + canonical event URL

    Fallback:
        source + normalized title + date
    """

    source_id = clean(e.get("source_id")).lower()
    event_url = clean(e.get("event_url")).lower()

    if event_url:
        identity = f"{source_id}|{event_url}"
    else:
        title = normalize_title(e.get("title"))
        date = clean(e.get("date"))[:10]
        identity = f"{source_id}|{title}|{date}"

    return hashlib.sha1(
        identity.encode("utf-8")
    ).hexdigest()


def normalize_title(value):
    """Normalize an event title for cross-run comparison."""
    text = clean(value).lower()
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def match_previous_event(event, previous_events, excluded_ids=None):
    """
    Match a freshly scraped event to an older record.

    Matching priority:
      1. Exact stable ID (normally source + URL).
      2. Same source + normalized title + same date.
      3. Same source + exact normalized title when unique.
      4. Very-high title similarity for the same source when the old
         title is unique. This catches minor title edits without making
         unrelated events look identical.
    """

    excluded_ids = excluded_ids or set()

    event_id = event.get("id")
    if event_id in previous_events and event_id not in excluded_ids:
        return previous_events[event_id]

    source_id = clean(event.get("source_id")).lower()
    title = normalize_title(event.get("title"))
    date = clean(event.get("date"))[:10]

    candidates = [
        old for old in previous_events.values()
        if clean(old.get("source_id")).lower() == source_id
        and old.get("id") not in excluded_ids
    ]

    # Strongest content-based match: same source, title and date.
    exact = [
        old for old in candidates
        if normalize_title(old.get("title")) == title
        and clean(old.get("date"))[:10] == date
        and date
    ]
    if len(exact) == 1:
        return exact[0]

    # If the title is unchanged but the date moved, treat it as the same
    # event. This is important for postponed/rescheduled techUK events.
    same_title = [
        old for old in candidates
        if normalize_title(old.get("title")) == title
    ]
    if len(same_title) == 1:
        return same_title[0]

    # Conservative fuzzy fallback for small title edits.
    if title:
        scored = []
        for old in candidates:
            old_title = normalize_title(old.get("title"))
            if not old_title:
                continue
            score = SequenceMatcher(None, title, old_title).ratio()
            scored.append((score, old))

        scored.sort(key=lambda x: x[0], reverse=True)
        if scored and scored[0][0] >= 0.94:
            if len(scored) == 1 or scored[0][0] - scored[1][0] >= 0.03:
                return scored[0][1]

    return None


# =========================================================
# EXTRACT EVENTS
# =========================================================

def extract(html, final_url, source):

    soup = BeautifulSoup(html, "lxml")

    if s["id"] == "techuk":
        # techUK's events page contains many generic navigation/footer list
        # items. Only inspect event cards plus links under the events path.
        cards = soup.select(
            "article, .event-item, .event, .event-card, "
            "[class*='event-card']"
        )
        event_links = soup.select(
            "a[href*='/what-we-deliver/events/']"
        )
        existing_hrefs = {
            a.get("href")
            for card in cards
            for a in card.select("a[href*='/what-we-deliver/events/']")
        }
        for a in event_links:
            href = a.get("href")
            if href and href not in existing_hrefs:
                cards.append(a)
                existing_hrefs.add(href)
    else:
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

            if s["id"] == "techuk" and "/what-we-deliver/events/" not in u:
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

# Track which sources completed successfully.
# We only mark missing events inactive for a source when that
# source was successfully scraped. A failed source must not
# accidentally make its historical events disappear.
successful_source_ids = set()

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

            # techUK loads only the first batch of events initially and
            # exposes the remaining events behind a "Show more events"
            # control.  We must expand that list before extracting data;
            # otherwise an event can be missed today and incorrectly look
            # like a brand-new event tomorrow.
            if s["id"] == "techuk":
                page.wait_for_timeout(2000)

                for _ in range(30):
                    try:
                        more = page.get_by_text(
                            "Show more events",
                            exact=True,
                        )

                        if more.count() == 0:
                            break

                        target = more.last

                        if not target.is_visible():
                            break

                        target.scroll_into_view_if_needed()
                        target.click(timeout=5000)
                        page.wait_for_timeout(800)

                    except Exception:
                        break

            else:
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

            successful_source_ids.add(s["id"])

            results.append(
                {
                    "source_id": s["id"],
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
                    "source_id": s["id"],
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


matched_previous_ids = set()

for e in dedup:

    old = match_previous_event(e, previous_events, matched_previous_ids)

    if old:

        old_id = old.get("id")
        if old_id:
            matched_previous_ids.add(old_id)
            # Keep the historical ID even if the source changed its URL.
            e["id"] = old_id

        # Preserve original first-seen date.
        e["first_seen"] = old.get(
            "first_seen",
            old.get(
                "scraped_at",
                scrape_started,
            ),
        )

        # Same event, even if its URL/title/date was updated.
        # Preserve the historical "genuinely discovered as new" flag.
        # Once an event has been verified as a real addition, later scrapes
        # must not erase that history.
        e["is_new"] = bool(old.get("is_new", False))
        e["active"] = True

    else:

        # No matching record from the previous dataset.
        e["first_seen"] = today
        e["is_new"] = True
        e["active"] = True

        new_count += 1


# =========================================================
# RETAIN HISTORICAL EVENTS
# =========================================================
#
# The old implementation replaced the database with only the
# events returned by today's scrape. That meant an event that
# was discovered yesterday could disappear today if the source
# stopped displaying it.
#
# We now retain those historical records. If their source was
# successfully scraped today but the event was not returned,
# mark it inactive. If the source failed, leave its previous
# active state unchanged so a temporary scrape failure cannot
# hide data.
# =========================================================

merged_events = list(dedup)

for old_id, old in previous_events.items():

    if old_id in matched_previous_ids:
        continue

    historical = dict(old)

    historical["first_seen"] = historical.get(
        "first_seen",
        historical.get(
            "scraped_at",
            scrape_started,
        ),
    )

    historical["is_new"] = False

    source_id = historical.get("source_id")

    if source_id in successful_source_ids:
        historical["active"] = False
    else:
        historical["active"] = historical.get(
            "active",
            True,
        )

    merged_events.append(historical)


# =========================================================
# SORT
# =========================================================

merged_events.sort(
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
        # Current event count = events found in this scrape.
        # Historical inactive records are retained separately
        # inside the events array.
        "current_event_count": len(
            dedup
        ),
        "historical_event_count": len(
            merged_events
        ),
        "inactive_event_count": sum(
            1
            for e in merged_events
            if e.get("active") is False
        ),
        "new_event_count": new_count,
        "scrape_started": scrape_started,
    },

    "events": merged_events,

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
    f"Saved {len(merged_events)} historical events "
    f"({len(dedup)} currently active)."
)

print(
    f"Previous events: {len(previous_events)}"
)

print(
    f"New events detected: {new_count}"
)
