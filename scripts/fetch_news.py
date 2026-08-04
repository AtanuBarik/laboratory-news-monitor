#!/usr/bin/env python3
"""Fetch Bing News RSS feeds and create data/news.json.

Uses only Python's standard library, so GitHub Actions does not need
to install any additional package.
"""

from __future__ import annotations

import hashlib
import html
import json
import re
import sys
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "data" / "news.json"

# The queries intentionally avoid bare "Quest" and bare "ARUP" because those
# words create many unrelated results. Edit these entries in GitHub whenever
# you want to add or change monitored terms.
TRACKERS = [
    {
        "company": "Labcorp",
        "query": '("Labcorp" OR "LabCorp" OR "Laboratory Corporation of America")',
        "official_domains": {"labcorp.com"},
    },
    {
        "company": "Quest Diagnostics",
        "query": '("Quest Diagnostics" OR "Quest Diagnostics Incorporated")',
        "official_domains": {"questdiagnostics.com"},
    },
    {
        "company": "ARUP Laboratories",
        "query": '("ARUP Laboratories" OR "ARUP Labs")',
        "official_domains": {"aruplab.com"},
    },
    {
        "company": "Mayo Clinic Laboratories",
        "query": '("Mayo Clinic Laboratories" OR "Mayo Clinic Labs" OR "Mayo Clinic")',
        "official_domains": {"mayocliniclabs.com", "mayoclinic.org"},
    },
]

CATEGORY_RULES = {
    "Financial": (
        "earnings", "quarter results", "annual results", "revenue", "guidance",
        "investor", "dividend", "financial results"
    ),
    "M&A / Investment": (
        "acquire", "acquisition", "merger", "invests", "investment", "divest",
        "transaction", "sale of"
    ),
    "Partnership": (
        "partner", "partnership", "collaboration", "agreement", "alliance",
        "selected by", "contract"
    ),
    "Product / Innovation": (
        "launch", "new test", "new assay", "platform", "artificial intelligence",
        " ai ", "digital pathology", "innovation", "genetic", "diagnostic"
    ),
    "Research / Clinical": (
        "study", "research", "clinical trial", "publication", "scientists",
        "disease", "positivity", "biomarker"
    ),
    "Regulatory / Policy": (
        "fda", "regulatory", "approval", "cleared", "policy", "cms", "medicare",
        "reimbursement", "compliance"
    ),
    "Leadership / Organization": (
        "appoint", "named", "chief executive", "ceo", "board of directors",
        "leadership", "reorganization"
    ),
}

TAG_RE = re.compile(r"<[^>]+>")
SPACE_RE = re.compile(r"\s+")


def clean_text(value: str | None, limit: int = 650) -> str:
    value = html.unescape(value or "")
    value = TAG_RE.sub(" ", value)
    value = SPACE_RE.sub(" ", value).strip()
    if len(value) > limit:
        value = value[: limit - 1].rstrip() + "…"
    return value


def child_text(item: ET.Element, local_name: str) -> str:
    for child in list(item):
        if child.tag.split("}")[-1].lower() == local_name.lower():
            return clean_text(child.text, 2000)
    return ""


def parse_date(value: str) -> datetime | None:
    if not value:
        return None
    try:
        parsed = parsedate_to_datetime(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except (TypeError, ValueError, OverflowError):
        return None


def category_for(title: str, description: str) -> str:
    text = f" {title} {description} ".lower()
    for category, terms in CATEGORY_RULES.items():
        if any(term in text for term in terms):
            return category
    return "Other"


def source_domain(url: str) -> str:
    try:
        return urlparse(url).netloc.lower().removeprefix("www.")
    except ValueError:
        return ""


def domain_matches(domain: str, official_domains: set[str]) -> bool:
    return any(domain == official or domain.endswith("." + official) for official in official_domains)


def fetch_feed(query: str) -> bytes:
    """Retrieve recent news through Google News RSS."""

    google_query = f"{query} when:30d"

    params = urllib.parse.urlencode(
        {
            "q": google_query,
            "hl": "en-US",
            "gl": "US",
            "ceid": "US:en",
        }
    )

    url = f"https://news.google.com/rss/search?{params}"

    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/151.0.0.0 Safari/537.36"
            ),
            "Accept": (
                "application/rss+xml, application/xml, "
                "text/xml, */*"
            ),
        },
    )

    with urllib.request.urlopen(request, timeout=30) as response:
        return response.read()


def parse_feed(company: str, xml_bytes: bytes, official_domains: set[str]) -> list[dict]:
    root = ET.fromstring(xml_bytes)
    records: list[dict] = []

    for item in root.findall(".//item"):
        title = child_text(item, "title")
        url = child_text(item, "link")
        description = clean_text(child_text(item, "description"))
        published_raw = child_text(item, "pubDate")
        published = parse_date(published_raw)
        source = child_text(item, "Source")

        domain = source_domain(url)
        if not source:
            source = domain or "Unknown source"

        unique_basis = (url or f"{title}|{source}").strip().lower()
        record_id = hashlib.sha256(unique_basis.encode("utf-8")).hexdigest()[:20]

        records.append(
            {
                "id": record_id,
                "company": company,
                "title": title or "Untitled article",
                "url": url,
                "description": description,
                "source": source,
                "source_domain": domain,
                "published_at": published.isoformat() if published else "",
                "published_display": published.strftime("%d %b %Y") if published else "",
                "category": category_for(title, description),
                "official_source": domain_matches(domain, official_domains),
            }
        )
    return records


def load_previous() -> dict:
    if not OUTPUT.exists():
        return {"items": []}
    try:
        return json.loads(OUTPUT.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"items": []}


def main() -> int:
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=90)
    previous = load_previous()
    merged = {item["id"]: item for item in previous.get("items", []) if item.get("id")}

    failures = []
    for tracker in TRACKERS:
        try:
            xml_bytes = fetch_feed(tracker["query"])
            for item in parse_feed(
                tracker["company"], xml_bytes, set(tracker["official_domains"])
            ):
                merged[item["id"]] = item
            print(f'Fetched {tracker["company"]}', file=sys.stderr)
        except Exception as exc:  # continue other feeds if one source fails
            failures.append(f'{tracker["company"]}: {exc}')
            print(f'Warning: {tracker["company"]}: {exc}', file=sys.stderr)

    items = []
    for item in merged.values():
        published = parse_date(item.get("published_at", ""))
        if published is None or published >= cutoff:
            items.append(item)

    items.sort(
        key=lambda item: item.get("published_at") or "0000-00-00T00:00:00+00:00",
        reverse=True,
    )
    items = items[:600]

    payload = {
        "generated_at": now.isoformat(),
        "generated_at_display": now.strftime("%d %b %Y, %H:%M UTC"),
        "item_count": len(items),
        "failures": failures,
        "items": items,
    }
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Wrote {len(items)} items to {OUTPUT}", file=sys.stderr)

    # Do not fail the workflow if one feed is temporarily unavailable and
    # previously collected data still exists.
    if not items:
    print(
        "No articles were collected. Review the failures field "
        "inside data/news.json.",
        file=sys.stderr,
    )

return 0


if __name__ == "__main__":
    raise SystemExit(main())
