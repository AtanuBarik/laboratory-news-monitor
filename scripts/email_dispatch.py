#!/usr/bin/env python3
"""Select substantive competitor events and dispatch the branded email report."""

from __future__ import annotations

import json
import os
import re
import urllib.request
from typing import Any

import email_new_items as core
from time_utils import format_datetime_ist, now_ist

core.SUMMARY_WORKERS = 2
TEST_TARGET = 5
PRODUCTION_TARGET = 40
WAVE_SIZE = 5
MAX_TEST_ATTEMPTS = 30
MAX_PRODUCTION_ATTEMPTS = 80

LOW_VALUE_TITLE_PATTERNS = (
    "fully valued", "undervalued", "overvalued", "a bargain", "good value", "fair value",
    "valuation", "stock rises", "stock falls", "stock slides", "stock slips", "stock surges",
    "shares rise", "shares fall", "shares slide", "shares slip", "investors ask",
    "investors keep an eye", "investor radar", "long-term potential", "prominent name",
    "should you buy", "is it time to buy", "price target", "insiders sold", "shorts surging",
    "market sell-off", "sector drag", "navigating the sector", "latest move", "outperforms market",
    "underperforms market", "ratings for", "analyst questions from",
)
LOW_VALUE_SOURCE_DOMAINS = (
    "simplywall.st", "kalkine.com.au", "marketbeat.com", "defenseworld.net", "etfdailynews.com",
    "tickerreport.com", "americanbankingnews.com", "stocktitan.net", "moomoo.com",
)
STRONG_EVENT_TERMS = (
    "launch", "introduc", "test", "assay", "diagnostic", "panel", "biomarker",
    "companion diagnostic", "fda", "approval", "clearance", "clinical", "study", "research",
    "trial", "results", "earnings", "revenue", "profit", "margin", "guidance", "10-k", "10-q",
    "8-k", "annual report", "acquire", "acquisition", "merger", "partnership", "partners with",
    "collaboration", "agreement", "contract", "appoint", "named ceo", "named cfo", "chief executive",
    "chief financial", "restructur", "new facility", "opens lab", "expands lab", "settlement",
    "regulatory", "reimbursement", "service", "platform", "screening",
)
MONITORED_ALIASES = {
    "Labcorp": ("labcorp", "laboratory corporation of america"),
    "Quest Diagnostics": ("quest diagnostics",),
    "ARUP Laboratories": ("arup laboratories", "arup labs"),
    "Mayo Clinic Laboratories": ("mayo clinic laboratories", "mayo clinic labs"),
    "Sonic Healthcare": ("sonic healthcare", "sonic reference laboratory"),
}
MULTI_COMPANY_EVENT_TERMS = (
    "acquire", "acquisition", "merger", "partnership", "partners with", "collaboration",
    "agreement", "joint venture", "contract",
)
CATEGORY_SCORE = {
    "Product & Services": 10,
    "Clinical, R&D": 10,
    "Partnership, M&A": 11,
    "Financials": 9,
    "Organizational Updates": 8,
    "Leadership Changes": 8,
    "Other": 2,
}
FORBIDDEN_SUMMARY_PHRASES = core.FORBIDDEN_SUMMARY_PHRASES


def normalized_text(item: dict[str, Any]) -> str:
    return re.sub(r"\s+", " ", f"{item.get('title', '')} {item.get('description', '')} {item.get('source', '')}".lower()).strip()


def source_domain(item: dict[str, Any]) -> str:
    return str(item.get("source_domain") or "").lower().removeprefix("www.")


def mentioned_companies(title: str) -> set[str]:
    lowered = title.lower()
    return {company for company, aliases in MONITORED_ALIASES.items() if any(alias in lowered for alias in aliases)}


def is_low_value(item: dict[str, Any]) -> bool:
    title = str(item.get("title") or "").lower()
    text = normalized_text(item)
    domain = source_domain(item)
    if any(pattern in title for pattern in LOW_VALUE_TITLE_PATTERNS):
        return True
    if any(domain == blocked or domain.endswith("." + blocked) for blocked in LOW_VALUE_SOURCE_DOMAINS):
        return True
    companies = mentioned_companies(title)
    if len(companies) > 1 and not any(term in text for term in MULTI_COMPANY_EVENT_TERMS):
        return True
    if str(item.get("category") or "Other") == "Other" and not any(term in text for term in STRONG_EVENT_TERMS):
        return True
    return False


