#!/usr/bin/env python3
"""Fetch, clean, classify, de-duplicate, and publish competitor news.

The collector uses only Python's standard library. It intentionally keeps only
updates in which a monitored company is the main subject or an active party.
Near-duplicate coverage is merged into one event with all discovered sources.
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
from difflib import SequenceMatcher
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "data" / "news.json"
KNOWLEDGE_DIR = ROOT / "knowledge"

TRACKERS = [
    {
        "company": "Labcorp",
        "slug": "labcorp",
        "query": '("Labcorp" OR "LabCorp" OR "Laboratory Corporation of America")',
        "aliases": ("labcorp", "lab corp", "laboratory corporation of america"),
        "official_domains": {"labcorp.com"},
    },
    {
        "company": "Quest Diagnostics",
        "slug": "quest-diagnostics",
        "query": '("Quest Diagnostics" OR "Quest Diagnostics Incorporated")',
        "aliases": ("quest diagnostics", "quest diagnostic"),
        "official_domains": {"questdiagnostics.com"},
    },
    {
        "company": "ARUP Laboratories",
        "slug": "arup-laboratories",
        "query": '("ARUP Laboratories" OR "ARUP Labs")',
        "aliases": ("arup laboratories", "arup labs"),
        "official_domains": {"aruplab.com"},
    },
    {
        "company": "Mayo Clinic Laboratories",
        "slug": "mayo-clinic-laboratories",
        "query": '("Mayo Clinic Laboratories" OR "Mayo Clinic Labs")',
        "aliases": ("mayo clinic laboratories", "mayo clinic labs"),
        "official_domains": {"mayocliniclabs.com", "mayoclinic.org"},
    },
    {
        "company": "Sonic Healthcare",
        "slug": "sonic-healthcare",
        "query": '("Sonic Healthcare" OR "Sonic Reference Laboratory")',
        "aliases": ("sonic healthcare", "sonic reference laboratory"),
        "official_domains": {"sonichealthcare.com"},
    },
]

TRACKER_BY_COMPANY = {tracker["company"]: tracker for tracker in TRACKERS}

CATEGORY_RULES: list[tuple[str, tuple[str, ...]]] = [
    (
        "Leadership Changes",
        (
            "appoint", "appointment", "named ceo", "named cfo", "new ceo", "new cfo",
            "chief executive", "chief financial", "chief operating", "chief medical",
            "president and ceo", "executive vice president", "joins as", "steps down",
            "resigns", "retire", "succession", "board appoints", "leadership change",
        ),
    ),
    (
        "Partnership, M&A",
        (
            "acquire", "acquisition", "merger", "divest", "transaction", "takeover",
            "joint venture", "strategic investment", "invests in", "investment in",
            "partnership", "partners with", "collaboration", "collaborates", "alliance",
            "agreement with", "selected by", "contract with", "deal with", "sale of",
        ),
    ),
    (
        "Financials",
        (
            "earnings", "financial results", "quarterly results", "half-year", "half year",
            "annual report", "annual results", "revenue", "profit", "ebitda", "margin",
            "guidance", "forecast", "investor presentation", "investor day", "sec filing",
            "10-k", "10-q", "8-k", "form 10", "dividend", "earnings call", "transcript",
            "fiscal year", "full-year", "full year", "q1 ", "q2 ", "q3 ", "q4 ",
        ),
    ),
    (
        "Organizational Updates",
        (
            "restructur", "reorgan", "workforce", "layoff", "job cuts", "headcount",
            "new facility", "opens laboratory", "opens lab", "expands laboratory", "expansion",
            "relocat", "consolidat", "operating model", "business unit", "division",
            "brand refresh", "new headquarters", "site closure", "closes laboratory",
        ),
    ),
    (
        "Clinical, R&D",
        (
            "clinical study", "clinical trial", "study finds", "research", "publication",
            "scientists", "clinical data", "validation study", "peer-reviewed", "biomarker",
            "discovery", "r&d", "research and development", "clinical evidence", "trial data",
            "scientific", "precision medicine", "genomic study", "pathology research",
        ),
    ),
    (
        "Product & Services",
        (
            "launch", "introduces", "new test", "new assay", "diagnostic test", "testing service",
            "service offering", "platform", "laboratory service", "digital pathology", "testing menu",
            "fda-approved", "fda approved", "fda-cleared", "fda cleared", "approval", "clearance",
            "screening", "companion diagnostic", "liquid biopsy", "genetic test", "molecular test",
            "pathology service", "laboratory-developed test", "patient service center", "at-home test",
        ),
    ),
]

LOW_VALUE_PATTERNS = (
    "stock rises", "stock falls", "stock slides", "stock slips", "stock surges",
    "shares are back", "shares good value", "remains a prominent name", "investor radar",
    "long-term potential", "should you buy", "is it time to buy", "insiders sold",
    "shorts surging", "underperforms market", "outperforms market", "stand up and fight back",
    "what do we do", "price target", "analyst rating", "simply wall st", "marketbeat",
    "defense world", "etfdailynews", "american banking news", "ticker report",
)

UNRELATED_PATTERNS = (
    "diet plan", "keto supplement", "supplements", "view 9f", "fc bayern",
    "health effects of pfas", "healthy recipe", "symptoms and causes", "patient education",
)

LEGAL_SUFFIX_RE = re.compile(
    r"\b(?:incorporated|inc\.?|pvt\.?\s*ltd\.?|private\s+limited|ltd\.?|limited|llc|plc|corp\.?|corporation|holdings)\b\.?,?",
    re.IGNORECASE,
)
TICKER_RE = re.compile(
    r"\s*[\[(](?:nasdaq|nyse|asx|lse|otc)?\s*[:.]?\s*(?:lh|shl|dgx)\s*[\])]",
    re.IGNORECASE,
)
TAG_RE = re.compile(r"<[^>]+>")
SPACE_RE = re.compile(r"\s+")
NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")

EVENT_STOPWORDS = {
    "a", "an", "and", "as", "at", "by", "for", "from", "in", "into", "is", "its",
    "of", "on", "or", "the", "to", "with", "update", "news", "announces", "announced",
    "reports", "reported", "says", "new", "latest", "company", "group", "laboratories",
    "laboratory", "healthcare", "diagnostics", "clinic", "labcorp", "quest", "arup", "mayo",
    "sonic", "inc", "ltd", "limited", "holdings", "asx", "nasdaq", "nyse", "lh", "shl", "dgx",
}


def clean_text(value: str | None, limit: int = 1200) -> str:
    value = html.unescape(value or "")
    value = TAG_RE.sub(" ", value)
    value = SPACE_RE.sub(" ", value).strip()
    if len(value) > limit:
        value = value[: limit - 1].rstrip() + "…"
    return value


def child_text(item: ET.Element, local_name: str) -> str:
    for child in list(item):
        if child.tag.split("}")[-1].lower() == local_name.lower():
            return clean_text(child.text, 2500)
    return ""


def source_info(item: ET.Element) -> tuple[str, str]:
    for child in list(item):
        if child.tag.split("}")[-1].lower() == "source":
            return clean_text(child.text, 500), clean_text(child.attrib.get("url"), 1000)
    return "", ""


def parse_date(value: str) -> datetime | None:
    if not value:
        return None
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


def source_domain(url: str) -> str:
    try:
        return urlparse(url).netloc.lower().removeprefix("www.")
    except ValueError:
        return ""


def domain_matches(domain: str, official_domains: set[str]) -> bool:
    return any(domain == official or domain.endswith("." + official) for official in official_domains)


def company_alias_in_title(company: str, title: str) -> bool:
    tracker = TRACKER_BY_COMPANY.get(company)
    if not tracker:
        return False
    lowered = title.lower()
    return any(alias in lowered for alias in tracker["aliases"])


def clean_title(value: str, company: str, source: str = "") -> str:
    title = clean_text(value, 500)
    if source:
        title = re.sub(
            rf"\s*[-–—|]\s*{re.escape(source)}\s*$",
            "",
            title,
            flags=re.IGNORECASE,
        )
    title = TICKER_RE.sub("", title)
    title = re.sub(r"\bASX\s*[:.]\s*SHL\b", "", title, flags=re.IGNORECASE)
    title = re.sub(r"\bLabcorp\s*\(?LH\)?\b", "Labcorp", title, flags=re.IGNORECASE)
    title = re.sub(r"\bLH\s+(?=Q[1-4]\b)", "Labcorp ", title, flags=re.IGNORECASE)
    title = re.sub(
        r"\bLaboratory Corporation of America(?: Holdings)?\b",
        "Labcorp",
        title,
        flags=re.IGNORECASE,
    )
    title = re.sub(r"\bLabcorp Holdings\b", "Labcorp", title, flags=re.IGNORECASE)
    title = re.sub(r"\bQuest Diagnostics Incorporated\b", "Quest Diagnostics", title, flags=re.IGNORECASE)
    title = re.sub(r"\bSonic Healthcare Limited\b", "Sonic Healthcare", title, flags=re.IGNORECASE)
    title = re.sub(r"\bSonic Healthcare Ltd\b", "Sonic Healthcare", title, flags=re.IGNORECASE)
    title = LEGAL_SUFFIX_RE.sub("", title)
    title = re.sub(r"\s+([,:;])", r"\1", title)
    title = SPACE_RE.sub(" ", title).strip(" -–—|,;:")
    return title or company


def category_for(title: str, description: str) -> str:
    text = f" {title} {description} ".lower()
    for category, terms in CATEGORY_RULES:
        if any(term in text for term in terms):
            return category
    return "Other"


def relevant_record(company: str, title: str, description: str, source: str, official: bool) -> bool:
    lowered_title = title.lower()
    lowered_all = f" {title} {description} {source} ".lower()
    if not company_alias_in_title(company, title):
        return False
    if any(pattern in lowered_all for pattern in UNRELATED_PATTERNS):
        return False
    if any(pattern in lowered_title for pattern in LOW_VALUE_PATTERNS):
        financial_event_terms = (
            "earnings", "results", "revenue", "profit", "guidance", "forecast", "10-k",
            "10-q", "8-k", "annual report", "investor presentation", "acquisition", "merger",
        )
        if not any(term in lowered_all for term in financial_event_terms):
            return False
    meaningful_words = [
        word for word in NON_ALNUM_RE.sub(" ", lowered_title).split()
        if word not in EVENT_STOPWORDS
    ]
    if len(meaningful_words) < 3 and not official:
        return False
    return True


def fetch_feed(query: str) -> bytes:
    params = urllib.parse.urlencode(
        {"q": f"{query} when:30d", "hl": "en-US", "gl": "US", "ceid": "US:en"}
    )
    url = f"https://news.google.com/rss/search?{params}"
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36"
            ),
            "Accept": "application/rss+xml, application/xml, text/xml, */*",
        },
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return response.read()


def make_source(name: str, url: str, domain: str, published_at: str) -> dict[str, str]:
    return {
        "name": name or domain or "Unknown source",
        "url": url,
        "domain": domain,
        "published_at": published_at,
    }


def parse_feed(tracker: dict[str, Any], xml_bytes: bytes) -> list[dict[str, Any]]:
    company = str(tracker["company"])
    official_domains = set(tracker["official_domains"])
    root = ET.fromstring(xml_bytes)
    records: list[dict[str, Any]] = []
    for item in root.findall(".//item"):
        raw_title = child_text(item, "title")
        url = child_text(item, "link")
        description = clean_text(child_text(item, "description"), 1400)
        published = parse_date(child_text(item, "pubDate"))
        publisher, publisher_url = source_info(item)
        domain = source_domain(publisher_url) or source_domain(url)
        source = publisher or domain or "Unknown source"
        published_at = published.isoformat() if published else ""
        title = clean_title(raw_title, company, source)
        official = domain_matches(domain, official_domains)
        if not relevant_record(company, title, description, source, official):
            continue
        unique_basis = f"{company}|{url or title}|{source}".strip().lower()
        record_id = hashlib.sha256(unique_basis.encode("utf-8")).hexdigest()[:20]
        records.append(
            {
                "id": record_id,
                "company": company,
                "title": title,
                "url": url,
                "description": description,
                "source": source,
                "source_url": publisher_url,
                "source_domain": domain,
                "published_at": published_at,
                "published_display": published.strftime("%d %b %Y") if published else "",
                "category": category_for(title, description),
                "official_source": official,
                "sources": [make_source(source, url, domain, published_at)],
            }
        )
    return records


def load_previous() -> dict[str, Any]:
    if not OUTPUT.exists():
        return {"items": []}
    try:
        return json.loads(OUTPUT.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"items": []}


def normalize_record(item: dict[str, Any]) -> dict[str, Any] | None:
    company = str(item.get("company") or "")
    if company not in TRACKER_BY_COMPANY:
        return None
    source = str(item.get("source") or "Unknown source")
    title = clean_title(str(item.get("title") or ""), company, source)
    description = clean_text(str(item.get("description") or ""), 1400)
    domain = str(
        item.get("source_domain")
        or source_domain(str(item.get("source_url") or item.get("url") or ""))
    )
    tracker = TRACKER_BY_COMPANY[company]
    official = bool(item.get("official_source")) or domain_matches(
        domain, set(tracker["official_domains"])
    )
    if not relevant_record(company, title, description, source, official):
        return None
    sources = []
    for existing in item.get("sources") or []:
        if isinstance(existing, dict):
            sources.append(
                make_source(
                    str(existing.get("name") or ""),
                    str(existing.get("url") or ""),
                    str(existing.get("domain") or ""),
                    str(existing.get("published_at") or ""),
                )
            )
    if not sources:
        sources.append(
            make_source(
                source,
                str(item.get("url") or ""),
                domain,
                str(item.get("published_at") or ""),
            )
        )
    normalized = dict(item)
    normalized.update(
        {
            "company": company,
            "title": title,
            "description": description,
            "source": source,
            "source_domain": domain,
            "category": category_for(title, description),
            "official_source": official,
            "sources": sources,
        }
    )
    if not normalized.get("id"):
        basis = f"{company}|{normalized.get('url') or title}|{source}".lower()
        normalized["id"] = hashlib.sha256(basis.encode("utf-8")).hexdigest()[:20]
    return normalized


def canonical_tokens(title: str) -> set[str]:
    tokens = NON_ALNUM_RE.sub(" ", title.lower()).split()
    return {token for token in tokens if len(token) > 2 and token not in EVENT_STOPWORDS}


def normalized_event_title(title: str) -> str:
    return " ".join(sorted(canonical_tokens(title)))


def title_similarity(first: str, second: str) -> float:
    first_norm = NON_ALNUM_RE.sub(" ", first.lower()).strip()
    second_norm = NON_ALNUM_RE.sub(" ", second.lower()).strip()
    sequence = SequenceMatcher(None, first_norm, second_norm).ratio()
    first_tokens = canonical_tokens(first)
    second_tokens = canonical_tokens(second)
    if not first_tokens or not second_tokens:
        return sequence
    jaccard = len(first_tokens & second_tokens) / len(first_tokens | second_tokens)
    containment = len(first_tokens & second_tokens) / min(len(first_tokens), len(second_tokens))
    return max(sequence, jaccard, containment * 0.96)


def dates_close(first: dict[str, Any], second: dict[str, Any], days: int = 7) -> bool:
    first_date = parse_date(str(first.get("published_at") or ""))
    second_date = parse_date(str(second.get("published_at") or ""))
    if not first_date or not second_date:
        return True
    return abs((first_date - second_date).days) <= days


def same_event(first: dict[str, Any], second: dict[str, Any]) -> bool:
    if first.get("company") != second.get("company") or not dates_close(first, second):
        return False
    similarity = title_similarity(str(first.get("title") or ""), str(second.get("title") or ""))
    if similarity >= 0.88:
        return True
    if first.get("category") == second.get("category") and similarity >= 0.76:
        return True
    return False


def merge_sources(target: dict[str, Any], incoming: dict[str, Any]) -> None:
    combined = list(target.get("sources") or []) + list(incoming.get("sources") or [])
    seen: set[str] = set()
    sources: list[dict[str, str]] = []
    for source in combined:
        if not isinstance(source, dict):
            continue
        url = str(source.get("url") or "").strip()
        key = url or f"{source.get('name')}|{source.get('published_at')}"
        if not key or key in seen:
            continue
        seen.add(key)
        sources.append(
            make_source(
                str(source.get("name") or ""),
                url,
                str(source.get("domain") or ""),
                str(source.get("published_at") or ""),
            )
        )
    target["sources"] = sources
    target["coverage_count"] = len(sources)


def deduplicate_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    ordered = sorted(
        items,
        key=lambda item: str(item.get("published_at") or "9999-12-31T23:59:59+00:00"),
    )
    clusters: list[dict[str, Any]] = []
    for item in ordered:
        match = next((cluster for cluster in clusters if same_event(cluster, item)), None)
        if match is None:
            new_item = dict(item)
            new_item["sources"] = list(item.get("sources") or [])
            new_item["coverage_count"] = len(new_item["sources"])
            clusters.append(new_item)
            continue
        merge_sources(match, item)
        if len(str(item.get("description") or "")) > len(str(match.get("description") or "")):
            match["description"] = item.get("description")
        match["official_source"] = bool(match.get("official_source") or item.get("official_source"))
    for item in clusters:
        item["category"] = category_for(
            str(item.get("title") or ""), str(item.get("description") or "")
        )
        sources = item.get("sources") or []
        if sources:
            primary = sources[0]
            item["source"] = primary.get("name") or item.get("source")
            item["url"] = primary.get("url") or item.get("url")
            item["source_domain"] = primary.get("domain") or item.get("source_domain")
        item["event_signature"] = normalized_event_title(str(item.get("title") or ""))
    return sorted(
        clusters,
        key=lambda item: str(item.get("published_at") or "0000-00-00T00:00:00+00:00"),
        reverse=True,
    )


def markdown_article(item: dict[str, Any], number: int) -> str:
    source_lines = [
        f"  - {source.get('name') or 'Source'}: {source.get('url') or 'Unavailable'}"
        for source in item.get("sources") or []
    ]
    return "\n".join(
        [
            f"## {number}. {item.get('title', 'Untitled article')}",
            "",
            f"- **Company:** {item.get('company', '')}",
            f"- **Publication date:** {item.get('published_display') or 'Date unavailable'}",
            f"- **Category:** {item.get('category') or 'Other'}",
            f"- **Coverage count:** {item.get('coverage_count') or 1}",
            f"- **Official source involved:** {'Yes' if item.get('official_source') else 'No'}",
            "- **Sources:**",
            *(source_lines or ["  - Unavailable"]),
            "",
            f"**Feed description:** {item.get('description') or 'No description supplied by the feed.'}",
            "",
        ]
    )


def write_markdown_file(
    path: Path, title: str, items: list[dict[str, Any]], payload: dict[str, Any]
) -> None:
    lines = [
        f"# {title}",
        "",
        f"- **Repository generated:** {payload['generated_at_display']}",
        f"- **Distinct events in this file:** {len(items)}",
        "- **Scope:** Relevant public updates where the monitored company is the main subject or an active party.",
        "- **De-duplication:** Similar coverage is merged and all identified source links are retained.",
        "",
    ]
    if not items:
        lines.extend(["No matching events are currently available.", ""])
    else:
        for number, item in enumerate(items, start=1):
            lines.append(markdown_article(item, number))
    path.write_text("\n".join(lines), encoding="utf-8")


def write_public_knowledge_page(items: list[dict[str, Any]], payload: dict[str, Any]) -> None:
    article_sections: list[str] = []
    for item in items[:300]:
        source_links = " ".join(
            f'<a href="{html.escape(str(source.get("url") or ""), quote=True)}">{html.escape(str(source.get("name") or "Source"))}</a>'
            for source in item.get("sources") or []
            if source.get("url")
        )
        article_sections.append(
            f"""
            <article>
              <h2>{html.escape(str(item.get('title') or 'Untitled article'))}</h2>
              <p><strong>Company:</strong> {html.escape(str(item.get('company') or ''))}</p>
              <p><strong>Category:</strong> {html.escape(str(item.get('category') or 'Other'))}</p>
              <p>{html.escape(str(item.get('description') or 'No description available.'))}</p>
              <p><strong>Sources:</strong> {source_links or 'Unavailable'}</p>
            </article>
            """
        )
    page = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Laboratory News Knowledge Base</title>
<style>body{{max-width:1050px;margin:40px auto;padding:0 20px;font-family:Arial,sans-serif;line-height:1.6;color:#18332b}}article{{margin:24px 0;padding:20px;border:1px solid #dbe7e1;border-radius:12px}}a{{color:#087f5b}}</style></head>
<body><main><h1>Laboratory Services Market News Knowledge Base</h1>
<p>Relevant, de-duplicated public competitor events. Last updated: {html.escape(str(payload['generated_at_display']))}</p>
{''.join(article_sections)}</main></body></html>"""
    (KNOWLEDGE_DIR / "index.html").write_text(page, encoding="utf-8")


