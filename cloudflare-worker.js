/*
 * Laboratory News AI - Cloudflare Worker
 *
 * Required Worker secret:
 *   GEMINI_API_KEY
 *
 * Optional Worker variables:
 *   GEMINI_MODEL       - final synthesis model
 *   GROUNDED_MODEL     - web-reading model; defaults to gemini-2.5-flash
 *
 * The Worker reads the public GitHub news repository, deep-reads a balanced
 * set of the selected updates with Google Search grounding, and then produces
 * a short strategic synthesis. If web grounding is unavailable, it falls back
 * to the repository records rather than failing the dashboard.
 */

const ALLOWED_ORIGINS = new Set([
  "https://atanubarik.github.io",
]);

const NEWS_URL =
  "https://raw.githubusercontent.com/AtanuBarik/laboratory-news-monitor/main/data/news.json";

const DEFAULT_MODELS = [
  "gemini-3.5-flash-lite",
  "gemini-3.6-flash",
];

const DEFAULT_GROUNDED_MODEL = "gemini-2.5-flash";
const MAX_REQUESTED_IDS = 600;
const MAX_REPOSITORY_CONTEXT = 70;
const MAX_DEEP_READ_ITEMS = 40;
const GROUNDING_BATCH_SIZE = 20;

const COMPANY_ALIASES = [
  {
    company: "Labcorp",
    aliases: ["labcorp", "lab corp", "laboratory corporation of america"],
  },
  {
    company: "Quest Diagnostics",
    aliases: ["quest diagnostics", "quest diagnostic", "quest"],
  },
  {
    company: "ARUP Laboratories",
    aliases: ["arup laboratories", "arup labs", "arup"],
  },
  {
    company: "Mayo Clinic Laboratories",
    aliases: ["mayo clinic laboratories", "mayo clinic labs", "mayo clinic", "mayo"],
  },
  {
    company: "Sonic Healthcare",
    aliases: ["sonic healthcare", "sonic reference laboratory", "sonic"],
  },
];

const STOP_WORDS = new Set([
  "a", "about", "all", "an", "and", "any", "are", "as", "at", "be", "been",
  "between", "by", "compare", "concerning", "could", "did", "do", "does", "for",
  "from", "give", "has", "have", "how", "in", "is", "it", "latest", "me", "most",
  "news", "of", "on", "or", "please", "recent", "repository", "show", "summarize",
  "tell", "than", "that", "the", "their", "these", "this", "to", "update", "updates",
  "was", "what", "when", "which", "who", "why", "with", "within",
]);

function corsHeaders(origin) {
  return {
    "Access-Control-Allow-Origin": origin,
    "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type",
    "Access-Control-Max-Age": "86400",
    "Vary": "Origin",
  };
}

function jsonResponse(data, status, origin) {
  return new Response(JSON.stringify(data), {
    status,
    headers: {
      "Content-Type": "application/json; charset=utf-8",
      "Cache-Control": "no-store",
      ...corsHeaders(origin),
    },
  });
}

function normalize(value) {
  return String(value || "").toLowerCase();
}

function articleDate(item) {
  const timestamp = Date.parse(item.published_at || "");
  return Number.isFinite(timestamp) ? timestamp : 0;
}

function sortByDate(items) {
  return [...items].sort((a, b) => articleDate(b) - articleDate(a));
}

function detectCompanies(question) {
  const text = normalize(question);
  return COMPANY_ALIASES
    .filter(({ aliases }) => aliases.some((alias) => text.includes(alias)))
    .map(({ company }) => company);
}

function detectDays(question) {
  const text = normalize(question);
  const explicit = text.match(/(?:past|last|previous|within)\s+(\d{1,3})\s+days?/);
  if (explicit) return Math.min(Math.max(Number(explicit[1]), 1), 90);
  if (/today|24 hours?/.test(text)) return 1;
  if (/week|7 days?/.test(text)) return 7;
  if (/fortnight|two weeks|14 days?/.test(text)) return 14;
  if (/quarter|90 days?/.test(text)) return 90;
  if (/month|30 days?/.test(text)) return 30;
  return 30;
}

