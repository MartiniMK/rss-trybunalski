# scraper.py
"""
Statyczny RSS dla https://trybunalski.pl/k/wiadomosci (paginacja ?page=1..20).
Zachowuje: tytuł, link, pubDate, opis (miniatura + lead), enclosure/media.

Kolejność pozyskania leadu/daty:
  JSON-LD → klasyczne akapity → (fallback) trafilatura.
"""

import re
import sys
import time
import json
import html
import hashlib
from datetime import datetime
from typing import Optional, List, Tuple, Dict
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
import trafilatura

SITE = "https://trybunalski.pl"
CATEGORY = f"{SITE}/k/wiadomosci"

# Strony listy: p1..p20
SOURCE_URLS = [f"{CATEGORY}"] + [f"{CATEGORY}?page={i}" for i in range(2, 21)]

FEED_TITLE = "Trybunalski.pl – Wiadomości"
FEED_LINK  = CATEGORY
FEED_DESC  = "Automatyczny RSS z kategorii Wiadomości portalu Trybunalski.pl."

HEADERS: Dict[str, str] = {
    "User-Agent": "Mozilla/5.0 (+https://github.com/) RSS static builder",
    "Accept-Language": "pl-PL,pl;q=0.9,en;q=0.8"
}

MAX_ITEMS = 500
DETAIL_LIMIT = 500          # ile artykułów wzbogacamy o datę/lead
LEAD_MAX_CHARS = 1000
LEAD_MIN_GOOD = 250

# --- utils ---

def guess_mime(url: Optional[str]) -> str:
    if not url:
        return "image/*"
    u = url.lower()
    if u.endswith(".webp"):
        return "image/webp"
    if u.endswith(".png"):
        return "image/png"
    if u.endswith(".jpg") or u.endswith(".jpeg"):
        return "image/jpeg"
    return "image/*"

def extract_bg_image_from_style(style: Optional[str]) -> Optional[str]:
    if not style:
        return None
    # szukamy background-image:url(....)
    m = re.search(r'background-image\s*:\s*url\(([^)]+)\)', style, re.IGNORECASE)
    if not m:
        return None
    url = m.group(1).strip().strip('"\'')
    if url.startswith("data:"):
        return None
    return url

def find_image_url_for_card(anchor: BeautifulSoup) -> Optional[str]:
    """
    Na listach Trybunalski.pl obraz jest zwykle w elemencie z atrybutem `lazy-background`
    i background-image w style. Szukamy w przodkach/rodzeństwie.
    """
    # 1) w obrębie <a> poszukaj elementu z lazy-background
    for elem in anchor.select("[lazy-background]"):
        # najpierw style
        url = extract_bg_image_from_style(elem.get("style"))
        if not url:
            # czasem realny adres jest w samym lazy-background (pełny URL)
            url = elem.get("lazy-background")
        if url:
            return url

    # 2) w rodzicach kilka poziomów w górę
    parent = anchor
    for _ in range(4):
        parent = getattr(parent, "parent", None)
        if not parent:
            break
        lazy = parent.find(attrs={"lazy-background": True})
        if lazy:
            url = extract_bg_image_from_style(lazy.get("style")) or lazy.get("lazy-background")
            if url:
                return url

    # 3) rodzeństwo
    sib = anchor.find_next(attrs={"lazy-background": True})
    if sib:
        url = extract_bg_image_from_style(sib.get("style")) or sib.get("lazy-background")
        if url:
            return url

    return None

def clean_text(s: str) -> str:
    s = html.unescape(" ".join(s.split()))
    # usuń „(...)” na końcu listingu, bo to teaser
    s = re.sub(r"\s*\(\.\.\.\)\s*$", "", s)
    return s.strip()

def to_rfc2822(dt: datetime) -> str:
    return dt.strftime("%a, %d %b %Y %H:%M:%S +0000")

# --- LEAD z artykułu ---

LEAD_SELECTORS: List[str] = [
    "[itemprop='articleBody'] p",
    "article .content p",
    ".article-content p",
    ".entry-content p",
    ".post-content p",
    ".post-text p",
    ".news-body p",
    ".news-content p",
    "article p",
    "main p",
]

def build_lead_from_paras(soup: BeautifulSoup, max_chars: int = LEAD_MAX_CHARS) -> Optional[str]:
    paras = []
    for sel in LEAD_SELECTORS:
        found = soup.select(sel)
        if found:
            paras = found
            break
    if not paras:
        paras = soup.find_all("p")

    chunks: List[str] = []
    total = 0
    for p in paras:
        # pomijaj akapity będące reklamami/nawigacją
        if p.find_parent(class_=re.compile(r"(adPlacement|ads|advert|promo)")):
            continue
        t = p.get_text(" ", strip=True)
        t = html.unescape(t)
        if not t or len(t) < 30:
            continue
        chunks.append(t)
        total += len(t) + 1
        if total >= max_chars:
            break

    if not chunks:
        return None

    lead = " ".join(chunks)
    if len(lead) > max_chars:
        cut = lead[:max_chars]
        cut = cut.rsplit(" ", 1)[0] if " " in cut else cut
        lead = cut.rstrip() + "…"
    return lead

