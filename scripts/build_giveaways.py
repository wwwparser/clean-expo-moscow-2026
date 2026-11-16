"""Build unique, source-linked offers and prize events from reviewed Telegram data."""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from analysis_queue import load_project, reusable  # noqa: E402


def write(path: Path, value) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def unique(values):
    result, seen = [], set()
    for value in values:
        key = json.dumps(value, ensure_ascii=False, sort_keys=True) if isinstance(value, (dict, list)) else str(value)
        if key not in seen:
            seen.add(key)
            result.append(value)
    return result


def main() -> None:
    config, source, cache, state = load_project(ROOT)
    posts = json.loads((ROOT / "data/portal/posts.json").read_text(encoding="utf-8"))
    post_by_id = {str(p["id"]): p for p in posts}
    events: dict[str, dict] = {}
    for post_id, source_record in source.items():
        record = state["records"].get(post_id)
        if not reusable(record, source_record) or record.get("classification") not in {"giveaway", "prize_contest", "gift_for_visit", "discount"}:
            continue
        post = post_by_id[post_id]
        for event in record.get("events", []):
            event_key = str(event.get("event_key") or event.get("id") or "").strip()
            if not event_key:
                event_key = hashlib.sha256((post_id + json.dumps(event, ensure_ascii=False, sort_keys=True)).encode()).hexdigest()[:20]
            item = events.get(event_key)
            when = event.get("starts_at")
            if not when and len(event.get("occurrences") or []) == 1:
                first = event["occurrences"][0]
                when = first.get("date")
                if first.get("time") and when and "T" not in when and first.get("timezone") == "Europe/Moscow":
                    when += "T" + first["time"] + ":00+03:00"
            event_type = event.get("kind") or record["classification"]
            if item is None:
                safe_id = "event-" + hashlib.sha256(event_key.encode()).hexdigest()[:20]
                item = {
                    "id": safe_id, "event_key": event_key,
                    "title": event.get("title") or ("Предложение участника" if event_type == "discount" else "Событие из Telegram-публикации"),
                    "kind": event_type, "relevance": record.get("relevance"), "when": when,
                    "draw_at": event.get("draw_at"), "entry_deadline": event.get("entry_deadline"),
                    "results_at": event.get("results_at"), "offer_until": event.get("offer_until"),
                    "prizes": event.get("prizes") or [], "conditions": event.get("conditions") or [],
                    "stand": event.get("stand"), "hall": event.get("hall"),
                    "status": event.get("status") or "needs_details",
                    "note": event.get("note"), "missing_fields": event.get("missing_fields") or [],
                    "review_status": "reviewed", "post_ids": [], "participant_ids": [], "people_ids": [], "sources": [],
                    "occurrences": event.get("occurrences") or [],
                }
                events[event_key] = item
            elif not item.get("when") and when:
                item["when"] = when
            item["post_ids"].append(post_id)
            item["participant_ids"].extend(post.get("participant_ids", []))
            item["people_ids"].extend(post.get("people_ids", []))
            item["sources"].extend(post.get("sources", []))
            item["conditions"].extend(event.get("conditions") or [])
            item["prizes"].extend(event.get("prizes") or [])
            item["occurrences"].extend(event.get("occurrences") or [])
            item["missing_fields"].extend(event.get("missing_fields") or [])
            if event.get("note") and event.get("note") not in (item.get("note") or ""):
                item["note"] = " ".join(dict.fromkeys(filter(None, [item.get("note"), event["note"]])))
            # When messages conflict, disclose the conflict instead of selecting a date silently.
            if item.get("when") and when and item["when"] != when:
                conflict = f"В источниках указаны разные даты/время: {item['when']} и {when}."
                item["note"] = " ".join(filter(None, [item.get("note"), conflict]))
                item["when"] = None
    output = []
    for item in events.values():
        for key in ("post_ids", "participant_ids", "people_ids", "conditions", "prizes", "missing_fields", "occurrences"):
            item[key] = unique(item[key])
        dedup_sources = {}
        for source_item in item["sources"]:
            dedup_sources[source_item.get("url")] = source_item
        item["sources"] = list(dedup_sources.values())
        output.append(item)
    output.sort(key=lambda x: (x.get("when") or "9999", x.get("title") or ""))
    write(ROOT / "data/portal/giveaways.json", output)
    expo_path = ROOT / "expo.json"
    expo = json.loads(expo_path.read_text(encoding="utf-8"))
    coverage = expo.setdefault("telegram_coverage", {})
    coverage["reviewed_posts"] = sum(reusable(state["records"].get(i), post) for i, post in source.items())
    coverage["confirmed_prize_events"] = sum(
        1 for item in output if item.get("relevance") == "current_expo" and
        item.get("kind") in {"giveaway", "prize_contest", "gift_for_visit"}
    )
    expo_path.write_text(json.dumps(expo, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"reviewed_posts": coverage["reviewed_posts"], "unique_events": len(output),
                      "current_expo_prize_events": coverage["confirmed_prize_events"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
