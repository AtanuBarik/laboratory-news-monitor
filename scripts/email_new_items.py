#!/usr/bin/env python3
"""Email newly identified competitor-news items in a modern grouped digest.

Supported delivery methods:
1. Brevo transactional email API
2. Generic SMTP, including Gmail with an app password

The first normal run establishes a baseline and does not email historical items.
Later runs email only article IDs that have not been notified before.
"""

from __future__ import annotations

import html
import json
import os
import re
import smtplib
import ssl
import sys
import urllib.error
import urllib.request
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import formataddr
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
NEWS_PATH = ROOT / "data" / "news.json"
STATE_PATH = ROOT / "data" / "notified_ids.json"
STATUS_PATH = ROOT / "data" / "email_status.json"
DASHBOARD_URL = "https://atanubarik.github.io/laboratory-news-monitor/"
DEFAULT_AI_WORKER_URL = "https://laboratory-news-ai.atanu-barik.workers.dev"
MAX_EMAIL_ITEMS = 40
AI_BATCH_SIZE = 5

IST = ZoneInfo("Asia/Kolkata")
PACIFIC = ZoneInfo("America/Los_Angeles")

COMPANY_ORDER = [
    "Labcorp",
    "Quest Diagnostics",
    "ARUP Laboratories",
    "Mayo Clinic Laboratories",
    "Sonic Healthcare",
]

CATEGORY_ORDER = [
    "Product / Innovation",
    "Partnership",
    "M&A / Investment",
    "Financial",
    "Research / Clinical",
    "Regulatory / Policy",
    "Leadership / Organization",
    "Other",
]

CATEGORY_LABELS = {
    "Product / Innovation": "Product & Innovation",
    "Partnership": "Partnerships",
    "M&A / Investment": "M&A & Investment",
    "Financial": "Financial",
    "Research / Clinical": "Research & Clinical",
    "Regulatory / Policy": "Regulatory & Policy",
    "Leadership / Organization": "Leadership & Organization",
    "Other": "Other Updates",
}

COMPANY_LOGOS = {
    "Labcorp": "https://logo.clearbit.com/labcorp.com?size=120",
    "Quest Diagnostics": "https://logo.clearbit.com/questdiagnostics.com?size=120",
    "ARUP Laboratories": "https://logo.clearbit.com/aruplab.com?size=120",
    "Mayo Clinic Laboratories": "https://logo.clearbit.com/mayocliniclabs.com?size=120",
    "Sonic Healthcare": "https://logo.clearbit.com/sonichealthcare.com?size=120",
}

COMPANY_ACCENTS = {
    "Labcorp": "#F37021",
    "Quest Diagnostics": "#5B2C83",
    "ARUP Laboratories": "#A6192E",
    "Mayo Clinic Laboratories": "#005EB8",
    "Sonic Healthcare": "#007A78",
}

CATEGORY_CONTEXT = {
    "Product / Innovation": (
        "The update appears to concern a product, test, platform, diagnostic capability, "
        "or technology expansion that may affect differentiation or service delivery."
    ),
    "Partnership": (
        "The development appears to involve a commercial, clinical, technology, or health-system "
        "relationship that may expand access, distribution, or customer integration."
    ),
    "M&A / Investment": (
        "The update appears linked to portfolio expansion, investment, acquisition, divestment, "
        "or another capital-allocation decision with potential competitive implications."
    ),
    "Financial": (
        "The development appears relevant to financial performance, investor expectations, "
        "capital priorities, or the outlook for the business."
    ),
    "Research / Clinical": (
        "The update appears related to clinical evidence, research activity, disease insights, "
        "or scientific validation that may influence adoption or positioning."
    ),
    "Regulatory / Policy": (
        "The development appears connected to regulation, reimbursement, compliance, approval, "
        "or policy conditions that may change market access or operating requirements."
    ),
    "Leadership / Organization": (
        "The update appears related to leadership, governance, workforce, or organizational change "
        "that may signal a shift in priorities or execution."
    ),
    "Other": (
        "The item may provide broader market, reputation, operational, or contextual intelligence."
    ),
}


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_now() -> str:
    return utc_now().isoformat()


