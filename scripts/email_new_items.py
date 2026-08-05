#!/usr/bin/env python3
"""Send de-duplicated competitor alerts with verified article summaries.

Every emailed item must receive a substantive article-content summary from the
Cloudflare/Gemini Worker. Items that cannot be read reliably are omitted and
left unnotified so a later run can retry them. Generic fallback commentary is
never inserted into the email.
"""

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
PACIFIC = ZoneInfo("America/Los_Angeles")
MAX_EMAIL_ITEMS = 40
SUMMARY_WORKERS = 4

# Quest brand palette supplied by the user.
DARK_GREEN = "#034C1F"
GREEN = "#35792A"
BRIGHT_GREEN = "#C6D52F"
DARK_BLUE = "#024C6A"
BLUE = "#3995BB"
PURPLE = "#80276C"
ORANGE = "#C78800"
GREY = "#9A9A9A"
DARK_GREY = "#646464"
LIGHT_GREEN = "#F4F8E8"
LIGHT_GREY = "#F5F6F5"
BORDER = "#D9DED8"

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

COMPANY_LOGOS = {
    "Labcorp": f"{DASH}assets/email-logos/rendered/labcorp.jpg?v=5",
    "Quest Diagnostics": f"{DASH}assets/email-logos/rendered/quest-diagnostics.jpg?v=5",
    "ARUP Laboratories": f"{DASH}assets/email-logos/rendered/arup-laboratories.jpg?v=5",
    "Mayo Clinic Laboratories": f"{DASH}assets/email-logos/rendered/mayo-clinic-laboratories.jpg?v=5",
    "Sonic Healthcare": f"{DASH}assets/email-logos/rendered/sonic-healthcare.jpg?v=5",
}

CATEGORY_ICONS = {
    "Product & Services": f"{DASH}assets/email-icons/rendered/product-services.png?v=1",
    "Clinical, R&D": f"{DASH}assets/email-icons/rendered/clinical-rd.png?v=1",
    "Partnership, M&A": f"{DASH}assets/email-icons/rendered/partnership-ma.png?v=1",
    "Financials": f"{DASH}assets/email-icons/rendered/financials.png?v=1",
    "Organizational Updates": f"{DASH}assets/email-icons/rendered/organizational.png?v=1",
    "Leadership Changes": f"{DASH}assets/email-icons/rendered/leadership.png?v=1",
    "Other": f"{DASH}assets/email-icons/rendered/other.png?v=1",
}

CATEGORY_ACCENTS = {
    "Product & Services": GREEN,
    "Clinical, R&D": PURPLE,
    "Partnership, M&A": ORANGE,
    "Financials": DARK_BLUE,
    "Organizational Updates": BLUE,
    "Leadership Changes": DARK_GREEN,
    "Other": GREY,
}

FORBIDDEN_SUMMARY_PHRASES = (
    "content_unavailable",
    "repository evidence",
    "available public report",
    "identified across",
    "separate reports",
    "coverage count",
    "unable to access",
    "cannot access",
    "insufficient information",
    "not enough information",
    "source article should be reviewed",
    "most important follow-up is to confirm",
    "may affect the company’s legal",
    "may affect the company's legal",
)


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
    return os.getenv(name, "").strip().lower() in {"1", "true", "yes", "on"}


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


def clean_summary(value: str) -> str:
    summary = re.sub(r"https?://\S+", "", str(value or ""))
    summary = re.sub(r"\[([^\]]+)\]\([^\)]+\)", r"\1", summary)
    summary = re.sub(r"^\s{0,3}#{1,6}\s*", "", summary, flags=re.MULTILINE)
    summary = re.sub(r"^\s*[-*•]\s*", "", summary, flags=re.MULTILINE)
    summary = re.sub(r"[*_`]+", "", summary)
    summary = re.sub(r"[ \t]+", " ", summary)
    summary = re.sub(r"\n{3,}", "\n\n", summary).strip(" -–—|:;.\n")

    lowered = summary.lower()
    if not summary or any(phrase in lowered for phrase in FORBIDDEN_SUMMARY_PHRASES):
        return ""

    word_count = len(summary.split())
    if word_count < 100:
        return ""
    if word_count > 330:
        summary = " ".join(summary.split()[:330]).rstrip(" ,;:") + "."
    elif not summary.endswith((".", "!", "?")):
        summary += "."
    return summary


