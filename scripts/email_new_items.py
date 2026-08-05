#!/usr/bin/env python3
"""Send newly identified competitor updates in a grouped executive email."""

from __future__ import annotations

import base64
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
LOGO_DIR = ROOT / "assets" / "email-logos"
DASHBOARD_URL = "https://atanubarik.github.io/laboratory-news-monitor/"
DEFAULT_AI_WORKER_URL = "https://laboratory-news-ai.atanu-barik.workers.dev"
MAX_EMAIL_ITEMS = 40
AI_BATCH_SIZE = 4

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
COMPANY_LOGO_FILES = {
    "Labcorp": "labcorp.jpg.b64",
    "Quest Diagnostics": "quest-diagnostics.jpg.b64",
    "ARUP Laboratories": "arup-laboratories.jpg.b64",
    "Mayo Clinic Laboratories": "mayo-clinic-laboratories.jpg.b64",
    "Sonic Healthcare": "sonic-healthcare.jpg.b64",
}
COMPANY_CIDS = {
    "Labcorp": "logo-labcorp",
    "Quest Diagnostics": "logo-quest",
    "ARUP Laboratories": "logo-arup",
    "Mayo Clinic Laboratories": "logo-mayo",
    "Sonic Healthcare": "logo-sonic",
}
COMPANY_ACCENTS = {
    "Labcorp": "#24A9E0",
    "Quest Diagnostics": "#005A2B",
    "ARUP Laboratories": "#C8102E",
    "Mayo Clinic Laboratories": "#005EB8",
    "Sonic Healthcare": "#00549A",
}
GENERIC_SUMMARY_PHRASES = (
    "available repository evidence is insufficient",
    "insufficient to generate a summary",
    "no relevant information was found",
    "unable to access the article",
    "could not read the article",
    "the source article should be reviewed",
    "this update appears to concern",
    "the reporting indicates that",
)


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def env_truthy(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in {"1", "true", "yes", "on"}


def addresses(name: str) -> list[str]:
    return list(dict.fromkeys(x.strip() for x in os.getenv(name, "").split(",") if x.strip()))


def recipient_lists() -> tuple[list[str], list[str]]:
    to_addresses = addresses("EMAIL_TO")
    visible = {x.lower() for x in to_addresses}
    bcc_addresses = [x for x in addresses("EMAIL_BCC") if x.lower() not in visible]
    return to_addresses, bcc_addresses


def article_id(item: dict[str, Any]) -> str:
    return str(item.get("id") or "").strip()


def sort_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(items, key=lambda x: str(x.get("published_at") or ""), reverse=True)


def company_sort_key(company: str) -> tuple[int, str]:
    return (COMPANY_ORDER.index(company), company) if company in COMPANY_ORDER else (99, company)


def category_sort_key(category: str) -> tuple[int, str]:
    return (CATEGORY_ORDER.index(category), category) if category in CATEGORY_ORDER else (99, category)


def clean_title(item: dict[str, Any]) -> str:
    title = " ".join(str(item.get("title") or "Untitled article").split())
    source = " ".join(str(item.get("source") or "").split())
    if source:
        title = re.sub(rf"\s*[-–—|]\s*{re.escape(source)}\s*$", "", title, flags=re.I)
    return title.strip() or "Untitled article"


def group_items(items: list[dict[str, Any]]) -> dict[str, dict[str, list[dict[str, Any]]]]:
    grouped: dict[str, dict[str, list[dict[str, Any]]]] = defaultdict(lambda: defaultdict(list))
    for item in items:
        grouped[str(item.get("company") or "Other")][str(item.get("category") or "Other")].append(item)
    return grouped


def extract_json(value: str) -> dict[str, Any] | None:
    text = value.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.I)
    text = re.sub(r"\s*```$", "", text)
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        parsed = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def summary_is_usable(summary: str) -> bool:
    normalized = " ".join(summary.split()).strip()
    if len(normalized) < 70 or len(normalized) > 900:
        return False
    lowered = normalized.lower()
    return not any(phrase in lowered for phrase in GENERIC_SUMMARY_PHRASES)