function searchTerms(question) {
  return normalize(question)
    .replace(/[^a-z0-9\s-]/g, " ")
    .split(/\s+/)
    .filter((word) => word.length > 2 && !STOP_WORDS.has(word))
    .filter((word, index, array) => array.indexOf(word) === index)
    .slice(0, 14);
}

function selectArticles(items, question) {
  const companies = detectCompanies(question);
  const days = detectDays(question);
  const terms = searchTerms(question);
  const cutoff = Date.now() - days * 86400000;

  let candidates = items.filter((item) => {
    const companyOk = !companies.length || companies.includes(item.company);
    const date = articleDate(item);
    const dateOk = !date || date >= cutoff;
    return companyOk && dateOk;
  });

  if (!candidates.length && companies.length) {
    candidates = items.filter((item) => companies.includes(item.company));
  }
  if (!candidates.length) candidates = items;

  const scored = candidates.map((item) => {
    const title = normalize(item.title);
    const description = normalize(item.description);
    const category = normalize(item.category);
    const source = normalize(item.source);
    let score = 0;

    for (const term of terms) {
      if (title.includes(term)) score += 7;
      if (category.includes(term)) score += 4;
      if (description.includes(term)) score += 2;
      if (source.includes(term)) score += 1;
    }

    if (companies.includes(item.company)) score += 9;
    if (item.official_source) score += 2;

    const date = articleDate(item);
    const ageDays = date ? Math.max(0, (Date.now() - date) / 86400000) : 90;
    score += Math.max(0, 6 - ageDays / 8);
    return { item, score };
  });

  scored.sort((a, b) => {
    if (b.score !== a.score) return b.score - a.score;
    return articleDate(b.item) - articleDate(a.item);
  });

  const relevant = scored.filter(({ score }) => score > 0);
  const selected = (relevant.length ? relevant : scored)
    .slice(0, MAX_REPOSITORY_CONTEXT)
    .map(({ item }) => item);

  return {
    selectedItems: selected,
    selectedTotal: selected.length,
    companies,
    days,
    selectionSource: "question",
  };
}

function selectionFromIds(items, requestedIds) {
  if (!Array.isArray(requestedIds)) return null;

  const normalizedIds = requestedIds
    .map((value) => String(value || "").trim())
    .filter(Boolean)
    .slice(0, MAX_REQUESTED_IDS);

  if (!normalizedIds.length) return null;

  const requested = new Set(normalizedIds);
  const selectedItems = sortByDate(
    items.filter((item) => requested.has(String(item.id || "")))
  );

  if (!selectedItems.length) return null;

  return {
    selectedItems,
    selectedTotal: selectedItems.length,
    companies: [...new Set(selectedItems.map((item) => item.company).filter(Boolean))],
    days: null,
    selectionSource: "dashboard_filters",
  };
}

function countBy(items, field) {
  const counts = {};
  for (const item of items) {
    const value = String(item[field] || "Unknown");
    counts[value] = (counts[value] || 0) + 1;
  }
  return Object.fromEntries(
    Object.entries(counts).sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]))
  );
}

function selectionStats(items) {
  const sorted = sortByDate(items);
  const dated = sorted.filter((item) => articleDate(item));
  return {
    article_count: items.length,
    company_count: new Set(items.map((item) => item.company).filter(Boolean)).size,
    official_source_count: items.filter((item) => item.official_source).length,
    newest_publication: dated[0]?.published_display || dated[0]?.published_at || null,
    oldest_publication: dated.at(-1)?.published_display || dated.at(-1)?.published_at || null,
    company_counts: countBy(items, "company"),
    category_counts: countBy(items, "category"),
  };
}

