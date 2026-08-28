import hashlib, json, re
from datetime import datetime
from urllib.parse import urljoin, urldefrag, urlparse
from pathlib import Path
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from sources import SOURCES

BASE = Path(__file__).resolve().parent.parent
OUT = BASE / "data" / "events.json"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/131 Safari/537.36"

def clean(x): return re.sub(r"\s+", " ", str(x or "")).strip()

def abs_url(raw, base):
    if not raw: return None
    raw = clean(raw)
    if raw.lower().startswith(("javascript:", "mailto:", "tel:", "#")): return None
    u = urljoin(base, raw)
    p = urlparse(u)
    if p.scheme not in ("http","https") or not p.netloc: return None
    u, _ = urldefrag(u)
    return u

def parse_date(text):
    text = clean(text)
    pats = [
        (r"\b(20\d{2})[-/](\d{1,2})[-/](\d{1,2})\b", lambda m:(int(m.group(1)),int(m.group(2)),int(m.group(3)))),
        (r"\b(\d{1,2})[-/](\d{1,2})[-/](20\d{2})\b", lambda m:(int(m.group(3)),int(m.group(2)),int(m.group(1)))),
    ]
    for pat, fn in pats:
        m=re.search(pat,text)
        if m:
            try:
                y,mo,d=fn(m); return datetime(y,mo,d).isoformat()
            except ValueError: pass
    months="January|February|March|April|May|June|July|August|September|October|November|December|Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec"
    m=re.search(rf"\b(?:\d{{1,2}}\s+(?:{months})\s+20\d{{2}}|(?:{months})\s+\d{{1,2}},?\s+20\d{{2}})\b",text,re.I)
    if m:
        for fmt in ("%d %B %Y","%d %b %Y","%B %d %Y","%b %d %Y"):
            try: return datetime.strptime(m.group(0).replace(",",""),fmt).isoformat()
            except ValueError: pass
    return None

def event_type(text, fallback):
    t=clean(text).lower()
    for word, typ in [("conference","conference"),("summit","conference"),("workshop","workshop"),("masterclass","workshop"),("seminar","seminar"),("meetup","meetup"),("webinar","webinar")]:
        if word in t: return typ
    return fallback

def category(text, fallback):
    t=" "+clean(text).lower()+" "
    for c, words in {
        "AI & Machine Learning":[" ai ","artificial intelligence","machine learning","generative ai","genai"],
        "Cloud Computing":["cloud","aws","azure","google cloud","gcp"],
        "Cybersecurity":["cyber","security","zero trust","infosec"],
        "Digital Transformation":["digital transformation","automation","modernization"],
    }.items():
        if any(w in t for w in words): return c
    return fallback

def eventish(url,text):
    return bool(re.search(r"(event|webinar|conference|summit|seminar|workshop|register|registration|meetup|session)", f"{url} {text}", re.I))

def extract(html, final_url, source):
    soup=BeautifulSoup(html,"lxml")
    cards=soup.select("article, .event-item, .event, .event-card, [class*='event-card'], li")
    out=[]
    def add(card):
        title_node=card.select_one("h1,h2,h3,h4,.event-title,.title,[class*='title']")
        title=clean(title_node.get_text(" ",strip=True) if title_node else "")
        if not title: title=clean(card.get_text(" ",strip=True))[:180]
        date_node=card.select_one("time,.event-date,.date,[class*='date']")
        date_text=clean(date_node.get_text(" ",strip=True) if date_node else "")
        if date_node and date_node.get("datetime"): date_text += " "+date_node["datetime"]
        desc_node=card.select_one(".event-description,.description,[class*='description'],p")
        desc=clean(desc_node.get_text(" ",strip=True) if desc_node else "")
        event_url=reg=None
        for a in card.select("a[href]"):
            u=abs_url(a.get("href"),final_url); txt=clean(a.get_text(" ",strip=True))
            if not u: continue
            if not event_url and eventish(u,txt): event_url=u
            if not reg and re.search(r"register|registration|sign up|book",txt,re.I): reg=u
            if not event_url: event_url=u
        if not title or not event_url: return
        combined=f"{title} {desc} {date_text}"
        e={"source_id":source["id"],"source_name":source["name"],"title":title,
           "description":desc[:800],"date":parse_date(combined),"type":event_type(combined,source["type"]),
           "category":category(combined,source["category"]),
           "location":"Online" if re.search(r"\bonline|virtual|webinar\b",combined,re.I) else "",
           "event_url":event_url,"registration_url":reg or event_url,"source_url":final_url,
           "scraped_at":datetime.utcnow().isoformat()+"Z"}
        e["id"]=hashlib.sha1(json.dumps(e,sort_keys=True).encode()).hexdigest()
        out.append(e)
    for c in cards[:500]: add(c)
    if not out:
        seen=set()
        for a in soup.select("a[href]"):
            u=abs_url(a.get("href"),final_url); txt=clean(a.get_text(" ",strip=True))
            if not u or not txt or len(txt)<8 or len(txt)>240 or u in seen or not eventish(u,txt): continue
            seen.add(u)
            parent=clean(a.parent.get_text(" ",strip=True) if a.parent else txt)
            combined=f"{txt} {parent}"
            e={"source_id":source["id"],"source_name":source["name"],"title":txt,
               "description":parent[:800],"date":parse_date(parent),"type":event_type(combined,source["type"]),
               "category":category(combined,source["category"]),
               "location":"Online" if re.search(r"\bonline|virtual|webinar\b",combined,re.I) else "",
               "event_url":u,"registration_url":u,"source_url":final_url,
               "scraped_at":datetime.utcnow().isoformat()+"Z"}
            e["id"]=hashlib.sha1(json.dumps(e,sort_keys=True).encode()).hexdigest()
            out.append(e)
    return out

all_events=[]
results=[]
with sync_playwright() as p:
    browser=p.chromium.launch(headless=True)
    for s in SOURCES:
        try:
            page=browser.new_page(user_agent=UA,viewport={"width":1440,"height":1000})
            page.goto(s["url"],wait_until="domcontentloaded",timeout=60000)
            page.wait_for_timeout(2500)
            # Scroll once to trigger lazy-loaded event cards.
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            page.wait_for_timeout(1500)
            html=page.content(); final=page.url()
            items=extract(html,final,s)
            all_events.extend(items)
            results.append({"source":s["name"],"success":True,"count":len(items)})
            print(f"[OK] {s['name']}: {len(items)}")
            page.close()
        except Exception as ex:
            results.append({"source":s["name"],"success":False,"count":0,"error":str(ex)})
            print(f"[FAIL] {s['name']}: {ex}")
    browser.close()

seen=set(); dedup=[]
for e in all_events:
    k=(e["source_id"],clean(e["title"]).lower(),(e.get("date") or "")[:10],e.get("event_url"))
    if k not in seen: seen.add(k); dedup.append(e)
dedup.sort(key=lambda e:e.get("date") or "9999-12-31")
payload={"generated_at":datetime.utcnow().isoformat()+"Z","events":dedup,"results":results}
OUT.write_text(json.dumps(payload,ensure_ascii=False,indent=2),encoding="utf-8")
print(f"Saved {len(dedup)} events.")
