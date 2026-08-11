#!/usr/bin/env python3
"""Merge verified ChatGPT Scheduled Task summaries into data/news.json."""

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


def verified_chatgpt_entry(entry: object, payload_provider: str) -> tuple[str, str, list]:
    if not isinstance(entry, dict):
        return "", "", []
    provider = str(entry.get("provider") or payload_provider or "").strip()
    verification = str(entry.get("verification") or "").strip().lower()
    if provider != "ChatGPT Scheduled Task" or verification != "verified":
        return "", "", []
    summary = clean(entry.get("summary") or entry.get("text") or "")
    updated_at = str(entry.get("updated_at") or "")
    evidence = entry.get("evidence") or entry.get("sources_used") or []
    return summary, updated_at, evidence if isinstance(evidence, list) else []


def main() -> int:
    repository = load(NEWS, {})
    payload = load(SUMMARIES, {"summaries": {}})
    summaries = payload.get("summaries") if isinstance(payload, dict) else {}
    payload_provider = str(payload.get("provider") or "") if isinstance(payload, dict) else ""
    if not isinstance(summaries, dict):
        summaries = {}

    applied = 0
    skipped_unverified = 0
    for item in repository.get("items") or []:
        identifier = str(item.get("id") or "")
        entry = summaries.get(identifier)
        summary, updated_at, evidence = verified_chatgpt_entry(entry, payload_provider)
        if not summary:
            if entry is not None:
                skipped_unverified += 1
            continue
        if not item.get("source_description"):
            item["source_description"] = str(item.get("description") or "")
        item["description"] = summary
        item["chatgpt_summary"] = summary
        item["summary_provider"] = "ChatGPT Scheduled Task"
        item["summary_verification"] = "verified"
        if updated_at:
            item["summary_updated_at"] = updated_at
        if evidence:
            item["summary_evidence"] = evidence
        applied += 1

    NEWS.write_text(json.dumps(repository, indent=2, ensure_ascii=False), encoding="utf-8")
    print(
        f"Applied {applied} verified ChatGPT summary/summaries to the current news dataset; "
        f"skipped {skipped_unverified} non-verified cached entrie(s)."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