function itemScore(item, terms) {
  const title = normalize(item.title);
  const description = normalize(item.description);
  const category = normalize(item.category);
  let score = item.official_source ? 3 : 0;

  for (const term of terms) {
    if (title.includes(term)) score += 8;
    if (category.includes(term)) score += 4;
    if (description.includes(term)) score += 2;
  }

  const date = articleDate(item);
  const ageDays = date ? Math.max(0, (Date.now() - date) / 86400000) : 90;
  score += Math.max(0, 8 - ageDays / 5);
  return score;
}

function chooseDeepReadItems(items, question) {
  const terms = searchTerms(question);
  const ranked = [...items].sort((a, b) => {
    const scoreDifference = itemScore(b, terms) - itemScore(a, terms);
    return scoreDifference || articleDate(b) - articleDate(a);
  });

  const chosen = [];
  const chosenIds = new Set();
  const seenCompanyCategory = new Set();

  // First cover different company/category combinations so the synthesis is
  // not dominated by one high-volume company or one repeated story type.
  for (const item of ranked) {
    const key = `${item.company || "Unknown"}|${item.category || "Other"}`;
    if (seenCompanyCategory.has(key)) continue;
    chosen.push(item);
    chosenIds.add(String(item.id || item.url || item.title));
    seenCompanyCategory.add(key);
    if (chosen.length >= MAX_DEEP_READ_ITEMS) return chosen;
  }

  // Then fill remaining capacity with the most relevant and recent records.
  for (const item of ranked) {
    const id = String(item.id || item.url || item.title);
    if (chosenIds.has(id)) continue;
    chosen.push(item);
    chosenIds.add(id);
    if (chosen.length >= MAX_DEEP_READ_ITEMS) break;
  }

  return chosen;
}

function compactRepositoryContext(items, generatedAt) {
  const records = items.slice(0, MAX_REPOSITORY_CONTEXT).map((item, index) => {
    const description = String(item.description || "No feed description available.")
      .replace(/\s+/g, " ")
      .slice(0, 650);

    return [
      `[${index + 1}]`,
      `Company: ${item.company || "Unknown"}`,
      `Headline: ${item.title || "Untitled"}`,
      `Publication date: ${item.published_display || item.published_at || "Unavailable"}`,
      `Source: ${item.source || "Unknown"}`,
      `Category: ${item.category || "Other"}`,
      `Feed description: ${description}`,
      `URL: ${item.url || "Unavailable"}`,
    ].join("\n");
  });

  return `Repository last generated: ${generatedAt || "Unknown"}\n\n${records.join("\n\n")}`;
}

function recentHistory(history) {
  if (!Array.isArray(history)) return [];
  return history
    .filter((entry) => entry && ["user", "assistant"].includes(entry.role))
    .slice(-6)
    .map((entry) => ({
      role: entry.role === "assistant" ? "model" : "user",
      parts: [{ text: String(entry.content || "").slice(0, 2500) }],
    }));
}

function extractGeminiText(payload) {
  const parts = payload?.candidates?.[0]?.content?.parts || [];
  return parts.map((part) => part.text || "").join("\n").trim();
}

