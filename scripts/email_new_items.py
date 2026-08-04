#!/usr/bin/env python3
"""Email newly identified laboratory-news items.

Supported delivery methods:
1. Brevo transactional email API (recommended when no personal mailbox is available)
2. Generic SMTP, including Gmail with an app password

The first normal run establishes a baseline and does not email historical items.
Later runs email only article IDs that have not been notified before.
"""

from __future__ import annotations

import html
import json
import os
import smtplib
import ssl
import sys
import urllib.error
import urllib.request
from collections import defaultdict
from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import formataddr
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
NEWS_PATH = ROOT / "data" / "news.json"
STATE_PATH = ROOT / "data" / "notified_ids.json"
STATUS_PATH = ROOT / "data" / "email_status.json"
DASHBOARD_URL = "https://atanubarik.github.io/laboratory-news-monitor/"
MAX_EMAIL_ITEMS = 100


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


def recipients_from_env() -> list[str]:
    raw = os.getenv("EMAIL_TO", "")
    recipients = [value.strip() for value in raw.split(",") if value.strip()]
    return list(dict.fromkeys(recipients))


def article_id(item: dict[str, Any]) -> str:
    return str(item.get("id") or "").strip()


def sort_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(items, key=lambda item: str(item.get("published_at") or ""), reverse=True)


def article_text(item: dict[str, Any], number: int) -> str:
    description = " ".join(str(item.get("description") or "No feed description available.").split())
    return "\n".join(
        [
            f"{number}. {item.get('title') or 'Untitled article'}",
            f"Company: {item.get('company') or 'Unknown'}",
            f"Published: {item.get('published_display') or item.get('published_at') or 'Unavailable'}",
            f"Source: {item.get('source') or 'Unknown'}",
            f"Category: {item.get('category') or 'Other'}",
            f"Description: {description}",
            f"Link: {item.get('url') or 'Unavailable'}",
            "",
        ]
    )


def article_html(item: dict[str, Any], number: int) -> str:
    title = html.escape(str(item.get("title") or "Untitled article"))
    company = html.escape(str(item.get("company") or "Unknown"))
    published = html.escape(str(item.get("published_display") or item.get("published_at") or "Unavailable"))
    source = html.escape(str(item.get("source") or "Unknown"))
    category = html.escape(str(item.get("category") or "Other"))
    description = html.escape(" ".join(str(item.get("description") or "No feed description available.").split()))
    url = html.escape(str(item.get("url") or ""), quote=True)
    link = (
        f'<a href="{url}" style="color:#087f5b;font-weight:600;">Open article</a>'
        if url
        else "Link unavailable"
    )
    return f"""
      <div style="margin:0 0 18px;padding:16px;border:1px solid #dbe7e1;border-radius:12px;background:#ffffff;">
        <div style="font-size:12px;font-weight:700;color:#087f5b;margin-bottom:6px;">{number}. {company} · {category}</div>
        <div style="font-size:17px;font-weight:700;color:#18332b;line-height:1.35;margin-bottom:8px;">{title}</div>
        <div style="font-size:13px;color:#60766e;margin-bottom:10px;">{source} · {published}</div>
        <div style="font-size:14px;color:#334e45;line-height:1.55;margin-bottom:10px;">{description}</div>
        <div style="font-size:13px;">{link}</div>
      </div>
    """


