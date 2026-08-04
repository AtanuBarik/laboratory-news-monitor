#!/usr/bin/env python3
"""Fetch Google News RSS feeds and build dashboard and AI knowledge files.

The script uses only Python's standard library, so GitHub Actions does not
need to install any additional package.
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
KNOWLEDGE_DIR = ROOT / "knowledge"

# Bare "Quest" and bare "ARUP" are intentionally avoided because they create
# many unrelated results. Add or edit trackers here as needed.
TRACKERS = [
    {
        "company": "Labcorp",
        "slug": "labcorp",
        "query": '("Labcorp" OR "LabCorp" OR "Laboratory Corporation of America")',
        "official_domains": {"labcorp.com"},
    },
    {
        "company": "Quest Diagnostics",
        "slug": "quest-diagnostics",
        "query": '("Quest Diagnostics" OR "Quest Diagnostics Incorporated")',
        "official_domains": {"questdiagnostics.com"},
    },
    {
        "company": "ARUP Laboratories",
        "slug": "arup-laboratories",
        "query": '("ARUP Laboratories" OR "ARUP Labs")',
        "official_domains": {"aruplab.com"},
    },
    {
        "company": "Mayo Clinic Laboratories",
        "slug": "mayo-clinic-laboratories",
        "query": '("Mayo Clinic Laboratories" OR "Mayo Clinic Labs" OR "Mayo Clinic")',
        "official_domains": {"mayocliniclabs.com", "mayoclinic.org"},
    },
    {
        "company": "Sonic Healthcare",
        "slug": "sonic-healthcare",
        "query": '("Sonic Healthcare" OR "Sonic Reference Laboratory")',
        "official_domains": {"sonichealthcare.com"},
    },
]

CATEGORY_RULES = {
    "Financial": (
        "earnings", "quarter results", "annual results", "revenue", "guidance",
        "investor", "dividend", "financial results",
    ),
    "M&A / Investment": (
        "acquire", "acquisition", "merger", "invests", "investment", "divest",
        "transaction", "sale of",
    ),
    "Partnership": (
        "partner", "partnership", "collaboration", "agreement", "alliance",
        "selected by", "contract",
    ),
    "Product / Innovation": (
        "launch", "new test", "new assay", "platform", "artificial intelligence",
        " ai ", "digital pathology", "innovation", "genetic", "diagnostic",
    ),
    "Research / Clinical": (
        "study", "research", "clinical trial", "publication", "scientists",
        "disease", "positivity", "biomarker",
    ),
    "Regulatory / Policy": (
        "fda", "regulatory", "approval", "cleared", "policy", "cms", "medicare",
        "reimbursement", "compliance",
    ),
    "Leadership / Organization": (
        "appoint", "named", "chief executive", "ceo", "board of directors",
        "leadership", "reorganization",
    ),
}

TAG_RE = re.compile(r"<[^>]+>")
SPACE_RE = re.compile(r"\s+")
NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")


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


def source_info(item: ET.Element) -> tuple[str, str]:
    """Return the publisher name and publisher URL from an RSS item."""

    for child in list(item):
        if child.tag.split("}")[-1].lower() == "source":
            return clean_text(child.text, 500), clean_text(child.attrib.get("url"), 1000)
    return "", ""


def parse_date(value: str) -> datetime | None:
    if not value:
        return None

    # Stored records use ISO 8601, while RSS feeds commonly use RFC 2822.
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except (TypeError, ValueError, OverflowError):
        pass

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
            "Accept": "application/rss+xml, application/xml, text/xml, */*",
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
        publisher, publisher_url = source_info(item)

        domain = source_domain(publisher_url) or source_domain(url)
        source = publisher or domain or "Unknown source"

        unique_basis = f"{company}|{url or title}|{source}".strip().lower()
        record_id = hashlib.sha256(unique_basis.encode("utf-8")).hexdigest()[:20]

        records.append(
            {
                "id": record_id,
                "company": company,
                "title": title or "Untitled article",
                "url": url,
                "description": description,
                "source": source,
                "source_url": publisher_url,
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


def normalized_title(value: str) -> str:
    return NON_ALNUM_RE.sub(" ", value.lower()).strip()


def deduplicate_items(items: list[dict]) -> list[dict]:
    """Remove repeated feed entries while retaining cross-company matches."""

    deduplicated: list[dict] = []
    seen: set[tuple[str, str, str]] = set()

    for item in items:
        published = str(item.get("published_at", ""))[:10]
        key = (
            str(item.get("company", "")).lower(),
            normalized_title(str(item.get("title", ""))),
            published,
        )
        if key in seen:
            continue
        seen.add(key)
        deduplicated.append(item)

    return deduplicated


def markdown_article(item: dict, number: int) -> str:
    description = str(item.get("description") or "No description supplied by the feed.")
    return "\n".join(
        [
            f"## {number}. {item.get('title', 'Untitled article')}",
            "",
            f"- **Company:** {item.get('company', '')}",
            f"- **Publication date:** {item.get('published_display') or 'Date unavailable'}",
            f"- **Published at (UTC):** {item.get('published_at') or 'Unavailable'}",
            f"- **Source:** {item.get('source') or 'Unknown source'}",
            f"- **Source domain:** {item.get('source_domain') or 'Unavailable'}",
            f"- **Category:** {item.get('category') or 'Other'}",
            f"- **Official source:** {'Yes' if item.get('official_source') else 'No'}",
            f"- **Original article:** {item.get('url') or 'Unavailable'}",
            "",
            f"**Feed description:** {description}",
            "",
        ]
    )


def write_markdown_file(path: Path, title: str, items: list[dict], payload: dict) -> None:
    lines = [
        f"# {title}",
        "",
        f"- **Repository generated:** {payload['generated_at_display']}",
        f"- **Articles in this file:** {len(items)}",
        "- **Primary use:** Ground Copilot Studio or another GitHub-connected AI agent.",
        "- **Data scope:** Public news collected through Google News RSS.",
        "",
        "Use the publication date, source, category, description, and URL fields below. "
        "Do not treat the feed description as a verified full-article summary.",
        "",
    ]

    if not items:
        lines.extend(["No matching articles are currently available.", ""])
    else:
        for number, item in enumerate(items, start=1):
            lines.append(markdown_article(item, number))

    path.write_text("\n".join(lines), encoding="utf-8")


def write_public_knowledge_page(items: list[dict], payload: dict) -> None:
    article_sections: list[str] = []

    for item in items[:300]:
        company = html.escape(str(item.get("company", "")))
        title = html.escape(str(item.get("title", "Untitled article")))
        source = html.escape(str(item.get("source", "Unknown source")))
        published = html.escape(str(item.get("published_display") or "Date unavailable"))
        category = html.escape(str(item.get("category", "Other")))
        description = html.escape(str(item.get("description") or "No description available."))
        article_url = html.escape(str(item.get("url", "")), quote=True)

        article_link = ""
        if article_url:
            article_link = (
                f'<p><a href="{article_url}" target="_blank" rel="noopener noreferrer">'
                "Open original article</a></p>"
            )

        article_sections.append(
            f"""
            <article>
              <h2>{title}</h2>
              <p><strong>Company:</strong> {company}</p>
              <p><strong>Publication date:</strong> {published}</p>
              <p><strong>Source:</strong> {source}</p>
              <p><strong>Category:</strong> {category}</p>
              <p>{description}</p>
              {article_link}
            </article>
            """
        )

    generated = html.escape(str(payload["generated_at_display"]))
    page = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="description" content="Current public news concerning major laboratory companies.">
  <title>Laboratory News Knowledge Base</title>
  <style>
    body {{ max-width: 1000px; margin: 40px auto; padding: 0 20px; font-family: Arial, sans-serif; line-height: 1.6; color: #18332b; }}
    article {{ margin: 24px 0; padding: 20px; border: 1px solid #dbe7e1; border-radius: 12px; }}
    a {{ color: #087f5b; }}
  </style>
</head>
<body>
  <main>
    <h1>Laboratory Services Market News Knowledge Base</h1>
    <p>Recent public news collected for the companies monitored by this repository.</p>
    <p><strong>Last updated:</strong> {generated}</p>
    {''.join(article_sections)}
  </main>
</body>
</html>
"""
    (KNOWLEDGE_DIR / "index.html").write_text(page, encoding="utf-8")


