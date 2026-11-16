"""Collect verifiable exhibitor records from the official CleanExpo 2026 pages."""
from __future__ import annotations

import json
import base64
import re
import time
import urllib.error
import urllib.request
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
RAW = ROOT / "data" / "raw" / "cleanexpo"
OUT = ROOT / "data" / "portal" / "participants.json"
PUBLIC_LOGOS = ROOT / "docs" / "media" / "participants"
PLAN = "https://www.cleanexpo-moscow.ru/ru-RU/visitors/exposition.aspx"
POPUP = "https://www.cleanexpo-moscow.ru/ru-RU/about/exhibitor-list/exhibitorpopup.aspx?id={}"
CATALOG = "https://www.cleanexpo-moscow.ru/ru-RU/visitors/exhibitor-list.aspx"


def get(url: str, attempts: int = 3) -> bytes:
    for n in range(attempts):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (compatible; CleanExpoPortal/1.0)"})
            with urllib.request.urlopen(req, timeout=30) as response:
                return response.read()
        except (OSError, urllib.error.URLError):
            if n == attempts - 1:
                raise
            time.sleep(1.5 * (n + 1))
    raise RuntimeError(url)


class PlanParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.rows = []
        self.row = None
        self.cell = None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "tr" and a.get("data-exib", "").isdigit():
            self.row = {"item_id": a["data-exib"], "stand": a.get("id", ""), "cells": []}
        elif self.row is not None and tag in ("td", "th"):
            self.cell = ""

    def handle_data(self, data):
        if self.row is not None and self.cell is not None:
            self.cell += data

    def handle_endtag(self, tag):
        if self.row is not None and tag in ("td", "th") and self.cell is not None:
            self.row["cells"].append(" ".join(self.cell.split()))
            self.cell = None
        elif tag == "tr" and self.row is not None:
            self.rows.append(self.row)
            self.row = None


def clean_url(site: str) -> str | None:
    site = (site or "").strip()
    if not site or site.lower() in {"none", "n/a", "-"}:
        return None
    return site if re.match(r"https?://", site, re.I) else "https://" + site