def display_now() -> str:
    return utc_now().strftime("%d %b %Y, %H:%M UTC")


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"Warning: could not read {path}: {exc}", file=sys.stderr)
        return default


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def env_truthy(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in {"1", "true", "yes", "on"}


def addresses_from_env(name: str) -> list[str]:
    raw = os.getenv(name, "")
    values = [value.strip() for value in raw.split(",") if value.strip()]
    return list(dict.fromkeys(values))


def recipient_lists() -> tuple[list[str], list[str]]:
    to_addresses = addresses_from_env("EMAIL_TO")
    bcc_addresses = [
        address for address in addresses_from_env("EMAIL_BCC")
        if address.lower() not in {value.lower() for value in to_addresses}
    ]
    return to_addresses, bcc_addresses


def article_id(item: dict[str, Any]) -> str:
    return str(item.get("id") or "").strip()


def sort_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(items, key=lambda item: str(item.get("published_at") or ""), reverse=True)


def company_sort_key(company: str) -> tuple[int, str]:
    try:
        return COMPANY_ORDER.index(company), company
    except ValueError:
        return len(COMPANY_ORDER), company


def category_sort_key(category: str) -> tuple[int, str]:
    try:
        return CATEGORY_ORDER.index(category), category
    except ValueError:
        return len(CATEGORY_ORDER), category


def display_title(item: dict[str, Any]) -> str:
    title = " ".join(str(item.get("title") or "Untitled article").split())
    source = " ".join(str(item.get("source") or "").split())
    if source:
        title = re.sub(
            rf"\s*[-–—|]\s*{re.escape(source)}\s*$",
            "",
            title,
            flags=re.IGNORECASE,
        ).strip()
    return title or "Untitled article"


def normalize_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", value.lower()).strip()


def compact_feed_description(item: dict[str, Any]) -> str:
    description = " ".join(str(item.get("description") or "").split())
    title = display_title(item)
    source = str(item.get("source") or "")

    for removable in (str(item.get("title") or ""), title, source):
        if removable:
            description = re.sub(re.escape(removable), "", description, flags=re.IGNORECASE)

    return re.sub(r"\s+", " ", description).strip(" -–—|:;.")


def fallback_summary(item: dict[str, Any]) -> str:
    title = display_title(item).rstrip(".")
    feed_detail = compact_feed_description(item)
    category = str(item.get("category") or "Other")
    category_context = CATEGORY_CONTEXT.get(category, CATEGORY_CONTEXT["Other"])

    parts = [f"The reporting indicates that {title[0].lower() + title[1:] if title else 'a new development has occurred' }."]
    if feed_detail and normalize_key(feed_detail) != normalize_key(title):
        parts.append(feed_detail.rstrip(".") + ".")
    parts.append(category_context)
    parts.append(
        "The source article should be reviewed for the full scope, timing, and any qualifications "
        "that are not available in the news feed."
    )
    return " ".join(parts)


def extract_json_object(value: str) -> dict[str, Any] | None:
    text = value.strip()
    if text.startswith("```"):
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


def request_ai_batch(items: list[dict[str, Any]], worker_url: str) -> dict[str, str]:
    lines = [
        f'- ID: {article_id(item)} | Title: {display_title(item)}'
        for item in items
    ]
    question = """
Return ONLY valid JSON using this exact schema:
{"summaries":[{"id":"ARTICLE_ID","summary":"SUMMARY"}]}

For every listed update, read the underlying public reporting and write a self-contained 65-95 word summary in 3-4 sentences. Explain what materially changed, the most important supporting detail, and why the development matters. The summary must help a business reader understand the entire update without opening the article. Do not mention the publisher, publication date, news category, article headline, or source URL. Avoid repeating the company name when the context is already clear. Do not add markdown, citations, preamble, or commentary outside the JSON.

Updates:
""".strip() + "\n" + "\n".join(lines)

    payload = {
        "mode": "chat",
        "question": question,
        "article_ids": [article_id(item) for item in items],
        "filters": {"email_digest": "true"},
    }
    request = urllib.request.Request(
        worker_url,
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "LaboratoryNewsEmail/2.0",
        },
    )

    with urllib.request.urlopen(request, timeout=120) as response:
        outer = json.loads(response.read().decode("utf-8"))

    parsed = extract_json_object(str(outer.get("answer") or ""))
    if not parsed:
        return {}

    summaries: dict[str, str] = {}
    for record in parsed.get("summaries", []):
        if not isinstance(record, dict):
            continue
        item_id = str(record.get("id") or "").strip()
        summary = " ".join(str(record.get("summary") or "").split())
        if item_id and summary:
            summaries[item_id] = summary
    return summaries


