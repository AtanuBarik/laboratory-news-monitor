#!/usr/bin/env python3
"""Send de-duplicated competitor alerts in a wide executive email."""

from __future__ import annotations

import html
import json
import os
import re
import smtplib
import ssl
import urllib.request
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import formataddr
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
NEWS = ROOT / "data/news.json"
STATE = ROOT / "data/notified_ids.json"
STATUS = ROOT / "data/email_status.json"
DASH = "https://atanubarik.github.io/laboratory-news-monitor/"
WORKER = os.getenv(
    "AI_WORKER_URL",
    "https://laboratory-news-ai.atanu-barik.workers.dev",
).strip()

IST = ZoneInfo("Asia/Kolkata")
PAC = ZoneInfo("America/Los_Angeles")
MAX_ITEMS = 40
SUMMARY_WORKERS = 4

COMPANIES = [
    "Labcorp",
    "Quest Diagnostics",
    "ARUP Laboratories",
    "Mayo Clinic Laboratories",
    "Sonic Healthcare",
]

CATEGORIES = [
    "Product & Services",
    "Clinical, R&D",
    "Partnership, M&A",
    "Financials",
    "Organizational Updates",
    "Leadership Changes",
    "Other",
]

LOGO_URLS = {
    "Labcorp": f"{DASH}assets/email-logos/rendered/labcorp.jpg?v=4",
    "Quest Diagnostics": f"{DASH}assets/email-logos/rendered/quest-diagnostics.jpg?v=4",
    "ARUP Laboratories": f"{DASH}assets/email-logos/rendered/arup-laboratories.jpg?v=4",
    "Mayo Clinic Laboratories": f"{DASH}assets/email-logos/rendered/mayo-clinic-laboratories.jpg?v=4",
    "Sonic Healthcare": f"{DASH}assets/email-logos/rendered/sonic-healthcare.jpg?v=4",
}

ACCENTS = {
    "Labcorp": "#25A9E0",
    "Quest Diagnostics": "#005A2B",
    "ARUP Laboratories": "#A6192E",
    "Mayo Clinic Laboratories": "#005EB8",
    "Sonic Healthcare": "#00599C",
}

CATEGORY_ICONS = {
    "Product & Services": ("P", "#0B8F72"),
    "Clinical, R&D": ("R&D", "#6B46C1"),
    "Partnership, M&A": ("M&A", "#B7791F"),
    "Financials": ("$", "#1769AA"),
    "Organizational Updates": ("ORG", "#4A5568"),
    "Leadership Changes": ("L", "#9C2C77"),
    "Other": ("•", "#6B7280"),
}

BAD_SUMMARY = (
    "insufficient",
    "unable to read",
    "cannot access",
    "not enough information",
    "repository evidence",
    "no relevant information",
    "source article should be reviewed",
    "i cannot",
    "i'm unable",
)

CATEGORY_IMPLICATION = {
    "Product & Services": (
        "This development changes the testing or service proposition and may affect differentiation, "
        "customer access, adoption, reimbursement potential, or the clinical workflow in which the offering competes."
    ),
    "Clinical, R&D": (
        "The evidence may influence clinical validation, adoption, guideline positioning, or future product development. "
        "The strategic value depends on the strength of the data and how directly it can be translated into routine use."
    ),
    "Partnership, M&A": (
        "The transaction or partnership may add capabilities, customers, technology, geographic reach, or operating scale. "
        "Its competitive impact will depend on execution, integration, commercial terms, and the speed at which benefits are realized."
    ),
    "Financials": (
        "The update provides a signal on growth, profitability, guidance, cash-generation capacity, and business-mix momentum. "
        "These factors influence the company’s ability to invest, price competitively, and execute its near-term strategy."
    ),
    "Organizational Updates": (
        "The change may alter operating capacity, cost structure, service coverage, workflow efficiency, or execution priorities. "
        "The main issue to watch is whether the change improves delivery without disrupting customers or employees."
    ),
    "Leadership Changes": (
        "The appointment or departure may signal a change in strategic priorities, governance, capital allocation, or execution focus. "
        "The practical impact will depend on the executive’s mandate and the pace of organizational follow-through."
    ),
    "Other": (
        "The update may affect the company’s legal, operational, commercial, or reputational position. "
        "Its materiality should be assessed against the scale of the event and the stakeholders directly affected."
    ),
}


