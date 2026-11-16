"""Discover public Telegram channels linked by official exhibitor websites."""
from __future__ import annotations

import concurrent.futures
import ipaddress
import json
import re
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data" / "portal"
RAW = ROOT / "data" / "raw" / "cleanexpo"
RESULT = RAW / "telegram-discovery.json"
POSTS = RAW / "telegram-discovered-posts.json"
CHECKED = datetime.now(ZoneInfo("Europe/Moscow")).isoformat(timespec="minutes")
MAX_BYTES = 1_000_000
UA = "Mozilla/5.0 (compatible; CleanExpoPortal/1.0; +https://github.com/)"


def public_host(url: str) -> bool:
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password:
        return False
    host = parsed.hostname.rstrip(".").lower()
    if host in {"localhost", "localhost.localdomain"}:
        return False
    try:
        addresses = {item[4][0] for item in socket.getaddrinfo(host, parsed.port or (443 if parsed.scheme == "https" else 80))}
        return bool(addresses) and all(ipaddress.ip_address(address).is_global for address in addresses)
    except (OSError, ValueError):
        return False


class PublicRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not public_host(newurl):
            raise urllib.error.HTTPError(newurl, code, "Redirect to a non-public host rejected", headers, fp)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def fetch(url: str, timeout: int = 8) -> tuple[str, bytes]:
    if not public_host(url):
        raise ValueError("non-public or invalid URL")
    opener = urllib.request.build_opener(PublicRedirect())
    request = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "text/html,application/xhtml+xml"})
    with opener.open(request, timeout=timeout) as response:
        final = response.geturl()
        if not public_host(final):
            raise ValueError("redirected outside public web")
        content = response.read(MAX_BYTES + 1)
        if len(content) > MAX_BYTES:
            content = content[:MAX_BYTES]
        return final, content


def normalized_channel(link: str) -> str | None:
    parsed = urllib.parse.urlsplit(link.strip())
    host = (parsed.hostname or "").lower()
    if host not in {"t.me", "telegram.me", "www.t.me", "www.telegram.me"}:
        return None
    parts = [urllib.parse.unquote(x) for x in parsed.path.split("/") if x]
    if parts and parts[0].lower() == "s":
        parts = parts[1:]
    if not parts:
        return None
    username = parts[0].lstrip("@").lower()
    if username in {"joinchat", "share", "proxy", "addstickers", "addemoji", "iv", "login", "boost"} or username.startswith("+"):
        return None
    if not re.fullmatch(r"[a-z0-9_]{5,32}", username):
        return None
    return username


def participant_home(p: dict):
    url = p.get("website")
    if not url:
        return p, None, [], "no_official_website"
    try:
        final, body = fetch(url)
        (RAW / "company-homepages").mkdir(parents=True, exist_ok=True)
        (RAW / "company-homepages" / f"{p['source_id']}.html").write_bytes(body)
        soup = BeautifulSoup(body, "html.parser")
        links = []
        for anchor in soup.select("a[href]"):
            raw = anchor.get("href", "").strip()
            absolute = urllib.parse.urljoin(final, raw)
            channel = normalized_channel(absolute)
            if channel:
                links.append((channel, absolute))
        # Also detect public channel URLs written as plain text in contact blocks.
        for raw in re.findall(r"https?://(?:www\.)?(?:t\.me|telegram\.me)/[^\s\"'<>]+", soup.get_text(" ", strip=True), re.I):
            channel = normalized_channel(raw)
            if channel:
                links.append((channel, raw.rstrip(".,);")))
        unique = list(dict(((name, link) for name, link in links)).items())
        # Reduce candidate payload to username and exact official website/page evidence.
        return p, final, [{"username": name, "linked_url": link} for name, link in unique], None
    except Exception as exc:
        return p, None, [], type(exc).__name__