def build_ai_summaries(items: list[dict[str, Any]]) -> dict[str, str]:
    worker_url = os.getenv("AI_WORKER_URL", DEFAULT_AI_WORKER_URL).strip()
    summaries: dict[str, str] = {}

    if worker_url:
        for start in range(0, len(items), AI_BATCH_SIZE):
            batch = items[start : start + AI_BATCH_SIZE]
            try:
                summaries.update(request_ai_batch(batch, worker_url))
            except (OSError, TimeoutError, urllib.error.URLError, json.JSONDecodeError) as exc:
                print(f"Warning: AI summary batch failed: {exc}", file=sys.stderr)

    for item in items:
        item_id = article_id(item)
        if item_id not in summaries:
            summaries[item_id] = fallback_summary(item)
    return summaries


def alert_times(now: datetime) -> dict[str, str]:
    now_ist = now.astimezone(IST)
    now_pacific = now.astimezone(PACIFIC)
    next_run = now + timedelta(hours=6)
    next_ist = next_run.astimezone(IST)
    next_pacific = next_run.astimezone(PACIFIC)
    return {
        "subject_date": now_ist.strftime("%b %d, %Y"),
        "date": now_ist.strftime("%b %d, %Y"),
        "ist_time": now_ist.strftime("%I:%M %p").lstrip("0"),
        "pacific_time": now_pacific.strftime("%I:%M %p").lstrip("0"),
        "next_ist": next_ist.strftime("%I:%M %p").lstrip("0"),
        "next_pacific": next_pacific.strftime("%I:%M %p").lstrip("0"),
    }


def grouped_items(items: list[dict[str, Any]]) -> dict[str, dict[str, list[dict[str, Any]]]]:
    grouped: dict[str, dict[str, list[dict[str, Any]]]] = defaultdict(lambda: defaultdict(list))
    for item in items:
        company = str(item.get("company") or "Other")
        category = str(item.get("category") or "Other")
        grouped[company][category].append(item)
    return grouped


def company_breakdown_html(items: list[dict[str, Any]]) -> str:
    counts = Counter(str(item.get("company") or "Other") for item in items)
    cells = []
    for company in sorted(counts, key=company_sort_key):
        logo = html.escape(COMPANY_LOGOS.get(company, ""), quote=True)
        accent = COMPANY_ACCENTS.get(company, "#0B6B57")
        alt = html.escape(company)
        image = (
            f'<img src="{logo}" width="42" height="42" alt="{alt}" '
            'style="display:block;width:42px;height:42px;object-fit:contain;border:0;">'
            if logo
            else f'<span style="font-size:12px;font-weight:700;color:{accent};">{alt}</span>'
        )
        cells.append(
            f"""
            <td valign="middle" style="padding:0 8px 10px 0;">
              <table role="presentation" cellpadding="0" cellspacing="0" border="0" style="background:#FFFFFF;border:1px solid #E5E7EB;border-radius:12px;">
                <tr>
                  <td style="padding:10px 10px 10px 12px;">{image}</td>
                  <td style="padding:10px 14px 10px 4px;font-family:Arial,sans-serif;color:#111827;">
                    <div style="font-size:22px;line-height:24px;font-weight:800;color:{accent};">{counts[company]}</div>
                    <div style="font-size:11px;line-height:15px;color:#6B7280;">alert{'s' if counts[company] != 1 else ''}</div>
                  </td>
                </tr>
              </table>
            </td>
            """
        )

    rows = []
    for index in range(0, len(cells), 3):
        row = cells[index : index + 3]
        while len(row) < 3:
            row.append('<td style="padding:0;"></td>')
        rows.append("<tr>" + "".join(row) + "</tr>")
    return "".join(rows)


