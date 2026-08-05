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
SUMMARY_BATCH_SIZE = 5

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
    "Labcorp": f"{DASH}assets/email-logos/rendered/labcorp.jpg?v=3",
    "Quest Diagnostics": f"{DASH}assets/email-logos/rendered/quest-diagnostics.jpg?v=3",
    "ARUP Laboratories": f"{DASH}assets/email-logos/rendered/arup-laboratories.jpg?v=3",
    "Mayo Clinic Laboratories": f"{DASH}assets/email-logos/rendered/mayo-clinic-laboratories.jpg?v=3",
    "Sonic Healthcare": f"{DASH}assets/email-logos/rendered/sonic-healthcare.jpg?v=3",
}

ACCENTS = {
    "Labcorp": "#25A9E0",
    "Quest Diagnostics": "#005A2B",
    "ARUP Laboratories": "#A6192E",
    "Mayo Clinic Laboratories": "#005EB8",
    "Sonic Healthcare": "#00599C",
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
        "The development expands the testing or service proposition and may strengthen differentiation, "
        "customer access, or adoption in the targeted clinical workflow."
    ),
    "Clinical, R&D": (
        "The evidence may influence clinical adoption, validation, or future product development, "
        "depending on the strength and applicability of the reported findings."
    ),
    "Partnership, M&A": (
        "The move may add capabilities, customers, technology, geographic reach, or scale, "
        "and should be assessed for its effect on competitive positioning and integration risk."
    ),
    "Financials": (
        "The update provides a signal on growth, profitability, guidance, or business-mix momentum "
        "and may affect expectations for investment capacity and near-term execution."
    ),
    "Organizational Updates": (
        "The change may alter operating capacity, cost structure, service coverage, or execution priorities."
    ),
    "Leadership Changes": (
        "The appointment or departure may signal a change in strategic priorities, governance, or execution focus."
    ),
    "Other": (
        "The update may affect the company’s legal, operational, commercial, or reputational position."
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
    lowered = title[:1].lower() + title[1:] if title else "the company reported a new development"
    company = str(item.get("company") or "The company")
    if lowered.lower().startswith(company.lower()):
        return f"{title}."
    return f"{company} {lowered}."


def fallback_summary(item: dict) -> str:
    category = str(item.get("category") or "Other")
    description = clean_description(item)
    sentences: list[str] = []

    if len(description) >= 45:
        sentences.append(description.rstrip(".") + ".")
    else:
        sentences.append(title_sentence(item))

    sentences.append(CATEGORY_IMPLICATION.get(category, CATEGORY_IMPLICATION["Other"]))

    summary = " ".join(sentences)
    summary = re.sub(r"\s+", " ", summary).strip()
    words = summary.split()
    if len(words) > 95:
        summary = " ".join(words[:95]).rstrip(" ,;:") + "."
    return summary


def extract_json(value: str) -> dict | None:
    text = value.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s*```$", "", text)
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        parsed = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def clean_ai_summary(value: str) -> str:
    summary = re.sub(r"https?://\S+", "", value)
    summary = re.sub(r"[*_`#>-]+", " ", summary)
    summary = re.sub(r"\b(?:source|publication date|category|headline)\s*:\s*", "", summary, flags=re.I)
    summary = re.sub(r"\s+", " ", summary).strip(" -–—|:;.")
    if not summary or any(term in summary.lower() for term in BAD_SUMMARY):
        return ""
    if len(summary.split()) < 25:
        return ""
    words = summary.split()
    if len(words) > 95:
        summary = " ".join(words[:95]).rstrip(" ,;:") + "."
    elif not summary.endswith(('.', '!', '?')):
        summary += "."
    return summary


def request_summary_batch(batch: list[dict]) -> dict[str, str]:
    updates = "\n".join(
        f"- ID: {item_id(item)} | Company: {item.get('company')} | Category: {item.get('category')} | Title: {clean_title(item)}"
        for item in batch
    )
    prompt = f"""
Return ONLY valid JSON with this exact shape:
{{"summaries":[{{"id":"ARTICLE_ID","summary":"SUMMARY"}}]}}

For every listed article, read the underlying public reporting and create a 55-90 word plain-English business-intelligence summary. Each summary must explain what happened, include the most important specific facts, and state why it matters. Do not repeat the headline, publisher, publication date, ticker, legal suffix, category label, or URL.

Financials: include revenue, growth, profit/earnings, margins, guidance, and segment details when reported.
Partnership, M&A: include parties, value or terms, timing or conditions, capabilities involved, and strategic rationale when reported.
Product & Services: include the product/service, clinical use, regulatory status, market/customer scope, and differentiation when reported.
Clinical, R&D: include study design, population, endpoints, quantitative findings, and implications when reported.
Organizational Updates or Leadership Changes: include the exact change, scope, effective timing, and expected impact.
Other: explain the event, material terms, affected stakeholders, and business implication.

Use only verified information. When a detail is not reported, omit it rather than adding a disclaimer. Do not write SKIP and do not include commentary outside the JSON.

Articles:
{updates}
""".strip()

    body = json.dumps(
        {
            "mode": "chat",
            "question": prompt,
            "article_ids": [item_id(item) for item in batch],
            "filters": {"email_digest": "true"},
        }
    ).encode()
    request = urllib.request.Request(
        WORKER,
        data=body,
        method="POST",
        headers={"Content-Type": "application/json", "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=150) as response:
            outer = json.loads(response.read().decode())
    except Exception:
        return {}

    parsed = extract_json(str(outer.get("answer") or ""))
    if not parsed:
        return {}

    result: dict[str, str] = {}
    for record in parsed.get("summaries") or []:
        if not isinstance(record, dict):
            continue
        record_id = str(record.get("id") or "").strip()
        summary = clean_ai_summary(str(record.get("summary") or ""))
        if record_id and summary:
            result[record_id] = summary
    return result


def summaries(items: list[dict]) -> tuple[dict[str, str], int]:
    batches = [items[index : index + SUMMARY_BATCH_SIZE] for index in range(0, len(items), SUMMARY_BATCH_SIZE)]
    ai_summaries: dict[str, str] = {}

    with ThreadPoolExecutor(max_workers=3) as pool:
        jobs = [pool.submit(request_summary_batch, batch) for batch in batches]
        for future in as_completed(jobs):
            try:
                ai_summaries.update(future.result())
            except Exception:
                pass

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
              <div style="margin-top:4px;font:800 19px/22px Arial;color:{ACCENTS[company]};">{counts[company]} <span style="font:400 11px Arial;color:#6B7280;">alerts</span></div>
            </td>
            """
        )
    return (
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        'style="table-layout:fixed;background:#fff;border:1px solid #E5E7EB;border-radius:12px;"><tr>'
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
    return f'<div style="margin-top:7px;font:11px/16px Arial;color:#6B7280;">Additional coverage: {linked}</div>'


def article_html(item: dict, summary: str) -> str:
    links = source_links(item)
    url = links[0][1] if links else str(item.get("url") or "")
    title = html.escape(clean_title(item))
    if url:
        title = (
            f'<a href="{html.escape(url, quote=True)}" '
            'style="color:#006A58;text-decoration:underline;font-weight:700;">'
            f"{title}</a>"
        )
    description = html.escape(summary)
    return f"""
    <tr>
      <td style="padding:0 0 20px 0;font:16px/23px Arial;">
        {title}
        <div style="margin-top:8px;font:14px/21px Arial;color:#4B5563;">{description}</div>
        {coverage_html(item)}
      </td>
    </tr>
    """


def company_header(company: str, count: int) -> str:
    logo_url = html.escape(LOGO_URLS[company], quote=True)
    logo_width = 230 if company == "Labcorp" else 165
    return f"""
    <tr>
      <td style="padding:18px 22px;border-bottom:1px solid #E5E7EB;">
        <div style="font:800 23px/29px Arial;color:#172B2A;">{html.escape(company)}</div>
        <div style="margin-top:3px;font:12px/17px Arial;color:#6B7280;">{count} alert{'s' if count != 1 else ''}</div>
      </td>
      <td align="right" valign="middle" style="padding:14px 22px;border-bottom:1px solid #E5E7EB;">
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
        category_sections: list[str] = []
        for category in CATEGORIES:
            if category not in data[company]:
                continue
            rows = "".join(
                article_html(item, summary_map[item_id(item)]) for item in data[company][category]
            )
            category_sections.append(
                f"""
                <tr><td colspan="2" style="padding:10px 14px;background:#F1F6F5;border-left:4px solid {ACCENTS[company]};font:800 13px Arial;color:#374151;text-transform:uppercase;">{html.escape(category)}</td></tr>
                <tr><td colspan="2" style="padding:16px 20px 2px;"><table role="presentation" width="100%">{rows}</table></td></tr>
                """
            )
        count = sum(len(values) for values in data[company].values())
        blocks.append(
            f"""
            <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="margin-bottom:24px;background:#fff;border:1px solid #DDE5E3;border-radius:14px;">
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
            <div style="margin-top:8px;font:800 30px Arial;">Quest Business Intelligence Alerts: {len(items)} alerts</div>
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
            <div style="font:11px/16px Arial;color:#6B7280;">Summaries are AI-assisted and use repository context when full reporting is unavailable. Review linked reporting before material decisions.</div>
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
            "to_count": len(to),
            "bcc_count": len(bcc),
            "subject": subject,
            "company_logos_attached": False,
        },
    )
    print(
        f"Sent {len(chosen)} alerts with {len(summary_map)} summaries "
        f"({ai_summary_count} AI, {len(summary_map) - ai_summary_count} fallback)."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
