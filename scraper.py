# scraper.py — Trybunalski.pl RSS
# Repo: rss-trybunalski
# Zaleznosci: requests, beautifulsoup4, lxml, python-dateutil

import os
import re
import sys
import html
import time
import hashlib
import logging
from datetime import datetime, timezone
from email.utils import format_datetime

import requests
from bs4 import BeautifulSoup
from dateutil import parser as dateparser

# ───────────────── CONFIG ─────────────────

BASE_URL = "https://trybunalski.pl"
CATEGORY_URL = "https://trybunalski.pl/k/wiadomosci"

MAX_PAGES = int(os.getenv("MAX_PAGES", "20"))          # ile stron listingu odwiedzić
MAX_ITEMS = int(os.getenv("MAX_ITEMS", "500"))         # maksymalna liczba itemów w RSS
MAX_LEAD_LEN = int(os.getenv("MAX_LEAD_LEN", "500"))   # maks. długość leadu (znaki)

OUTPUT_FILE = "feed.xml"

# Wersjonowanie feedu – ułatwia „odświeżenie” w czytnikach:
FEED_GUID_SALT = os.getenv("FEED_GUID_SALT", "trybunalski-v4")
FEED_SELF_URL = os.getenv(
    "FEED_SELF_URL",
    "https://martinimk.github.io/rss-trybunalski/feed.xml?v=4"
)

CHANNEL_TITLE = "Trybunalski.pl – Wiadomości"
CHANNEL_LINK = CATEGORY_URL
CHANNEL_DESC = "Automatyczny RSS z kategorii Wiadomości portalu Trybunalski.pl."
CHANNEL_LANG = "pl-PL"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; rss-trybunalski/1.0; +https://github.com/)",
    "Accept-Language": "pl-PL,pl;q=0.9,en-US;q=0.8,en;q=0.7",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Referer": CATEGORY_URL,
}

REQ_TIMEOUT = (12, 25)

# ───────────────── LOGGING ─────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("rss-trybunalski")

# ───────────────── HTTP ─────────────────

session = requests.Session()
session.headers.update(HEADERS)

def get(url):
    for attempt in range(3):
        try:
            r = session.get(url, timeout=REQ_TIMEOUT, allow_redirects=True)
            if r.status_code == 200 and r.text:
                return r
            log.warning("GET %s -> %s", url, r.status_code)
        except requests.RequestException as e:
            log.warning("GET %s failed (%s) attempt %d", url, e, attempt + 1)
        time.sleep(0.8 + attempt * 0.7)
    return None

# ───────────────── HELPERS ─────────────────

def absolutize(href: str) -> str:
    if not href:
        return ""
    if href.startswith("http://") or href.startswith("https://"):
        return href
    if href.startswith("/"):
        return BASE_URL + href
    return BASE_URL + "/" + href.lstrip("./")

def clean_text(s: str) -> str:
    if not s:
        return ""
    s = html.unescape(s)
    s = re.sub(r"\s+", " ", s).strip()
    return s

def truncate(s: str, limit: int) -> str:
    s = (s or "").strip()
    if len(s) <= limit:
        return s
    return s[:limit].rstrip() + "…"

# ───────────────── LISTING (BARDZO ODPORNY) ─────────────────
# Zamiast polegać na strukturze DOM, wyciągamy URL-e regexami z całego HTML:
# 1) href="https://trybunalski.pl/wiadomosci/..."
# 2) href="/wiadomosci/..."
# 3) surowe wystąpienia https://trybunalski.pl/wiadomosci/...
# 4) surowe "/wiadomosci/..." (np. w JSON-ie Nuxta)

RE_HREF_ABS = re.compile(r'href=["\'](https?://trybunalski\.pl/wiadomosci/[^\s"\'<>]+)["\']', re.IGNORECASE)
RE_HREF_REL = re.compile(r'href=["\'](/wiadomosci/[^\s"\'<>]+)["\']', re.IGNORECASE)
RE_ABS = re.compile(r'https?://trybunalski\.pl/wiadomosci/[^\s"\'<>]+', re.IGNORECASE)
RE_REL = re.compile(r'["\'](/wiadomosci/[^\s"\'<>]+)["\']', re.IGNORECASE)

def parse_listing_raw(html_text: str):
    links = []
    seen = set()

    def add(u: str):
        if not u:
            return
        u2 = absolutize(u)
        if u2 not in seen:
            seen.add(u2)
            links.append(u2)

    for m in RE_HREF_ABS.finditer(html_text):
        add(m.group(1))

    for m in RE_HREF_REL.finditer(html_text):
        add(m.group(1))

    # jeśli nadal mało linków, przeszukaj cały tekst (np. inline JSON)
    if len(links) < 10:
        for m in RE_ABS.finditer(html_text):
            add(m.group(0))
        for m in RE_REL.finditer(html_text):
            add(m.group(1))

    return links

# ───────────────── ARTICLE PARSE ─────────────────

DATE_META_SELECTORS = [
    'meta[property="article:published_time"]',
    'meta[name="article:published_time"]',
    'meta[name="pubdate"]',
    'meta[itemprop="datePublished"]',
    'meta[name="date"]',
]