def article_html(item: dict[str, Any], summary: str) -> str:
    title = html.escape(display_title(item))
    url = html.escape(str(item.get("url") or ""), quote=True)
    description = html.escape(summary)
    title_html = (
        f'<a href="{url}" target="_blank" style="color:#0B6B57;text-decoration:underline;font-weight:700;">{title}</a>'
        if url
        else f'<span style="color:#111827;font-weight:700;">{title}</span>'
    )
    return f"""
      <tr>
        <td style="padding:0 0 18px 0;">
          <div style="font-family:Arial,sans-serif;font-size:16px;line-height:23px;margin:0 0 7px 0;">{title_html}</div>
          <div style="font-family:Arial,sans-serif;font-size:14px;line-height:21px;color:#4B5563;">{description}</div>
        </td>
      </tr>
    """


def grouped_news_html(items: list[dict[str, Any]], summaries: dict[str, str]) -> str:
    grouped = grouped_items(items)
    sections = []

    for company in sorted(grouped, key=company_sort_key):
        logo = html.escape(COMPANY_LOGOS.get(company, ""), quote=True)
        accent = COMPANY_ACCENTS.get(company, "#0B6B57")
        alt = html.escape(company)
        company_logo = (
            f'<img src="{logo}" width="76" height="44" alt="{alt}" '
            'style="display:block;max-width:120px;width:auto;height:44px;object-fit:contain;border:0;">'
            if logo
            else f'<span style="font-size:17px;font-weight:800;color:{accent};">{alt}</span>'
        )
        category_blocks = []

        for category in sorted(grouped[company], key=category_sort_key):
            label = html.escape(CATEGORY_LABELS.get(category, category))
            article_rows = "".join(
                article_html(item, summaries.get(article_id(item), fallback_summary(item)))
                for item in grouped[company][category]
            )
            category_blocks.append(
                f"""
                <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="margin:0 0 10px 0;">
                  <tr>
                    <td style="padding:10px 14px;background:#F3F7F6;border-left:4px solid {accent};font-family:Arial,sans-serif;font-size:13px;line-height:18px;font-weight:800;letter-spacing:.3px;text-transform:uppercase;color:#374151;">{label}</td>
                  </tr>
                  <tr>
                    <td style="padding:16px 16px 2px 16px;">
                      <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">{article_rows}</table>
                    </td>
                  </tr>
                </table>
                """
            )

        sections.append(
            f"""
            <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="margin:0 0 24px 0;background:#FFFFFF;border:1px solid #E5E7EB;border-radius:16px;overflow:hidden;">
              <tr>
                <td style="padding:16px 18px;border-bottom:1px solid #E5E7EB;background:#FFFFFF;">{company_logo}</td>
                <td align="right" style="padding:16px 18px;border-bottom:1px solid #E5E7EB;background:#FFFFFF;font-family:Arial,sans-serif;font-size:12px;line-height:18px;color:#6B7280;">{sum(len(value) for value in grouped[company].values())} alert{'s' if sum(len(value) for value in grouped[company].values()) != 1 else ''}</td>
              </tr>
              <tr><td colspan="2" style="padding:16px;">{''.join(category_blocks)}</td></tr>
            </table>
            """
        )

    return "".join(sections)