def parse_preview(username: str, participant_ids: list[str], proof_urls: list[str]):
    preview_url = f"https://t.me/s/{username}"
    try:
        final, body = fetch(preview_url, timeout=12)
        RAW.mkdir(parents=True, exist_ok=True)
        (RAW / f"telegram-{username}.html").write_bytes(body)
        soup = BeautifulSoup(body, "html.parser")
        wrappers = soup.select(".tgme_widget_message_wrap")
        if not wrappers:
            return None, [], "no_public_message_preview"
        title_node = soup.select_one('meta[property="og:title"]')
        title = title_node.get("content", "").strip() if title_node else username
        channel_id = re.sub(r"[^a-z0-9_-]+", "-", username.lower()).strip("-")
        channel = {"id": channel_id, "title": title or username, "url": f"https://t.me/{username}",
                   "participant_ids": participant_ids,
                   "sources": [{"title": "Публичное Telegram-превью канала, связанное с официальным сайтом участника", "url": preview_url,
                                "date": CHECKED[:10], "note": "Ссылка на канал обнаружена на официальном сайте участника."}]}
        posts = []
        for message in wrappers[-10:]:
            post_node = message.select_one("[data-post]")
            if not post_node:
                continue
            ident = post_node.get("data-post")
            date_link = message.select_one(".tgme_widget_message_date")
            time_node = date_link.select_one("time") if date_link else None
            text_node = message.select_one(".tgme_widget_message_text")
            body_text = text_node.get_text(" ", strip=True) if text_node else ""
            permalink = date_link.get("href") if date_link else f"https://t.me/{ident}"
            posts.append({"id": ident, "channel_id": channel_id, "url": permalink,
                          "published_at": time_node.get("datetime") if time_node else None, "text": body_text,
                          "participant_ids": participant_ids, "people_ids": [], "grouped_message_ids": [],
                          "sources": [{"title": f"Публичный канал {title}", "url": permalink, "date": CHECKED[:10]}],
                          "checked_at": CHECKED})
        return channel, posts, None
    except Exception as exc:
        return None, [], type(exc).__name__


def main():
    RAW.mkdir(parents=True, exist_ok=True)
    participants = json.loads((DATA / "participants.json").read_text(encoding="utf-8"))
    eligible = [p for p in participants if p.get("website")]
    discovery = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(participant_home, p) for p in eligible]
        for future in concurrent.futures.as_completed(futures):
            p, final, links, error = future.result()
            discovery.append({"participant_id": p["id"], "source_id": p["source_id"], "website": p.get("website"),
                              "final_url": final, "channels": links, "error": error, "checked_at": CHECKED})
    groups = {}
    for row in discovery:
        for candidate in row["channels"]:
            groups.setdefault(candidate["username"], {"participant_ids": set(), "proof_urls": set()})
            groups[candidate["username"]]["participant_ids"].add(row["participant_id"])
            groups[candidate["username"]]["proof_urls"].add(row["website"])
    channels, posts, channel_results = [], [], []
    for username, evidence in groups.items():
        channel, rows, error = parse_preview(username, sorted(evidence["participant_ids"]), sorted(evidence["proof_urls"]))
        if channel:
            channels.append(channel)
            posts.extend(rows)
        channel_results.append({"username": username, "participant_ids": sorted(evidence["participant_ids"]),
                                "proof_urls": sorted(evidence["proof_urls"]), "post_count": len(rows), "error": error})
        time.sleep(.25)
    RESULT.write_text(json.dumps({"checked_at": CHECKED, "websites_checked": len(eligible), "website_errors": sum(bool(x["error"]) for x in discovery),
        "candidate_channels": len(groups), "public_channels": len(channels), "posts": len(posts),
        "website_results": discovery, "channel_results": channel_results}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    POSTS.write_text(json.dumps(posts, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"websites_checked": len(eligible), "website_errors": sum(bool(x["error"]) for x in discovery),
                      "candidate_channels": len(groups), "verified_public_channels": len(channels), "posts": len(posts)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
