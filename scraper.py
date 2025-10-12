# scraper.py
"""
Generator statycznego RSS dla epiotrkow.pl hostowany na GitHub Pages.
Uruchamiany co godzinę przez GitHub Actions.

Kolejność pozyskania treści:
  JSON-LD → AMP (rel + heurystyki) → klasyczne akapity → /galeria → trafilatura
Do opisu <description> wstrzykujemy <img> (miniaturę) + lead (do ~800 znaków).
"""

import re
import sys
import time
import json
import html
import hashlib
from datetime import datetime
from typing import Optional, List, Tuple, Dict
from urllib.parse import urljoin, urlparse, urlunparse, parse_qsl, urlencode

import requests
from bs4 import BeautifulSoup
import trafilatura

SITE = "https://epiotrkow.pl"

# Strony: p1 = /news/, p2..p20 = /news/wydarzenia-pX
SOURCE_URLS = [f"{SITE}/news/"] + [f"{SITE}/news/wydarzenia-p{i}" for i in range(2, 21)]

FEED_TITLE = "epiotrkow.pl – Wydarzenia v3"
FEED_LINK  = f"{SITE}/news/"
FEED_DESC  = "Automatyczny RSS z list newsów epiotrkow.pl."

ARTICLE_LINK_SELECTORS: List[str] = [
    ".tn-img a[href^='/news/']",
    ".bg-white a[href^='/news/']",
    "a[href^='/news/']",
]

ID_LINK = re.compile(r"^/news/.+,\d+$")

HEADERS: Dict[str, str] = {
    "User-Agent": "Mozilla/5.0 (+https://github.com/) RSS static builder",
    "Accept-Language": "pl-PL,pl;q=0.9,en;q=0.8"
}

MAX_ITEMS = 500
DETAIL_LIMIT = 500          # ile artykułów wzbogacamy o datę/lead
LEAD_MAX_CHARS = 1000        # docelowa długość leada
LEAD_MIN_GOOD = 250         # minimalna długość, by uznać lead za „wystarczający”

# --- pomocnicze ---

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

def find_image_url(a: BeautifulSoup, site_base: str) -> Optional[str]:
    # 1) w tym samym <a>
    img = a.find("img")
    if img:
        src = img.get("data-src") or img.get("src")
        if src and not src.startswith("data:"):
            return urljoin(site_base, src)
    # 2) w rodzicach
    parent = a
    for _ in range(4):
        parent = parent.parent  # type: ignore
        if not parent:
            break
        img = parent.find("img")
        if img:
            src = img.get("data-src") or img.get("src")
            if src and not src.startswith("data:"):
                return urljoin(site_base, src)
    # 3) najbliższy następny <img>
    sib_img = a.find_next("img")
    if sib_img:
        src = sib_img.get("data-src") or sib_img.get("src")
        if src and not src.startswith("data:"):
            return urljoin(site_base, src)
    return None

PL_MONTHS = {
    "stycznia": 1, "lutego": 2, "marca": 3, "kwietnia": 4, "maja": 5, "czerwca": 6,
    "lipca": 7, "sierpnia": 8, "września": 9, "wrzesnia": 9, "października": 10,
    "pazdziernika": 10, "listopada": 11, "grudnia": 12
}

def to_rfc2822(dt: datetime) -> str:
    return dt.strftime("%a, %d %b %Y %H:%M:%S +0000")

def parse_polish_date(text: str) -> Optional[str]:
    if not text:
        return None
    m = re.search(r"(\d{1,2})\s+([A-Za-ząćęłńóśźżĄĆĘŁŃÓŚŹŻ]+)\s+(\d{4})", text, re.IGNORECASE)
    if not m:
        return None
    day = int(m.group(1)); month_name = m.group(2).lower(); year = int(m.group(3))
    month = PL_MONTHS.get(month_name)
    if not month:
        return None
    try:
        dt = datetime(year, month, day, 12, 0, 0)
        return to_rfc2822(dt)
    except Exception:
        return None

LEAD_SELECTORS: List[str] = [
    "[itemprop='articleBody'] p",
    ".news-body p",
    ".news-content p",
    ".article-body p",
    ".article-content p",
    ".entry-content p",
    "article .content p",
    "article p",
    ".post-content p",
    ".post-text p",
    ".content p",
]

