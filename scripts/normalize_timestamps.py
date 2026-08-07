#!/usr/bin/env python3
"""Normalize user-visible repository timestamps to India Standard Time."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import fetch_news
from time_utils import format_datetime_ist, next_scheduled_ist, to_ist

ROOT = Path(__file__).resolve().parents[1]
NEWS_PATH = ROOT / "data" / "news.json"


def parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def main() -> int:
    payload = json.loads(NEWS_PATH.read_text(encoding="utf-8"))
    generated = parse_iso(str(payload.get("generated_at") or datetime.now().astimezone().isoformat()))
    generated_ist = to_ist(generated)
    next_update = next_scheduled_ist(generated_ist)

    payload["generated_at"] = generated_ist.isoformat()
    payload["generated_at_display"] = format_datetime_ist(generated_ist)
    payload["next_update_at"] = next_update.isoformat()
    payload["next_update_display"] = format_datetime_ist(next_update)
    payload["display_timezone"] = "IST"

    NEWS_PATH.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    fetch_news.write_knowledge_files(list(payload.get("items") or []), payload)
    print(f"Normalized repository timestamps to IST; next update {payload['next_update_display']}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