def extract_from_jsonld(soup: BeautifulSoup) -> Tuple[Optional[str], Optional[str]]:
    pub_rfc: Optional[str] = None
    lead: Optional[str] = None
    for tag in soup.find_all("script", type="application/ld+json"):
        raw = tag.string or (tag.contents[0] if tag.contents else "")
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except Exception:
            continue
        candidates = data if isinstance(data, list) else [data]
        for obj in candidates:
            if not isinstance(obj, dict):
                continue
            typ = obj.get("@type") or obj.get("type")
            if isinstance(typ, list):
                typ = next((t for t in typ if isinstance(t, str)), None)
            if not (isinstance(typ, str) and ("Article" in typ or "NewsArticle" in typ)):
                continue
            # data publikacji
            dp = obj.get("datePublished") or obj.get("dateCreated") or obj.get("uploadDate")
            if dp and not pub_rfc:
                try:
                    # ISO 8601 → RFC 2822
                    dt = datetime.fromisoformat(dp.replace("Z", "+00:00"))
                    pub_rfc = to_rfc2822(dt.replace(tzinfo=None))
                except Exception:
                    pass
            # lead: articleBody/description
            desc = obj.get("description")
            body = obj.get("articleBody")
            txt = (body or desc)
            if txt and not lead:
                clean = html.unescape(" ".join(str(txt).split()))
                if len(clean) >= 40:
                    lead = clean
        if pub_rfc or lead:
            break
    return pub_rfc, lead

def trafilatura_lead(url: str, max_chars: int = LEAD_MAX_CHARS) -> Optional[str]:
    try:
        downloaded = trafilatura.fetch_url(url)
        if not downloaded:
            return None
        text = trafilatura.extract(
            downloaded,
            include_comments=False,
            favor_recall=True,
            with_metadata=False
        )
        if not text:
            return None
        text = html.unescape(" ".join(text.split()))
        if len(text) > max_chars:
            cut = text[:max_chars]
            cut = cut.rsplit(" ", 1)[0] if " " in cut else cut
            text = cut.rstrip() + "…"
        return text if len(text) >= 120 else None
    except Exception as e:
        print(f"[WARN] trafilatura failed for {url}: {e}", file=sys.stderr)
        return None

def fetch_article_details(url: str) -> Tuple[Optional[str], Optional[str]]:
    """Zwraca (pubDate_rfc2822, lead_txt) z podstrony artykułu."""
    def _get(url_: str) -> BeautifulSoup:
        r = requests.get(url_, headers=HEADERS, timeout=25)
        r.raise_for_status()
        return BeautifulSoup(r.text, "lxml")

    pub_rfc: Optional[str] = None
    lead: Optional[str] = None

    try:
        soup = _get(url)
    except Exception as e:
        print(f"[WARN] article fetch failed {url}: {e}", file=sys.stderr)
        return None, None

    # 1) JSON-LD
    j_pub, j_lead = extract_from_jsonld(soup)
    if j_pub:
        pub_rfc = j_pub
    if j_lead:
        lead = j_lead

    # 2) Meta
    if not pub_rfc:
        meta = soup.find("meta", attrs={"property": "article:published_time"}) \
            or soup.find("meta", attrs={"name": "article:published_time"}) \
            or soup.find("meta", attrs={"itemprop": "datePublished"}) \
            or soup.find("meta", attrs={"name": "date"})
        if meta and meta.get("content"):
            iso = meta["content"].strip()
            try:
                dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
                pub_rfc = to_rfc2822(dt.replace(tzinfo=None))
            except Exception:
                pass

    # 3) Akapity
    if not lead or len(lead) < LEAD_MIN_GOOD:
        built = build_lead_from_paras(soup, max_chars=LEAD_MAX_CHARS)
        if built and len(built) >= LEAD_MIN_GOOD:
            lead = built

    # 4) Trafialtura jako ostatni fallback
    if not lead or len(lead) < LEAD_MIN_GOOD:
        t_lead = trafilatura_lead(url, max_chars=LEAD_MAX_CHARS)
        if t_lead and len(t_lead) >= LEAD_MIN_GOOD:
            lead = t_lead

    if lead:
        lead = " ".join(lead.split())
        if len(lead) < 80 and not re.search(r"[.!?…]$", lead):
            lead = None

    return pub_rfc, lead

# --- listy artykułów i RSS ---