def build_lead_from_paras(soup: BeautifulSoup, max_chars: int = LEAD_MAX_CHARS) -> Optional[str]:
    paras: List = []
    for sel in LEAD_SELECTORS:
        found = soup.select(sel)
        if found:
            paras = found
            break
    if not paras:
        paras = soup.select("main p") or soup.find_all("p")

    chunks: List[str] = []
    total = 0
    for p in paras:
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
            # data
            dp = obj.get("datePublished") or obj.get("dateCreated")
            if dp and not pub_rfc:
                try:
                    if isinstance(dp, str) and dp.endswith("Z"):
                        dt = datetime.fromisoformat(dp.replace("Z", "+00:00"))
                        pub_rfc = to_rfc2822(dt.astimezone(tz=None).replace(tzinfo=None))
                    elif isinstance(dp, str):
                        dt = datetime.fromisoformat(dp)
                        pub_rfc = to_rfc2822(dt.replace(tzinfo=None))
                except Exception:
                    pass
            # opis/treść
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

def try_amp_variants(url: str) -> List[str]:
    """Wygeneruj kandydatów AMP, nawet gdy brak rel=amphtml."""
    cand = set()
    def with_query(u, kv):
        pr = urlparse(u)
        q = dict(parse_qsl(pr.query))
        q.update(kv)
        return urlunparse(pr._replace(query=urlencode(q)))
    cand.add(with_query(url, {"amp": ""}))
    cand.add(with_query(url, {"amp": "1"}))
    cand.add(with_query(url, {"output": "amp"}))
    pr = urlparse(url)
    if not pr.path.endswith("/amp"):
        cand.add(urlunparse(pr._replace(path=pr.path.rstrip("/") + "/amp")))
    return [c for c in cand if c != url]

def try_gallery_variant(url: str) -> Optional[str]:
    """Dla /news/slug,ID -> /galeria/slug,ID"""
    try:
        if "/news/" not in url or "," not in url:
            return None
        before, after = url.split("/news/", 1)
        return before + "/galeria/" + after
    except Exception:
        return None

# --- główne pobranie szczegółów ---

