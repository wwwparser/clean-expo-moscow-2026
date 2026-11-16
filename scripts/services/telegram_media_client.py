"""Public Telegram preview and image client. Never uses an authenticated session."""
from __future__ import annotations

import hashlib
import io
import re
import time
from pathlib import Path
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from PIL import Image, UnidentifiedImageError

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"
)
SESSION = requests.Session()
SESSION.headers.update({"User-Agent": USER_AGENT})


def _get(url: str, timeout: tuple[int, int] = (8, 25)) -> bytes:
    error = None
    for attempt in range(3):
        try:
            response = SESSION.get(url, timeout=timeout)
            response.raise_for_status()
            return response.content
        except requests.RequestException as exc:
            error = exc
            if attempt < 2:
                time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"Telegram preview request failed: {url}: {type(error).__name__}")


def fetch_channel_page(username: str, before: int | None, cache_dir: Path) -> BeautifulSoup:
    url = f"https://t.me/s/{username}" + (f"?before={before}" if before else "")
    key = hashlib.sha256(url.encode("utf-8")).hexdigest() + ".html"
    path = cache_dir / key
    if path.is_file():
        body = path.read_bytes()
    else:
        body = _get(url)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)
    return BeautifulSoup(body, "html.parser")


def extract_channel_posts(soup: BeautifulSoup) -> dict[str, dict]:
    found = {}
    for message in soup.select(".tgme_widget_message[data-post]"):
        post_id = (message.get("data-post") or "").strip()
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{3,}/\d+", post_id):
            continue
        title = soup.select_one(".tgme_channel_info_header_title")
        date = message.select_one("time[datetime]")
        text = message.select_one(".tgme_widget_message_text")
        photos, types = [], []
        for element in message.select(
            ".tgme_widget_message_photo_wrap[style],"
            ".tgme_widget_message_video_thumb[style]"
        ):
            match = re.search(r"background-image\s*:\s*url\(['\"]?([^'\")]+)", element.get("style", ""))
            if match:
                url = urljoin("https://t.me/", match.group(1).replace("&amp;", "&"))
                kind = "video" if "video_thumb" in (element.get("class") or []) else "photo"
                if url not in photos:
                    photos.append(url)
                    types.append(kind)
        has_video = bool(message.select_one("video,.tgme_widget_message_video_player,.tgme_widget_message_video_thumb"))
        found[post_id] = {
            "channel_title": title.get_text(" ", strip=True) if title else "",
            "date": date.get("datetime", "") if date else "",
            "preview_text": text.get_text("\n", strip=True) if text else "",
            "remote_photos": photos[:2],
            "media_types": types[:2],
            "has_video": has_video,
        }
    return found


def download_preview(url: str, destination: Path) -> tuple[str, str]:
    """Store a small local JPEG preview and return (relative path, status)."""
    try:
        body = _get(url, timeout=(8, 20))
        if len(body) > 12_000_000:
            return "", "too_large"
        try:
            image = Image.open(io.BytesIO(body))
            image.load()
        except (UnidentifiedImageError, OSError):
            return "", "not_an_image"
        image.thumbnail((1000, 1000))
        destination.parent.mkdir(parents=True, exist_ok=True)
        image.convert("RGB").save(destination, "JPEG", quality=78, optimize=True)
        return destination.as_posix(), "saved"
    except (RuntimeError, OSError, ValueError) as exc:
        return "", type(exc).__name__
