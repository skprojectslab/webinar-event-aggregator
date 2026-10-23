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
    """Classify from the event title/URL, not a long description.

    Descriptions can mention other events (for example, a roundup article
    mentioning a summit or webinar) and must not determine the event type.
    Call this function with the title plus the individual event URL only.
    """
    t = clean(text).lower()

    # Match specific event formats only when stated in the title or URL.
    for word, typ in [
        ("webinar", "webinar"),
        ("conference", "conference"),
        ("summit", "conference"),
        ("workshop", "workshop"),
        ("masterclass", "workshop"),
        ("seminar", "seminar"),
        ("meetup", "meetup"),
    ]:
        if re.search(r"\b" + re.escape(word) + r"\b", t):
            return typ

    return fallback

def is_non_event_article(url, title=""):
    """Reject generic pages and editorial content, while preserving individual events."""
    parsed = urlparse(clean(url))
    path = parsed.path.lower().rstrip("/")
    title_text = clean(title).lower()

    # Exclude user profile pages.
    if re.search(r"/user/\d+(?:/profile)?$", path):
        return True

    # Exclude generic utility and navigation pages.
    if re.search(
        r"/(?:contact(?:-us)?|about-us|privacy-policy|terms(?:-and-conditions)?"
        r"|accessibility|search|login|register|signup|sign-up"
        r"|press-releases?|media-contacts?)/?$",
        path,
        re.I,
    ):
        return True

    # Exclude generic listing/index pages, not individual event detail pages.
    if re.search(
        r"/(?:blogs?|news|articles?|insights?|events?|webinars?|"
        r"conferences?|resources?|press-releases?)/?$",
        path,
        re.I,
    ):
        return True

    # Exclude known non-event content patterns.
    if re.search(
        r"event[\s_-]*round[\s_-]*up|event[\s_-]*roundup",
        path + " " + title_text,
        re.I,
    ):
        return True

    # Exclude clearly editorial titles.
    if re.search(
        r"\b(?:opinion|commentary|editorial|"
        r"insights and publications)\b",
        title_text,
        re.I,
    ):
        return True

    return False