def fetch_article_details(url: str) -> Tuple[Optional[str], Optional[str]]:
    """Zwraca (pubDate_rfc2822, lead_txt) z podstrony artykułu."""
    def _get(url_: str) -> BeautifulSoup:
        r = requests.get(url_, headers=HEADERS, timeout=25)
        r.raise_for_status()
        return BeautifulSoup(r.text, "lxml")

    pub_rfc: Optional[str] = None
    lead: Optional[str] = None

    # 0) Strona podstawowa
    try:
        soup = _get(url)
    except Exception as e:
        print(f"[WARN] Nie udało się pobrać artykułu: {url} -> {e}", file=sys.stderr)
        return None, None

    # 1) JSON-LD
    j_pub, j_lead = extract_from_jsonld(soup)
    if j_pub:
        pub_rfc = j_pub
    if j_lead:
        lead = j_lead

    # 2) AMP (link rel oraz heurystyki)
    amp_hrefs: List[str] = []
    amp_tag = soup.find("link", rel=lambda v: v and "amphtml" in str(v).lower())
    if amp_tag and amp_tag.get("href"):
        amp_hrefs.append(urljoin(url, amp_tag["href"]))
    amp_hrefs.extend(try_amp_variants(url))

    for amp_url in amp_hrefs:
        if lead and pub_rfc and len(lead) >= LEAD_MIN_GOOD:
            break
        try:
            amp_soup = _get(amp_url)
        except Exception as e:
            print(f"[WARN] AMP fetch failed: {amp_url} -> {e}", file=sys.stderr)
            continue
        if not pub_rfc:
            a_pub, _ = extract_from_jsonld(amp_soup)
            if a_pub:
                pub_rfc = a_pub
        if not lead or len(lead) < LEAD_MIN_GOOD:
            a_lead = build_lead_from_paras(amp_soup, max_chars=LEAD_MAX_CHARS)
            if a_lead and len(a_lead) >= LEAD_MIN_GOOD:
                lead = a_lead

    # 3) Klasyczny HTML – meta + akapity
    if not pub_rfc:
        meta = soup.find("meta", attrs={"property": "article:published_time"}) \
            or soup.find("meta", attrs={"name": "article:published_time"}) \
            or soup.find("meta", attrs={"itemprop": "datePublished"}) \
            or soup.find("meta", attrs={"name": "date"})
        if meta and meta.get("content"):
            iso = meta["content"].strip()
            try:
                if isinstance(iso, str) and iso.endswith("Z"):
                    dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
                    pub_rfc = to_rfc2822(dt.astimezone(tz=None).replace(tzinfo=None))
                elif isinstance(iso, str):
                    dt = datetime.fromisoformat(iso)
                    pub_rfc = to_rfc2822(dt.replace(tzinfo=None))
            except Exception:
                pass
        if not pub_rfc:
            date_el = soup.select_one(".news-date") or soup.find("time")
            if date_el:
                pub_rfc = parse_polish_date(date_el.get_text(" ", strip=True))

    if not lead or len(lead) < LEAD_MIN_GOOD:
        built = build_lead_from_paras(soup, max_chars=LEAD_MAX_CHARS)
        if built and len(built) >= LEAD_MIN_GOOD:
            lead = built

    # 4) GALERIA (często przy artykułach z „ZDJĘCIA”)
    if not lead or len(lead) < LEAD_MIN_GOOD:
        gal_url = try_gallery_variant(url)
        if gal_url:
            try:
                gal_soup = _get(gal_url)
                if not pub_rfc:
                    g_pub, _ = extract_from_jsonld(gal_soup)
                    if g_pub:
                        pub_rfc = g_pub
                g_lead = build_lead_from_paras(gal_soup, max_chars=LEAD_MAX_CHARS)
                if g_lead and len(g_lead) >= LEAD_MIN_GOOD:
                    lead = g_lead
            except Exception as e:
                print(f"[WARN] gallery fetch failed: {gal_url} -> {e}", file=sys.stderr)

    # 5) TRAFILATURA (ostatnia deska ratunku)
    if not lead or len(lead) < LEAD_MIN_GOOD:
        t_lead = trafilatura_lead(url, max_chars=LEAD_MAX_CHARS)
        if t_lead and len(t_lead) >= LEAD_MIN_GOOD:
            lead = t_lead

    # czyszczenie
    if lead:
        lead = " ".join(lead.split())
        if len(lead) < 80 and not re.search(r"[.!?…]$", lead):
            lead = None

    return pub_rfc, lead

# --- pobranie listy i budowa RSS ---

def fetch_items():
    items = []
    for url in SOURCE_URLS:
        try:
            r = requests.get(url, headers=HEADERS, timeout=25)
            r.raise_for_status()
        except Exception as e:
            print(f"[WARN] Nie udało się pobrać listy: {url} -> {e}", file=sys.stderr)
            continue

        soup = BeautifulSoup(r.text, "lxml")

        anchors = []
        for sel in ARTICLE_LINK_SELECTORS:
            anchors.extend(soup.select(sel))

        seen_href = set()
        clean = []
        for a in anchors:
            href = a.get("href")
            if not href or href in seen_href:
                continue
            seen_href.add(href)
            clean.append(a)

        for a in clean:
            href = a.get("href")
            if not ID_LINK.match(href):
                continue
            link = urljoin(SITE, href)

            # tytuł
            title_el = a.select_one(".tn-title")
            if title_el:
                title = title_el.get_text(" ", strip=True)
            else:
                h5 = a.select_one("h5.tn-title")
                title = h5.get_text(" ", strip=True) if h5 else ""
            if not title:
                title = a.get_text(" ", strip=True)
            if not title:
                sibling = a.find_next(class_="tn-title")
                if sibling:
                    title = sibling.get_text(" ", strip=True)
            if not title:
                img_in_a = a.find("img")
                if img_in_a and img_in_a.get("alt"):
                    title = img_in_a["alt"].strip()
            if not title:
                title = "Bez tytułu"

            img_url = find_image_url(a, SITE)
            mime = guess_mime(img_url) if img_url else None

            guid = hashlib.sha1(link.encode("utf-8")).hexdigest()
            items.append({
                "title": html.unescape(title),
                "link": link,
                "guid": guid,
                "image": img_url,
                "mime": mime
            })

    # deduplikacja po linku
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