def summary_prompt(items: list[dict[str, Any]]) -> str:
    records = []
    for item in items:
        records.append(
            " | ".join(
                [
                    f"ID={article_id(item)}",
                    f"Company={item.get('company') or 'Unknown'}",
                    f"Category={item.get('category') or 'Other'}",
                    f"Title={clean_title(item)}",
                    f"Publisher={item.get('source') or 'Unknown'}",
                    f"Date={item.get('published_display') or item.get('published_at') or 'Unknown'}",
                ]
            )
        )

    return """
Return ONLY valid JSON in this schema:
{"summaries":[{"id":"ARTICLE_ID","summary":"SUMMARY"}]}

Read the underlying public article for each listed update. Include a record only when you can verify enough substantive content to write a useful summary. Omit any article you cannot read or verify; do not return an insufficiency message.

Each summary must be 45-80 words, short, crisp, specific, and understandable without opening the article. Explain what happened and why it matters strategically. Do not mention the publisher, publication date, category label, headline, or URL.

Apply category-specific depth:
- Financial: include reported revenue, growth, margin, earnings, guidance, segment performance, or other material numbers.
- M&A / Investment: identify the parties, transaction value or terms when available, conditions/timing, assets or capabilities involved, and the strategic rationale.
- Product / Innovation: identify the test/product/platform, intended use, differentiation, availability, and likely commercial or clinical effect.
- Partnership: identify the parties, scope, capabilities combined, target customers/geography, and intended result.
- Leadership / Organization: identify the role/person or organizational change and the likely strategic implication.
- Regulatory / Policy: identify the decision, regulator or policy, effective timing, and market-access or operating impact.
- Research / Clinical: identify the study finding, population or scale, and why the evidence matters.

Avoid generic phrases such as "this may strengthen its position" unless tied to a specific mechanism or fact.

Updates:
""".strip() + "\n" + "\n".join(records)


