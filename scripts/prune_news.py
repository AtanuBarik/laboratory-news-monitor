#!/usr/bin/env python3
"""Remove low-value market commentary after collection and rebuild knowledge files."""

from __future__ import annotations

import json
from pathlib import Path

import email_dispatch
import fetch_news

ROOT = Path(__file__).resolve().parents[1]
NEWS_PATH = ROOT / "data" / "news.json"


def main() -> int:
    payload = json.loads(NEWS_PATH.read_text(encoding="utf-8"))
    original_items = list(payload.get("items") or [])
    retained = [item for item in original_items if not email_dispatch.is_low_value(item)]
    removed = [item for item in original_items if email_dispatch.is_low_value(item)]

    payload["items"] = retained
    payload["item_count"] = len(retained)
    payload["low_value_items_removed"] = len(removed)
    payload["filter_policy"] = (
        "Excludes valuation articles, stock-price commentary, investment listicles, "
        "and multi-company market roundups without a direct transaction or partnership."
    )

    NEWS_PATH.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    fetch_news.write_knowledge_files(retained, payload)

    print(
        f"Pruned {len(removed)} low-value records; "
        f"published {len(retained)} substantive competitor events."
    )
    for item in removed[:10]:
        print(f"  removed: {item.get('title', 'Untitled')}")
    if len(removed) > 10:
        print(f"  ... and {len(removed) - 10} more")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