def plain_text_body(items: list[dict[str, Any]], summaries: dict[str, str], times: dict[str, str]) -> str:
    grouped = grouped_items(items)
    parts = [
        f"Quest Business Intelligence Alerts: {len(items)} alerts",
        f"Date: {times['date']}",
        f"Time: {times['ist_time']} (IST)",
        f"Time: {times['pacific_time']} (PTC)",
        f"Next Update will come by {times['next_ist']} (IST) or {times['next_pacific']} (PTC).",
        "",
        "Company breakdown: " + ", ".join(
            f"{company}: {sum(len(values) for values in grouped[company].values())}"
            for company in sorted(grouped, key=company_sort_key)
        ),
        f"Dashboard: {DASHBOARD_URL}",
        "",
    ]

    for company in sorted(grouped, key=company_sort_key):
        parts.extend([company.upper(), "=" * len(company)])
        for category in sorted(grouped[company], key=category_sort_key):
            parts.append(CATEGORY_LABELS.get(category, category))
            for item in grouped[company][category]:
                parts.extend(
                    [
                        display_title(item),
                        summaries.get(article_id(item), fallback_summary(item)),
                        str(item.get("url") or ""),
                        "",
                    ]
                )
    return "\n".join(parts)


def build_content(
    items: list[dict[str, Any]],
    summaries: dict[str, str],
    is_test: bool,
    generated_at: datetime,
) -> tuple[str, str, str]:
    times = alert_times(generated_at)
    subject = f"Quest - Competitor Updates | {times['subject_date']}"
    test_badge = (
        '<span style="display:inline-block;margin-bottom:10px;padding:5px 9px;border-radius:999px;background:#FEF3C7;color:#92400E;font-family:Arial,sans-serif;font-size:11px;font-weight:700;">TEST PREVIEW</span>'
        if is_test
        else ""
    )
    breakdown_rows = company_breakdown_html(items)
    news_sections = grouped_news_html(items, summaries)
    plain = plain_text_body(items, summaries, times)

    html_body = f"""
    <!doctype html>
    <html lang="en">
      <head><meta name="viewport" content="width=device-width, initial-scale=1"></head>
      <body style="margin:0;padding:0;background:#EEF3F2;">
        <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="background:#EEF3F2;">
          <tr>
            <td align="center" style="padding:26px 12px;">
              <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="max-width:820px;">
                <tr>
                  <td style="padding:0 0 12px 2px;">{test_badge}</td>
                </tr>
                <tr>
                  <td style="padding:28px 30px;background:#073B3A;border-radius:18px 18px 0 0;">
                    <div style="font-family:Arial,sans-serif;font-size:12px;line-height:18px;font-weight:700;letter-spacing:1.2px;text-transform:uppercase;color:#9FE3D5;margin-bottom:8px;">Quest Business Intelligence</div>
                    <div style="font-family:Arial,sans-serif;font-size:29px;line-height:36px;font-weight:800;color:#FFFFFF;">Quest Business Intelligence Alerts: {len(items)} alerts</div>
                    <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="margin-top:22px;">
                      <tr>
                        <td width="50%" valign="top" style="padding:0 10px 0 0;font-family:Arial,sans-serif;color:#D7EFEB;">
                          <div style="font-size:12px;line-height:18px;color:#9FCBC4;">Date</div>
                          <div style="font-size:15px;line-height:22px;font-weight:700;color:#FFFFFF;">{times['date']}</div>
                        </td>
                        <td width="25%" valign="top" style="padding:0 10px;font-family:Arial,sans-serif;color:#D7EFEB;">
                          <div style="font-size:12px;line-height:18px;color:#9FCBC4;">Time (IST)</div>
                          <div style="font-size:15px;line-height:22px;font-weight:700;color:#FFFFFF;">{times['ist_time']}</div>
                        </td>
                        <td width="25%" valign="top" style="padding:0 0 0 10px;font-family:Arial,sans-serif;color:#D7EFEB;">
                          <div style="font-size:12px;line-height:18px;color:#9FCBC4;">Time (PTC)</div>
                          <div style="font-size:15px;line-height:22px;font-weight:700;color:#FFFFFF;">{times['pacific_time']}</div>
                        </td>
                      </tr>
                    </table>
                    <div style="margin-top:18px;padding:12px 14px;border-radius:10px;background:#0D4B49;font-family:Arial,sans-serif;font-size:13px;line-height:20px;color:#D7EFEB;">Next Update will come by <strong style="color:#FFFFFF;">{times['next_ist']} (IST)</strong> or <strong style="color:#FFFFFF;">{times['next_pacific']} (PTC)</strong>.</div>
                  </td>
                </tr>
                <tr>
                  <td style="padding:22px 24px;background:#F8FAFA;border-left:1px solid #DDE7E5;border-right:1px solid #DDE7E5;">
                    <div style="font-family:Arial,sans-serif;font-size:13px;line-height:18px;font-weight:800;color:#374151;margin-bottom:12px;">COMPANY-WISE BREAKDOWN</div>
                    <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">{breakdown_rows}</table>
                    <div style="margin-top:4px;"><a href="{DASHBOARD_URL}" target="_blank" style="font-family:Arial,sans-serif;font-size:13px;line-height:19px;color:#0B6B57;font-weight:700;text-decoration:underline;">Open the live dashboard and AI assistant</a></div>
                  </td>
                </tr>
                <tr>
                  <td style="padding:24px;background:#F8FAFA;border:1px solid #DDE7E5;border-top:0;border-radius:0 0 18px 18px;">{news_sections}
                    <div style="padding:4px 4px 0 4px;font-family:Arial,sans-serif;font-size:11px;line-height:17px;color:#6B7280;">Descriptions are AI-assisted summaries of public reporting. Review the linked article before making material decisions.</div>
                  </td>
                </tr>
              </table>
            </td>
          </tr>
        </table>
      </body>
    </html>
    """
    return subject, plain, html_body