def request_summary(item: dict) -> str:
    if not WORKER or not item_id(item):
        return ""

    payload = {
        "mode": "email_article_summary",
        "article_ids": [item_id(item)],
    }
    request = urllib.request.Request(
        WORKER,
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "QuestCompetitorUpdates/3.0",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=210) as response:
            result = json.loads(response.read().decode("utf-8"))
    except Exception as exc:
        print(f"Summary request failed for {item_id(item)}: {exc}")
        return ""

    if result.get("content_verified") is not True:
        return ""
    return clean_summary(str(result.get("answer") or ""))


def verified_summaries(items: list[dict]) -> dict[str, str]:
    summaries: dict[str, str] = {}
    with ThreadPoolExecutor(max_workers=SUMMARY_WORKERS) as pool:
        jobs = {pool.submit(request_summary, item): item for item in items}
        for future in as_completed(jobs):
            item = jobs[future]
            try:
                summary = future.result()
            except Exception as exc:
                print(f"Summary worker failed for {item_id(item)}: {exc}")
                summary = ""
            if summary:
                summaries[item_id(item)] = summary
    return summaries


def grouped(items: list[dict]):
    result = defaultdict(lambda: defaultdict(list))
    for item in items:
        company = str(item.get("company") or "Other")
        category = str(item.get("category") or "Other")
        result[company][category].append(item)
    return result


def breakdown_html(items: list[dict]) -> str:
    counts = Counter(str(item.get("company") or "Other") for item in items)
    companies = [company for company in COMPANIES if counts.get(company)]
    cells: list[str] = []
    for company in companies:
        cells.append(
            f"""
            <td align="center" valign="middle" style="padding:15px 9px;border-right:1px solid {BORDER};white-space:nowrap;font-family:Arial,sans-serif;">
              <div style="font-size:13px;line-height:18px;font-weight:700;color:{DARK_GREY};">{html.escape(company)}</div>
              <div style="margin-top:4px;font-size:18px;line-height:22px;font-weight:800;color:{GREEN};">{html.escape(alert_label(counts[company]))}</div>
            </td>
            """
        )
    return (
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        f'style="table-layout:fixed;background:#FFFFFF;border:1px solid {BORDER};">'
        "<tr>" + "".join(cells) + "</tr></table>"
    )


def coverage_html(item: dict) -> str:
    links = source_links(item)
    if len(links) < 2:
        return ""
    linked = " | ".join(
        f'<a href="{html.escape(url, quote=True)}" style="color:{DARK_BLUE};text-decoration:underline;">{html.escape(name)}</a>'
        for name, url in links[1:]
    )
    return (
        f'<div style="margin-top:10px;font-family:Arial,sans-serif;font-size:11px;line-height:16px;color:{GREY};">'
        f"Additional coverage: {linked}</div>"
    )


