# Laboratory Market Intelligence Monitor

This repository runs a browser-managed competitive-intelligence workflow for:

- Labcorp
- Quest Diagnostics
- ARUP Laboratories
- Mayo Clinic Laboratories
- Sonic Healthcare

## Current workflow

The **Update laboratory news** GitHub Action runs every six hours in India Standard Time at approximately:

- 12:17 AM IST
- 6:17 AM IST
- 12:17 PM IST
- 6:17 PM IST

The non-round minute reduces the risk of GitHub Actions congestion at the start of an hour.

Each run:

1. Searches public news feeds for the monitored companies.
2. Keeps updates in which the monitored company is the subject or an active party.
3. Cleans company names and titles.
4. Maps stories into the current category taxonomy.
5. Merges duplicate or near-duplicate coverage and retains all identified source links.
6. Removes low-value valuation, stock-movement, and unrelated market commentary.
7. Stores the resulting distinct events in `data/news.json`.
8. Rebuilds the files under `knowledge/`.
9. Normalizes all user-visible timestamps to IST.
10. Checks the deployed Cloudflare/Gemini AI Worker.
11. If the Worker is healthy, generates article-content summaries for substantive new events and sends the branded email report through the configured Gmail SMTP account.
12. If the Worker or email stage is unavailable, the news dataset is still committed and published; affected items remain eligible for a later email retry.
13. Writes `data/workflow_health.json` with collection and email health details.
14. Commits updated data back to `main`.

## Duplicate handling

Duplicate coverage is clustered by company, publication timing, category, and normalized title similarity. One distinct event is retained while all identified source URLs are preserved in its `sources` field.

## Categories

- Product & Services
- Clinical, R&D
- Partnership, M&A
- Financials
- Organizational Updates
- Leadership Changes
- Other

## Dashboard publishing

A separate **Publish laboratory monitor** workflow publishes the latest dashboard, news JSON, workflow-health JSON, company logos, category icons, and knowledge files to the `gh-pages` branch whenever relevant files change on `main`.

Public dashboard:

`https://atanubarik.github.io/laboratory-news-monitor/`

The dashboard shows the last update and next scheduled update in IST only.

## AI summarization

The Cloudflare Worker code is stored in `cloudflare-worker.js`. The deployed Worker requires the `GEMINI_API_KEY` secret in Cloudflare.

For email summaries, the Worker first attempts to read the underlying public article page. If that is not usable, Gemini uses URL Context and Google Search to locate and read the exact development. An alert is emailed only when a substantive article-specific summary can be produced; unreadable items are left for a later retry rather than receiving generic filler text.

After changing `cloudflare-worker.js` in GitHub, manually copy the latest file into the `laboratory-news-ai` Cloudflare Worker and deploy it. The GitHub workflow health check detects an outdated Worker and skips the AI/email stage without blocking news collection or dashboard publication.

## Email configuration

The active Gmail SMTP setup uses GitHub repository secrets/variables rather than storing credentials in the repository. Expected settings include:

- `EMAIL_FROM`
- `EMAIL_TO`
- `EMAIL_BCC`
- `SMTP_HOST`
- `SMTP_PORT`
- `SMTP_SECURITY`
- `SMTP_USERNAME`
- `SMTP_APP_PASSWORD`
- `EMAIL_SENDER_NAME` as a repository variable

The email report and its next-update time use IST only.

## Manual test

Open **Actions → Update laboratory news → Run workflow**. Enable the test-email input when you want a preview using up to five substantive verified events.

Do not place confidential data, passwords, API keys, or corporate credentials in this public repository.
