# Laboratory Market News Monitor

A zero-additional-cost, browser-only starter project that:

- monitors Labcorp, Quest Diagnostics, ARUP Laboratories, and Mayo Clinic Laboratories;
- retrieves public Bing News RSS results every six hours;
- removes duplicate links and retains up to 90 days of results;
- classifies articles into simple rule-based categories;
- publishes a searchable dashboard through GitHub Pages;
- uses no OpenAI API, paid news API, database, server, or locally installed software.

## Important limitation

The dashboard performs collection, filtering, categorization, and display. It does **not**
produce automatic AI summaries because ChatGPT and Microsoft Copilot subscriptions do
not provide a general-purpose API for a custom GitHub website. Use Microsoft Copilot or
ChatGPT separately to analyze the collected headlines, or use a Microsoft 365 Copilot
scheduled prompt if that feature is included and enabled for your account.

## Browser-only setup

### 1. Create the repository

1. Sign in at GitHub.
2. Select **New repository**.
3. Name it `laboratory-news-monitor`.
4. Choose **Public** if you are using GitHub Free and want GitHub Pages.
5. Create the repository with no template files.

Do not place confidential information in a public repository.

### 2. Upload this starter folder

1. Open the new repository.
2. Select **Add file > Upload files**.
3. Drag the contents of this folder into the upload page.
4. Confirm that the hidden `.github` folder is included.
5. Commit directly to the `main` branch.

If your browser does not upload the hidden `.github` folder, create
`.github/workflows/update-news.yml` using **Add file > Create new file**.

### 3. Run the collector once

1. Open the repository's **Actions** tab.
2. Select **Update laboratory news**.
3. Select **Run workflow**.
4. Wait for the run to finish.
5. Confirm that `data/news.json` now contains articles.

If Actions are disabled by an organization policy, use a personal GitHub repository or
ask the GitHub administrator to permit this workflow.

### 4. Publish the dashboard

1. Open **Settings > Pages**.
2. Under **Build and deployment**, choose **Deploy from a branch**.
3. Select branch `main` and folder `/ (root)`.
4. Save.
5. GitHub displays the public site address after deployment.

### 5. Modify search terms

Open `scripts/fetch_news.py`, select the pencil icon, and edit `TRACKERS`.

The starter intentionally avoids bare `Quest` and bare `ARUP` because those terms
produce many unrelated stories. Mayo Clinic is included broadly because it was one of
the requested terms; remove `"Mayo Clinic"` if you only need laboratory-business news.

### 6. Change the refresh frequency

Open `.github/workflows/update-news.yml` and edit:

```yaml
- cron: "30 */6 * * *"
```

Examples, all in UTC:

- Daily at 02:30 UTC / 08:00 IST: `30 2 * * *`
- Twice daily at 02:30 and 14:30 UTC: `30 2,14 * * *`

GitHub may start scheduled workflows a little later than the exact cron time.

## Suggested Microsoft Copilot agent

Create an agent in Microsoft 365 Copilot Agent Builder and add these four public website
knowledge sources:

1. `https://www.labcorp.com/newsroom`
2. `https://newsroom.questdiagnostics.com/`
3. `https://www.aruplab.com/newsroom`
4. `https://news.mayocliniclabs.com/homepage/news/`

Suggested instructions:

```text
You are a competitive-intelligence news analyst for the clinical laboratory market.

Track and analyze developments concerning:
- Labcorp
- Quest Diagnostics
- ARUP Laboratories
- Mayo Clinic Laboratories

When asked for an update:
1. Search current public web information and the configured official newsrooms.
2. Use an explicit date range supplied by the user.
3. Separate genuinely new developments from older background information.
4. Group duplicate reports about the same event.
5. Prioritize official releases, regulators, SEC filings, and credible trade publications.
6. For every development provide company, event date, publication date, category,
   factual summary, strategic implication, and source link.
7. Clearly state when no relevant update is found.
8. Do not infer facts that are not supported by the sources.
```

## Suggested daily Copilot prompt

```text
Find public news published during the past 24 hours concerning Labcorp, Quest
Diagnostics, ARUP Laboratories, or Mayo Clinic Laboratories. Group duplicate coverage
of the same event. For each distinct development, provide the company, event date,
publication date, category, a two-sentence factual summary, why it matters to the U.S.
clinical laboratory market, and source links. Put official company announcements first.
State "No material update found" for any company without a relevant development.
```