def summary_html(summary: str) -> str:
    paragraphs = [part.strip() for part in re.split(r"\n\s*\n", summary) if part.strip()]
    if len(paragraphs) == 1:
        sentences = re.split(r"(?<=[.!?])\s+", paragraphs[0])
        if len(sentences) >= 5:
            split_at = max(2, len(sentences) // 2)
            paragraphs = [" ".join(sentences[:split_at]), " ".join(sentences[split_at:])]
    return "".join(
        f'<p style="margin:0 0 11px 0;font-family:Arial,sans-serif;font-size:14px;line-height:22px;color:{DARK_GREY};">{html.escape(paragraph)}</p>'
        for paragraph in paragraphs
    )


def article_html(item: dict, summary: str) -> str:
    links = source_links(item)
    url = links[0][1] if links else str(item.get("url") or "")
    title_text = html.escape(clean_title(item))
    title = (
        f'<a href="{html.escape(url, quote=True)}" style="color:{DARK_GREEN};text-decoration:underline;font-weight:700;">{title_text}</a>'
        if url
        else f'<span style="color:{DARK_GREEN};font-weight:700;">{title_text}</span>'
    )
    return f"""
    <tr>
      <td style="padding:0 0 14px 0;">
        <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#FFFFFF;border:1px solid {BORDER};">
          <tr>
            <td width="6" style="width:6px;background:{BRIGHT_GREEN};font-size:0;line-height:0;">&nbsp;</td>
            <td style="padding:18px 20px;">
              <div style="font-family:Arial,sans-serif;font-size:16px;line-height:23px;">{title}</div>
              <div style="margin-top:11px;">{summary_html(summary)}</div>
              {coverage_html(item)}
            </td>
          </tr>
        </table>
      </td>
    </tr>
    """


def category_header(category: str) -> str:
    icon_url = html.escape(CATEGORY_ICONS.get(category, CATEGORY_ICONS["Other"]), quote=True)
    accent = CATEGORY_ACCENTS.get(category, GREEN)
    return f"""
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:{LIGHT_GREEN};border-top:1px solid {BORDER};border-bottom:1px solid {BORDER};">
      <tr>
        <td width="66" align="center" valign="middle" style="padding:10px 4px 10px 12px;">
          <img src="{icon_url}" width="46" height="46" alt="" style="display:block;width:46px;height:46px;border:0;object-fit:contain;">
        </td>
        <td valign="middle" style="padding:10px 16px 10px 5px;font-family:Arial,sans-serif;font-size:15px;line-height:20px;font-weight:800;color:{accent};text-transform:uppercase;letter-spacing:.2px;">{html.escape(category)}</td>
      </tr>
    </table>
    """


def company_header(company: str, count: int) -> str:
    logo_url = html.escape(COMPANY_LOGOS[company], quote=True)
    logo_width = 220 if company == "Labcorp" else 160
    return f"""
    <tr>
      <td colspan="2" style="height:8px;line-height:8px;background:{BRIGHT_GREEN};font-size:0;">&nbsp;</td>
    </tr>
    <tr>
      <td valign="middle" style="padding:22px 26px;border-bottom:1px solid {BORDER};background:#FFFFFF;">
        <div style="font-family:Arial,sans-serif;font-size:25px;line-height:31px;font-weight:800;color:{DARK_GREEN};">{html.escape(company)}</div>
        <div style="margin-top:4px;font-family:Arial,sans-serif;font-size:12px;line-height:17px;font-weight:700;color:{GREEN};">{html.escape(alert_label(count))}</div>
      </td>
      <td align="right" valign="middle" style="padding:15px 26px;border-bottom:1px solid {BORDER};background:#FFFFFF;">
        <img src="{logo_url}" width="{logo_width}" alt="{html.escape(company)}" style="display:block;width:{logo_width}px;max-width:100%;height:auto;border:0;object-fit:contain;">
      </td>
    </tr>
    """


def news_html(items: list[dict], summaries: dict[str, str]) -> str:
    data = grouped(items)
    sections: list[str] = []
    for company in COMPANIES:
        if company not in data:
            continue
        category_sections: list[str] = []
        for category in CATEGORIES:
            category_items = data[company].get(category)
            if not category_items:
                continue
            rows = "".join(
                article_html(item, summaries[item_id(item)]) for item in category_items
            )
            category_sections.append(
                f"""
                <tr><td colspan="2" style="padding:20px 22px 8px;">{category_header(category)}</td></tr>
                <tr><td colspan="2" style="padding:6px 22px 12px;"><table role="presentation" width="100%" cellpadding="0" cellspacing="0">{rows}</table></td></tr>
                """
            )
        count = sum(len(values) for values in data[company].values())
        sections.append(
            f"""
            <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="margin-bottom:30px;background:{LIGHT_GREY};border:3px solid {DARK_GREEN};">
              {company_header(company, count)}
              {''.join(category_sections)}
            </table>
            """
        )
    return "".join(sections)


def timing(now: datetime) -> tuple[str, str, str, str, str]:
    now_ist = now.astimezone(IST)
    now_pacific = now.astimezone(PACIFIC)
    next_run = now + timedelta(hours=6)

    def format_time(value: datetime) -> str:
        return value.strftime("%I:%M %p").lstrip("0")

    return (
        now_ist.strftime("%b %d, %Y"),
        format_time(now_ist),
        format_time(now_pacific),
        format_time(next_run.astimezone(IST)),
        format_time(next_run.astimezone(PACIFIC)),
    )


def build_email(items: list[dict], summaries: dict[str, str], is_test: bool, now: datetime):
    date, time_ist, time_pacific, next_ist, next_pacific = timing(now)
    label = alert_label(len(items))
    subject = f"Quest Competitor Updates | {date}"
    test_label = (
        f'<div style="margin-bottom:9px;font-family:Arial,sans-serif;font-size:11px;font-weight:700;color:{ORANGE};">TEST PREVIEW</div>'
        if is_test
        else ""
    )

    html_body = f"""
    <!doctype html>
    <html lang="en">
      <head><meta name="viewport" content="width=device-width,initial-scale=1"></head>
      <body style="margin:0;padding:0;background:#EEF1EE;font-family:Arial,sans-serif;">
        <table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr><td align="center" style="padding:25px 10px;">
          <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="max-width:1160px;">
            <tr><td>{test_label}</td></tr>
            <tr>
              <td style="padding:30px 36px;background:{DARK_GREEN};border-bottom:7px solid {BRIGHT_GREEN};color:#FFFFFF;">
                <div style="font-family:Arial,sans-serif;font-size:12px;line-height:18px;font-weight:700;letter-spacing:1px;color:{BRIGHT_GREEN};">QUEST BUSINESS INTELLIGENCE</div>
                <div style="margin-top:8px;font-family:Arial,sans-serif;font-size:30px;line-height:37px;font-weight:800;color:#FFFFFF;">Quest Business Intelligence Report: {html.escape(label)}</div>
                <div style="margin-top:18px;font-family:Arial,sans-serif;font-size:13px;line-height:19px;font-weight:700;color:#FFFFFF;">{date} &nbsp;|&nbsp; {time_ist} (IST) &nbsp;|&nbsp; {time_pacific} (PTC)</div>
                <div style="margin-top:7px;font-family:Arial,sans-serif;font-size:10px;line-height:15px;font-style:italic;color:#DDE8DA;">Next Update will come by {next_ist} (IST) or {next_pacific} (PTC).</div>
              </td>
            </tr>
            <tr>
              <td style="padding:23px 24px;background:#FFFFFF;border-left:1px solid {BORDER};border-right:1px solid {BORDER};">
                <div style="margin-bottom:12px;font-family:Arial,sans-serif;font-size:12px;line-height:18px;font-weight:800;color:{DARK_GREY};">COMPANY-WISE BREAKDOWN</div>
                {breakdown_html(items)}
                <div style="margin-top:14px;"><a href="{DASH}" style="font-family:Arial,sans-serif;font-size:12px;line-height:18px;font-weight:700;color:{DARK_GREEN};text-decoration:underline;">Open the live dashboard and AI assistant</a></div>
              </td>
            </tr>
            <tr>
              <td style="padding:26px;background:{LIGHT_GREY};border:1px solid {BORDER};border-top:0;">
                {news_html(items, summaries)}
                <div style="font-family:Arial,sans-serif;font-size:11px;line-height:17px;color:{DARK_GREY};">Only updates whose underlying public content could be read and summarized are included. Items that could not be verified are deferred for a later retry. Review linked reporting before material decisions.</div>
              </td>
            </tr>
          </table>
        </td></tr></table>
      </body>
    </html>
    """

    plain_articles = [
        f"{clean_title(item)}\n{summaries[item_id(item)]}\n{source_links(item)[0][1] if source_links(item) else ''}"
        for item in items
    ]
    plain = "\n\n".join(
        [
            subject,
            f"Quest Business Intelligence Report: {label}",
            f"{date} | {time_ist} (IST) | {time_pacific} (PTC)",
            f"Next update: {next_ist} IST / {next_pacific} PTC",
            *plain_articles,
        ]
    )
    return subject, plain, html_body


def send_email(
    subject: str,
    plain: str,
    html_body: str,
    to_addresses: list[str],
    bcc_addresses: list[str],
    from_address: str,
    sender_name: str,
) -> None:
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = formataddr((sender_name, from_address))
    message["To"] = ", ".join(to_addresses)
    message["Reply-To"] = from_address
    if bcc_addresses:
        message["Bcc"] = ", ".join(bcc_addresses)
    message.set_content(plain)
    message.add_alternative(html_body, subtype="html")

    host = os.getenv("SMTP_HOST", "smtp.gmail.com")
    port = int(os.getenv("SMTP_PORT", "465"))
    username = os.getenv("SMTP_USERNAME", "")
    password = os.getenv("SMTP_APP_PASSWORD", "")
    security = os.getenv("SMTP_SECURITY", "ssl").strip().lower()
    if not username or not password:
        raise RuntimeError("SMTP_USERNAME or SMTP_APP_PASSWORD is missing.")

    context = ssl.create_default_context()
    if security == "starttls":
        with smtplib.SMTP(host, port, timeout=60) as server:
            server.ehlo()
            server.starttls(context=context)
            server.ehlo()
            server.login(username, password)
            server.send_message(message)
    else:
        with smtplib.SMTP_SSL(host, port, context=context, timeout=60) as server:
            server.login(username, password)
            server.send_message(message)


def main() -> int:
    repository = load(NEWS, {})
    items = sorted(
        repository.get("items") or [],
        key=lambda item: str(item.get("published_at") or ""),
        reverse=True,
    )
    current_ids = {item_id(item) for item in items if item_id(item)}
    state_exists = STATE.exists()
    state = load(STATE, {"notified_ids": []})
    notified_ids = set(state.get("notified_ids") or [])

    if not state_exists:
        state = {
            "initialized_at": datetime.now(timezone.utc).isoformat(),
            "notified_ids": sorted(current_ids),
        }
        save(STATE, state)
        notified_ids = current_ids

    is_test = truthy("SEND_TEST_EMAIL")
    candidates = (
        items[:5]
        if is_test
        else [item for item in items if item_id(item) not in notified_ids][:MAX_EMAIL_ITEMS]
    )

    if not candidates:
        save(
            STATUS,
            {
                "checked_at": datetime.now(timezone.utc).isoformat(),
                "status": "no_new_items",
            },
        )
        print("No candidate items; no email sent.")
        return 0

    summary_map = verified_summaries(candidates)
    sendable = [item for item in candidates if item_id(item) in summary_map]
    skipped = [item for item in candidates if item_id(item) not in summary_map]

    if not sendable:
        save(
            STATUS,
            {
                "checked_at": datetime.now(timezone.utc).isoformat(),
                "status": "no_summarizable_items",
                "candidate_item_count": len(candidates),
                "email_item_count": 0,
                "verified_summary_count": 0,
                "skipped_unreadable_count": len(skipped),
                "skipped_ids": [item_id(item) for item in skipped],
            },
        )
        print(
            f"No verified article summaries were produced for {len(candidates)} candidate items; "
            "no email sent and no IDs marked as notified."
        )
        return 0

    to_addresses = env_list("EMAIL_TO")
    bcc_addresses = [
        address
        for address in env_list("EMAIL_BCC")
        if address.lower() not in {value.lower() for value in to_addresses}
    ]
    from_address = os.getenv("EMAIL_FROM") or os.getenv("SMTP_USERNAME", "")
    sender_name = os.getenv("EMAIL_SENDER_NAME", "Quest Updates")
    if not to_addresses or not from_address:
        raise RuntimeError("EMAIL_TO or EMAIL_FROM is missing.")

    subject, plain, html_body = build_email(
        sendable,
        summary_map,
        is_test,
        datetime.now(timezone.utc),
    )
    send_email(
        subject,
        plain,
        html_body,
        to_addresses,
        bcc_addresses,
        from_address,
        sender_name,
    )

    if not is_test:
        # Only successfully emailed, content-verified items are marked notified.
        notified_ids.update(item_id(item) for item in sendable)
        state["notified_ids"] = sorted(notified_ids)
        state["last_successful_email_at"] = datetime.now(timezone.utc).isoformat()
        state["last_successful_email_count"] = len(sendable)
        save(STATE, state)

    save(
        STATUS,
        {
            "checked_at": datetime.now(timezone.utc).isoformat(),
            "status": "test_email_sent" if is_test else "email_sent",
            "candidate_item_count": len(candidates),
            "email_item_count": len(sendable),
            "verified_summary_count": len(summary_map),
            "skipped_unreadable_count": len(skipped),
            "skipped_ids": [item_id(item) for item in skipped],
            "to_count": len(to_addresses),
            "bcc_count": len(bcc_addresses),
            "subject": subject,
            "company_logos_attached": False,
            "generic_fallback_summaries": False,
        },
    )
    print(
        f"Sent {len(sendable)} of {len(candidates)} candidate alerts with verified article summaries; "
        f"skipped {len(skipped)} unreadable items."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