# Linki do artykułów – różne layouty kart na liście:
ARTICLE_LINK_SELECTORS: List[str] = [
    # kafle overlay (duże i mobile)
    ".image-tile-overlay[href*='/wiadomosci/']",
    ".image-tile-overlay-mobile a[href*='/wiadomosci/']",
    # wiersze listy
    ".news-listing-item a[href*='/wiadomosci/']",
    # „Przeczytaj jeszcze”
    ".latest-news__wrapper a[href*='/wiadomosci/']",
    # bezpieczny fallback
    "a[href^='https://trybunalski.pl/wiadomosci/']",
]

def fetch_items():
    items = []
    for url in SOURCE_URLS:
        try:
            r = requests.get(url, headers=HEADERS, timeout=25)
            r.raise_for_status()
        except Exception as e:
            print(f"[WARN] list fetch failed {url}: {e}", file=sys.stderr)
            continue

        soup = BeautifulSoup(r.text, "lxml")

        anchors = []
        for sel in ARTICLE_LINK_SELECTORS:
            anchors.extend(soup.select(sel))

        # deduplikacja po href
        seen_href = set()
        clean = []
        for a in anchors:
            href = a.get("href")
            if not href:
                continue
            # pełne/relatywne
            if href.startswith("/"):
                href = urljoin(SITE, href)
            if not href.startswith("http"):
                href = urljoin(SITE, href)
            if href in seen_href:
                continue
            # tylko artykuły z /wiadomosci/
            if "/wiadomosci/" not in href:
                continue
            seen_href.add(href)
            clean.append((a, href))

        for a, link in clean:
            # Tytuł
            title_el = a.select_one(".image-tile-overlay__title") or a.select_one(".image-tile-overlay-mobile__title")
            if not title_el:
                # w wierszach listy tytuł jest w <p class="news-listing-item__text"><strong>...</strong>
                strong = a.select_one(".news-listing-item__text strong")
                if strong:
                    title_el = strong
            title = title_el.get_text(" ", strip=True) if title_el else a.get_text(" ", strip=True)
            title = clean_text(title) if title else "Bez tytułu"

            # Miniatura
            img_url = find_image_url_for_card(a)
            if img_url and img_url.startswith("//"):
                img_url = "https:" + img_url
            if img_url and img_url.startswith("/"):
                img_url = urljoin(SITE, img_url)
            mime = guess_mime(img_url) if img_url else None

            guid = hashlib.sha1(link.encode("utf-8")).hexdigest()
            items.append({
                "title": html.unescape(title),
                "link": link,
                "guid": guid,
                "image": img_url,
                "mime": mime
            })

    # deduplikacja po linku + ograniczenie
    seen, unique = set(), []
    for it in items:
        if it["link"] in seen:
            continue
        seen.add(it["link"])
        unique.append(it)

    # szczegóły
    for idx, it in enumerate(unique):
        if idx >= DETAIL_LIMIT:
            break
        pub, lead = fetch_article_details(it["link"])
        if pub:
            it["pubDate"] = pub
        if lead:
            it["lead"] = lead

    return unique[:MAX_ITEMS]

def rfc2822_now():
    return time.strftime("%a, %d %b %Y %H:%M:%S +0000", time.gmtime())

def build_rss(items):
    build_date = rfc2822_now()
    head = f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"
     xmlns:media="http://search.yahoo.com/mrss/">
<channel>
<title>{FEED_TITLE}</title>
<link>{FEED_LINK}</link>
<description>{FEED_DESC}</description>
<lastBuildDate>{build_date}</lastBuildDate>
<ttl>60</ttl>
"""
    body = []
    for it in items:
        pubdate = it.get("pubDate", build_date)
        lead_text = html.unescape(it.get("lead") or it["title"])
        img_html = f'<p><img src="{it["image"]}" alt="miniatura"/></p>' if it.get("image") else ""
        desc_html = f"{img_html}<p>{lead_text}</p>"

        enclosure = media = media_thumb = ""
        if it.get("image"):
            enclosure   = f'\n  <enclosure url="{it["image"]}" type="{it.get("mime","image/*")}" />'
            media       = f'\n  <media:content url="{it["image"]}" medium="image" />'
            media_thumb = f'\n  <media:thumbnail url="{it["image"]}" />'

        body.append(f"""
<item>
  <title><![CDATA[{it['title']}]]></title>
  <link>{it['link']}</link>
  <guid isPermaLink="false">{it['guid']}</guid>
  <pubDate>{pubdate}</pubDate>
  <description><![CDATA[{desc_html}]]></description>{enclosure}{media}{media_thumb}
</item>""")

    tail = "\n</channel>\n</rss>\n"
    return head + "".join(body) + tail

if __name__ == "__main__":
    items = fetch_items()
    rss = build_rss(items)
    with open("feed.xml", "w", encoding="utf-8") as f:
        f.write(rss)
    print(f"Generated feed.xml with {len(items)} items (images + miniatures + dates/leads for first {min(len(items), DETAIL_LIMIT)} items)")
