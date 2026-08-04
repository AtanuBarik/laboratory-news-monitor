/*
 * Laboratory News AI - Cloudflare Worker
 *
 * Required Worker secret:
 *   GEMINI_API_KEY
 *
 * Optional Worker variable:
 *   GEMINI_MODEL (defaults to gemini-3.5-flash-lite)
 *
 * This Worker reads public news from:
 *   https://raw.githubusercontent.com/AtanuBarik/laboratory-news-monitor/main/data/news.json
 *
 * Do not commit a Gemini API key to GitHub. Add it in Cloudflare under:
 * Settings > Variables and Secrets > Add > Secret.
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
    .slice(0, 12);
}

function articleDate(item) {
  const timestamp = Date.parse(item.published_at || "");
  return Number.isFinite(timestamp) ? timestamp : 0;
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
      if (title.includes(term)) score += 5;
      if (category.includes(term)) score += 3;
      if (description.includes(term)) score += 2;
      if (source.includes(term)) score += 1;
    }

    if (companies.includes(item.company)) score += 8;
    if (item.official_source) score += 1;

    const ageDays = Math.max(0, (Date.now() - articleDate(item)) / 86400000);
    score += Math.max(0, 4 - ageDays / 10);

    return { item, score };
  });

  scored.sort((a, b) => {
    if (b.score !== a.score) return b.score - a.score;
    return articleDate(b.item) - articleDate(a.item);
  });

  const relevant = scored.filter(({ score }) => score > 0);
  const selected = (relevant.length ? relevant : scored)
    .slice(0, companies.length > 1 ? 35 : 25)
    .map(({ item }) => item);

  return { selected, companies, days };
}

function compactContext(items, generatedAt) {
  const records = items.map((item, index) => {
    const description = String(item.description || "No feed description available.")
      .replace(/\s+/g, " ")
      .slice(0, 550);

    return [
      `[${index + 1}]`,
      `Company: ${item.company || "Unknown"}`,
      `Headline: ${item.title || "Untitled"}`,
      `Publication date: ${item.published_display || item.published_at || "Unavailable"}`,
      `Source: ${item.source || "Unknown"}`,
      `Category: ${item.category || "Other"}`,
      `Official source: ${item.official_source ? "Yes" : "No"}`,
      `Feed description: ${description}`,
      `Original URL: ${item.url || "Unavailable"}`,
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
      parts: [{ text: String(entry.content || "").slice(0, 3000) }],
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
  return status === 404 || /no longer available|not available|not found|unsupported model|model .* unavailable/i.test(message);
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

async function askGemini(env, question, history, repository) {
  if (!env.GEMINI_API_KEY) {
    throw new Error("The GEMINI_API_KEY secret has not been configured in Cloudflare.");
  }

  const { selected, companies, days } = selectArticles(repository.items || [], question);
  const context = compactContext(selected, repository.generated_at_display);

  const systemInstruction = `
You are a competitive-intelligence assistant for the clinical laboratory market.
Answer only from the supplied Laboratory News Repository records.

Rules:
1. Do not invent events, dates, sources, quotations, URLs, or company actions.
2. Distinguish factual reporting from strategic interpretation.
3. For each material development, include company, headline, publication date, source, category, factual summary, strategic implication, and original URL.
4. Group obvious duplicate coverage of the same event.
5. When comparing companies, cover each company separately before the cross-company conclusion.
6. When the supplied records do not answer the question, state: "No relevant information was found in the current news repository."
7. Treat feed descriptions as short source-provided descriptions, not full verified article text.
8. Use readable Markdown with concise headings and bullets. Avoid overly long responses.
`.trim();

  const userPrompt = `
User question: ${question}
Detected companies: ${companies.length ? companies.join(", ") : "All monitored companies"}
Default/identified date range: past ${days} days

LABORATORY NEWS REPOSITORY RECORDS
${context}
`.trim();

  const body = {
    systemInstruction: {
      parts: [{ text: systemInstruction }],
    },
    contents: [
      ...recentHistory(history),
      {
        role: "user",
        parts: [{ text: userPrompt }],
      },
    ],
    generationConfig: {
      maxOutputTokens: 1400,
    },
  };

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
      body: JSON.stringify(body),
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
    if (!answer) {
      throw new Error(`Gemini model ${model} returned an empty response.`);
    }

    return {
      answer,
      repository_updated: repository.generated_at_display || null,
      articles_considered: selected.length,
      model,
    };
  }

  throw lastModelError || new Error("No supported Gemini model was available.");
}

export default {
  async fetch(request, env) {
    const requestOrigin = request.headers.get("Origin");
    const origin = ALLOWED_ORIGINS.has(requestOrigin) ? requestOrigin : "https://atanubarik.github.io";
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
          fallback_models: DEFAULT_MODELS,
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
      const question = String(body?.question || "").trim();
      const history = body?.history;

      if (question.length < 3) {
        return jsonResponse({ error: "Please enter a longer question." }, 400, origin);
      }
      if (question.length > 1500) {
        return jsonResponse({ error: "Question must be 1,500 characters or fewer." }, 400, origin);
      }

      const repository = await fetchNewsRepository();
      const result = await askGemini(env, question, history, repository);
      return jsonResponse(result, 200, origin);
    } catch (error) {
      console.error(error);
      return jsonResponse(
        {
          error: error instanceof Error ? error.message : "Unexpected server error.",
        },
        500,
        origin,
      );
    }
  },
};