function modelCandidates(configuredModel) {
  const configured = String(configuredModel || "").trim();
  const retiredModels = new Set([
    "gemini-2.5-flash-lite",
    "models/gemini-2.5-flash-lite",
  ]);

  const candidates = [];
  if (configured && !retiredModels.has(configured)) {
    candidates.push(configured.replace(/^models\//, ""));
  }
  candidates.push(...DEFAULT_MODELS);
  return [...new Set(candidates)];
}

function isModelAvailabilityError(status, message) {
  return status === 404 ||
    /no longer available|not available|not found|unsupported model|model .* unavailable/i.test(message);
}

function safeFilters(filters) {
  if (!filters || typeof filters !== "object") return {};
  const allowed = ["search", "company", "category", "period"];
  return Object.fromEntries(
    allowed
      .map((key) => [key, String(filters[key] || "").slice(0, 250)])
      .filter(([, value]) => value)
  );
}

async function fetchNewsRepository() {
  const response = await fetch(`${NEWS_URL}?cache=${Date.now()}`, {
    headers: { Accept: "application/json" },
  });
  if (!response.ok) {
    throw new Error(`GitHub repository returned HTTP ${response.status}.`);
  }
  return response.json();
}

function groundingBatchPrompt(items, question, batchNumber) {
  const records = items.map((item, index) => [
    `${index + 1}. ${item.title || "Untitled"}`,
    `Company hint: ${item.company || "Unknown"}`,
    `Publisher hint: ${item.source || "Unknown"}`,
    `Date hint: ${item.published_display || item.published_at || "Unknown"}`,
    `Category hint: ${item.category || "Other"}`,
  ].join(" | ")).join("\n");

  return `
You are building evidence notes for a clinical-laboratory competitive-intelligence analyst.
Use Google Search to locate and read the underlying public reporting for the updates below.
Search by the exact headline, company, publisher, and date. Do not merely repeat headlines.

For each update that you can verify, extract only:
- what materially changed;
- the important supporting detail, scale, partner, product, indication, geography, financial metric, or timing;
- why it may matter competitively, commercially, operationally, clinically, or strategically;
- any uncertainty or limitation in the reporting.

Combine duplicate coverage of the same event. Ignore irrelevant false matches. Write compact factual evidence notes for a later synthesis; do not write a polished final answer and do not pad with counts.

User's analytical question, when applicable:
${question || "Create a strategic synthesis of the selected updates."}

Batch ${batchNumber} updates:
${records}
`.trim();
}

async function callGroundedBatch(env, items, question, batchNumber) {
  const model = String(env.GROUNDED_MODEL || DEFAULT_GROUNDED_MODEL)
    .trim()
    .replace(/^models\//, "");
  const endpoint =
    `https://generativelanguage.googleapis.com/v1beta/models/${encodeURIComponent(model)}:generateContent`;

  const response = await fetch(endpoint, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "x-goog-api-key": env.GEMINI_API_KEY,
    },
    body: JSON.stringify({
      contents: [
        {
          role: "user",
          parts: [{ text: groundingBatchPrompt(items, question, batchNumber) }],
        },
      ],
      tools: [{ google_search: {} }],
      generationConfig: { maxOutputTokens: 2400 },
    }),
  });

  const payload = await response.json();
  if (!response.ok) {
    const message = payload?.error?.message || `Grounding returned HTTP ${response.status}.`;
    throw new Error(message);
  }

  const text = extractGeminiText(payload);
  if (!text) throw new Error("Grounding returned an empty response.");
  return { text, model };
}

async function buildGroundedEvidence(env, selectedItems, question) {
  const deepReadItems = chooseDeepReadItems(selectedItems, question);
  if (!deepReadItems.length) {
    return { evidence: "", used: false, itemCount: 0, model: null, error: null };
  }

  const batches = [];
  for (let index = 0; index < deepReadItems.length; index += GROUNDING_BATCH_SIZE) {
    batches.push(deepReadItems.slice(index, index + GROUNDING_BATCH_SIZE));
  }

  try {
    const results = await Promise.all(
      batches.map((batch, index) => callGroundedBatch(env, batch, question, index + 1))
    );
    return {
      evidence: results.map((result, index) => `EVIDENCE BATCH ${index + 1}\n${result.text}`).join("\n\n"),
      used: true,
      itemCount: deepReadItems.length,
      model: results[0]?.model || null,
      error: null,
    };
  } catch (error) {
    console.warn("Web grounding unavailable; using repository context only.", error);
    return {
      evidence: "",
      used: false,
      itemCount: 0,
      model: null,
      error: error instanceof Error ? error.message : "Grounding unavailable",
    };
  }
}

function buildFinalPrompt(mode, question, selection, filters, repository, grounded) {
  const stats = selectionStats(selection.selectedItems);
  const repositoryContext = compactRepositoryContext(
    selection.selectedItems,
    repository.generated_at_display,
  );
  const filterText = Object.keys(filters).length
    ? JSON.stringify(filters, null, 2)
    : "No explicit dashboard filters supplied.";
  const evidenceText = grounded.used
    ? `\nDEEP-READ WEB EVIDENCE\n${grounded.evidence}`
    : "\nNo deeper web evidence was available. Use the repository context cautiously.";

  if (mode === "filtered_summary") {
    const systemInstruction = `
You are a senior clinical-laboratory competitive-intelligence strategist.
Write like an experienced human analyst, not a news aggregator.

The user wants the meaning behind the developments, not a list of headlines, companies, sources, or article counts.
Synthesize patterns across the evidence and explain what is changing, why it matters, and what decision-makers should watch.

Output requirements:
- Start with one sharp executive takeaway, maximum two sentences.
- Follow with three to five short strategic bullets.
- End with a brief "What to watch" section containing two or three forward-looking signals.
- Keep the whole response concise, normally 180-320 words.
- Mention a company, product, partner, or metric only when it is necessary to make the insight concrete.
- Do not use labels such as Headline, Source, Publication date, Category, or Article URL.
- Do not list every story and do not repeat dashboard counts.
- Separate confirmed facts from inference through wording such as "suggests", "could", or "signals".
- Never invent information. If the evidence is thin, say so briefly.
`.trim();

    const userPrompt = `
Create a strategic executive synthesis of the currently filtered news.

Dashboard filters:
${filterText}

Aggregate pattern data across the complete filtered set:
${JSON.stringify(stats, null, 2)}

${evidenceText}

REPOSITORY CONTEXT FOR COVERAGE AND FALLBACK
${repositoryContext}
`.trim();

    return { systemInstruction, userPrompt, stats };
  }

  const systemInstruction = `
You are a sharp clinical-laboratory competitive-intelligence advisor.
Respond like a human colleague giving a concise strategic answer, not like a database or search engine.

Rules:
- Answer the user's question directly in the first sentence.
- Use one to three short paragraphs or up to five bullets.
- Focus on implications, patterns, trade-offs, competitive signals, likely impact, and what to watch.
- Do not automatically repeat company names, headlines, publishers, dates, categories, article counts, or links.
- Mention a specific name or fact only when it materially supports the answer or the user asks for it.
- Do not provide a source list unless the user explicitly requests sources.
- Avoid generic statements; connect insights to the actual deep-read evidence.
- Clearly soften interpretation with words such as "suggests", "likely", or "could".
- Never invent facts. If the available evidence is insufficient, say so in one sentence.
- Keep the response short, crisp, and decision-oriented.
`.trim();

  const userPrompt = `
User question:
${question}

Dashboard filters:
${filterText}

Aggregate pattern data:
${JSON.stringify(stats, null, 2)}

${evidenceText}

REPOSITORY CONTEXT FOR COVERAGE AND FALLBACK
${repositoryContext}
`.trim();

  return { systemInstruction, userPrompt, stats };
}

async function callSynthesisModel(env, prompt, history, maxOutputTokens) {
  let lastModelError = null;

  for (const model of modelCandidates(env.GEMINI_MODEL)) {
    const endpoint =
      `https://generativelanguage.googleapis.com/v1beta/models/${encodeURIComponent(model)}:generateContent`;

    const response = await fetch(endpoint, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "x-goog-api-key": env.GEMINI_API_KEY,
      },
      body: JSON.stringify({
        systemInstruction: { parts: [{ text: prompt.systemInstruction }] },
        contents: [
          ...recentHistory(history),
          { role: "user", parts: [{ text: prompt.userPrompt }] },
        ],
        generationConfig: { maxOutputTokens },
      }),
    });

    const payload = await response.json();
    if (!response.ok) {
      const message = payload?.error?.message || `Gemini returned HTTP ${response.status}.`;
      if (isModelAvailabilityError(response.status, message)) {
        lastModelError = new Error(`${model}: ${message}`);
        continue;
      }
      throw new Error(message);
    }

    const answer = extractGeminiText(payload);
    if (!answer) throw new Error(`Gemini model ${model} returned an empty response.`);
    return { answer, model };
  }

  throw lastModelError || new Error("No supported Gemini model was available.");
}