def send_smtp(
    subject: str,
    plain: str,
    html_body: str,
    sender_address: str,
    sender_name: str,
    to_addresses: list[str],
    bcc_addresses: list[str],
) -> None:
    host = os.getenv("SMTP_HOST", "").strip()
    port_text = os.getenv("SMTP_PORT", "465").strip()
    security = os.getenv("SMTP_SECURITY", "ssl").strip().lower()
    username = os.getenv("SMTP_USERNAME", "").strip()
    password = os.getenv("SMTP_APP_PASSWORD", "").strip()

    if not host or not username or not password:
        raise RuntimeError("SMTP configuration is incomplete.")

    try:
        port = int(port_text)
    except ValueError as exc:
        raise RuntimeError("SMTP_PORT must be a number.") from exc

    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = formataddr((sender_name, sender_address))
    message["Reply-To"] = sender_address
    message["To"] = ", ".join(to_addresses)
    if bcc_addresses:
        message["Bcc"] = ", ".join(bcc_addresses)
    message.set_content(plain)
    message.add_alternative(html_body, subtype="html")

    context = ssl.create_default_context()
    if security == "starttls":
        with smtplib.SMTP(host, port, timeout=30) as server:
            server.ehlo()
            server.starttls(context=context)
            server.ehlo()
            server.login(username, password)
            server.send_message(message)
    elif security == "ssl":
        with smtplib.SMTP_SSL(host, port, context=context, timeout=30) as server:
            server.login(username, password)
            server.send_message(message)
    else:
        raise RuntimeError("SMTP_SECURITY must be 'ssl' or 'starttls'.")


