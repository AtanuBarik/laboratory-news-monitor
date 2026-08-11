#!/usr/bin/env python3
"""Email dispatcher that sends new alerts only with verified cached ChatGPT summaries."""

from __future__ import annotations

from typing import Any

import email_dispatch as base
import email_new_items as core

# Email alerts are ChatGPT-summary-required. The scheduled ChatGPT task writes
# verified summaries into data/chatgpt_summaries.json; apply_chatgpt_summaries.py
# merges only verified ChatGPT entries into data/news.json before this dispatcher runs.
# Articles without a verified cached ChatGPT summary remain unnotified and therefore
# stay eligible for a later run after their summary has been prepared.
core.WORKER = ""
base.GEMINI_QUOTA_EXHAUSTED = False
base.GEMINI_QUOTA_MESSAGE = ""


def cached_chatgpt_summary(item: dict[str, Any]) -> str:
    provider = str(item.get("summary_provider") or "").strip()
    verification = str(item.get("summary_verification") or "").strip().lower()
    if provider != "ChatGPT Scheduled Task" or verification != "verified":
        return ""
    return core.clean_summary(str(item.get("chatgpt_summary") or ""))


def collect_chatgpt_only(
    candidates: list[dict[str, Any]],
    target: int,
    max_attempts: int,
) -> tuple[list[dict[str, Any]], dict[str, str], list[str], int]:
    selected: list[dict[str, Any]] = []
    summary_map: dict[str, str] = {}
    awaiting: list[str] = []
    attempted = 0
    limit = min(len(candidates), max_attempts)

    for item in candidates[:limit]:
        if len(selected) >= target:
            break
        attempted += 1
        identifier = core.item_id(item)
        summary = cached_chatgpt_summary(item)
        if summary:
            selected.append(item)
            summary_map[identifier] = summary
            print(f"Using verified scheduled ChatGPT summary for {identifier}: {len(summary.split())} words.")
        elif identifier:
            awaiting.append(identifier)

    base.HYBRID_CHATGPT_COUNT = len(selected)
    base.HYBRID_GEMINI_COUNT = 0
    base.AWAITING_CHATGPT_IDS = awaiting
    print(
        f"ChatGPT-only email selection: {len(selected)} ready of {min(len(candidates), target)} requested; "
        f"{len(awaiting)} candidate(s) remain queued until a verified ChatGPT summary is available."
    )
    return selected, summary_map, awaiting, attempted


base.collect_verified = collect_chatgpt_only

# Make summary provenance explicit in both HTML and plain-text email alerts.
_original_article_html = core.article_html
_original_build_email = core.build_email


def chatgpt_article_html(item: dict[str, Any], summary: str) -> str:
    rendered = _original_article_html(item, summary)
    marker = '<div style="margin-top:11px;">'
    badge = (
        '<div style="margin-top:11px;margin-bottom:8px;font-family:Arial,sans-serif;'
        'font-size:10px;line-height:14px;font-weight:800;letter-spacing:.5px;color:#35792A;">'
        'CHATGPT SUMMARY</div><div>'
    )
    return rendered.replace(marker, badge, 1)


def chatgpt_build_email(items: list[dict], summaries: dict[str, str], is_test: bool, now):
    subject, plain, html_body = _original_build_email(items, summaries, is_test, now)
    plain = (
        subject
        + "\n\nEvery article in this alert includes a verified summary prepared by the scheduled ChatGPT workflow.\n\n"
        + plain[len(subject):].lstrip("\n")
    )
    html_body = html_body.replace(
        "COMPANY-WISE BREAKDOWN",
        "CHATGPT-SUMMARIZED NEW ARTICLES"
        '<div style="margin-top:5px;font-family:Arial,sans-serif;font-size:11px;line-height:16px;font-weight:400;color:#646464;">'
        "Every article below includes a verified summary prepared by the scheduled ChatGPT workflow.</div>",
        1,
    )
    return subject, plain, html_body


core.article_html = chatgpt_article_html
core.build_email = chatgpt_build_email


def main() -> int:
    result = base.main()
    try:
        status = core.load(core.STATUS, {})
        chatgpt_count = int(getattr(base, "HYBRID_CHATGPT_COUNT", 0))
        awaiting = list(getattr(base, "AWAITING_CHATGPT_IDS", []))
        status["scheduled_chatgpt_summary_count"] = chatgpt_count
        status["gemini_summary_count"] = 0
        status["gemini_fallback_enabled"] = False
        status["fallback_summary_count"] = 0
        status["summary_delivery_policy"] = "verified_chatgpt_required"
        status["awaiting_chatgpt_summary_count"] = len(awaiting)
        status["awaiting_chatgpt_summary_ids"] = awaiting[:80]
        if chatgpt_count:
            status["primary_summary_provider"] = "ChatGPT Scheduled Task"
        elif status.get("ranked_candidate_count", 0) or awaiting:
            status["primary_summary_provider"] = "Awaiting ChatGPT Scheduled Task"
            if status.get("status") in {"no_summarizable_items", "no_new_items"} and awaiting:
                status["status"] = "awaiting_chatgpt_summaries"
        core.save(core.STATUS, status)
    except Exception as exc:
        print(f"Could not append ChatGPT-only provider status metadata: {exc}")
    return result


if __name__ == "__main__":
    raise SystemExit(main())
