#!/usr/bin/env python3
"""Prepare a compact, quality-ranked queue for ChatGPT Scheduled Tasks."""

from __future__ import annotations

import json
from pathlib import Path

import email_dispatch
from time_utils import format_datetime_ist, now_ist

ROOT = Path(__file__).resolve().parents[1]
NEWS = ROOT / "data/news.json"
SUMMARIES = ROOT / "data/chatgpt_summaries.json"
STATE = ROOT / "data/notified_ids.json"
QUEUE = ROOT / "data/chatgpt_queue.json"
MAX_QUEUE_ITEMS = 30

CATEGORY_SCORE = {
    "Partnership, M&A": 12,
    "Product & Services": 11,
    "Clinical, R&D": 11,
    "Financials": 10,
    "Leadership Changes": 9,
    "Organizational Updates": 9,
    "Other": 2,
}

EXTRA_LOW_VALUE_PATTERNS = (
    "stock analysis",
    "risk assessment",
    "investor sentiment",
    "financial health",
    "growth potential",
    "bull case",
    "bear case",
    "looks cheap",
    "looks expensive",
    "fair value",
    "valuation",
    "shares are",
    "stock looks",
)


def load(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def save(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def summary_ids(payload) -> set[str]:
    entries = payload.get("summaries") if isinstance(payload, dict) else {}
    return set(entries or {}) if isinstance(entries, dict) else set()


def low_value(item: dict) -> bool:
    if email_dispatch.is_low_value(item):
        return True
    title = str(item.get("title") or "").lower()
    return any(pattern in title for pattern in EXTRA_LOW_VALUE_PATTERNS)


def quality_score(item: dict, notified: set[str]) -> tuple[int, int, int, str]:
    identifier = str(item.get("id") or "")
    return (
        1 if identifier not in notified else 0,
        1 if item.get("official_source") else 0,
        CATEGORY_SCORE.get(str(item.get("category") or "Other"), 0),
        str(item.get("published_at") or ""),
    )


def compact_item(item: dict) -> dict:
    sources = []
    for source in item.get("sources") or []:
        if not isinstance(source, dict):
            continue
        url = str(source.get("url") or "").strip()
        if not url:
            continue
        sources.append({
            "name": str(source.get("name") or "Source").strip(),
            "url": url,
        })
    if not sources and item.get("url"):
        sources = [{"name": str(item.get("source") or "Source"), "url": str(item.get("url"))}]
    return {
        "id": str(item.get("id") or ""),
        "company": str(item.get("company") or ""),
        "category": str(item.get("category") or "Other"),
        "title": str(item.get("title") or ""),
        "source": str(item.get("source") or ""),
        "description": str(item.get("source_description") or item.get("description") or ""),
        "published_at": str(item.get("published_at") or ""),
        "published_display": str(item.get("published_display") or ""),
        "official_source": bool(item.get("official_source")),
        "coverage_count": int(item.get("coverage_count") or max(1, len(sources))),
        "url": str(item.get("url") or (sources[0]["url"] if sources else "")),
        "sources": sources[:8],
    }


def main() -> int:
    repository = load(NEWS, {})
    summaries = load(SUMMARIES, {"summaries": {}})
    state = load(STATE, {"notified_ids": []})
    done = summary_ids(summaries)
    notified = set(state.get("notified_ids") or [])

    missing = [
        item for item in repository.get("items") or []
        if str(item.get("id") or "")
        and str(item.get("id")) not in done
        and not low_value(item)
    ]
    missing.sort(key=lambda item: quality_score(item, notified), reverse=True)
    queued = [compact_item(item) for item in missing[:MAX_QUEUE_ITEMS]]
    now = now_ist()
    payload = {
        "generated_at": now.isoformat(),
        "generated_at_display": format_datetime_ist(now),
        "provider": "ChatGPT Scheduled Task",
        "instructions_version": "2026-08-07.2",
        "queue_limit": MAX_QUEUE_ITEMS,
        "item_count": len(queued),
        "remaining_unsummarized_count": len(missing),
        "priority_policy": "Unnotified first, then official sources, substantive categories, and recency; stock/valuation commentary excluded.",
        "items": queued,
    }
    save(QUEUE, payload)
    unnotified_count = sum(1 for item in queued if str(item.get("id") or "") not in notified)
    print(
        f"Prepared {len(queued)} substantive item(s) for ChatGPT; "
        f"{unnotified_count} queued item(s) are not yet notified; "
        f"{len(missing)} total substantive item(s) still lack ChatGPT summaries."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
