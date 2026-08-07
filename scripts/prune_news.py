#!/usr/bin/env python3
"""Remove low-value market commentary after collection and rebuild knowledge files."""

from __future__ import annotations

import json
from pathlib import Path

import email_dispatch
import fetch_news

ROOT = Path(__file__).resolve().parents[1]
NEWS_PATH = ROOT / "data" / "news.json"

SUPPLEMENTAL_LOW_VALUE_PATTERNS = (
    "stock outperforms competitors",
    "stock underperforms competitors",
    "strong trading day",
    "weak trading day",
    "shares outperform competitors",
    "shares underperform competitors",
    "stock gains on the day",
    "stock drops on the day",
    "stock closes higher",
    "stock closes lower",
    "shares close higher",
    "shares close lower",
)


def should_remove(item: dict) -> bool:
    if email_dispatch.is_low_value(item):
        return True
    title = str(item.get("title") or "").lower()
    return any(pattern in title for pattern in SUPPLEMENTAL_LOW_VALUE_PATTERNS)


def main() -> int:
    payload = json.loads(NEWS_PATH.read_text(encoding="utf-8"))
    original_items = list(payload.get("items") or [])
    retained = [item for item in original_items if not should_remove(item)]
    removed = [item for item in original_items if should_remove(item)]

    payload["items"] = retained
    payload["item_count"] = len(retained)
    payload["low_value_items_removed"] = len(removed)
    payload["filter_policy"] = (
        "Excludes valuation articles, stock-price/trading-day commentary, investment listicles, "
        "and multi-company market roundups without a direct transaction or partnership."
    )

    NEWS_PATH.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    fetch_news.write_knowledge_files(retained, payload)

    print(f"Pruned {len(removed)} low-value records; published {len(retained)} substantive competitor events.")
    for item in removed[:12]:
        print(f"  removed: {item.get('title', 'Untitled')}")
    if len(removed) > 12:
        print(f"  ... and {len(removed) - 12} more")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