def request_ai_summaries(items: list[dict[str, Any]], worker_url: str) -> dict[str, str]:
    payload = {
        "mode": "chat",
        "question": summary_prompt(items),
        "article_ids": [article_id(item) for item in items],
        "filters": {"email_digest": "true"},
    }
    request = urllib.request.Request(
        worker_url,
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/json", "Accept": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=120) as response:
        outer = json.loads(response.read().decode("utf-8"))
    parsed = extract_json(str(outer.get("answer") or ""))
    if not parsed:
        return {}
    result: dict[str, str] = {}
    for record in parsed.get("summaries", []):
        if not isinstance(record, dict):
            continue
        item_id = str(record.get("id") or "").strip()
        summary = " ".join(str(record.get("summary") or "").split()).strip()
        if item_id and summary_is_usable(summary):
            result[item_id] = summary
    return result


def build_summaries(items: list[dict[str, Any]]) -> dict[str, str]:
    worker_url = os.getenv("AI_WORKER_URL", DEFAULT_AI_WORKER_URL).strip()
    summaries: dict[str, str] = {}
    if not worker_url:
        return summaries
    for start in range(0, len(items), AI_BATCH_SIZE):
        try:
            summaries.update(request_ai_summaries(items[start : start + AI_BATCH_SIZE], worker_url))
        except Exception as exc:
            print(f"Warning: AI summary batch skipped: {exc}", file=sys.stderr)
    return summaries


def time_fields(timestamp: datetime) -> dict[str, str]:
    ist = timestamp.astimezone(IST)
    pacific = timestamp.astimezone(PACIFIC)
    next_run = timestamp + timedelta(hours=6)
    return {
        "date": ist.strftime("%b %d, %Y"),
        "ist": ist.strftime("%I:%M %p").lstrip("0"),
        "ptc": pacific.strftime("%I:%M %p").lstrip("0"),
        "next_ist": next_run.astimezone(IST).strftime("%I:%M %p").lstrip("0"),
        "next_ptc": next_run.astimezone(PACIFIC).strftime("%I:%M %p").lstrip("0"),
    }


def logo_img(company: str, width: int, height: int) -> str:
    cid = COMPANY_CIDS.get(company)
    if not cid:
        return html.escape(company)
    return (
        f'<img src="cid:{cid}" width="{width}" height="{height}" alt="{html.escape(company)}" '
        f'style="display:block;width:{width}px;height:{height}px;object-fit:contain;border:0;">'
    )


def breakdown_html(items: list[dict[str, Any]]) -> str:
    counts = Counter(str(item.get("company") or "Other") for item in items)
    cells = []
    for company in sorted(counts, key=company_sort_key):
        accent = COMPANY_ACCENTS.get(company, "#0B6B57")
        cells.append(
            f"""
            <td valign="middle" style="padding:0 8px 10px 0;">
              <table role="presentation" cellpadding="0" cellspacing="0" border="0" style="background:#FFFFFF;border:1px solid #E5E7EB;border-radius:12px;">
                <tr>
                  <td style="padding:9px 8px 9px 10px;">{logo_img(company, 92, 36)}</td>
                  <td style="padding:9px 13px 9px 2px;font-family:Arial,sans-serif;">
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
        row = cells[index:index + 3]
        row.extend(['<td></td>'] * (3 - len(row)))
        rows.append("<tr>" + "".join(row) + "</tr>")
    return "".join(rows)


def article_html(item: dict[str, Any], summary: str | None) -> str:
    title = html.escape(clean_title(item))
    url = html.escape(str(item.get("url") or ""), quote=True)
    linked_title = (
        f'<a href="{url}" target="_blank" style="color:#0B6B57;text-decoration:underline;font-weight:700;">{title}</a>'
        if url else f'<span style="font-weight:700;color:#111827;">{title}</span>'
    )
    summary_block = ""
    if summary:
        summary_block = (
            f'<div style="margin-top:7px;font-family:Arial,sans-serif;font-size:14px;line-height:21px;color:#4B5563;">'
            f'{html.escape(summary)}</div>'
        )
    return (
        '<tr><td style="padding:0 0 18px 0;">'
        f'<div style="font-family:Arial,sans-serif;font-size:16px;line-height:23px;">{linked_title}</div>'
        f'{summary_block}</td></tr>'
    )


def news_sections(items: list[dict[str, Any]], summaries: dict[str, str]) -> str:
    grouped = group_items(items)
    sections = []
    for company in sorted(grouped, key=company_sort_key):
        accent = COMPANY_ACCENTS.get(company, "#0B6B57")
        categories = []
        for category in sorted(grouped[company], key=category_sort_key):
            rows = "".join(article_html(item, summaries.get(article_id(item))) for item in grouped[company][category])
            categories.append(
                f"""
                <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="margin-bottom:10px;">
                  <tr><td style="padding:9px 13px;background:#F3F7F6;border-left:4px solid {accent};font-family:Arial,sans-serif;font-size:13px;line-height:18px;font-weight:800;text-transform:uppercase;color:#374151;">{html.escape(CATEGORY_LABELS.get(category, category))}</td></tr>
                  <tr><td style="padding:16px 16px 0 16px;"><table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">{rows}</table></td></tr>
                </table>
                """
            )
        count = sum(len(values) for values in grouped[company].values())
        sections.append(
            f"""
            <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="margin-bottom:24px;background:#FFFFFF;border:1px solid #E5E7EB;border-radius:16px;overflow:hidden;">
              <tr>
                <td style="padding:15px 18px;border-bottom:1px solid #E5E7EB;">{logo_img(company, 150, 52)}</td>
                <td align="right" style="padding:15px 18px;border-bottom:1px solid #E5E7EB;font-family:Arial,sans-serif;font-size:12px;color:#6B7280;">{count} alert{'s' if count != 1 else ''}</td>
              </tr>
              <tr><td colspan="2" style="padding:16px;">{''.join(categories)}</td></tr>
            </table>
            """
        )
    return "".join(sections)


def build_content(items: list[dict[str, Any]], summaries: dict[str, str], is_test: bool, timestamp: datetime) -> tuple[str, str, str]:
    fields = time_fields(timestamp)
    subject = f"Quest Competitor Updates | {fields['date']}"
    test_badge = '<div style="font-family:Arial,sans-serif;font-size:11px;font-weight:700;color:#92400E;margin-bottom:10px;">TEST PREVIEW</div>' if is_test else ""
    grouped = group_items(items)
    plain = [
        f"Quest Business Intelligence Alerts: {len(items)} alerts",
        f"{fields['date']} | {fields['ist']} (IST) | {fields['ptc']} (PTC)",
        f"Next update: {fields['next_ist']} (IST) or {fields['next_ptc']} (PTC)",
        "",
    ]
    for company in sorted(grouped, key=company_sort_key):
        plain.append(company.upper())
        for category in sorted(grouped[company], key=category_sort_key):
            plain.append(CATEGORY_LABELS.get(category, category))
            for item in grouped[company][category]:
                plain.append(clean_title(item))
                if summaries.get(article_id(item)):
                    plain.append(summaries[article_id(item)])
                plain.append(str(item.get("url") or ""))
                plain.append("")

    body = f"""
    <!doctype html><html><body style="margin:0;padding:0;background:#EEF3F2;">
      <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="background:#EEF3F2;"><tr><td align="center" style="padding:26px 12px;">
        <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="max-width:820px;">
          <tr><td>{test_badge}</td></tr>
          <tr><td style="padding:27px 30px;background:#073B3A;border-radius:18px 18px 0 0;">
            <div style="font-family:Arial,sans-serif;font-size:12px;line-height:18px;font-weight:700;letter-spacing:1.2px;color:#9FE3D5;">QUEST BUSINESS INTELLIGENCE</div>
            <div style="margin-top:7px;font-family:Arial,sans-serif;font-size:29px;line-height:36px;font-weight:800;color:#FFFFFF;">Quest Business Intelligence Alerts: {len(items)} alerts</div>
            <div style="margin-top:18px;font-family:Arial,sans-serif;font-size:14px;line-height:21px;font-weight:700;color:#FFFFFF;">{fields['date']} <span style="color:#76BDB3;">|</span> {fields['ist']} (IST) <span style="color:#76BDB3;">|</span> {fields['ptc']} (PTC)</div>
            <div style="margin-top:7px;font-family:Arial,sans-serif;font-size:11px;line-height:16px;font-style:italic;color:#B9D8D3;">Next Update will come by {fields['next_ist']} (IST) or {fields['next_ptc']} (PTC).</div>
          </td></tr>
          <tr><td style="padding:22px 24px;background:#F8FAFA;border-left:1px solid #DDE7E5;border-right:1px solid #DDE7E5;">
            <div style="font-family:Arial,sans-serif;font-size:13px;font-weight:800;color:#374151;margin-bottom:12px;">COMPANY-WISE BREAKDOWN</div>
            <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">{breakdown_html(items)}</table>
            <a href="{DASHBOARD_URL}" target="_blank" style="font-family:Arial,sans-serif;font-size:13px;color:#0B6B57;font-weight:700;text-decoration:underline;">Open the live dashboard and AI assistant</a>
          </td></tr>
          <tr><td style="padding:24px;background:#F8FAFA;border:1px solid #DDE7E5;border-top:0;border-radius:0 0 18px 18px;">
            {news_sections(items, summaries)}
            <div style="font-family:Arial,sans-serif;font-size:11px;line-height:17px;color:#6B7280;">Summaries are AI-assisted and shown only when substantive article content could be verified. Review the linked article before making material decisions.</div>
          </td></tr>
        </table>
      </td></tr></table>
    </body></html>
    """
    return subject, "\n".join(plain), body


def attach_logos(message: EmailMessage, companies: set[str]) -> None:
    html_part = message.get_payload()[-1]
    for company in sorted(companies, key=company_sort_key):
        filename = COMPANY_LOGO_FILES.get(company)
        cid = COMPANY_CIDS.get(company)
        if not filename or not cid:
            continue
        path = LOGO_DIR / filename
        if not path.exists():
            continue
        try:
            image_bytes = base64.b64decode("".join(path.read_text(encoding="utf-8").split()))
        except (OSError, ValueError):
            continue
        html_part.add_related(
            image_bytes,
            maintype="image",
            subtype="jpeg",
            cid=f"<{cid}>",
            filename=filename.replace(".b64", ""),
            disposition="inline",
        )


def send_smtp(subject: str, plain: str, body: str, sender: str, sender_name: str, to_addresses: list[str], bcc_addresses: list[str], companies: set[str]) -> None:
    host = os.getenv("SMTP_HOST", "").strip()
    username = os.getenv("SMTP_USERNAME", "").strip()
    password = os.getenv("SMTP_APP_PASSWORD", "").strip()
    port = int(os.getenv("SMTP_PORT", "465"))
    security = os.getenv("SMTP_SECURITY", "ssl").strip().lower()
    if not host or not username or not password:
        raise RuntimeError("SMTP configuration is incomplete.")

    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = formataddr((sender_name, sender))
    message["Reply-To"] = sender
    message["To"] = ", ".join(to_addresses)
    if bcc_addresses:
        message["Bcc"] = ", ".join(bcc_addresses)
    message.set_content(plain)
    message.add_alternative(body, subtype="html")
    attach_logos(message, companies)

    context = ssl.create_default_context()
    if security == "starttls":
        with smtplib.SMTP(host, port, timeout=45) as server:
            server.starttls(context=context)
            server.login(username, password)
            server.send_message(message)
    else:
        with smtplib.SMTP_SSL(host, port, context=context, timeout=45) as server:
            server.login(username, password)
            server.send_message(message)


def send_brevo(subject: str, plain: str, body: str, sender: str, sender_name: str, to_addresses: list[str], bcc_addresses: list[str]) -> None:
    api_key = os.getenv("BREVO_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("BREVO_API_KEY is not configured.")
    payload: dict[str, Any] = {
        "sender": {"name": sender_name, "email": sender},
        "to": [{"email": x} for x in to_addresses],
        "replyTo": {"name": sender_name, "email": sender},
        "subject": subject,
        "textContent": plain,
        "htmlContent": body,
    }
    if bcc_addresses:
        payload["bcc"] = [{"email": x} for x in bcc_addresses]
    request = urllib.request.Request(
        "https://api.brevo.com/v3/smtp/email",
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={"api-key": api_key, "content-type": "application/json", "accept": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=45) as response:
        if response.status not in {200, 201, 202}:
            raise RuntimeError(f"Brevo returned HTTP {response.status}.")


def write_status(status: str, **details: Any) -> None:
    write_json(
        STATUS_PATH,
        {
            "checked_at": now_utc().isoformat(),
            "checked_at_display": now_utc().strftime("%d %b %Y, %H:%M UTC"),
            "status": status,
            **details,
        },
    )


def main() -> int:
    repository = load_json(NEWS_PATH, {})
    items = sort_items(list(repository.get("items") or []))
    current_ids = {article_id(item) for item in items if article_id(item)}
    state_exists = STATE_PATH.exists()
    state = load_json(STATE_PATH, {"notified_ids": []})
    notified = {str(value) for value in state.get("notified_ids", []) if value}
    is_test = env_truthy("SEND_TEST_EMAIL")

    if not state_exists:
        state = {"initialized_at": now_utc().isoformat(), "last_successful_email_at": None, "notified_ids": sorted(current_ids)}
        write_json(STATE_PATH, state)
        notified = set(current_ids)

    new_items = [item for item in items if article_id(item) not in notified]
    email_items = items[:5] if is_test else new_items[:MAX_EMAIL_ITEMS]
    if not email_items:
        write_status("no_new_items", new_item_count=0)
        print("No newly identified items; no email sent.")
        return 0

    to_addresses, bcc_addresses = recipient_lists()
    sender = os.getenv("EMAIL_FROM", "").strip() or os.getenv("SMTP_USERNAME", "").strip()
    sender_name = os.getenv("EMAIL_SENDER_NAME", "Quest Updates").strip() or "Quest Updates"
    provider = os.getenv("EMAIL_PROVIDER", "smtp").strip().lower()
    if not to_addresses or not sender:
        write_status("skipped_missing_email_configuration")
        return 0

    summaries = build_summaries(email_items)
    subject, plain, body = build_content(email_items, summaries, is_test, now_utc())
    try:
        if provider == "brevo":
            send_brevo(subject, plain, body, sender, sender_name, to_addresses, bcc_addresses)
        else:
            send_smtp(
                subject,
                plain,
                body,
                sender,
                sender_name,
                to_addresses,
                bcc_addresses,
                {str(item.get("company") or "") for item in email_items},
            )
    except Exception as exc:
        write_status("email_failed", error=str(exc), provider=provider)
        print(f"Email send failed: {exc}", file=sys.stderr)
        return 0

    if not is_test:
        notified.update(article_id(item) for item in email_items if article_id(item))
        state["notified_ids"] = sorted(notified)
        state["last_successful_email_at"] = now_utc().isoformat()
        state["last_successful_email_count"] = len(email_items)
        write_json(STATE_PATH, state)

    write_status(
        "test_email_sent" if is_test else "email_sent",
        provider=provider,
        email_item_count=len(email_items),
        summary_count=len(summaries),
        skipped_summary_count=len(email_items) - len(summaries),
        to_count=len(to_addresses),
        bcc_count=len(bcc_addresses),
        subject=subject,
    )
    print(f"Sent {'test ' if is_test else ''}email with {len(email_items)} alerts and {len(summaries)} verified summaries.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
