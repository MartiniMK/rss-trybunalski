# epiotrkow-rss (fixed v2)

Statyczny kanał RSS generowany z list newsów epiotrkow.pl.

- Zbiera artykuły z `/news/` oraz `/news/wydarzenia-p2 … p20`.
- Tytuły pobierane z `.tn-title`, `<h5.tn-title>`, alt obrazka itd.
- Workflow w `.github/workflows/rss.yml` uruchamia `scraper.py` co godzinę (UTC).
- Wynik to `feed.xml` gotowy do publikacji na GitHub Pages.

Adres RSS po włączeniu Pages:
