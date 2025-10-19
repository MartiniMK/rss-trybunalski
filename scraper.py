import os
import re
import time
import hashlib
import logging
from datetime import datetime, timezone
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from email.utils import format_datetime

# -------------------- Konfiguracja --------------------
BASE = "https://trybunalski.pl"
LIST_URL = f"{BASE}/k/wiadomosci"
OUTPUT_FEED = "feed.xml"

MAX_PAGES = int(os.environ.get("MAX_PAGES", "20"))     # ile stron listy przelecieć
MAX_ITEMS = int(os.environ.get("MAX_ITEMS", "500"))    # limit łączny itemów w RSS
MAX_LEAD_LEN = int(os.environ.get("MAX_LEAD_LEN", "400"))  # przycięcie leadu

TIMEOUT = 20
RETRIES = 3
SLEEP_BETWEEN = 0.6

# proxy “reader” – fallback gdy 403/CF
PROXY_PREFIX = "https://r.jina.ai/http://"

# nagłówki, które wyglądają jak realna przeglądarka
DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/128.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "pl-PL,pl;q=0.9,en-US;q=0.8,en;q=0.7",
    "Connection": "keep-alive",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
    "Referer": BASE + "/",
}

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


# -------------------- Pomocnicze --------------------
def fetch(url: str, allow_proxy=True) -> str | None:
    """Pobierz HTML. Najpierw bezpośrednio; gdy 403/brak treści – użyj fallbacku r.jina.ai."""
    sess = requests.Session()
    for attempt in range(RETRIES):
        try:
            r = sess.get(url, headers=DEFAULT_HEADERS, timeout=TIMEOUT)
            if r.status_code == 200 and r.text and len(r.text) > 500:
                return r.text
            logging.warning("GET %s -> %s", url, r.status_code)
        except Exception as e:
            logging.warning("GET %s error: %s", url, e)
        time.sleep(0.7 + attempt * 0.7)

    if allow_proxy:
        # proxy działa tylko dla http/https – wytnij schemat i zbuduj adres proxy
        parsed = urlparse(url)
        proxied = PROXY_PREFIX + parsed.netloc + parsed.path
        if parsed.query:
            proxied += "?" + parsed.query
        logging.info("Trying proxy -> %s", proxied)

        for attempt in range(RETRIES):
            try:
                r = sess.get(proxied, headers=DEFAULT_HEADERS, timeout=TIMEOUT)
                if r.status_code == 200 and r.text and len(r.text) > 300:
                    return r.text
                logging.warning("PROXY %s -> %s", proxied, r.status_code)
            except Exception as e:
                logging.warning("PROXY %s error: %s", proxied, e)
            time.sleep(0.8 + attempt * 0.6)

    return None