def send_brevo(
    subject: str,
    plain: str,
    html_body: str,
    sender_address: str,
    sender_name: str,
    to_addresses: list[str],
    bcc_addresses: list[str],
) -> None:
    api_key = os.getenv("BREVO_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("BREVO_API_KEY is not configured.")

    payload: dict[str, Any] = {
        "sender": {"name": sender_name, "email": sender_address},
        "to": [{"email": address} for address in to_addresses],
        "replyTo": {"email": sender_address, "name": sender_name},
        "subject": subject,
        "textContent": plain,
        "htmlContent": html_body,
    }
    if bcc_addresses:
        payload["bcc"] = [{"email": address} for address in bcc_addresses]

    request = urllib.request.Request(
        "https://api.brevo.com/v3/smtp/email",
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={
            "accept": "application/json",
            "api-key": api_key,
            "content-type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            if response.status not in {200, 201, 202}:
                raise RuntimeError(f"Brevo returned HTTP {response.status}.")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Brevo returned HTTP {exc.code}: {detail}") from exc


def choose_provider() -> str:
    configured = os.getenv("EMAIL_PROVIDER", "auto").strip().lower()
    if configured in {"brevo", "smtp"}:
        return configured
    if os.getenv("BREVO_API_KEY", "").strip():
        return "brevo"
    return "smtp"


def write_status(status: str, **details: Any) -> None:
    payload = {
        "checked_at": iso_now(),
        "checked_at_display": display_now(),
        "status": status,
        **details,
    }
    write_json(STATUS_PATH, payload)


def main() -> int:
    repository = load_json(NEWS_PATH, {})
    items = sort_items(list(repository.get("items") or []))
    current_ids = {article_id(item) for item in items if article_id(item)}

    state_exists = STATE_PATH.exists()
    state = load_json(
        STATE_PATH,
        {"initialized_at": None, "last_successful_email_at": None, "notified_ids": []},
    )
    notified_ids = {str(value) for value in state.get("notified_ids", []) if value}
    send_test = env_truthy("SEND_TEST_EMAIL")

    if not state_exists:
        state = {
            "initialized_at": iso_now(),
            "last_successful_email_at": None,
            "notified_ids": sorted(current_ids),
        }
        write_json(STATE_PATH, state)
        notified_ids = set(current_ids)
        print(f"Initialized email baseline with {len(current_ids)} existing article IDs.")

    new_items = [item for item in items if article_id(item) not in notified_ids]
    email_items = items[:5] if send_test else new_items[:MAX_EMAIL_ITEMS]
    is_test = send_test

    if not email_items:
        write_status("no_new_items", new_item_count=0)
        print("No newly identified items; no email sent.")
        return 0

    to_addresses, bcc_addresses = recipient_lists()
    sender_address = os.getenv("EMAIL_FROM", "").strip() or os.getenv("SMTP_USERNAME", "").strip()
    sender_name = os.getenv("EMAIL_SENDER_NAME", "Quest Updates").strip() or "Quest Updates"
    provider = choose_provider()

    if not to_addresses or not sender_address:
        write_status(
            "skipped_missing_email_configuration",
            new_item_count=len(new_items),
            email_item_count=len(email_items),
            provider=provider,
            missing_to=not bool(to_addresses),
            missing_sender=not bool(sender_address),
        )
        print("Email configuration is incomplete. Add EMAIL_TO and EMAIL_FROM.", file=sys.stderr)
        return 0

    summaries = build_ai_summaries(email_items)
    generated_at = utc_now()
    subject, plain, html_body = build_content(email_items, summaries, is_test, generated_at)

    try:
        if provider == "brevo":
            send_brevo(
                subject,
                plain,
                html_body,
                sender_address,
                sender_name,
                to_addresses,
                bcc_addresses,
            )
        else:
            send_smtp(
                subject,
                plain,
                html_body,
                sender_address,
                sender_name,
                to_addresses,
                bcc_addresses,
            )
    except Exception as exc:
        write_status(
            "email_failed",
            new_item_count=len(new_items),
            email_item_count=len(email_items),
            provider=provider,
            to_count=len(to_addresses),
            bcc_count=len(bcc_addresses),
            error=str(exc),
        )
        print(f"Email send failed: {exc}", file=sys.stderr)
        return 0

    if not is_test:
        notified_ids.update(article_id(item) for item in email_items if article_id(item))
        state["notified_ids"] = sorted(notified_ids)
        state["last_successful_email_at"] = iso_now()
        state["last_successful_email_count"] = len(email_items)
        write_json(STATE_PATH, state)

    write_status(
        "test_email_sent" if is_test else "email_sent",
        new_item_count=len(new_items),
        email_item_count=len(email_items),
        provider=provider,
        to_count=len(to_addresses),
        bcc_count=len(bcc_addresses),
        sender_name=sender_name,
        ai_summary_count=len(summaries),
        subject=subject,
    )
    print(
        f"Sent {'test ' if is_test else ''}email with {len(email_items)} item(s) "
        f"through {provider}; To={len(to_addresses)}, Bcc={len(bcc_addresses)}."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