def write_knowledge_files(items: list[dict], payload: dict) -> None:
    KNOWLEDGE_DIR.mkdir(parents=True, exist_ok=True)

    write_markdown_file(
        KNOWLEDGE_DIR / "latest.md",
        "Laboratory Market News - Latest Repository",
        items[:200],
        payload,
    )

    for tracker in TRACKERS:
        company_items = [item for item in items if item.get("company") == tracker["company"]]
        write_markdown_file(
            KNOWLEDGE_DIR / f"{tracker['slug']}.md",
            f"{tracker['company']} News",
            company_items[:150],
            payload,
        )

    manifest = {
        "generated_at": payload["generated_at"],
        "generated_at_display": payload["generated_at_display"],
        "total_articles": len(items),
        "files": {
            "all_companies": "knowledge/latest.md",
            **{
                tracker["company"]: f"knowledge/{tracker['slug']}.md"
                for tracker in TRACKERS
            },
        },
    }
    (KNOWLEDGE_DIR / "manifest.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    write_public_knowledge_page(items, payload)


def main() -> int:
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=90)
    previous = load_previous()
    merged = {item["id"]: item for item in previous.get("items", []) if item.get("id")}

    failures: list[str] = []
    for tracker in TRACKERS:
        try:
            xml_bytes = fetch_feed(tracker["query"])
            for item in parse_feed(
                tracker["company"], xml_bytes, set(tracker["official_domains"])
            ):
                merged[item["id"]] = item
            print(f'Fetched {tracker["company"]}', file=sys.stderr)
        except Exception as exc:
            failures.append(f'{tracker["company"]}: {exc}')
            print(f'Warning: {tracker["company"]}: {exc}', file=sys.stderr)

    items: list[dict] = []
    for item in merged.values():
        published = parse_date(str(item.get("published_at", "")))
        if published is None or published >= cutoff:
            items.append(item)

    items.sort(
        key=lambda item: item.get("published_at") or "0000-00-00T00:00:00+00:00",
        reverse=True,
    )
    items = deduplicate_items(items)[:600]

    payload = {
        "generated_at": now.isoformat(),
        "generated_at_display": now.strftime("%d %b %Y, %H:%M UTC"),
        "item_count": len(items),
        "failures": failures,
        "items": items,
    }

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    write_knowledge_files(items, payload)
    print(f"Wrote {len(items)} items to {OUTPUT}", file=sys.stderr)

    if not items:
        print(
            "No articles were collected. Review the failures field inside data/news.json.",
            file=sys.stderr,
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