def quality_score(item: dict[str, Any]) -> tuple[int, str]:
    text = normalized_text(item)
    score = CATEGORY_SCORE.get(str(item.get("category") or "Other"), 0)
    if item.get("official_source"):
        score += 18
    score += min(int(item.get("coverage_count") or 1), 4)
    score += sum(2 for term in STRONG_EVENT_TERMS if term in text)
    if len(str(item.get("description") or "")) >= 180:
        score += 3
    return score, str(item.get("published_at") or "")


def rank_candidates(items: list[dict[str, Any]], excluded_ids: set[str]) -> tuple[list[dict[str, Any]], list[str]]:
    substantive: list[dict[str, Any]] = []
    dismissed: list[str] = []
    for item in items:
        identifier = core.item_id(item)
        if not identifier or identifier in excluded_ids:
            continue
        if is_low_value(item):
            dismissed.append(identifier)
        else:
            substantive.append(item)
    substantive.sort(key=quality_score, reverse=True)
    return substantive, dismissed


def print_worker_diagnostics(identifier: str, result: dict[str, Any]) -> None:
    print(f"Summary rejected for {identifier}: {result.get('error') or 'content_verified was false'}")
    for attempt in (result.get("diagnostic_attempts") or [])[-10:]:
        if not isinstance(attempt, dict):
            continue
        if attempt.get("stage") in {"direct_page_retrieval", "publisher_search_resolution"}:
            print("  retrieval:", json.dumps(attempt, ensure_ascii=False)[:900])
        else:
            print("  AI attempt:", attempt.get("api"), attempt.get("model"), attempt.get("tools", "none"), attempt.get("status"), str(attempt.get("error") or "")[:220])


def diagnostic_request_summary(item: dict[str, Any]) -> str:
    identifier = core.item_id(item)
    if not core.WORKER or not identifier:
        return ""
    request = urllib.request.Request(
        core.WORKER,
        data=json.dumps({"mode": "email_article_summary", "article_ids": [identifier]}).encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/json", "Accept": "application/json", "User-Agent": "QuestCompetitorUpdates/6.0"},
    )
    try:
        with urllib.request.urlopen(request, timeout=240) as response:
            result = json.loads(response.read().decode("utf-8"))
    except Exception as exc:
        print(f"Summary HTTP request failed for {identifier}: {exc}")
        return ""
    if result.get("content_verified") is not True:
        print_worker_diagnostics(identifier, result)
        return ""
    summary = core.clean_summary(str(result.get("answer") or ""))
    if not summary:
        print(f"Summary rejected locally for {identifier}: {len(str(result.get('answer') or '').split())} words or prohibited wording.")
        return ""
    print(f"Summary verified for {identifier}: {len(summary.split())} words; evidence={result.get('evidence_mode')}; model={result.get('model')}.")
    return summary


core.request_summary = diagnostic_request_summary


def collect_verified(candidates: list[dict[str, Any]], target: int, max_attempts: int) -> tuple[list[dict[str, Any]], dict[str, str], list[str], int]:
    selected: list[dict[str, Any]] = []
    summary_map: dict[str, str] = {}
    unreadable: list[str] = []
    attempted = 0
    limit = min(len(candidates), max_attempts)
    while len(selected) < target and attempted < limit:
        wave = candidates[attempted : min(attempted + WAVE_SIZE, limit)]
        attempted += len(wave)
        print("Summary wave:", ", ".join(f"{core.item_id(item)} ({item.get('title', '')[:70]})" for item in wave))
        wave_summaries = core.verified_summaries(wave)
        for item in wave:
            identifier = core.item_id(item)
            summary = wave_summaries.get(identifier)
            if summary and len(selected) < target:
                selected.append(item)
                summary_map[identifier] = summary
            elif not summary:
                unreadable.append(identifier)
        print(f"Wave result: {len(wave_summaries)} verified; report now has {len(selected)} of {target} requested alerts.")
    return selected, summary_map, unreadable, attempted


