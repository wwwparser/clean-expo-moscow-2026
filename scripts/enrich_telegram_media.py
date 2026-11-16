"""Restore public Telegram photo/video previews for the saved post slice."""
from __future__ import annotations

import concurrent.futures as cf
import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from services.telegram_media_client import extract_channel_posts, fetch_channel_page, download_preview

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data/portal"
OUT = ROOT / "docs/media/telegram"
CACHE = ROOT / "data/raw/telegram-media/pages"


def read_json(path: Path, default):
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else default


def write_json(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temp.replace(path)


def main():
    posts = read_json(DATA / "posts.json", [])
    channels = read_json(DATA / "channels.json", [])
    if not posts or not channels:
        raise SystemExit("Restore the local Telegram post and channel snapshots first.")
    channel_map = {c["id"]: c for c in channels}
    targets = {}
    for post in posts:
        channel = channel_map.get(post.get("channel_id"))
        if not channel:
            continue
        username = urlparse(channel["url"]).path.strip("/").split("/")[-1]
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{3,}", username):
            continue
        targets.setdefault(username, set()).add(post["id"])

    def read_channel(username):
        target_ids = targets[username]
        missing = set(target_ids)
        found = {}
        before = None
        error = ""
        for _ in range(6):
            try:
                soup = fetch_channel_page(username, before, CACHE)
            except Exception as exc:
                error = type(exc).__name__
                break
            batch = extract_channel_posts(soup)
            found.update(batch)
            missing -= set(batch)
            numeric_ids = [int(pid.rsplit("/", 1)[1]) for pid in batch]
            if not missing or not numeric_ids:
                break
            next_before = min(numeric_ids)
            if before == next_before:
                break
            before = next_before
        return username, found, missing, error

    pages = {}
    missing_by_channel = {}
    errors = {}
    with cf.ThreadPoolExecutor(max_workers=6) as pool:
        for n, result in enumerate(pool.map(read_channel, targets), 1):
            username, found, missing, error = result
            for key, value in found.items():
                pages[key.casefold()] = value
            if missing:
                missing_by_channel[username] = len(missing)
            if error:
                errors[username] = error
            print(f"PREVIEW {n}/{len(targets)} {username}: {len(found)} messages", flush=True)

    url_to_path = {}
    photo_urls = list(dict.fromkeys(
        url for page in pages.values() for url in page.get("remote_photos", [])
    ))

    def save_photo(url):
        filename = hashlib.sha256(url.encode("utf-8")).hexdigest()[:24] + ".jpg"
        destination = OUT / filename
        if destination.is_file():
            return url, f"media/telegram/{filename}", "cached"
        relative, status = download_preview(url, destination)
        return url, f"media/telegram/{filename}" if relative else "", status

    with cf.ThreadPoolExecutor(max_workers=6) as pool:
        for n, (url, relative, status) in enumerate(pool.map(save_photo, photo_urls), 1):
            url_to_path[url] = relative
            if n % 50 == 0 or n == len(photo_urls):
                print(f"IMAGES {n}/{len(photo_urls)}", flush=True)

    post_by_id = {}
    for post in posts:
        preview = pages.get(str(post.get("id", "")).casefold())
        if not preview:
            post["media_status"] = "preview_unavailable"
            post_by_id[post["id"]] = post
            continue
        remote = preview.get("remote_photos", [])
        paths = [url_to_path[u] for u in remote if url_to_path.get(u)]
        post["channel_title"] = post.get("channel_title") or preview.get("channel_title", "")
        post["preview_caption"] = preview.get("preview_text", "")
        post["remote_photos"] = remote
        # Keep classified input fields untouched; this is presentation-only media.
        post["display_photos"] = paths[:2]
        post["photo_media_types"] = preview.get("media_types", [])[:2]
        post["has_video"] = bool(preview.get("has_video"))
        post["media_status"] = "saved" if paths else "video_without_thumbnail" if post["has_video"] else "no_media"
        post["media_checked_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        post_by_id[post["id"]] = post

    posts.sort(key=lambda x: x.get("published_at", ""), reverse=True)
    write_json(DATA / "posts.json", posts)
    (DATA / "telegram_posts.jsonl").write_text(
        "\n".join(json.dumps(p, ensure_ascii=False) for p in posts) + "\n", encoding="utf-8"
    )

    participants = {p["id"]: p for p in read_json(DATA / "participants.json", [])}
    videos = []
    for post in posts:
        if not post.get("has_video"):
            continue
        excerpt = re.sub(r"\s+", " ", post.get("text") or post.get("preview_caption") or "").strip()
        title = excerpt[:125].rstrip(" …") + ("…" if len(excerpt) > 125 else "")
        channel = channel_map.get(post.get("channel_id"), {})
        participant_ids = list(dict.fromkeys(post.get("participant_ids", []) + channel.get("participant_ids", [])))
        if not title:
            title = f"Видео: {channel.get('title') or post.get('channel_id')}"
        videos.append({
            "id": "telegram-" + re.sub(r"[^A-Za-z0-9_-]", "-", post["id"]),
            "title": title,
            "channel": channel.get("title") or post.get("channel_title") or post.get("channel_id", "Telegram"),
            "url": post["url"],
            "image": (post.get("display_photos") or [""])[0],
            "date": (post.get("published_at") or "")[:10],
            "year": int((post.get("published_at") or "2026")[:4]),
            "duration": None,
            "status": "Видео в публичной публикации; длительность в превью не указана",
            "kind": "Публикация участника",
            "participant_ids": [pid for pid in participant_ids if pid in participants],
            "sources": [{"title": channel.get("title") or "Публичный Telegram-канал участника", "url": post["url"], "date": (post.get("published_at") or "")[:10]}],
        })
    write_json(DATA / "videos.json", videos)

    statuses = {}
    for post in posts:
        status = post.get("media_status", "unprocessed")
        statuses[status] = statuses.get(status, 0) + 1
    report = {
        "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "channels": len(targets),
        "posts": len(posts),
        "public_preview_messages": len(pages),
        "photo_urls": len(photo_urls),
        "local_photo_previews": sum(bool(x.get("display_photos")) for x in posts),
        "videos": len(videos),
        "statuses": statuses,
        "missing_post_count_by_channel": missing_by_channel,
        "channel_errors": errors,
        "notes": "Only public Telegram preview media is used; each image card links to its original post.",
    }
    write_json(DATA / "telegram_media_coverage.json", report)
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