def is_techuk_event_url(url):
    """Allow individual techUK event pages, but not editorial content."""
    path = urlparse(clean(url)).path.lower()
    if is_non_event_article(url):
        return False
    return (
        path.startswith("/what-we-deliver/events/")
        or path.startswith("/events/")
    )


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

    if source["id"] == "techuk":
        # techUK's events page contains many generic navigation/footer list
        # items. Only inspect event cards plus links under the events path.
        cards = soup.select(
            "article, .event-item, .event, .event-card, "
            "[class*='event-card']"
        )
        event_links = soup.select(
            "a[href*='/what-we-deliver/events/'], a[href*='/events/']"
        )
        existing_hrefs = {
            a.get("href")
            for card in cards
            for a in card.select(
                "a[href*='/what-we-deliver/events/'], a[href*='/events/']"
            )
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

            if source["id"] == "techuk" and not is_techuk_event_url(u):
                continue
            if is_non_event_article(u, txt):
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
                f"{title} {event_url}",
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
                or is_non_event_article(u, txt)
                or (source["id"] == "techuk" and not is_techuk_event_url(u))
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
                    f"{txt} {u}",
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

            # techUK now redirects the older calendar URL to /events.
            # Do not assume a control named exactly "Show more events" exists.
            # Repeatedly inspect likely load-more/next controls and scroll the
            # current listing, stopping after several rounds without growth.
            if s["id"] == "techuk":
                page.wait_for_timeout(4000)
                print(f"[techUK DEBUG] URL: {page.url}")
                print(f"[techUK DEBUG] Page title: {page.title()}")

                event_link_selector = (
                    "a[href*='/what-we-deliver/events/'], "
                    "a[href^='/events/'], "
                    "a[href*='techuk.org/events/']"
                )
                stagnant_rounds = 0
                total_clicks = 0
                previous_count = page.locator(event_link_selector).count()

                print(
                    f"[techUK DEBUG] Initial event-path links: {previous_count}"
                )

                for attempt in range(15):
                    clicked = False

                    # Try common accessible names first; the exact wording
                    # can change between versions of the techUK site.
                    candidate_selectors = [
                        page.get_by_role(
                            "button",
                            name=re.compile(r"show\s*more|load\s*more|next|more events", re.I),
                    ),
                        page.get_by_role(
                            "link",
                            name=re.compile(r"show\s*more|load\s*more|next|more events", re.I),
                    ),
                        page.locator(
                            "button[class*='more'], a[class*='more'], "
                            "button[class*='load'], a[class*='load'], "
                            "button[aria-label*='next' i], a[aria-label*='next' i], "
                            "[role='button'][aria-label*='next' i]"
                        ),
                    ]

                    for locator in candidate_selectors:
                        try:
                            count = locator.count()
                        except Exception:
                            continue

                        for index in range(count - 1, -1, -1):
                            candidate = locator.nth(index)
                            try:
                                if not candidate.is_visible():
                                    continue
                                label = " ".join(
                                    candidate.inner_text(timeout=700).split()
                                )
                                # Don't click generic "Events" navigation.
                                if label and not re.search(
                                    r"show\s*more|load\s*more|more events|^next\b|next page",
                                    label,
                                    re.I,
                                ):
                                    continue

                                before_click = page.locator(
                                    event_link_selector
                                ).count()
                                candidate.scroll_into_view_if_needed(timeout=2500)
                                candidate.click(timeout=4000)
                                page.wait_for_timeout(1400)
                                after_click = page.locator(
                                    event_link_selector
                                ).count()

                                if after_click > before_click:
                                    total_clicks += 1
                                    clicked = True
                                    print(
                                        f"[techUK DEBUG] Click {total_clicks}: "
                                        f"{label[:100]!r}; links {before_click}->{after_click}"
                                    )
                                    previous_count = after_click
                                    break
                            except Exception as exc:
                                print(
                                    f"[techUK DEBUG] Candidate control skipped: "
                                    f"{type(exc).__name__}: {exc}"
                                )

                        if clicked:
                            break

                    if not clicked:
                        # The current /events page may lazy-load as the page
                        # scrolls, even when no explicit load-more control exists.
                        try:
                            page.evaluate(
                                "window.scrollTo(0, document.body.scrollHeight)"
                            )
                        except Exception:
                            pass
                        page.wait_for_timeout(1300)
                        current_count = page.locator(
                            event_link_selector
                        ).count()
                        print(
                            f"[techUK DEBUG] Scroll round {attempt + 1}: "
                            f"event-path links={current_count}"
                        )

                        if current_count > previous_count:
                            previous_count = current_count
                            stagnant_rounds = 0
                        else:
                            stagnant_rounds += 1

                        if stagnant_rounds >= 3:
                            print(
                                "[techUK DEBUG] Stopping after 3 rounds "
                                "without additional event links."
                            )
                            break
                    else:
                        stagnant_rounds = 0

                print(f"[techUK DEBUG] Successful load-more clicks: {total_clicks}")
                print(
                    "[techUK DEBUG] Final event-path links: "
                    + str(page.locator(event_link_selector).count())
                )

                # Collect actual event records from techUK's official ticket
                # listing to supplement the redirected /events page.
                ticket_items = []
                ticket_page = None
                try:
                    ticket_page = browser.new_page(
                        user_agent=UA,
                        viewport={"width": 1440, "height": 1000},
                    )
                    ticket_page.goto(
                        "https://tickets.techuk.org/events/techukuat/",
                        wait_until="domcontentloaded",
                        timeout=60000,
                    )
                    ticket_page.wait_for_timeout(2500)
                    ticket_html = ticket_page.content()
                    ticket_final_url = ticket_page.url

                    # Temporary diagnostic: print HTML lines that reveal the
                    # ticket platform's event-card structure and detail links.
                    print("[techUK DEBUG] Ticket HTML length:", len(ticket_html))
                    print("[techUK DEBUG] Ticket HTML structure clues:")
                    clue_count = 0
                    for line in ticket_html.splitlines():
                        lower_line = line.lower()
                        if any(
                            marker in lower_line
                            for marker in (
                                "application/ld+json",
                                "eventname",
                                "startdate",
                                "enddate",
                                "event",
                                "location",
                                "href=",
                                "title=",
                                "datetime",
                            )
                        ):
                            print("[techUK HTML]", line[:700])
                            clue_count += 1
                            if clue_count >= 35:
                                break
                    if clue_count == 0:
                        print("[techUK HTML] No matching lines; HTML prefix:")
                        print("[techUK HTML]", ticket_html[:2500])

                    ticket_items = extract(ticket_html, ticket_final_url, s)
                    print(
                        f"[techUK DEBUG] Ticket-list URL: {ticket_final_url}"
                    )
                    print(
                        f"[techUK DEBUG] Ticket-list extracted event records: "
                        f"{len(ticket_items)}"
                    )

                    # Fallback for ticket-platform markup that the generic
                    # extractor doesn't recognize. Start from visible headings
                    # (the indexed ticket page renders event titles as h2/h3),
                    # inspect their nearby card/container, then locate a likely
                    # event-detail link without assuming a specific URL path.
                    if not ticket_items:
                        ticket_soup = BeautifulSoup(ticket_html, "lxml")
                        seen_ticket_keys = set()
                        heading_nodes = ticket_soup.select(
                            "h1, h2, h3, h4, "
                            "[class*='event-title'], [class*='event-name']"
                        )

                        ignored_titles = {
                            "buy tickets", "event details", "select tickets",
                            "sold out", "unavailable", "the uk's technology trade association",
                        }

                        for heading in heading_nodes:
                            title = clean(heading.get_text(" ", strip=True))
                            title_key = normalize_title(title)
                            if (
                                not title
                                or len(title) < 12
                                or len(title) > 220
                                or title_key in ignored_titles
                                or title_key in seen_ticket_keys
                            ):
                                continue

                            # Look up a compact event card around the title.
                            card = heading
                            for _ in range(5):
                                if not card.parent:
                                    break
                                card = card.parent
                                card_text = clean(card.get_text(" ", strip=True))
                                card_links = card.select("a[href]")
                                if (
                                    len(card_text) >= len(title)
                                    and len(card_text) <= 1800
                                    and card_links
                                ):
                                    # Prefer a detail link; avoid broad list,
                                    # login, checkout, and ticket-selection URLs.
                                    usable_links = []
                                    for anchor in card_links:
                                        href = abs_url(
                                            anchor.get("href"), ticket_final_url
                                        )
                                        if not href:
                                            continue
                                        parsed = urlparse(href)
                                        path_lower = parsed.path.lower().rstrip("/")
                                        anchor_text = clean(
                                            anchor.get_text(" ", strip=True)
                                        ).lower()
                                        if re.search(
                                            r"login|register|checkout|basket|"
                                            r"select-ticket|ticket-selection|"
                                            r"privacy|terms|contact|about",
                                            path_lower,
                                            re.I,
                                        ):
                                            continue
                                        if anchor_text in {
                                            "event details", "select tickets",
                                            "buy tickets", "sold out", "unavailable",
                                        }:
                                            usable_links.append(href)
                                            continue
                                        if (
                                            parsed.netloc.lower()
                                            in {
                                                "tickets.techuk.org",
                                                "www.tickets.techuk.org",
                                            }
                                            and path_lower
                                            not in {
                                                "/events",
                                                "/events/techukuat",
                                            }
                                        ):
                                            usable_links.append(href)

                                    if usable_links:
                                        break

                            if not card_links:
                                continue

                            # Prefer the event-details link if present; otherwise
                            # use the first non-listing link associated with card.
                            event_url = None
                            for anchor in card_links:
                                href = abs_url(anchor.get("href"), ticket_final_url)
                                anchor_text = clean(
                                    anchor.get_text(" ", strip=True)
                                ).lower()
                                if not href:
                                    continue
                                parsed = urlparse(href)
                                path_lower = parsed.path.lower().rstrip("/")
                                if (
                                    anchor_text == "event details"
                                    and path_lower not in {
                                        "/events",
                                        "/events/techukuat",
                                    }
                                ):
                                    event_url = href
                                    break

                            if not event_url:
                                for anchor in card_links:
                                    href = abs_url(anchor.get("href"), ticket_final_url)
                                    if not href:
                                        continue
                                    parsed = urlparse(href)
                                    if (
                                        parsed.netloc.lower()
                                        in {
                                            "tickets.techuk.org",
                                            "www.tickets.techuk.org",
                                        }
                                        and parsed.path.lower().rstrip("/")
                                        not in {
                                            "/events",
                                            "/events/techukuat",
                                        }
                                    ):
                                        event_url = href
                                        break

                            if not event_url:
                                continue

                            seen_ticket_keys.add(title_key)
                            card_text = clean(card.get_text(" ", strip=True))
                            combined = f"{title} {card_text}"
                            event = {
                                "source_id": s["id"],
                                "source_name": s["name"],
                                "title": title,
                                "description": card_text[:800],
                                "date": parse_date(combined),
                                "type": event_type(
                                    f"{title} {event_url}", s["type"]
                                ),
                                "category": category(combined, s["category"]),
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
                                "registration_url": event_url,
                                "source_url": ticket_final_url,
                                "scraped_at": now_utc(),
                            }
                            event["id"] = stable_event_id(event)
                            ticket_items.append(event)

                    print(
                        f"[techUK DEBUG] Ticket-list final candidate records: "
                        f"{len(ticket_items)}"
                    )
                    for event in ticket_items[:5]:
                        print(
                            f"[techUK DEBUG] Ticket event sample: "
                            f"{event.get('title', '')[:120]!r} | "
                            f"{event.get('date')}"
                        )
                except Exception as exc:
                    print(
                        f"[techUK DEBUG] Ticket-list extraction failed: "
                        f"{type(exc).__name__}: {exc}"
                    )
                finally:
                    if ticket_page:
                        ticket_page.close()

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

            if s["id"] == "techuk":
                items.extend(ticket_items)

                # Deduplicate the combined main-page and ticket-page results.
                unique_items = []
                seen_item_ids = set()
                for item in items:
                    item_id = stable_event_id(item)
                    if item_id in seen_item_ids:
                        continue
                    seen_item_ids.add(item_id)
                    unique_items.append(item)
                items = unique_items
                print(
                    f"[techUK DEBUG] Combined unique techUK records: {len(items)}"
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

    # Exclude historical records identified as editorial content.
    # This prevents previously saved blog/round-up pages from being
    # reintroduced after the extraction filters have rejected them.
    if is_non_event_article(
        old.get("event_url", ""),
        old.get("title", ""),
    ):
        print(
            f"[FILTER] Excluding historical editorial record: "
            f"{old.get('title', '')} | {old.get('event_url', '')}"
        )
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
