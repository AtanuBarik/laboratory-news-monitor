#!/usr/bin/env python3
"""Merge cached ChatGPT Scheduled Task summaries into data/news.json."""

from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
NEWS = ROOT / "data/news.json"
SUMMARIES = ROOT / "data/chatgpt_summaries.json"

FORBIDDEN = (
    "repository evidence",
    "identified across",
    "separate reports",
    "coverage count",
    "unable to access",
    "cannot access",
    "insufficient information",
    "most important follow-up is to confirm",
)


def load(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def clean(value: str) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if len(text.split()) < 80:
        return ""
    lowered = text.lower()
    if any(phrase in lowered for phrase in FORBIDDEN):
        return ""
    return text


def main() -> int:
    repository = load(NEWS, {})
    payload = load(SUMMARIES, {"summaries": {}})
    summaries = payload.get("summaries") if isinstance(payload, dict) else {}
    if not isinstance(summaries, dict):
        summaries = {}

    applied = 0
    for item in repository.get("items") or []:
        identifier = str(item.get("id") or "")
        entry = summaries.get(identifier)
        if isinstance(entry, dict):
            summary = clean(entry.get("summary") or entry.get("text") or "")
            updated_at = str(entry.get("updated_at") or payload.get("updated_at") or "")
            evidence = entry.get("evidence") or entry.get("sources_used") or []
        else:
            summary = clean(entry or "")
            updated_at = str(payload.get("updated_at") or "")
            evidence = []
        if not summary:
            continue
        if not item.get("source_description"):
            item["source_description"] = str(item.get("description") or "")
        item["description"] = summary
        item["chatgpt_summary"] = summary
        item["summary_provider"] = "ChatGPT Scheduled Task"
        if updated_at:
            item["summary_updated_at"] = updated_at
        if evidence:
            item["summary_evidence"] = evidence
        applied += 1

    NEWS.write_text(json.dumps(repository, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Applied {applied} cached ChatGPT summary/summaries to the current news dataset.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