def status_payload(status: str, **extra: Any) -> dict[str, Any]:
    now = now_ist()
    return {"checked_at": now.isoformat(), "checked_at_display": format_datetime_ist(now), "status": status, **extra}


def main() -> int:
    repository = core.load(core.NEWS, {})
    items = sorted(repository.get("items") or [], key=lambda item: str(item.get("published_at") or ""), reverse=True)
    current_ids = {core.item_id(item) for item in items if core.item_id(item)}
    state_exists = core.STATE.exists()
    state = core.load(core.STATE, {"notified_ids": [], "dismissed_ids": []})
    notified_ids = set(state.get("notified_ids") or [])
    dismissed_ids = set(state.get("dismissed_ids") or [])
    if not state_exists:
        state = {"initialized_at": now_ist().isoformat(), "notified_ids": sorted(current_ids), "dismissed_ids": []}
        core.save(core.STATE, state)
        notified_ids = current_ids

    is_test = core.truthy("SEND_TEST_EMAIL")
    excluded = dismissed_ids if is_test else dismissed_ids | notified_ids
    ranked, newly_dismissed = rank_candidates(items, excluded)
    dismissed_ids.update(newly_dismissed)
    state["dismissed_ids"] = sorted(dismissed_ids)

    if not ranked:
        core.save(core.STATE, state)
        core.save(core.STATUS, status_payload("no_new_items", ranked_candidate_count=0, low_value_dismissed_count=len(newly_dismissed)))
        print("No new substantive competitor events require an email.")
        return 0

    target = TEST_TARGET if is_test else PRODUCTION_TARGET
    max_attempts = MAX_TEST_ATTEMPTS if is_test else MAX_PRODUCTION_ATTEMPTS
    sendable, summary_map, unreadable_ids, attempted = collect_verified(ranked, target, max_attempts)
    if not sendable:
        core.save(core.STATE, state)
        core.save(core.STATUS, status_payload("no_summarizable_items", ranked_candidate_count=len(ranked), low_value_dismissed_count=len(newly_dismissed), attempted_summary_count=attempted, verified_summary_count=0, unreadable_ids=unreadable_ids))
        print("No verified summaries were produced after scanning substantive candidates; no email sent. Unreadable items remain eligible for later retry.")
        return 0

    to_addresses = core.env_list("EMAIL_TO")
    bcc_addresses = [address for address in core.env_list("EMAIL_BCC") if address.lower() not in {value.lower() for value in to_addresses}]
    from_address = os.getenv("EMAIL_FROM") or os.getenv("SMTP_USERNAME", "")
    sender_name = os.getenv("EMAIL_SENDER_NAME", "Quest Updates")
    if not to_addresses or not from_address:
        core.save(core.STATUS, status_payload("email_configuration_missing", email_item_count=len(sendable)))
        print("EMAIL_TO or EMAIL_FROM is missing; collected news remains stored and unnotified.")
        return 0

    subject, plain, html_body = core.build_email(sendable, summary_map, is_test, now_ist())
    try:
        core.send_email(subject, plain, html_body, to_addresses, bcc_addresses, from_address, sender_name)
    except Exception as exc:
        core.save(core.STATUS, status_payload("email_failed", email_item_count=len(sendable), verified_summary_count=len(summary_map), error=str(exc)))
        print(f"Email send failed: {exc}. No IDs were marked notified; the next run can retry them.")
        return 0

    if not is_test:
        notified_ids.update(core.item_id(item) for item in sendable)
        state["notified_ids"] = sorted(notified_ids)
        state["last_successful_email_at"] = now_ist().isoformat()
        state["last_successful_email_count"] = len(sendable)
    core.save(core.STATE, state)
    core.save(core.STATUS, status_payload("test_email_sent" if is_test else "email_sent", ranked_candidate_count=len(ranked), low_value_dismissed_count=len(newly_dismissed), attempted_summary_count=attempted, email_item_count=len(sendable), verified_summary_count=len(summary_map), unreadable_count=len(unreadable_ids), unreadable_ids=unreadable_ids, to_count=len(to_addresses), bcc_count=len(bcc_addresses), subject=subject, selection_strategy="quality_ranked_waves", display_timezone="IST"))
    print(f"Sent {len(sendable)} substantive alerts after scanning {attempted} ranked candidates.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