async function answerRequest(env, requestBody, repository) {
  if (!env.GEMINI_API_KEY) {
    throw new Error("The GEMINI_API_KEY secret has not been configured in Cloudflare.");
  }

  const mode = requestBody.mode === "filtered_summary" ? "filtered_summary" : "chat";
  const question = String(requestBody.question || "").trim();
  const filters = safeFilters(requestBody.filters);
  const repositoryItems = Array.isArray(repository.items) ? repository.items : [];

  const idSelection = selectionFromIds(repositoryItems, requestBody.article_ids);
  const selection = idSelection || selectArticles(repositoryItems, question);
  const grounded = await buildGroundedEvidence(env, selection.selectedItems, question);
  const prompt = buildFinalPrompt(mode, question, selection, filters, repository, grounded);
  const result = await callSynthesisModel(
    env,
    prompt,
    requestBody.history,
    mode === "filtered_summary" ? 1300 : 900,
  );

  return {
    answer: result.answer,
    mode,
    repository_updated: repository.generated_at_display || null,
    selected_total: selection.selectedTotal,
    selection_source: selection.selectionSource,
    selection_stats: prompt.stats,
    model: result.model,
    grounding_used: grounded.used,
    grounding_model: grounded.model,
    deep_read_items: grounded.itemCount,
    grounding_warning: grounded.error,
  };
}

