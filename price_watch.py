"""Surveillance directe des prix PS5 chez les marchands + alertes Telegram.

La liste des pages à surveiller est dans watchlist.json (aucun code à toucher).

Usage :
  python price_watch.py          # exécution normale (alertes + sauvegarde de l'état)
  python price_watch.py --test   # affiche, pour chaque site, le prix trouvé / bloqué / introuvable
  python price_watch.py --ping   # envoie un message de test sur Telegram et s'arrête

Règle respectée : aucun contournement des protections anti-bot. Si un site renvoie
une page de captcha ou un refus, il est noté « bloqué » et signalé, c'est tout.
"""
import html
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
from bs4 import BeautifulSoup

BASE = Path(__file__).parent
WATCHLIST = json.loads((BASE / "watchlist.json").read_text(encoding="utf-8"))
SETTINGS = WATCHLIST.get("settings", {})
STATE_FILE = BASE / "price_state.json"
TEST_MODE = "--test" in sys.argv
PING_MODE = "--ping" in sys.argv
NOW = datetime.now(timezone.utc).isoformat(timespec="seconds")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/131.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.5",
}
# Indices d'une page de blocage (captcha, défi anti-bot, accès refusé).
BLOCK_MARKERS = [
    "captcha-delivery.com", "cf-chl", "challenge-platform", "just a moment...",
    "px-captcha", "validatecaptcha", "api-services-support@amazon.com",
    "pardon our interruption", "access denied", "accès refusé", "attention required",
]
OUT_OF_STOCK = ("outofstock", "soldout", "discontinued")
ALT_HINT = ("alternative : alerte prix native du site, page Idealo du modèle, "
            "ou favori sur l'appli du marchand")


# ---------------------------------------------------------------- extraction
def to_price(value):
    """'1 099,99 €' / '599.99' / 599 -> 599.99 (float) ou None."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value).replace("\xa0", " ").replace(" ", " ")
    m = re.search(r"\d[\d .]*(?:,\d{1,2})?|\d+(?:\.\d{1,2})?", s)
    if not m:
        return None
    num = m.group(0).strip()
    if "," in num:                       # format français : 1 099,99
        num = num.replace(" ", "").replace(".", "").replace(",", ".")
    else:                                # 599.99 ou 1 099
        num = num.replace(" ", "")
        if num.count(".") > 1 or re.fullmatch(r"\d{1,3}\.\d{3}", num):
            num = num.replace(".", "")
    try:
        return float(num)
    except ValueError:
        return None


def walk_jsonld(node, found):
    if isinstance(node, list):
        for x in node:
            walk_jsonld(x, found)
        return
    if not isinstance(node, dict):
        return
    if "@graph" in node:
        walk_jsonld(node["@graph"], found)
    if "itemListElement" in node:
        walk_jsonld(node["itemListElement"], found)
    if "item" in node and isinstance(node["item"], dict):
        walk_jsonld(node["item"], found)
    offers = node.get("offers")
    if offers:
        for o in offers if isinstance(offers, list) else [offers]:
            if not isinstance(o, dict):
                continue
            spec = o.get("priceSpecification") or {}
            if isinstance(spec, list):
                spec = spec[0] if spec else {}
            price = to_price(o.get("lowPrice") or o.get("price") or spec.get("price"))
            avail = str(o.get("availability", "")).rsplit("/", 1)[-1].lower()
            if price is not None:
                found.append((price, avail))
            if o.get("offers"):            # AggregateOffer contenant des offres
                walk_jsonld(o, found)


def extract(page_html, site, smin, smax):
    """Renvoie (prix, disponible: True/False/None, méthode) ou (None, None, None)."""
    soup = BeautifulSoup(page_html, "html.parser")
    ok = lambda p: p is not None and smin <= p <= smax

    found = []
    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            walk_jsonld(json.loads(tag.string or tag.get_text() or "{}"), found)
        except (json.JSONDecodeError, TypeError):
            continue
    found = [(p, a) for p, a in found if ok(p)]
    if found:
        in_stock = [x for x in found if x[1] not in OUT_OF_STOCK]
        price, avail = min(in_stock or found)
        return price, (None if not avail else avail not in OUT_OF_STOCK), "json-ld"

    for sel in ('meta[property="product:price:amount"]', 'meta[property="og:price:amount"]',
                'meta[itemprop="price"]', '[itemprop="price"][content]'):
        el = soup.select_one(sel)
        if el and ok(to_price(el.get("content"))):
            return to_price(el.get("content")), None, "meta"

    css = site.get("css")
    if css:
        for el in soup.select(css):
            p = to_price(el.get("content") or el.get_text(" "))
            if ok(p):
                return p, None, "css"
    return None, None, None


def looks_blocked(status, page_html):
    if status in (401, 403, 429, 503):
        return True
    head = page_html[:60000].lower()
    return any(m in head for m in BLOCK_MARKERS)


# ------------------------------------------------------------------- réseau
_PW = {}


def fetch_requests(url):
    r = requests.get(url, headers=HEADERS, timeout=30, allow_redirects=True)
    r.encoding = r.encoding or "utf-8"
    return r.status_code, r.text


def fetch_playwright(url):
    """Navigateur Chromium sans interface, pour les pages affichées en JavaScript.
    Aucun mode furtif, aucun contournement : c'est un navigateur ordinaire."""
    if "browser" not in _PW:
        from playwright.sync_api import sync_playwright
        _PW["pw"] = sync_playwright().start()
        _PW["browser"] = _PW["pw"].chromium.launch()
        _PW["ctx"] = _PW["browser"].new_context(locale="fr-FR", user_agent=HEADERS["User-Agent"])
    page = _PW["ctx"].new_page()
    try:
        resp = page.goto(url, wait_until="domcontentloaded", timeout=45000)
        page.wait_for_timeout(5000)
        return (resp.status if resp else 0), page.content()
    finally:
        page.close()