def sha1(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8", errors="ignore")).hexdigest()


def clean_html(html: str) -> str:
    # proste sprzątanie (bezpieczne do <description>)
    html = re.sub(r"\s+", " ", html)
    return html.strip()


def build_pubdate(dt: datetime | None) -> str:
    if not dt:
        dt = datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return format_datetime(dt)


# -------------------- Parsowanie listy --------------------
def extract_listing_links(html: str) -> list[str]:
    """Z listy wyciągnij linki do artykułów. Działa zarówno na pełnym HTML, jak i na 'readerze'."""
    soup = BeautifulSoup(html, "html.parser")

    # 1) ogólne: każdy link, który wygląda jak artykuł pod /wiadomosci/
    links = set()
    for a in soup.select('a[href*="/wiadomosci/"]'):
        href = a.get("href") or ""
        # pomijamy podkategorie typu /wiadomosci/na-sygnale/ – ale artykuły też tak mają;
        # dlatego przepuszczamy wszystkie, byle nie /k/wiadomosci itp.
        if "/k/wiadomosci" in href:
            continue
        if href.startswith("mailto:") or href.startswith("javascript:"):
            continue
        links.add(urljoin(BASE, href))

    # 2) dodatkowo kafelki typu image/news-listing (gdyby #1 było za mało)
    for a in soup.select(".image-tile-overlay, .image-tile, .news-listing-item a"):
        href = a.get("href") or ""
        if "/wiadomosci/" in href:
            links.add(urljoin(BASE, href))

    return list(links)


# -------------------- Parsowanie artykułu --------------------
DATE_PAT = re.compile(
    r"Opublikowano:\s*([^<|]+?)(?:\s*Aktualizacja:|Autor:|$)", re.IGNORECASE
)

def parse_polish_datetime(text: str) -> datetime | None:
    """
    Próba zparsowania daty z tekstu 'Opublikowano: sobota, 18 paź 2025 12:28'.
    Jeśli się nie uda, zwraca None.
    """
    # usuń wielokrotne spacje
    t = re.sub(r"\s+", " ", text).strip()

    # mapy miesięcy (różne możliwe skróty)
    months = {
        "sty": 1, "stycz": 1,
        "lut": 2, "luty": 2,
        "mar": 3, "marz": 3,
        "kwi": 4,
        "maj": 5,
        "cze": 6, "czew": 6,
        "lip": 7,
        "sie": 8, "sier": 8,
        "wrz": 9, "wrze": 9,
        "paź": 10, "paz": 10, "paźdz": 10,
        "lis": 11, "list": 11,
        "gru": 12, "grud": 12,
    }

    # wyciągnij '18 paź 2025 12:28' lub '18 paz 2025 12:28'
    m = re.search(r"(\d{1,2})\s+([A-Za-ząćęłńóśźżĄĆĘŁŃÓŚŹŻ\.]+)\s+(\d{4})\s+(\d{1,2}):(\d{2})", t)
    if not m:
        return None

    day = int(m.group(1))
    mon_raw = m.group(2).lower().replace(".", "")
    year = int(m.group(3))
    hour = int(m.group(4))
    minute = int(m.group(5))

    # znormalizuj klucz miesiąca do skrótu 3–5 znaków
    key = mon_raw[:3]
    month = months.get(key)
    if month is None:
        # spróbuj pełniejszego klucza
        month = months.get(mon_raw[:4])
    if month is None:
        return None

    try:
        return datetime(year, month, day, hour, minute, tzinfo=timezone.utc)
    except Exception:
        return None


def parse_article(url: str) -> dict | None:
    html = fetch(url)
    if not html:
        return None

    soup = BeautifulSoup(html, "html.parser")

    # Tytuł – h1 albo meta og:title
    title = None
    h1 = soup.find("h1")
    if h1 and h1.get_text(strip=True):
        title = h1.get_text(strip=True)
    if not title:
        ogt = soup.find("meta", attrs={"property": "og:title"})
        if ogt and ogt.get("content"):
            title = ogt["content"].strip()

    # Obrazek – najpierw og:image, potem pierwszy <img>
    image = None
    ogi = soup.find("meta", attrs={"property": "og:image"})
    if ogi and ogi.get("content"):
        image = ogi["content"].strip()
        if image and image.startswith("//"):
            image = "https:" + image
    if not image:
        imgtag = soup.find("img")
        if imgtag and imgtag.get("src"):
            image = imgtag["src"]
            if image.startswith("//"):
                image = "https:" + image
            image = urljoin(BASE, image)

    # Data – znajdź tekst "Opublikowano: ..."
    pub_dt = None
    full_text = soup.get_text(" ", strip=True)
    dm = DATE_PAT.search(full_text)
    if dm:
        when_txt = dm.group(1)
        pub_dt = parse_polish_datetime(when_txt)

    # Lead – pierwszy sensowny <p> niebędący "Opublikowano"/"Autor"
    lead = None
    for p in soup.find_all("p"):
        txt = p.get_text(" ", strip=True)
        if not txt:
            continue
        if txt.lower().startswith("opublikowano:") or txt.lower().startswith("autor:") or txt.lower().startswith("aktualizacja:"):
            continue
        # pomijamy bardzo krótkie szumowe akapity
        if len(txt) < 30:
            continue
        lead = txt
        break

    # przycięcie leadu + enkapsulacja z obrazkiem
    if lead:
        if len(lead) > MAX_LEAD_LEN:
            lead = lead[:MAX_LEAD_LEN].rstrip() + "…"
        lead_html = f'<p>{lead}</p>'
    else:
        # fallback: tytuł jako lead (lepiej mieć coś)
        lead_html = f"<p>{title or ''}</p>"

    if image:
        lead_html = f'<p><img src="{image}" alt="miniatura"/></p>' + lead_html

    return {
        "url": url,
        "title": title or url,
        "image": image,
        "lead_html": clean_html(lead_html),
        "pub_dt": pub_dt,
    }


# -------------------- RSS --------------------
def write_rss(items: list[dict], self_url: str):
    last_build = datetime.now(timezone.utc)
    parts = []
    parts.append('<?xml version="1.0" encoding="UTF-8"?>')
    parts.append('<rss xmlns:media="http://search.yahoo.com/mrss/" xmlns:atom="http://www.w3.org/2005/Atom" version="2.0">')
    parts.append("<channel>")
    parts.append("<title>Trybunalski.pl – Wiadomości</title>")
    parts.append(f"<link>{LIST_URL}</link>")
    parts.append("<description>Automatyczny RSS z kategorii Wiadomości portalu Trybunalski.pl.</description>")
    parts.append(f'<atom:link rel="self" type="application/rss+xml" href="{self_url}"/>')
    parts.append("<language>pl-PL</language>")
    parts.append(f"<lastBuildDate>{build_pubdate(last_build)}</lastBuildDate>")
    parts.append("<ttl>60</ttl>")

    for it in items:
        guid = sha1(it["url"])
        pub = build_pubdate(it["pub_dt"])
        title = it["title"]
        link = it["url"]
        desc = it["lead_html"]
        img = it.get("image")

        parts.append("<item>")
        parts.append("<title><![CDATA[ " + title + " ]]></title>")
        parts.append(f"<link>{link}</link>")
        parts.append(f'<guid isPermaLink="false">{guid}</guid>')
        parts.append(f"<pubDate>{pub}</pubDate>")
        parts.append("<description><![CDATA[ " + desc + " ]]></description>")
        if img:
            parts.append(f'<enclosure url="{img}" type="image/*"/>')
            parts.append(f'<media:content url="{img}" medium="image"/>')
            parts.append(f'<media:thumbnail url="{img}"/>')
        parts.append("</item>")

    parts.append("</channel></rss>")
    xml = "\n".join(parts)

    with open(OUTPUT_FEED, "w", encoding="utf-8") as f:
        f.write(xml)

    logging.info("Wrote %s (%d items)", OUTPUT_FEED, len(items))


# -------------------- Główna pętla --------------------
def main():
    all_links: list[str] = []
    for page in range(1, MAX_PAGES + 1):
        url = LIST_URL if page == 1 else f"{LIST_URL}?page={page}"
        logging.info("Listing %s -> %s", page, url)
        html = fetch(url)
        if not html:
            logging.warning("No response for page %s", page)
            continue

        links = extract_listing_links(html)
        if not links:
            logging.warning("No links found on page %s", page)
        all_links.extend(links)
        time.sleep(SLEEP_BETWEEN)

    # unikalne i ograniczenie
    seen = set()
    uniq = []
    for u in all_links:
        if u not in seen:
            seen.add(u)
            uniq.append(u)
    logging.info("Collected %d unique article URLs", len(uniq))

    items: list[dict] = []
    for u in uniq:
        if len(items) >= MAX_ITEMS:
            break
        art = parse_article(u)
        if not art:
            continue
        items.append(art)
        time.sleep(SLEEP_BETWEEN)

    # sort: najnowsze na górze (po pub_dt, fallback na teraz)
    def sort_key(x):
        return x["pub_dt"] or datetime.now(timezone.utc)
    items.sort(key=sort_key, reverse=True)

    # self link z paramem wersji, żeby móc “odświeżać” w czytnikach
    self_url = "https://{user}.github.io/rss-trybunalski/feed.xml?v=4"
    # spróbuj wyczytać autora z GITHUB_REPOSITORY
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    if "/" in repo:
        user = repo.split("/")[0]
        self_url = f"https://{user}.github.io/rss-trybunalski/feed.xml?v=4"

    write_rss(items, self_url)


if __name__ == "__main__":
    main()
