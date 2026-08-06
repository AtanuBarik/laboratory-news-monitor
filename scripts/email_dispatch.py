#!/usr/bin/env python3
"""Select substantive competitor events and dispatch the branded email report.

This wrapper reuses the existing email rendering and SMTP functions, but avoids
an all-or-nothing failure when the newest feed entries are valuation articles,
stock commentary, or multi-company market roundups. It scans ranked candidates
in small waves until it has enough verified article-content summaries.
"""

from __future__ import annotations

import os
import re
from datetime import datetime, timezone
from typing import Any

import email_new_items as core

# Stay below common free-tier burst limits.
core.SUMMARY_WORKERS = 2

TEST_TARGET = 5
PRODUCTION_TARGET = 40
WAVE_SIZE = 5
MAX_TEST_ATTEMPTS = 25
MAX_PRODUCTION_ATTEMPTS = 60

LOW_VALUE_TITLE_PATTERNS = (
    "fully valued",
    "undervalued",
    "overvalued",
    "a bargain",
    "good value",
    "fair value",
    "valuation",
    "stock rises",
    "stock falls",
    "stock slides",
    "stock slips",
    "stock surges",
    "shares rise",
    "shares fall",
    "shares slide",
    "shares slip",
    "investors ask",
    "investors keep an eye",
    "investor radar",
    "long-term potential",
    "prominent name",
    "should you buy",
    "is it time to buy",
    "price target",
    "insiders sold",
    "shorts surging",
    "market sell-off",
    "sector drag",
    "navigating the sector",
    "latest move",
    "outperforms market",
    "underperforms market",
)

LOW_VALUE_SOURCE_DOMAINS = (
    "simplywall.st",
    "kalkine.com.au",
    "marketbeat.com",
    "defenseworld.net",
    "etfdailynews.com",
    "tickerreport.com",
    "americanbankingnews.com",
    "stocktitan.net",
)

STRONG_EVENT_TERMS = (
    "launch",
    "introduc",
    "new test",
    "new assay",
    "fda",
    "approval",
    "clearance",
    "clinical",
    "study",
    "research",
    "trial",
    "results",
    "earnings",
    "revenue",
    "profit",
    "margin",
    "guidance",
    "10-k",
    "10-q",
    "8-k",
    "annual report",
    "acquire",
    "acquisition",
    "merger",
    "partnership",
    "partners with",
    "collaboration",
    "agreement",
    "contract",
    "appoint",
    "named ceo",
    "named cfo",
    "chief executive",
    "chief financial",
    "restructur",
    "new facility",
    "opens lab",
    "expands lab",
    "settlement",
    "regulatory",
    "reimbursement",
)

MONITORED_ALIASES = {
    "Labcorp": ("labcorp", "laboratory corporation of america"),
    "Quest Diagnostics": ("quest diagnostics",),
    "ARUP Laboratories": ("arup laboratories", "arup labs"),
    "Mayo Clinic Laboratories": ("mayo clinic laboratories", "mayo clinic labs"),
    "Sonic Healthcare": ("sonic healthcare", "sonic reference laboratory"),
}

MULTI_COMPANY_EVENT_TERMS = (
    "acquire",
    "acquisition",
    "merger",
    "partnership",
    "partners with",
    "collaboration",
    "agreement",
    "joint venture",
    "contract",
)

CATEGORY_SCORE = {
    "Product & Services": 8,
    "Clinical, R&D": 8,
    "Partnership, M&A": 9,
    "Financials": 7,
    "Organizational Updates": 7,
    "Leadership Changes": 7,
    "Other": 1,
}


def normalized_text(item: dict[str, Any]) -> str:
    return re.sub(
        r"\s+",
        " ",
        f"{item.get('title', '')} {item.get('description', '')} {item.get('source', '')}".lower(),
    ).strip()


def source_domain(item: dict[str, Any]) -> str:
    return str(item.get("source_domain") or "").lower().removeprefix("www.")


def mentioned_companies(title: str) -> set[str]:
    lowered = title.lower()
    return {
        company
        for company, aliases in MONITORED_ALIASES.items()
        if any(alias in lowered for alias in aliases)
    }


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

    if str(item.get("category") or "Other") == "Other" and not any(
        term in text for term in STRONG_EVENT_TERMS
    ):
        return True

    return False


def quality_score(item: dict[str, Any]) -> tuple[int, str]:
    text = normalized_text(item)
    score = CATEGORY_SCORE.get(str(item.get("category") or "Other"), 0)
    if item.get("official_source"):
        score += 15
    score += min(int(item.get("coverage_count") or 1), 4)
    score += sum(2 for term in STRONG_EVENT_TERMS if term in text)
    if len(str(item.get("description") or "")) >= 180:
        score += 3
    return score, str(item.get("published_at") or "")


def rank_candidates(items: list[dict[str, Any]], excluded_ids: set[str]) -> tuple[list[dict[str, Any]], list[str]]:
    substantive: list[dict[str, Any]] = []
    dismissed_ids: list[str] = []
    for item in items:
        identifier = core.item_id(item)
        if not identifier or identifier in excluded_ids:
            continue
        if is_low_value(item):
            dismissed_ids.append(identifier)
            continue
        substantive.append(item)
    substantive.sort(key=quality_score, reverse=True)
    return substantive, dismissed_ids