def build_content(items: list[dict[str, Any]], repository_updated: str, is_test: bool) -> tuple[str, str, str]:
    grouped: dict[str, int] = defaultdict(int)
    for item in items:
        grouped[str(item.get("company") or "Unknown")] += 1

    company_summary = ", ".join(f"{company}: {count}" for company, count in sorted(grouped.items()))
    subject_prefix = "TEST - " if is_test else ""
    subject = f"{subject_prefix}Laboratory News Alert: {len(items)} new item{'s' if len(items) != 1 else ''}"

    plain_parts = [
        subject,
        "",
        f"Repository updated: {repository_updated}",
        f"Company breakdown: {company_summary or 'Not available'}",
        f"Dashboard: {DASHBOARD_URL}",
        "",
    ]
    plain_parts.extend(article_text(item, number) for number, item in enumerate(items, 1))
    plain = "\n".join(plain_parts)

    html_items = "".join(article_html(item, number) for number, item in enumerate(items, 1))
    html_body = f"""
    <!doctype html>
    <html><body style="margin:0;padding:0;background:#f4f8f6;font-family:Arial,sans-serif;color:#18332b;">
      <div style="max-width:760px;margin:0 auto;padding:24px;">
        <div style="background:#087f5b;color:#ffffff;padding:22px;border-radius:16px 16px 0 0;">
          <div style="font-size:24px;font-weight:700;">{html.escape(subject)}</div>
          <div style="font-size:13px;opacity:.9;margin-top:6px;">Repository updated: {html.escape(repository_updated)}</div>
        </div>
        <div style="background:#ffffff;padding:18px 22px;border:1px solid #dbe7e1;border-top:0;">
          <p style="margin:0 0 6px;font-size:14px;"><strong>Company breakdown:</strong> {html.escape(company_summary or 'Not available')}</p>
          <p style="margin:0 0 18px;font-size:14px;"><a href="{DASHBOARD_URL}" style="color:#087f5b;font-weight:700;">Open the live dashboard and AI assistant</a></p>
          {html_items}
          <p style="font-size:12px;color:#60766e;margin-top:20px;">These are newly identified public-feed items. Verify important details in the original articles.</p>
        </div>
      </div>
    </body></html>
    """
    return subject, plain, html_body


def send_smtp(
    subject: str,
    plain: str,
    html_body: str,
    sender_address: str,
    sender_name: str,
    recipients: list[str],
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
    message["To"] = ", ".join(recipients)
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
    recipients: list[str],
) -> None:
    api_key = os.getenv("BREVO_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("BREVO_API_KEY is not configured.")

    payload = {
        "sender": {"name": sender_name, "email": sender_address},
        "to": [{"email": address} for address in recipients],
        "replyTo": {"email": sender_address, "name": sender_name},
        "subject": subject,
        "textContent": plain,
        "htmlContent": html_body,
    }
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
    repository_updated = str(repository.get("generated_at_display") or repository.get("generated_at") or "Unknown")

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
        write_status("no_new_items", repository_updated=repository_updated, new_item_count=0)
        print("No newly identified items; no email sent.")
        return 0

    recipients = recipients_from_env()
    sender_address = os.getenv("EMAIL_FROM", "").strip() or os.getenv("SMTP_USERNAME", "").strip()
    sender_name = os.getenv("EMAIL_SENDER_NAME", "Quest Updates").strip() or "Quest Updates"
    provider = choose_provider()

    if not recipients or not sender_address:
        write_status(
            "skipped_missing_email_configuration",
            repository_updated=repository_updated,
            new_item_count=len(new_items),
            email_item_count=len(email_items),
            provider=provider,
            missing_recipients=not bool(recipients),
            missing_sender=not bool(sender_address),
        )
        print("Email configuration is incomplete. Add EMAIL_TO and EMAIL_FROM.", file=sys.stderr)
        return 0

    subject, plain, html_body = build_content(email_items, repository_updated, is_test)

    try:
        if provider == "brevo":
            send_brevo(subject, plain, html_body, sender_address, sender_name, recipients)
        else:
            send_smtp(subject, plain, html_body, sender_address, sender_name, recipients)
    except Exception as exc:
        write_status(
            "email_failed",
            repository_updated=repository_updated,
            new_item_count=len(new_items),
            email_item_count=len(email_items),
            provider=provider,
            recipient_count=len(recipients),
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
        repository_updated=repository_updated,
        new_item_count=len(new_items),
        email_item_count=len(email_items),
        provider=provider,
        recipient_count=len(recipients),
        sender_name=sender_name,
    )
    print(f"Sent {'test ' if is_test else ''}email with {len(email_items)} item(s) through {provider}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
