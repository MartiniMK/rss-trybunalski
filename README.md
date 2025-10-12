# 📰 RSS – Trybunalski.pl

Automatycznie generowany kanał RSS dla portalu [**Trybunalski.pl**](https://trybunalski.pl)  
z kategorii **Wiadomości** (`/k/wiadomosci`).

---

## 🔧 Działanie

- Skrypt (`scraper.py`) pobiera artykuły z **pierwszych 20 stron** kategorii.
- Dla każdego newsa zapisuje:
  - tytuł  
  - link  
  - datę publikacji  
  - miniaturę  
  - lead (pierwszy akapit)
- Wynik zapisywany jest do `feed.xml`.

---

## ⏰ Automatyczne aktualizacje

GitHub Actions uruchamia scraper **co godzinę**  
i automatycznie publikuje zaktualizowany plik RSS.

---

## 🌐 Gotowy kanał RSS

Po włączeniu GitHub Pages (Settings → Pages → Branch `main` / `/root`):

📎 **Adres RSS:**  
