#!/usr/bin/env python3
"""Remove low-value market commentary, merge obvious event duplicates, and rebuild knowledge files."""

from __future__ import annotations

import json
import re
from datetime import datetime
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
    "stock analysis",
    "risk assessment",
    "investor sentiment",
    "financial health",
    "growth potential",
    "bull case",
    "bear case",
    "looks cheap",
    "looks expensive",
)

QUARTER_PATTERNS = {
    "Q1": ("q1", "first quarter", "1st quarter"),
    "Q2": ("q2", "second quarter", "2nd quarter", "six months"),
    "Q3": ("q3", "third quarter", "3rd quarter", "nine months"),
    "Q4": ("q4", "fourth quarter", "4th quarter", "full year", "full-year"),
}
FINANCIAL_EVENT_TERMS = (
    "earnings", "results", "revenue", "profit", "eps", "guidance", "outlook",
    "earnings call", "transcript", "presentation", "quarterly", "financial results",
)


def should_remove(item: dict) -> bool:
    if email_dispatch.is_low_value(item):
        return True
    title = str(item.get("title") or "").lower()
    return any(pattern in title for pattern in SUPPLEMENTAL_LOW_VALUE_PATTERNS)


def published_date(item: dict) -> datetime | None:
    value = str(item.get("published_at") or "")
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except Exception:
        return None


def financial_event_key(item: dict) -> tuple[str, str, str] | None:
    if str(item.get("category") or "") != "Financials":
        return None
    text = f"{item.get('title', '')} {item.get('description', '')}".lower()
    if not any(term in text for term in FINANCIAL_EVENT_TERMS):
        return None
    quarter = next((label for label, patterns in QUARTER_PATTERNS.items() if any(pattern in text for pattern in patterns)), None)
    if not quarter:
        return None
    year_match = re.search(r"\b20\d{2}\b", text)
    year = year_match.group(0) if year_match else str((published_date(item) or datetime.now()).year)
    return str(item.get("company") or ""), year, quarter


def merge_source_lists(primary: dict, duplicate: dict) -> None:
    combined = list(primary.get("sources") or []) + list(duplicate.get("sources") or [])
    seen: set[str] = set()
    merged: list[dict] = []
    for source in combined:
        if not isinstance(source, dict):
            continue
        url = str(source.get("url") or "").strip()
        name = str(source.get("name") or "Source").strip()
        key = url or name
        if not key or key in seen:
            continue
        seen.add(key)
        merged.append(source)
    primary["sources"] = merged
    primary["coverage_count"] = len(merged) or int(primary.get("coverage_count") or 1)
    if len(str(duplicate.get("description") or "")) > len(str(primary.get("description") or "")):
        primary["description"] = duplicate.get("description")
    if duplicate.get("official_source"):
        primary["official_source"] = True


def merge_financial_duplicates(items: list[dict]) -> tuple[list[dict], int]:
    clusters: dict[tuple[str, str, str], dict] = {}
    passthrough: list[dict] = []
    merged_count = 0

    # Oldest first so the first published report becomes the canonical event, while later coverage is retained as sources.
    ordered = sorted(items, key=lambda item: str(item.get("published_at") or ""))
    for item in ordered:
        key = financial_event_key(item)
        if key is None:
            passthrough.append(item)
            continue
        existing = clusters.get(key)
        if existing is None:
            clusters[key] = item
            continue
        first_date = published_date(existing)
        next_date = published_date(item)
        if first_date and next_date and abs((next_date - first_date).days) > 10:
            passthrough.append(item)
            continue
        merge_source_lists(existing, item)
        merged_count += 1

    retained = passthrough + list(clusters.values())
    retained.sort(key=lambda item: str(item.get("published_at") or ""), reverse=True)
    return retained, merged_count


def main() -> int:
    payload = json.loads(NEWS_PATH.read_text(encoding="utf-8"))
    original_items = list(payload.get("items") or [])
    filtered = [item for item in original_items if not should_remove(item)]
    removed = [item for item in original_items if should_remove(item)]
    retained, financial_duplicates = merge_financial_duplicates(filtered)

    payload["items"] = retained
    payload["item_count"] = len(retained)
    payload["low_value_items_removed"] = len(removed)
    payload["post_prune_financial_duplicates_merged"] = financial_duplicates
    payload["filter_policy"] = (
        "Excludes valuation articles, stock-price/trading-day commentary, investment listicles, "
        "and multi-company market roundups without a direct transaction or partnership. "
        "Quarterly earnings/results/guidance coverage for the same company and quarter is consolidated into one event with multiple source links."
    )

    NEWS_PATH.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    fetch_news.write_knowledge_files(retained, payload)

    print(
        f"Pruned {len(removed)} low-value records; merged {financial_duplicates} duplicate quarterly financial records; "
        f"published {len(retained)} substantive competitor events."
    )
    for item in removed[:12]:
        print(f"  removed: {item.get('title', 'Untitled')}")
    if len(removed) > 12:
        print(f"  ... and {len(removed) - 12} more")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