def close_playwright():
    if "browser" in _PW:
        _PW["browser"].close()
        _PW["pw"].stop()


def check_site(site):
    smin = SETTINGS.get("sanity_min", 150)
    smax = SETTINGS.get("sanity_max", 1500)
    attempts = ["playwright"] if site.get("playwright") else ["requests"]
    if attempts == ["requests"] and SETTINGS.get("playwright_fallback", True):
        attempts.append("playwright")
    result = {"status": "erreur", "price": None, "available": None, "how": "", "detail": ""}
    for engine in attempts:
        try:
            status, page = fetch_requests(site["url"]) if engine == "requests" \
                else fetch_playwright(site["url"])
        except Exception as exc:          # réseau, timeout, Playwright absent...
            if result["status"] == "erreur":   # on garde « bloqué »/« introuvable » si déjà vu
                result["detail"] = f"{engine}: {type(exc).__name__}"
            else:
                result["detail"] += f" ; {engine}: {type(exc).__name__}"
            continue
        if looks_blocked(status, page):
            result.update(status="bloqué", detail=f"{engine}: HTTP {status}")
            continue                      # on tente le moteur suivant, sans plus
        price, available, how = extract(page, site, smin, smax)
        low = page.lower()
        if any(t.lower() in low for t in site.get("out_of_stock_text", [])):
            available = False
        if price is not None:
            return {"status": "ok", "price": price, "available": available,
                    "how": f"{engine}/{how}", "detail": f"HTTP {status}"}
        result.update(status="introuvable", detail=f"{engine}: HTTP {status}")
    return result


# ---------------------------------------------------------------- telegram
def send_telegram(text):
    token, chat_id = os.environ["TELEGRAM_BOT_TOKEN"], os.environ["TELEGRAM_CHAT_ID"]
    r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                      json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"}, timeout=20)
    r.raise_for_status()


def eur(p):
    return f"{p:,.2f} €".replace(",", " ").replace(".", ",")


# --------------------------------------------------------------------- main
def main():
    if PING_MODE:
        send_telegram("✅ <b>Test price_watch</b> : le bot Telegram fonctionne.")
        print("Message de test envoyé.")
        return

    state = json.loads(STATE_FILE.read_text(encoding="utf-8")) if STATE_FILE.exists() else {}
    realert = SETTINGS.get("realert", "baisse")
    block_after = SETTINGS.get("block_alert_after", 3)
    default_max = SETTINGS.get("default_max_price", 500)
    messages, rows = [], []

    for site in WATCHLIST["sites"]:
        name, url = site.get("name", "?"), site.get("url", "").strip()
        if not site.get("enabled", True) or not url.startswith("http"):
            rows.append((name, "désactivé", "", site.get("note", "")))
            continue
        max_price = site.get("max_price", default_max)
        res = check_site(site)
        st = state.setdefault(url, {"fails": 0, "last_alert_price": None})
        st.update(name=name, last_status=res["status"], last_checked=NOW)

        if res["status"] == "ok":
            p, avail = res["price"], res["available"]
            st["last_price"] = p
            stock = "rupture" if avail is False else ("en stock" if avail else "stock ?")
            rows.append((name, eur(p), f"{stock} · seuil {eur(max_price)}", res["how"]))
            if st["fails"] >= block_after:
                messages.append(f"✅ <b>{html.escape(name)}</b> est de nouveau lisible ({eur(p)}).")
            st["fails"] = 0
            last = st.get("last_alert_price")
            if p <= max_price and avail is not False:
                if last is None:
                    icon = "🎯"
                elif (realert == "baisse" and p < last) or (realert == "changement" and p != last):
                    icon = "📉" if p < last else "📈"
                else:
                    icon = None
                if icon:
                    old = f" (avant : {eur(last)})" if last is not None else ""
                    messages.append(f"{icon} <b>{html.escape(name)}</b>\n<b>{eur(p)}</b>{old} "
                                    f"— seuil {eur(max_price)}\n{url}")
                    st["last_alert_price"] = p
            else:
                st["last_alert_price"] = None   # réalertera quand ça repasse sous le seuil
        else:
            st["fails"] = st.get("fails", 0) + 1
            label = "bloqué" if res["status"] == "bloqué" else (
                "prix introuvable" if res["status"] == "introuvable" else "erreur")
            rows.append((name, label, f"{st['fails']}× de suite", res["detail"]))
            if st["fails"] == block_after:
                messages.append(f"⚠️ <b>{html.escape(name)}</b> : {label} {block_after} fois de suite. "
                                f"Ce site n'est PAS réellement surveillé.\n{ALT_HINT}.\n{url}")
        time.sleep(SETTINGS.get("delay_seconds", 3))

    close_playwright()
    width = max(len(r[0]) for r in rows) if rows else 10
    for r in rows:
        print(f"{r[0]:<{width}} | {r[1]:<16} | {r[2]:<28} | {r[3]}")
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as f:
            f.write("| Site | Résultat | Détail | Méthode / note |\n|---|---|---|---|\n")
            f.writelines(f"| {a} | {b} | {c} | {d} |\n" for a, b, c, d in rows)

    if TEST_MODE:
        print(f"\nMode test : {len(messages)} alerte(s) auraient été envoyées :")
        for m in messages:
            print(" -", re.sub(r"<[^>]+>", "", m).replace("\n", " | "))
        return
    for m in messages:
        send_telegram(m)
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"{len(messages)} alerte(s) envoyée(s).")


if __name__ == "__main__":
    main()