def write_knowledge_files(items: list[dict[str, Any]], payload: dict[str, Any]) -> None:
    KNOWLEDGE_DIR.mkdir(parents=True, exist_ok=True)
    write_markdown_file(
        KNOWLEDGE_DIR / "latest.md", "Laboratory Market News - Latest Events", items[:250], payload
    )
    for tracker in TRACKERS:
        company_items = [item for item in items if item.get("company") == tracker["company"]]
        write_markdown_file(
            KNOWLEDGE_DIR / f"{tracker['slug']}.md",
            f"{tracker['company']} News",
            company_items[:180],
            payload,
        )
    manifest = {
        "generated_at": payload["generated_at"],
        "generated_at_display": payload["generated_at_display"],
        "total_distinct_events": len(items),
        "categories": [
            "Product & Services",
            "Clinical, R&D",
            "Partnership, M&A",
            "Financials",
            "Organizational Updates",
            "Leadership Changes",
            "Other",
        ],
        "files": {
            "all_companies": "knowledge/latest.md",
            **{tracker["company"]: f"knowledge/{tracker['slug']}.md" for tracker in TRACKERS},
        },
    }
    (KNOWLEDGE_DIR / "manifest.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    write_public_knowledge_page(items, payload)


def main() -> int:
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=90)
    previous = load_previous()
    candidates: list[dict[str, Any]] = []
    for item in previous.get("items") or []:
        if isinstance(item, dict):
            normalized = normalize_record(item)
            if normalized:
                candidates.append(normalized)
    failures: list[str] = []
    for tracker in TRACKERS:
        try:
            xml_bytes = fetch_feed(str(tracker["query"]))
            fetched = parse_feed(tracker, xml_bytes)
            candidates.extend(fetched)
            print(f"Fetched {tracker['company']}: {len(fetched)} relevant items", file=sys.stderr)
        except Exception as exc:
            failures.append(f"{tracker['company']}: {exc}")
            print(f"Warning: {tracker['company']}: {exc}", file=sys.stderr)
    recent_candidates = []
    for item in candidates:
        published = parse_date(str(item.get("published_at") or ""))
        if published is None or published >= cutoff:
            recent_candidates.append(item)
    items = deduplicate_items(recent_candidates)[:600]
    payload = {
        "generated_at": now.isoformat(),
        "generated_at_display": now.strftime("%d %b %Y, %H:%M UTC"),
        "item_count": len(items),
        "raw_relevant_item_count": len(recent_candidates),
        "duplicates_merged": max(0, len(recent_candidates) - len(items)),
        "failures": failures,
        "items": items,
    }
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    write_knowledge_files(items, payload)
    print(
        f"Wrote {len(items)} distinct relevant events; merged {payload['duplicates_merged']} duplicate coverage items.",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