export default {
  async fetch(request, env) {
    const requestOrigin = request.headers.get("Origin");
    const origin = ALLOWED_ORIGINS.has(requestOrigin)
      ? requestOrigin
      : "https://atanubarik.github.io";
    const url = new URL(request.url);

    if (request.method === "OPTIONS") {
      if (requestOrigin && !ALLOWED_ORIGINS.has(requestOrigin)) {
        return jsonResponse({ error: "Origin not allowed." }, 403, origin);
      }
      return new Response(null, { status: 204, headers: corsHeaders(origin) });
    }

    if (request.method === "GET") {
      return jsonResponse(
        {
          ok: true,
          service: "Laboratory News AI",
          endpoint: url.pathname,
          gemini_configured: Boolean(env.GEMINI_API_KEY),
          configured_model: env.GEMINI_MODEL || null,
          grounded_model: env.GROUNDED_MODEL || DEFAULT_GROUNDED_MODEL,
          fallback_models: DEFAULT_MODELS,
          capabilities: [
            "strategic_repository_chat",
            "strategic_filtered_summary",
            "google_search_deep_read",
          ],
        },
        200,
        origin,
      );
    }

    if (request.method !== "POST") {
      return jsonResponse({ error: "Method not allowed." }, 405, origin);
    }

    if (requestOrigin && !ALLOWED_ORIGINS.has(requestOrigin)) {
      return jsonResponse({ error: "Origin not allowed." }, 403, origin);
    }

    try {
      const contentType = request.headers.get("Content-Type") || "";
      if (!contentType.includes("application/json")) {
        return jsonResponse({ error: "Content-Type must be application/json." }, 415, origin);
      }

      const body = await request.json();
      const mode = body?.mode === "filtered_summary" ? "filtered_summary" : "chat";
      const question = String(body?.question || "").trim();

      if (mode === "chat" && question.length < 3) {
        return jsonResponse({ error: "Please enter a longer question." }, 400, origin);
      }
      if (question.length > 1500) {
        return jsonResponse({ error: "Question must be 1,500 characters or fewer." }, 400, origin);
      }
      if (mode === "filtered_summary" && !Array.isArray(body?.article_ids)) {
        return jsonResponse({ error: "Filtered summaries require article_ids." }, 400, origin);
      }

      const repository = await fetchNewsRepository();
      const result = await answerRequest(env, body, repository);
      return jsonResponse(result, 200, origin);
    } catch (error) {
      console.error(error);
      return jsonResponse(
        { error: error instanceof Error ? error.message : "Unexpected server error." },
        500,
        origin,
      );
    }
  },
};
