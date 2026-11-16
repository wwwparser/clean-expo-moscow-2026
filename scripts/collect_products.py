"""Fetch every public page of CleanExpo's 2026 product catalog."""
from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parents[1]
RAW = ROOT / "data" / "raw" / "cleanexpo"
URL = "https://www.cleanexpo-moscow.ru/ru-RU/visitors/products.aspx"
PAGER = "p$lt$ctl05$pageplaceholder$p$lt$ctl01$UniPagerVisitorProductCatalogMobile$pagerElem"
CHECKED_DATE = datetime.now(ZoneInfo("Europe/Moscow")).date().isoformat()


def fetch(opener, data=None):
    headers = {"User-Agent": "Mozilla/5.0 (compatible; CleanExpoPortal/1.0)"}
    if data is None:
        request = urllib.request.Request(URL, headers=headers)
    else:
        request = urllib.request.Request(URL, data=urllib.parse.urlencode(data).encode("utf-8"),
            headers={**headers, "Content-Type": "application/x-www-form-urlencoded", "Referer": URL})
    for attempt in range(3):
        try:
            with opener.open(request, timeout=60) as response:
                return response.read()
        except (OSError, urllib.error.URLError):
            if attempt == 2:
                raise
            time.sleep(1.5 * (attempt + 1))


def rows(soup):
    result = {}
    for card in soup.select(".participantproduct"):
        info = card.select_one(".participantproduct_info")
        name = info.get_text(" ", strip=True) if info else card.get_text(" ", strip=True)
        if not name:
            continue
        exhibitor_id = str(card.get("data-exib") or "")
        result[exhibitor_id + "|" + name] = {
            "name": name,
            "participant_source_id": exhibitor_id or None,
            "sources": [{"title": "Официальный каталог продукции CleanExpo Moscow 2026", "url": URL, "date": CHECKED_DATE}],
        }
    return result


def main():
    RAW.mkdir(parents=True, exist_ok=True)
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor())
    first_bytes = fetch(opener)
    first = BeautifulSoup(first_bytes, "html.parser")
    form = first.select_one("form")
    if not form:
        raise RuntimeError("The official product catalog form was not found")
    hidden = {x.get("name"): x.get("value", "") for x in form.select('input[type="hidden"]') if x.get("name")}
    targets = []
    for link in first.select('a[href*="__doPostBack"]'):
        match = re.search(r"__doPostBack\('([^']*UniPagerVisitorProductCatalogMobile[^']*)','(\d+)'\)", link.get("href", ""))
        if match:
            targets.append(int(match.group(2)))
    last_page = max(targets, default=1)
    if last_page < 1 or last_page > 100:
        raise RuntimeError(f"Unexpected product catalog page count: {last_page}")

    collected = {}
    page_soup = first
    pages = {1: first_bytes}
    for page in range(1, last_page + 1):
        if page > 1:
            posted = dict(hidden, __EVENTTARGET=PAGER, __EVENTARGUMENT=str(page))
            page_bytes = fetch(opener, posted)
            page_soup = BeautifulSoup(page_bytes, "html.parser")
            form = page_soup.select_one("form")
            if not form:
                raise RuntimeError(f"Page {page} did not return the paging form")
            hidden = {x.get("name"): x.get("value", "") for x in form.select('input[type="hidden"]') if x.get("name")}
            pages[page] = page_bytes
            time.sleep(.25)
        collected.update(rows(page_soup))
        (RAW / f"products-page{page}.html").write_bytes(pages[page])
        (RAW / "products-progress.json").write_text(json.dumps({"page": page, "total_pages": last_page,
            "products_so_far": list(collected.values())}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"page={page}/{last_page} products={len(collected)}")

    target = RAW / "products-collected.json"
    temporary = target.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(list(collected.values()), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(target)
    print(f"saved={len(collected)} path={target}")


if __name__ == "__main__":
    main()