def collect_verified(
    candidates: list[dict[str, Any]],
    target: int,
    max_attempts: int,
) -> tuple[list[dict[str, Any]], dict[str, str], list[str]]:
    selected: list[dict[str, Any]] = []
    summaries: dict[str, str] = {}
    unreadable_ids: list[str] = []

    attempted = 0
    while len(selected) < target and attempted < min(len(candidates), max_attempts):
        wave = candidates[attempted : attempted + WAVE_SIZE]
        if not wave:
            break
        attempted += len(wave)
        print(
            "Summary wave:",
            ", ".join(f"{core.item_id(item)} ({item.get('title', '')[:70]})" for item in wave),
        )
        wave_summaries = core.verified_summaries(wave)
        for item in wave:
            identifier = core.item_id(item)
            summary = wave_summaries.get(identifier)
            if summary and len(selected) < target:
                selected.append(item)
                summaries[identifier] = summary
            elif not summary:
                unreadable_ids.append(identifier)
        print(
            f"Wave result: {len(wave_summaries)} verified; "
            f"report now has {len(selected)} of {target} requested alerts."
        )

    return selected, summaries, unreadable_ids


def main() -> int:
    repository = core.load(core.NEWS, {})
    items = sorted(
        repository.get("items") or [],
        key=lambda item: str(item.get("published_at") or ""),
        reverse=True,
    )
    current_ids = {core.item_id(item) for item in items if core.item_id(item)}
    state_exists = core.STATE.exists()
    state = core.load(core.STATE, {"notified_ids": [], "dismissed_ids": []})
    notified_ids = set(state.get("notified_ids") or [])
    dismissed_ids = set(state.get("dismissed_ids") or [])

    if not state_exists:
        state = {
            "initialized_at": datetime.now(timezone.utc).isoformat(),
            "notified_ids": sorted(current_ids),
            "dismissed_ids": [],
        }
        core.save(core.STATE, state)
        notified_ids = current_ids

    is_test = core.truthy("SEND_TEST_EMAIL")
    excluded_ids = dismissed_ids if is_test else dismissed_ids | notified_ids
    ranked, newly_dismissed = rank_candidates(items, excluded_ids)

    target = TEST_TARGET if is_test else PRODUCTION_TARGET
    max_attempts = MAX_TEST_ATTEMPTS if is_test else MAX_PRODUCTION_ATTEMPTS
    sendable, summary_map, unreadable_ids = collect_verified(ranked, target, max_attempts)

    dismissed_ids.update(newly_dismissed)
    state["dismissed_ids"] = sorted(dismissed_ids)

    if not sendable:
        core.save(core.STATE, state)
        core.save(
            core.STATUS,
            {
                "checked_at": datetime.now(timezone.utc).isoformat(),
                "status": "no_summarizable_items",
                "ranked_candidate_count": len(ranked),
                "low_value_dismissed_count": len(newly_dismissed),
                "attempted_summary_count": min(len(ranked), max_attempts),
                "verified_summary_count": 0,
                "unreadable_ids": unreadable_ids,
            },
        )
        print(
            "No verified summaries were produced after scanning substantive candidates; "
            "no email sent."
        )
        return 0

    to_addresses = core.env_list("EMAIL_TO")
    bcc_addresses = [
        address
        for address in core.env_list("EMAIL_BCC")
        if address.lower() not in {value.lower() for value in to_addresses}
    ]
    from_address = os.getenv("EMAIL_FROM") or os.getenv("SMTP_USERNAME", "")
    sender_name = os.getenv("EMAIL_SENDER_NAME", "Quest Updates")
    if not to_addresses or not from_address:
        raise RuntimeError("EMAIL_TO or EMAIL_FROM is missing.")

    subject, plain, html_body = core.build_email(
        sendable,
        summary_map,
        is_test,
        datetime.now(timezone.utc),
    )
    core.send_email(
        subject,
        plain,
        html_body,
        to_addresses,
        bcc_addresses,
        from_address,
        sender_name,
    )

    if not is_test:
        notified_ids.update(core.item_id(item) for item in sendable)
        state["notified_ids"] = sorted(notified_ids)
        state["last_successful_email_at"] = datetime.now(timezone.utc).isoformat()
        state["last_successful_email_count"] = len(sendable)

    core.save(core.STATE, state)
    core.save(
        core.STATUS,
        {
            "checked_at": datetime.now(timezone.utc).isoformat(),
            "status": "test_email_sent" if is_test else "email_sent",
            "ranked_candidate_count": len(ranked),
            "low_value_dismissed_count": len(newly_dismissed),
            "attempted_summary_count": min(len(ranked), max_attempts),
            "email_item_count": len(sendable),
            "verified_summary_count": len(summary_map),
            "unreadable_count": len(unreadable_ids),
            "unreadable_ids": unreadable_ids,
            "to_count": len(to_addresses),
            "bcc_count": len(bcc_addresses),
            "subject": subject,
            "selection_strategy": "quality_ranked_waves",
        },
    )
    print(
        f"Sent {len(sendable)} substantive alerts after scanning "
        f"{min(len(ranked), max_attempts)} ranked candidates."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
