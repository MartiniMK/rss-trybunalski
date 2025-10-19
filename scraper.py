# scraper.py
# RSS generator for https://trybunalski.pl/k/wiadomosci
# Works on GitHub Actions (Python 3.8+). Dependencies: requests, beautifulsoup4, lxml, python-dateutil

import os
import re
import sys
import html
import json
import time
import hashlib
import logging
from datetime import datetime, timezone
from email.utils import format_datetime

import requests
from bs4 import BeautifulSoup
from dateutil import parser as dateparser

# -------------------- CONFIG --------------------

BASE_URL = "https://trybunalski.pl"
CATEGORY_URL = "https://trybunalski.pl/k/wiadomosci"

MAX_PAGES = int(os.getenv("MAX_PAGES", "20"))          # ile stron kategorii zeskrobać
MAX_ITEMS = int(os.getenv("MAX_ITEMS", "500"))         # maks. elementów w RSS
MAX_LEAD_LEN = int(os.getenv("MAX_LEAD_LEN", "500"))   # maks. długość leadu w opisie

OUTPUT_FILE = "feed.xml"

# Wersjonowanie feedu (wymuszenie „odświeżenia” w czytnikach):
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

REQ_TIMEOUT = (12, 25)  # (connect, read)

# -------------------- LOGGING --------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("rss-trybunalski")

# -------------------- HTTP --------------------

session = requests.Session()
session.headers.update(HEADERS)

def get(url):
    for attempt in range(3):
        try:
            resp = session.get(url, timeout=REQ_TIMEOUT, allow_redirects=True)
            if resp.status_code == 200 and resp.text:
                return resp
            log.warning("GET %s -> %s", url, resp.status_code)
        except requests.RequestException as e:
            log.warning("GET %s failed (%s) attempt %d", url, e, attempt + 1)
        time.sleep(0.9 + attempt * 0.7)
    return None

# -------------------- HELPERS --------------------

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

# -------------------- LISTING PARSE --------------------

A_HREF_NEWS = re.compile(r"^https?://trybunalski\.pl/wiadomosci/|^/wiadomosci/")

# Regexy fallbackowe (HTML/JSON):
RE_ABS = re.compile(r'https?://trybunalski\.pl/wiadomosci/[^\s"<>]+')
RE_REL = re.compile(r'"/wiadomosci/[^"\s<>]+"' )
RE_REL_SINGLE = re.compile(r"'/wiadomosci/[^'\s<>]+'")

def parse_listing(page_html: str):
    soup = BeautifulSoup(page_html, "lxml")
    links = set()

    # 1) Klasyczne <a href=...> z atrybutem
    for a in soup.select("a[href]"):
        href = a.get("href", "")
        if A_HREF_NEWS.search(href):
            links.add(absolutize(href))

    # 2) Jeśli nic lub bardzo mało – fallback: regex po surowym HTML (wariant SSR/Nuxt)
    if len(links) < 6:
        abs_urls = RE_ABS.findall(page_html) or []
        for u in abs_urls:
            links.add(u)

        rel_urls = RE_REL.findall(page_html) or []
        for m in rel_urls:
            u = m.strip('"')
            links.add(absolutize(u))

        rel_urls2 = RE_REL_SINGLE.findall(page_html) or []
        for m in rel_urls2:
            u = m.strip("'")
            links.add(absolutize(u))

    # 3) Fallback: spróbuj wyczytać JSON z Nuxt (__NUXT_DATA__)
    if len(links) < 6:
        nuxt_tag = soup.find("script", id="__NUXT_DATA__", attrs={"type": "application/json"})
        if nuxt_tag and nuxt_tag.string:
            try:
                data = json.loads(nuxt_tag.string)
                # Rzut oka na strukturę: lecimy po stringach i wyciągamy ścieżki /wiadomosci/...
                def walk(obj):
                    if isinstance(obj, dict):
                        for v in obj.values():
                            yield from walk(v)
                    elif isinstance(obj, list):
                        for v in obj:
                            yield from walk(v)
                    elif isinstance(obj, str):
                        yield obj
                for s in walk(data):
                    if isinstance(s, str) and "/wiadomosci/" in s:
                        # wytnij pełny segment URL jeśli jest sklejony
                        for part in re.findall(r"/wiadomosci/[A-Za-z0-9_\-/%\.]+", s):
                            links.add(absolutize(part))
            except Exception as e:
                log.warning("Failed to parse __NUXT_DATA__: %s", e)

    # Porządkuj i zwróć
    out = []
    seen = set()
    for u in links:
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out

# -------------------- ARTICLE PARSE --------------------