def load(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def save(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def env_list(name: str) -> list[str]:
    return list(
        dict.fromkeys(value.strip() for value in os.getenv(name, "").split(",") if value.strip())
    )


def truthy(name: str) -> bool:
    return os.getenv(name, "").lower() in {"1", "true", "yes", "on"}


def item_id(item: dict) -> str:
    return str(item.get("id") or "").strip()


def alert_label(count: int) -> str:
    return f"{count} alert" if count == 1 else f"{count} alerts"


def clean_title(item: dict) -> str:
    return re.sub(r"\s+", " ", str(item.get("title") or "Untitled update")).strip()


def source_links(item: dict) -> list[tuple[str, str]]:
    result: list[tuple[str, str]] = []
    seen: set[str] = set()
    sources = item.get("sources") or [{"name": item.get("source"), "url": item.get("url")}]
    for source in sources:
        if not isinstance(source, dict):
            continue
        url = str(source.get("url") or "").strip()
        name = str(source.get("name") or "Source").strip()
        if url and url not in seen:
            seen.add(url)
            result.append((name, url))
    return result


def clean_description(item: dict) -> str:
    description = re.sub(r"<[^>]+>", " ", str(item.get("description") or ""))
    description = html.unescape(description)
    description = re.sub(r"\s+", " ", description).strip(" -–—|:;.")
    title = clean_title(item)
    source = str(item.get("source") or "")
    for removable in (str(item.get("title") or ""), title, source):
        if removable:
            description = re.sub(re.escape(removable), "", description, flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", description).strip(" -–—|:;.")


def title_sentence(item: dict) -> str:
    title = clean_title(item).rstrip(" .")
    lowered = title[:1].lower() + title[1:] if title else "reported a new development"
    company = str(item.get("company") or "The company")
    if lowered.lower().startswith(company.lower()):
        return f"{title}."
    return f"{company} {lowered}."


def fallback_summary(item: dict) -> str:
    """Create a longer evidence-bound fallback when the AI request fails."""
    category = str(item.get("category") or "Other")
    description = clean_description(item)
    coverage_count = max(1, len(source_links(item)))

    opening = description.rstrip(".") + "." if len(description) >= 45 else title_sentence(item)
    coverage = (
        f"The event was identified across {coverage_count} separate reports, which increases confidence that it is a distinct development. "
        if coverage_count > 1
        else "The alert is based on the available public report and should be interpreted within the scope of the details disclosed. "
    )
    implication = CATEGORY_IMPLICATION.get(category, CATEGORY_IMPLICATION["Other"])
    close = (
        "For competitive-intelligence purposes, the most important follow-up is to confirm the scale, timing, affected customer groups, "
        "and any operational or financial commitments that determine whether the development is incremental or strategically material."
    )

    summary = re.sub(r"\s+", " ", f"{opening} {coverage}{implication} {close}").strip()
    words = summary.split()
    if len(words) > 190:
        summary = " ".join(words[:190]).rstrip(" ,;:") + "."
    return summary


def clean_ai_summary(value: str) -> str:
    summary = re.sub(r"https?://\S+", "", value)
    summary = re.sub(r"\[([^\]]+)\]\([^\)]+\)", r"\1", summary)
    summary = re.sub(r"^\s{0,3}#{1,6}\s*", "", summary, flags=re.MULTILINE)
    summary = re.sub(r"^\s*[-*•]\s*", "", summary, flags=re.MULTILINE)
    summary = re.sub(r"[*_`]+", "", summary)
    summary = re.sub(r"\b(?:source|publication date|category|headline)\s*:\s*", "", summary, flags=re.I)
    summary = re.sub(r"[ \t]+", " ", summary)
    summary = re.sub(r"\n{3,}", "\n\n", summary).strip(" -–—|:;.\n")

    if not summary or any(term in summary.lower() for term in BAD_SUMMARY):
        return ""

    word_count = len(summary.split())
    if word_count < 110:
        return ""
    if word_count > 300:
        summary = " ".join(summary.split()[:300]).rstrip(" ,;:") + "."
    elif not summary.endswith((".", "!", "?")):
        summary += "."
    return summary


def summary_prompt(item: dict, retry: bool = False) -> str:
    retry_text = (
        "A previous response was too short. Provide the full requested depth and include all material disclosed facts. "
        if retry
        else ""
    )
    return f"""
{retry_text}Write a comprehensive 180-260 word business-intelligence brief about the single selected article. Use the underlying public reporting, not only the headline. The reader should understand the complete development without opening the article.

Article context:
Company: {item.get('company')}
Category: {item.get('category')}
Title: {clean_title(item)}

Required content:
1. Explain exactly what happened and the parties, product, service, business unit, or stakeholders involved.
2. Include all material quantitative details, dates, transaction terms, financial metrics, study results, regulatory status, geography, or segment information that are actually reported.
3. Explain the business and competitive significance, including likely impact on customers, market access, capabilities, economics, or execution.
4. End with one concise sentence on what decision-makers should monitor next.

Category-specific requirements:
- Financials: revenue, organic growth, earnings, margins, guidance, cash flow, and segment performance when reported.
- Partnership, M&A: parties, value/terms, assets or capabilities, conditions, timing, and strategic rationale when reported.
- Product & Services: product/service, use case, regulatory status, target users, commercial scope, and differentiation when reported.
- Clinical, R&D: study design, population, endpoints, quantitative findings, limitations, and implications when reported.
- Organizational Updates or Leadership Changes: exact change, scope, effective timing, mandate, and expected operational impact.

Write two or three short paragraphs. Do not use headings, bullets, citations, URLs, source names, publication dates, ticker symbols, or legal suffixes. Do not repeat the title. Do not invent missing details and do not include a disclaimer about unavailable information.
""".strip()


def request_summary(item: dict) -> str:
    if not WORKER:
        return ""

    for retry in (False, True):
        body = json.dumps(
            {
                "mode": "chat",
                "question": summary_prompt(item, retry=retry),
                "article_ids": [item_id(item)],
                "filters": {"email_digest": "true", "category": item.get("category")},
            }
        ).encode()
        request = urllib.request.Request(
            WORKER,
            data=body,
            method="POST",
            headers={"Content-Type": "application/json", "Accept": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=180) as response:
                outer = json.loads(response.read().decode())
        except Exception:
            continue

        summary = clean_ai_summary(str(outer.get("answer") or ""))
        if summary:
            return summary
    return ""


def summaries(items: list[dict]) -> tuple[dict[str, str], int]:
    ai_summaries: dict[str, str] = {}
    with ThreadPoolExecutor(max_workers=SUMMARY_WORKERS) as pool:
        jobs = {pool.submit(request_summary, item): item for item in items}
        for future in as_completed(jobs):
            item = jobs[future]
            try:
                summary = future.result()
            except Exception:
                summary = ""
            if summary:
                ai_summaries[item_id(item)] = summary

    complete: dict[str, str] = {}
    for item in items:
        complete[item_id(item)] = ai_summaries.get(item_id(item)) or fallback_summary(item)
    return complete, len(ai_summaries)


def grouped(items: list[dict]):
    result = defaultdict(lambda: defaultdict(list))
    for item in items:
        result[str(item.get("company") or "Other")][str(item.get("category") or "Other")].append(item)
    return result


def breakdown_html(items: list[dict]) -> str:
    counts = Counter(str(item.get("company") or "Other") for item in items)
    companies = [company for company in COMPANIES if counts.get(company)]
    cells = []
    for company in companies:
        cells.append(
            f"""
            <td align="center" valign="middle" style="padding:14px 10px;border-right:1px solid #E5E7EB;white-space:nowrap;">
              <div style="font:700 13px/18px Arial;color:#1F2937;">{html.escape(company)}</div>
              <div style="margin-top:4px;font:800 19px/22px Arial;color:{ACCENTS[company]};">{html.escape(alert_label(counts[company]))}</div>
            </td>
            """
        )
    return (
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        'style="table-layout:fixed;background:#fff;border:1px solid #D8E1DF;border-radius:12px;"><tr>'
        + "".join(cells)
        + "</tr></table>"
    )


def coverage_html(item: dict) -> str:
    links = source_links(item)
    if len(links) < 2:
        return ""
    linked = " | ".join(
        f'<a href="{html.escape(url, quote=True)}" style="color:#6B7280;text-decoration:underline;">{html.escape(name)}</a>'
        for name, url in links[1:]
    )
    return f'<div style="margin-top:9px;font:11px/16px Arial;color:#6B7280;">Additional coverage: {linked}</div>'


def summary_html(summary: str) -> str:
    paragraphs = [part.strip() for part in re.split(r"\n\s*\n", summary) if part.strip()]
    if len(paragraphs) == 1:
        sentences = re.split(r"(?<=[.!?])\s+", paragraphs[0])
        if len(sentences) >= 4:
            midpoint = max(2, len(sentences) // 2)
            paragraphs = [" ".join(sentences[:midpoint]), " ".join(sentences[midpoint:])]
    return "".join(
        f'<p style="margin:0 0 10px 0;font:14px/22px Arial;color:#4B5563;">{html.escape(paragraph)}</p>'
        for paragraph in paragraphs
    )


def article_html(item: dict, summary: str, accent: str) -> str:
    links = source_links(item)
    url = links[0][1] if links else str(item.get("url") or "")
    title = html.escape(clean_title(item))
    if url:
        title = (
            f'<a href="{html.escape(url, quote=True)}" '
            'style="color:#006A58;text-decoration:underline;font-weight:700;">'
            f"{title}</a>"
        )
    return f"""
    <tr>
      <td style="padding:0 0 14px 0;">
        <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#FFFFFF;border:1px solid #E2E8E6;border-left:4px solid {accent};border-radius:10px;">
          <tr><td style="padding:16px 18px;">
            <div style="font:16px/23px Arial;">{title}</div>
            <div style="margin-top:10px;">{summary_html(summary)}</div>
            {coverage_html(item)}
          </td></tr>
        </table>
      </td>
    </tr>
    """


def category_header(category: str, accent: str) -> str:
    icon, icon_color = CATEGORY_ICONS.get(category, ("•", accent))
    return f"""
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#EFF5F3;border:1px solid #E0E8E6;border-radius:10px;">
      <tr>
        <td width="44" align="center" valign="middle" style="padding:9px 0 9px 10px;">
          <table role="presentation" cellpadding="0" cellspacing="0"><tr><td align="center" valign="middle" style="width:30px;height:30px;border-radius:7px;background:{icon_color};font:800 10px Arial;color:#FFFFFF;">{html.escape(icon)}</td></tr></table>
        </td>
        <td style="padding:9px 14px 9px 8px;font:800 13px/18px Arial;color:#374151;text-transform:uppercase;letter-spacing:.2px;">{html.escape(category)}</td>
      </tr>
    </table>
    """


def company_header(company: str, count: int) -> str:
    logo_url = html.escape(LOGO_URLS[company], quote=True)
    logo_width = 235 if company == "Labcorp" else 165
    return f"""
    <tr><td colspan="2" style="height:7px;line-height:7px;background:{ACCENTS[company]};font-size:0;">&nbsp;</td></tr>
    <tr>
      <td style="padding:21px 24px;border-bottom:1px solid #D7E1DF;background:#F9FBFB;">
        <div style="font:800 25px/31px Arial;color:#172B2A;">{html.escape(company)}</div>
        <div style="margin-top:4px;font:700 12px/17px Arial;color:{ACCENTS[company]};">{html.escape(alert_label(count))}</div>
      </td>
      <td align="right" valign="middle" style="padding:16px 24px;border-bottom:1px solid #D7E1DF;background:#F9FBFB;">
        <img src="{logo_url}" width="{logo_width}" alt="{html.escape(company)}" style="display:block;width:{logo_width}px;max-width:100%;height:auto;border:0;object-fit:contain;">
      </td>
    </tr>
    """


def news_html(items: list[dict], summary_map: dict[str, str]) -> str:
    data = grouped(items)
    blocks: list[str] = []
    for company in COMPANIES:
        if company not in data:
            continue
        accent = ACCENTS[company]
        category_sections: list[str] = []
        for category in CATEGORIES:
            if category not in data[company]:
                continue
            rows = "".join(
                article_html(item, summary_map[item_id(item)], accent)
                for item in data[company][category]
            )
            category_sections.append(
                f"""
                <tr><td colspan="2" style="padding:18px 22px 8px;">{category_header(category, accent)}</td></tr>
                <tr><td colspan="2" style="padding:4px 22px 10px;"><table role="presentation" width="100%" cellpadding="0" cellspacing="0">{rows}</table></td></tr>
                """
            )
        count = sum(len(values) for values in data[company].values())
        blocks.append(
            f"""
            <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="margin-bottom:28px;background:#F7FAF9;border:3px solid {accent};border-radius:16px;overflow:hidden;">
              {company_header(company, count)}
              {''.join(category_sections)}
            </table>
            """
        )
    return "".join(blocks)


def timing(now: datetime) -> tuple[str, str, str, str, str]:
    now_ist = now.astimezone(IST)
    now_pacific = now.astimezone(PAC)
    next_run = now + timedelta(hours=6)
    format_time = lambda value: value.strftime("%I:%M %p").lstrip("0")
    return (
        now_ist.strftime("%b %d, %Y"),
        format_time(now_ist),
        format_time(now_pacific),
        format_time(next_run.astimezone(IST)),
        format_time(next_run.astimezone(PAC)),
    )


def build(items: list[dict], summary_map: dict[str, str], is_test: bool, now: datetime):
    date, time_ist, time_pacific, next_ist, next_pacific = timing(now)
    label = alert_label(len(items))
    subject = f"Quest Competitor Updates | {date}"
    test_label = (
        '<div style="margin-bottom:8px;font:700 11px Arial;color:#92400E;">TEST PREVIEW</div>'
        if is_test
        else ""
    )
    body = f"""
    <!doctype html>
    <html><body style="margin:0;background:#EDF3F2;">
      <table role="presentation" width="100%"><tr><td align="center" style="padding:24px 10px;">
        <table role="presentation" width="100%" style="max-width:1160px;">
          <tr><td>{test_label}</td></tr>
          <tr><td style="padding:28px 34px;background:#073B3A;border-radius:16px 16px 0 0;color:#fff;">
            <div style="font:700 12px Arial;letter-spacing:1px;color:#9FE3D5;">QUEST BUSINESS INTELLIGENCE</div>
            <div style="margin-top:8px;font:800 30px Arial;">Quest Business Intelligence Report: {html.escape(label)}</div>
            <div style="margin-top:17px;font:700 13px Arial;">{date} &nbsp;|&nbsp; {time_ist} (IST) &nbsp;|&nbsp; {time_pacific} (PTC)</div>
            <div style="margin-top:7px;font:italic 10px/15px Arial;color:#CBE6E1;">Next Update will come by {next_ist} (IST) or {next_pacific} (PTC).</div>
          </td></tr>
          <tr><td style="padding:22px;background:#F8FAFA;border-left:1px solid #DDE5E3;border-right:1px solid #DDE5E3;">
            <div style="margin-bottom:12px;font:800 12px Arial;color:#374151;">COMPANY-WISE BREAKDOWN</div>
            {breakdown_html(items)}
            <div style="margin-top:13px;"><a href="{DASH}" style="font:700 12px Arial;color:#006A58;">Open the live dashboard and AI assistant</a></div>
          </td></tr>
          <tr><td style="padding:24px;background:#F8FAFA;border:1px solid #DDE5E3;border-top:0;border-radius:0 0 16px 16px;">
            {news_html(items, summary_map)}
            <div style="font:11px/16px Arial;color:#6B7280;">Summaries are AI-assisted and use repository evidence when full reporting is unavailable. Review linked reporting before material decisions.</div>
          </td></tr>
        </table>
      </td></tr></table>
    </body></html>
    """
    plain_articles = [
        f"{clean_title(item)}\n{summary_map[item_id(item)]}\n{source_links(item)[0][1] if source_links(item) else ''}"
        for item in items
    ]
    plain = "\n".join(
        [
            subject,
            f"Quest Business Intelligence Report: {label}",
            f"{date} | {time_ist} (IST) | {time_pacific} (PTC)",
            f"Next update: {next_ist} IST / {next_pacific} PTC",
            "",
            *plain_articles,
        ]
    )
    return subject, plain, body


def send(subject: str, plain: str, body: str, to: list[str], bcc: list[str], from_addr: str, name: str):
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = formataddr((name, from_addr))
    message["To"] = ", ".join(to)
    message["Reply-To"] = from_addr
    if bcc:
        message["Bcc"] = ", ".join(bcc)
    message.set_content(plain)
    message.add_alternative(body, subtype="html")

    host = os.getenv("SMTP_HOST", "smtp.gmail.com")
    port = int(os.getenv("SMTP_PORT", "465"))
    user = os.getenv("SMTP_USERNAME", "")
    password = os.getenv("SMTP_APP_PASSWORD", "")
    security = os.getenv("SMTP_SECURITY", "ssl")
    context = ssl.create_default_context()

    if security == "starttls":
        with smtplib.SMTP(host, port, timeout=45) as server:
            server.starttls(context=context)
            server.login(user, password)
            server.send_message(message)
    else:
        with smtplib.SMTP_SSL(host, port, context=context, timeout=45) as server:
            server.login(user, password)
            server.send_message(message)


def main() -> int:
    repository = load(NEWS, {})
    items = sorted(
        repository.get("items") or [],
        key=lambda item: str(item.get("published_at") or ""),
        reverse=True,
    )
    current = {item_id(item) for item in items if item_id(item)}
    state_exists = STATE.exists()
    state = load(STATE, {"notified_ids": []})
    known = set(state.get("notified_ids") or [])

    if not state_exists:
        state = {
            "initialized_at": datetime.now(timezone.utc).isoformat(),
            "notified_ids": sorted(current),
        }
        save(STATE, state)
        known = current

    is_test = truthy("SEND_TEST_EMAIL")
    chosen = items[:5] if is_test else [item for item in items if item_id(item) not in known][:MAX_ITEMS]

    if not chosen:
        save(
            STATUS,
            {"checked_at": datetime.now(timezone.utc).isoformat(), "status": "no_new_items"},
        )
        return 0

    to = env_list("EMAIL_TO")
    bcc = [address for address in env_list("EMAIL_BCC") if address.lower() not in {value.lower() for value in to}]
    from_addr = os.getenv("EMAIL_FROM") or os.getenv("SMTP_USERNAME", "")
    sender_name = os.getenv("EMAIL_SENDER_NAME", "Quest Updates")

    summary_map, ai_summary_count = summaries(chosen)
    subject, plain, body = build(chosen, summary_map, is_test, datetime.now(timezone.utc))
    send(subject, plain, body, to, bcc, from_addr, sender_name)

    if not is_test:
        known.update(item_id(item) for item in chosen)
        state["notified_ids"] = sorted(known)
        state["last_successful_email_at"] = datetime.now(timezone.utc).isoformat()
        save(STATE, state)

    save(
        STATUS,
        {
            "checked_at": datetime.now(timezone.utc).isoformat(),
            "status": "test_email_sent" if is_test else "email_sent",
            "email_item_count": len(chosen),
            "summary_count": len(summary_map),
            "ai_summary_count": ai_summary_count,
            "fallback_summary_count": len(summary_map) - ai_summary_count,
            "summary_target_words": "180-260",
            "to_count": len(to),
            "bcc_count": len(bcc),
            "subject": subject,
            "company_logos_attached": False,
        },
    )
    print(
        f"Sent {len(chosen)} alerts with {len(summary_map)} comprehensive summaries "
        f"({ai_summary_count} AI, {len(summary_map) - ai_summary_count} fallback)."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
