"""Assemble participant, schedule, speaker and public Telegram snapshots."""
import json
import re
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data" / "portal"
RAW = ROOT / "data" / "raw" / "cleanexpo"
PROGRAM_URL = "https://www.cleanexpo-moscow.ru/ru-RU/business-program/events.aspx"
SPEAKERS_URL = "https://www.cleanexpo-moscow.ru/ru-RU/about/speakers.aspx"
CHANNEL_URL = "https://t.me/CleanExpoRussia"
NOW = datetime.now(ZoneInfo("Europe/Moscow")).isoformat(timespec="minutes")
DATE = NOW[:10]


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def js_array(text, name):
    match = re.search(rf"const\s+{name}\s*=\s*\[", text)
    if not match:
        raise ValueError(f"Missing const {name} in official program data")
    end = text.find("].sort(", match.end())
    if end < 0:
        end = text.find("];", match.end())
    if end < 0:
        raise ValueError(f"Cannot find end of {name} array")
    payload = text[match.end() - 1:end + 1]
    # The official site escapes characters such as spaces and parentheses in JS strings.
    payload = re.sub(r'\\(?!["\\/bfnrtu])', "", payload)
    payload = re.sub(r",\s*([}\]])", r"\1", payload)
    return json.loads(payload)


def main():
    participants = json.loads((DATA / "participants.json").read_text(encoding="utf-8"))
    for p in participants:
        description = p.pop("description", "") or ""
        p["about"] = BeautifulSoup(description, "html.parser").get_text(" ", strip=True)
        contacts = []
        if p.get("email"):
            contacts.append({"label": "Электронная почта", "value": p["email"], "url": "mailto:" + p["email"]})
        if p.get("website"):
            contacts.append({"label": "Сайт", "value": p["website"], "url": p["website"]})
        p["contacts"] = contacts
        p["contacts_text"] = " · ".join(v for v in [(("Регион: " + p["region"]) if p.get("region") else ""), (("Адрес: " + p["address"]) if p.get("address") else "")] if v)
    write_json(DATA / "participants.json", participants)

    program_text = (RAW / "program.html").read_text(encoding="utf-8", errors="replace")
    events = js_array(program_text, "events")
    speakers = js_array(program_text, "speakers")
    program_soup = BeautifulSoup(program_text, "html.parser")
    day_map = {}
    for day_node in program_soup.select(".day_item[data-day-id]"):
        mobile_date = day_node.select_one(".day_item--mobile")
        match_day = re.search(r"\b(\d{1,2})\.(\d{1,2})\b", mobile_date.get_text(" ", strip=True) if mobile_date else "")
        if match_day:
            day_map[str(day_node.get("data-day-id"))] = f"2026-{int(match_day.group(2)):02d}-{int(match_day.group(1)):02d}"
    people = []
    speaker_ids = set()
    for s in speakers:
        sid = str(s["id"])
        speaker_ids.add(sid)
        name = BeautifulSoup(str(s.get("name", "")), "html.parser").get_text(" ", strip=True)
        job = BeautifulSoup(str(s.get("job", "")), "html.parser").get_text(" ", strip=True)
        image = s.get("img") or s.get("image") or s.get("photo")
        person = {"id": f"speaker-{sid}", "source_id": sid, "name": name, "role": job or None,
                  "sources": [{"title": "Официальная деловая программа CleanExpo Moscow 2026", "url": PROGRAM_URL, "date": DATE}],
                  "checked_at": NOW}
        if image:
            person["photo"] = urljoin("https://www.cleanexpo-moscow.ru", str(image))
        people.append(person)

    program = []
    for e in events:
        raw_date = str(e.get("event-date") or "")
        day = re.search(r"\b(\d{1,2})\b", raw_date)
        date_number = int(day.group(1)) if day else None
        # Schedule tabs provide the displayed calendar day; retain conflicting
        # per-event date text in the source note for transparent review.
        date = day_map.get(str(e.get("day-id")))
        conflicting_date = bool(date and date_number and date_number != int(date[-2:]))
        raw_description = str(e.get("info-text") or e.get("info-description") or "")
        description = BeautifulSoup(raw_description, "html.parser").get_text(" ", strip=True)
        linked_speakers = [f"speaker-{sid}" for sid in map(str, e.get("info-speakers", [])) if sid in speaker_ids]
        program.append({
            "id": f"event-{e['event-id']}", "title": BeautifulSoup(str(e.get("event-title", "")), "html.parser").get_text(" ", strip=True),
            "date": date, "start": e.get("event-start") or None, "end": e.get("event-end") or None,
            "venue": BeautifulSoup(str(e.get("info-location") or e.get("event-tagHall") or ""), "html.parser").get_text(" ", strip=True),
            "description": description, "people_ids": linked_speakers,
            "sources": [{"title": "Официальная деловая программа CleanExpo Moscow 2026", "url": PROGRAM_URL, "date": DATE,
                         "note": (f"Группировка страницы относит событие к {date}; поле event-date отдельно содержит «{raw_date}». Сохранена дата группы; проверьте возможную опечатку у организатора." if conflicting_date else "Расписание получено из публичных данных страницы программы; организатор может изменить время и площадку.")}],
            "checked_at": NOW,
        })
    write_json(DATA / "people.json", people)
    write_json(DATA / "program.json", program)

    participant_by_source = {str(p.get("source_id")): p["id"] for p in participants}
    products = json.loads((RAW / "products-collected.json").read_text(encoding="utf-8"))
    for i, item in enumerate(products):
        item["id"] = f"product-{i + 1:04d}"
        item["sources"] = [{"title": "Официальный каталог продукции CleanExpo Moscow 2026",
                            "url": "https://www.cleanexpo-moscow.ru/ru-RU/visitors/products.aspx", "date": DATE}]
        source_id = str(item.pop("participant_source_id", "") or "")
        if source_id in participant_by_source:
            item["participant_ids"] = [participant_by_source[source_id]]
        item["checked_at"] = NOW
    write_json(DATA / "products.json", products)

    soup = BeautifulSoup((RAW / "tg.html").read_text(encoding="utf-8", errors="replace"), "html.parser")
    messages = soup.select(".tgme_widget_message_wrap")[-10:]
    posts = []
    for message in messages:
        node = message.select_one("[data-post]")
        if not node:
            continue
        ident = node.get("data-post")
        date_link = message.select_one(".tgme_widget_message_date")
        timestamp = date_link.select_one("time") if date_link else None
        text_node = message.select_one(".tgme_widget_message_text")
        body = text_node.get_text(" ", strip=True) if text_node else ""
        permalink = date_link.get("href") if date_link else "https://t.me/" + ident
        posts.append({"id": ident, "channel_id": "cleanexpo-russia", "url": permalink,
                      "published_at": timestamp.get("datetime") if timestamp else None, "text": body,
                      "participant_ids": [], "people_ids": [], "grouped_message_ids": [],
                      "sources": [{"title": "Официальный публичный канал CleanExpo Russia", "url": permalink, "date": DATE}],
                      "checked_at": NOW})
    organizer_channel = {"id": "cleanexpo-russia", "title": "CleanExpo Russia", "url": CHANNEL_URL,
                         "participant_ids": [],
                         "sources": [{"title": "Ссылка на Telegram-канал организатора с официального сайта CleanExpo", "url": "https://www.cleanexpo-moscow.ru/ru-RU/", "date": DATE}]}
    discovered_channels, discovered_posts = [], []
    discovery_path = RAW / "telegram-discovery.json"
    discovered_posts_path = RAW / "telegram-discovered-posts.json"
    if discovery_path.exists() and discovered_posts_path.exists():
        discovery = json.loads(discovery_path.read_text(encoding="utf-8"))
        raw_posts = json.loads(discovered_posts_path.read_text(encoding="utf-8"))
        # Discovery is a snapshot of public channel previews linked from participant websites.
        # Preserve participant/source links supplied by that crawler and merge organizer last.
        discovered_channels = []
        for result in discovery.get("channel_results", []):
            if result.get("post_count", 0) <= 0:
                continue
            username = result["username"]
            channel_id = re.sub(r"[^a-z0-9_-]+", "-", username.lower()).strip("-")
            title = username
            match = next((p for p in raw_posts if p.get("channel_id") == channel_id), None)
            if match:
                title = match.get("sources", [{}])[0].get("title", username).replace("Публичный канал ", "")
            linked_ids = sorted(set(result.get("participant_ids", [])))
            proof_urls = sorted(set(result.get("proof_urls", [])))
            discovered_channels.append({"id": channel_id, "title": title, "url": f"https://t.me/{username}",
                "participant_ids": linked_ids, "sources": [{"title": "Telegram-ссылка на официальном сайте участника", "url": proof_urls[0] if proof_urls else f"https://t.me/{username}", "date": DATE}]})
        for p in raw_posts:
            p["sources"] = [{"title": f"Публичная публикация канала {p.get('channel_id')}", "url": p.get("url"), "date": DATE}]
        discovered_posts = raw_posts
    contact_channels_path = RAW / "telegram-contactpage-channels.json"
    contact_posts_path = RAW / "telegram-contactpage-posts.json"
    if contact_channels_path.exists() and contact_posts_path.exists():
        extra_channels = json.loads(contact_channels_path.read_text(encoding="utf-8"))
        extra_posts = json.loads(contact_posts_path.read_text(encoding="utf-8"))
        existing_ids = {c["id"] for c in discovered_channels} | {organizer_channel["id"]}
        discovered_channels.extend(c for c in extra_channels if c.get("id") not in existing_ids)
        existing_post_ids = {p["id"] for p in posts} | {p["id"] for p in discovered_posts}
        discovered_posts.extend(p for p in extra_posts if p.get("id") not in existing_post_ids)
    # Persist the bounded source snapshot both in build format and the skill's raw JSONL archive.
    posts.extend(discovered_posts)
    write_json(DATA / "posts.json", posts)
    (DATA / "telegram_posts.jsonl").write_text("".join(json.dumps(p, ensure_ascii=False) + "\n" for p in posts), encoding="utf-8")
    write_json(DATA / "channels.json", [organizer_channel, *discovered_channels])
    expo_path = ROOT / "expo.json"
    expo = json.loads(expo_path.read_text(encoding="utf-8"))
    readable = sum(bool(str(p.get("text", "")).strip()) for p in posts)
    coverage = expo.setdefault("telegram_coverage", {})
    coverage.update({"channels": len(discovered_channels) + 1, "participant_channels": len(discovered_channels),
        "posts": len(posts), "reviewed_posts": 0,
        "readable_posts": readable, "unreadable_posts": len(posts) - readable,
        "websites_checked": discovery.get("websites_checked", 0) if discovery_path.exists() else 0,
        "website_errors": discovery.get("website_errors", 0) if discovery_path.exists() else 0,
        "scope": "До 10 последних публичных публикаций организатора и каналов, чьи ссылки найдены на официальных сайтах участников и их доступных страницах контактов; при ошибке доступа сайт не считался проверенным."})
    coverage.pop("unreadable_post", None)
    expo["checked_at"] = NOW
    expo_path.write_text(json.dumps(expo, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"participants": len(participants), "products": len(products), "speakers": len(people), "program": len(program),
                      "telegram_posts": len(posts), "telegram_channels": len(discovered_channels) + 1,
                      "program_dates": sorted({p["date"] for p in program if p["date"]})}, ensure_ascii=False))


if __name__ == "__main__":
    main()