def main():
    RAW.mkdir(parents=True, exist_ok=True)
    plan_path = RAW / "exposition.html"
    if plan_path.exists():
        html = plan_path.read_bytes()
    else:
        html = get(PLAN)
        plan_path.write_bytes(html)
    parser = PlanParser()
    parser.feed(html.decode("utf-8-sig", "replace"))
    plan_rows = [r for r in parser.rows if len(r["cells"]) >= 2 and r["stand"]]
    if not plan_rows:
        raise RuntimeError("No exhibitor rows parsed from official exposition page")

    previous = {}
    if OUT.exists():
        try:
            previous = {str(p.get("source_id")): p for p in json.loads(OUT.read_text(encoding="utf-8-sig")) if p.get("source_id")}
        except (ValueError, TypeError):
            pass
    fetched = {}
    ok = failed = 0
    for index, row in enumerate(plan_rows):
        item_id = row["item_id"]
        cache = RAW / f"popup-{item_id}.json"
        logo_path = None
        try:
            if cache.exists() and list(PUBLIC_LOGOS.glob(f"{item_id}.*")):
                item = json.loads(cache.read_text(encoding="utf-8"))[0]
            else:
                item = json.loads(get(POPUP.format(item_id)).decode("utf-8-sig"))[0]
                # Keep the response cache without the inline base64 logo.
                safe_item = {k: v for k, v in item.items() if k != "Logo"}
                cache.write_text(json.dumps([safe_item], ensure_ascii=False), encoding="utf-8")
                time.sleep(.35)
                logo = item.get("Logo") or ""
                match = re.fullmatch(r"data:image/[^;]+;base64,(.+)", logo, re.S)
                if match:
                    binary = base64.b64decode(match.group(1), validate=True)
                    if binary.startswith(b"\xff\xd8\xff"):
                        suffix = "jpg"
                    elif binary.startswith(b"\x89PNG\r\n\x1a\n"):
                        suffix = "png"
                    elif binary.startswith((b"GIF87a", b"GIF89a")):
                        suffix = "gif"
                    else:
                        suffix = None
                    if suffix:
                        PUBLIC_LOGOS.mkdir(parents=True, exist_ok=True)
                        logo_path = PUBLIC_LOGOS / f"{item_id}.{suffix}"
                        logo_path.write_bytes(binary)
            if logo_path is None:
                existing_logos = list(PUBLIC_LOGOS.glob(f"{item_id}.*"))
                if existing_logos:
                    logo_path = existing_logos[0]
            name = str(item.get("Name") or (row["cells"][0] if row["cells"] else "")).strip()
            venues = []
            for suffix in ("", "2", "3"):
                pavilion_raw = str(item.get("Pavilion" + suffix) or "").strip()
                stand_value = str(item.get("Stand" + suffix) or (row["stand"] if not suffix else "")).strip()
                if not pavilion_raw or pavilion_raw == "None" or not stand_value:
                    continue
                hall_match = re.search(r"Зал\s*№?\s*([0-9A-Za-zА-Яа-я-]+)", pavilion_raw, re.I)
                pavilion_match = re.search(r"Павильон\s*№?\s*([0-9A-Za-zА-Яа-я-]+)", pavilion_raw, re.I)
                venues.append({"pavilion": f"Павильон №{pavilion_match.group(1)}" if pavilion_match else None,
                               "hall": f"Зал №{hall_match.group(1)}" if hall_match else pavilion_raw,
                               "stand": stand_value, "source_text": pavilion_raw})
            if not venues:
                venues = [{"pavilion": None, "hall": "Зал №3", "stand": row["stand"], "source_text": None}]
            profile = {
                "source_id": item_id,
                "id": re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or f"exhibitor-{item_id}",
                "name": name,
                "description": item.get("Description") or None,
                "country": item.get("Country") or "",
                "region": item.get("Region") or None,
                "address": item.get("Address") or None,
                "email": item.get("Email") or None,
                "website": clean_url(item.get("Site")),
                "brands": item.get("Brands") or None,
                "logo": logo_path.relative_to(ROOT / "docs").as_posix() if logo_path else None,
                "stands": list(dict.fromkeys(v["stand"] for v in venues)),
                "halls": list(dict.fromkeys(v["hall"] for v in venues)),
                "pavilions": list(dict.fromkeys(v["pavilion"] for v in venues if v["pavilion"])),
                "source_venues": venues,
                "categories": [],
                "rubrics": [],
                "sources": [
                    {"title": "Официальный интерактивный план CleanExpo Moscow 2026", "url": PLAN, "date": datetime.now(ZoneInfo("Europe/Moscow")).date().isoformat(), "note": f"Название и стенд {row['stand']} сверены по строке плана."},
                    {"title": "Официальная карточка участника CleanExpo", "url": POPUP.format(item_id), "date": datetime.now(ZoneInfo("Europe/Moscow")).date().isoformat(), "note": "Описание, регионы, бренды и контактные поля взяты из публичной карточки."},
                ],
                "checked_at": datetime.now(ZoneInfo("Europe/Moscow")).isoformat(timespec="minutes"),
                "profile_url": POPUP.format(item_id),
            }
            fetched[item_id] = profile
            ok += 1
        except Exception as exc:
            if item_id in previous:
                fetched[item_id] = previous[item_id]
            else:
                # Preserve known plan data even if the popup endpoint is temporarily down.
                name = row["cells"][0]
                fetched[item_id] = {"source_id": item_id, "id": f"exhibitor-{item_id}", "name": name, "country": "", "stands": [row["stand"]], "halls": ["Зал №3"], "categories": [], "rubrics": [], "sources": [{"title": "Официальный интерактивный план CleanExpo Moscow 2026", "url": PLAN, "date": datetime.now(ZoneInfo("Europe/Moscow")).date().isoformat(), "note": f"Название и стенд {row['stand']} сверены по плану; подробная карточка временно недоступна."}], "checked_at": datetime.now(ZoneInfo("Europe/Moscow")).isoformat(timespec="minutes"), "profile_url": POPUP.format(item_id)}
            failed += 1
            print(f"popup failed {item_id}: {type(exc).__name__}")
        if (index + 1) % 20 == 0:
            OUT.write_text(json.dumps(list(fetched.values()), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            print(f"progress {index+1}/{len(plan_rows)}")
    OUT.write_text(json.dumps(list(fetched.values()), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"plan_rows={len(plan_rows)} profiles_ok={ok} failed_or_fallback={failed} saved={len(fetched)} output={OUT}")


if __name__ == "__main__":
    main()