def extract_article(resp_text: str, url: str):
    soup = BeautifulSoup(resp_text, "lxml")

    # tytuł
    title = None
    ogt = soup.select_one('meta[property="og:title"]')
    if ogt and ogt.get("content"):
        title = ogt["content"].strip()
    if not title:
        h1 = soup.find("h1")
        if h1:
            title = clean_text(h1.get_text(" "))
    if not title:
        title = clean_text(url.rstrip("/").split("/")[-2].replace("-", " "))

    # miniatura
    image = None
    ogimg = soup.select_one('meta[property="og:image"]')
    if ogimg and ogimg.get("content"):
        image = absolutize(ogimg["content"])

    # lead: pierwszy sensowny <p> z treści
    lead = None
    for sel in [".article__content", ".article-content", "article"]:
        blk = soup.select_one(sel)
        if blk:
            p = blk.find("p")
            if p:
                lead = clean_text(p.get_text(" "))
                break
    if not lead:
        md = soup.select_one('meta[name="description"]')
        if md and md.get("content"):
            lead = clean_text(md["content"])
    if not lead:
        for p in soup.find_all("p"):
            txt = clean_text(p.get_text(" "))
            if len(txt) >= 80:
                lead = txt
                break
    if not lead:
        lead = title
    lead = truncate(lead, MAX_LEAD_LEN)

    # data publikacji
    pub_dt = None
    for sel in DATE_META_SELECTORS:
        el = soup.select_one(sel)
        if el and el.get("content"):
            try:
                pub_dt = dateparser.parse(el["content"])
                break
            except Exception:
                pass

    if not pub_dt:
        # fallback: „Opublikowano: ...”
        txt = soup.get_text(" ", strip=True)
        m = re.search(r"Opublikowano:\s*([^|]+?)\s*(Aktualizacja:|Autor:|$)", txt, re.IGNORECASE)
        if m:
            frag = m.group(1)
            try:
                pub_dt = dateparser.parse(frag, dayfirst=True, fuzzy=True, languages=["pl"])
            except Exception:
                pub_dt = None

    if not pub_dt:
        pub_dt = datetime.now(timezone.utc)
    else:
        if pub_dt.tzinfo is None:
            pub_dt = pub_dt.replace(tzinfo=timezone.utc)
        else:
            pub_dt = pub_dt.astimezone(timezone.utc)

    return {
        "title": clean_text(title),
        "link": url,
        "image": image,
        "lead": lead,
        "pubdate": pub_dt,
    }

# ───────────────── BUILD RSS ─────────────────

def build_rss(items):
    now_utc = datetime.now(timezone.utc)
    last_build = format_datetime(now_utc)

    out = []
    out.append(
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<rss version="2.0"\n'
        '     xmlns:media="http://search.yahoo.com/mrss/"\n'
        '     xmlns:atom="http://www.w3.org/2005/Atom">\n'
        "<channel>\n"
        f"<title>{html.escape(CHANNEL_TITLE)}</title>\n"
        f"<link>{html.escape(CHANNEL_LINK)}</link>\n"
        f"<description>{html.escape(CHANNEL_DESC)}</description>\n"
        f'<atom:link rel="self" type="application/rss+xml" href="{html.escape(FEED_SELF_URL)}"/>\n'
        f"<language>{CHANNEL_LANG}</language>\n"
        f"<lastBuildDate>{last_build}</lastBuildDate>\n"
        "<ttl>60</ttl>\n"
    )

    for it in items[:MAX_ITEMS]:
        guid = hashlib.sha1((it["link"] + "|" + FEED_GUID_SALT).encode("utf-8")).hexdigest()
        pubdate = format_datetime(it["pubdate"])

        # czysty tekst w <description> (czytniki jak Inoreader lepiej to trawią)
        safe_lead = clean_text(html.unescape(it.get("lead") or it["title"]))
        safe_lead = truncate(safe_lead, MAX_LEAD_LEN)
        description = f"<![CDATA[{safe_lead}]]>"

        enclosure = media = media_thumb = ""
        if it.get("image"):
            img = html.escape(it["image"])
            enclosure = f'\n<enclosure url="{img}" type="image/*"/>'
            media = f'\n<media:content url="{img}" medium="image"/>'
            media_thumb = f'\n<media:thumbnail url="{img}"/>'

        out.append(
            "<item>\n"
            "<title>\n"
            f"<![CDATA[ {it['title']} ]]>\n"
            "</title>\n"
            f"<link>{html.escape(it['link'])}</link>\n"
            f'<guid isPermaLink="false">{guid}</guid>\n'
            f"<pubDate>{pubdate}</pubDate>\n"
            f"<description>\n{description}\n</description>{enclosure}{media}{media_thumb}\n"
            "</item>\n"
        )

    out.append("</channel>\n</rss>\n")
    return "".join(out)

# ───────────────── MAIN ─────────────────

def main():
    all_links = []
    seen = set()

    for page in range(1, MAX_PAGES + 1):
        url = CATEGORY_URL if page == 1 else f"{CATEGORY_URL}?page={page}"
        log.info("Listing %d -> %s", page, url)
        resp = get(url)
        if not resp:
            log.warning("No response for page %d", page)
            continue

        links = parse_listing_raw(resp.text)
        log.info("Found %d candidates on page %d", len(links), page)

        for u in links:
            if u not in seen:
                seen.add(u)
                all_links.append(u)

        if len(all_links) >= MAX_ITEMS:
            break
        time.sleep(0.4)

    log.info("Collected %d unique article URLs", len(all_links))

    items = []
    for i, url in enumerate(all_links[:MAX_ITEMS], 1):
        r = get(url)
        if not r:
            log.warning("Skip (no response): %s", url)
            continue
        try:
            it = extract_article(r.text, url)
            items.append(it)
        except Exception as e:
            log.exception("Parse failed for %s: %s", url, e)
        time.sleep(0.3)

    items.sort(key=lambda x: x["pubdate"], reverse=True)

    rss = build_rss(items)
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        f.write(rss)
    log.info("Wrote %s (%d items)", OUTPUT_FILE, len(items))

if __name__ == "__main__":
    main()