DATE_META_SELECTORS = [
    'meta[property="article:published_time"]',
    'meta[name="article:published_time"]',
    'meta[name="pubdate"]',
    'meta[itemprop="datePublished"]',
    'meta[name="date"]',
]

def extract_article(resp_text: str, url: str):
    soup = BeautifulSoup(resp_text, "lxml")

    # Tytuł
    title = None
    ogt = soup.select_one('meta[property="og:title"]')
    if ogt and ogt.get("content"):
        title = ogt["content"].strip()
    if not title:
        h1 = soup.find("h1")
        if h1:
            title = clean_text(h1.get_text(" "))
    if not title:
        title = clean_text(url.rstrip("/").split("/")[-1].replace("-", " "))

    # Miniatura
    image = None
    ogimg = soup.select_one('meta[property="og:image"]')
    if ogimg and ogimg.get("content"):
        image = absolutize(ogimg["content"])

    # Lead – pierwszy sensowny <p> w treści
    lead = None
    article_blocks = []
    article_blocks += soup.select(".article__content")
    article_blocks += soup.select(".article-content")
    article_blocks += soup.select("article")

    for blk in article_blocks:
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

    # Data publikacji
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
        txt = soup.get_text(" ", strip=True)
        m = re.search(r"Opublikowano:\s*([^|]+?)\s*(Aktualizacja:|Autor:|$)", txt, re.IGNORECASE)
        if m:
            date_fragment = m.group(1)
            try:
                pub_dt = dateparser.parse(date_fragment, dayfirst=True, fuzzy=True, languages=["pl"])
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

# -------------------- BUILD RSS --------------------

def build_rss(items):
    now_utc = datetime.now(timezone.utc)
    last_build = format_datetime(now_utc)

    parts = []
    parts.append(
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
        title = html.escape(it["title"])
        link = html.escape(it["link"])
        guid = hashlib.sha1((it["link"] + "|" + FEED_GUID_SALT).encode("utf-8")).hexdigest()
        pubdate = format_datetime(it["pubdate"])

        # Opis BEZ HTML (przyjazny dla Inoreadera)
        safe_lead = it.get("lead") or it["title"]
        safe_lead = clean_text(html.unescape(safe_lead))
        safe_lead = truncate(safe_lead, MAX_LEAD_LEN)
        description = f"<![CDATA[{safe_lead}]]>"

        # Media (miniatury)
        enclosure = media = media_thumb = ""
        image = it.get("image")
        if image:
            img_esc = html.escape(image)
            enclosure = f'\n<enclosure url="{img_esc}" type="image/*"/>'
            media = f'\n<media:content url="{img_esc}" medium="image"/>'
            media_thumb = f'\n<media:thumbnail url="{img_esc}"/>'

        parts.append(
            "<item>\n"
            "<title>\n"
            f"<![CDATA[ {it['title']} ]]>\n"
            "</title>\n"
            f"<link>{link}</link>\n"
            f'<guid isPermaLink="false">{guid}</guid>\n'
            f"<pubDate>{pubdate}</pubDate>\n"
            f"<description>\n{description}\n</description>{enclosure}{media}{media_thumb}\n"
            "</item>\n"
        )

    parts.append("</channel>\n</rss>\n")
    return "".join(parts)

# -------------------- MAIN --------------------

def main():
    all_links = []
    seen = set()

    for page in range(1, MAX_PAGES + 1):
        url = CATEGORY_URL if page == 1 else f"{CATEGORY_URL}?page={page}"
        log.info("Listing page %d -> %s", page, url)
        resp = get(url)
        if not resp:
            log.warning("Skipping page %d (no response)", page)
            continue

        links = parse_listing(resp.text)
        log.info("Found %d candidates on page %d", len(links), page)

        new_links = [u for u in links if u not in seen]
        for u in new_links:
            seen.add(u)
        all_links.extend(new_links)

        if len(all_links) >= MAX_ITEMS:
            break

        time.sleep(0.5)

    log.info("Collected %d unique article URLs total", len(all_links))

    items = []
    for i, url in enumerate(all_links[:MAX_ITEMS], 1):
        resp = get(url)
        if not resp:
            log.warning("Skip article (no response): %s", url)
            continue
        try:
            item = extract_article(resp.text, url)
            items.append(item)
        except Exception as e:
            log.exception("Parse failed for %s: %s", url, e)
        time.sleep(0.35)

    items.sort(key=lambda x: x["pubdate"], reverse=True)

    rss = build_rss(items)

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        f.write(rss)

    log.info("Wrote %s (%d items)", OUTPUT_FILE, len(items))

if __name__ == "__main__":
    main()
