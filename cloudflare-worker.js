/*
 * Laboratory News AI - Cloudflare Worker
 *
 * Required secret:
 *   GEMINI_API_KEY
 *
 * Optional variables:
 *   GEMINI_MODEL
 *   GROUNDED_MODEL
 *
 * The Worker always tries current stable Gemini models before older configured
 * values. Email-summary requests degrade from URL Context + Google Search to
 * Google Search only, and return CONTENT_UNAVAILABLE with diagnostics instead
 * of an HTTP 500 when the underlying content cannot be verified.
 */

const ALLOWED_ORIGINS = new Set(["https://atanubarik.github.io"]);
const NEWS_URL =
  "https://raw.githubusercontent.com/AtanuBarik/laboratory-news-monitor/main/data/news.json";
const DEFAULT_MODEL = "gemini-3.6-flash";
const FALLBACK_MODELS = ["gemini-3.5-flash", "gemini-3.5-flash-lite"];
const RETIRED_OR_BLOCKED_MODELS = new Set([
  "gemini-2.5-flash-lite",
  "models/gemini-2.5-flash-lite",
]);
const MAX_REQUESTED_IDS = 600;
const MAX_CHAT_ITEMS = 50;

const COMPANY_ALIASES = [
  ["Labcorp", ["labcorp", "lab corp", "laboratory corporation of america"]],
  ["Quest Diagnostics", ["quest diagnostics", "quest diagnostic", "quest"]],
  ["ARUP Laboratories", ["arup laboratories", "arup labs", "arup"]],
  ["Mayo Clinic Laboratories", ["mayo clinic laboratories", "mayo clinic labs", "mayo clinic", "mayo"]],
  ["Sonic Healthcare", ["sonic healthcare", "sonic reference laboratory", "sonic"]],
];

function corsHeaders(origin) {
  return {
    "Access-Control-Allow-Origin": origin,
    "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type",
    "Access-Control-Max-Age": "86400",
    Vary: "Origin",
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
  const timestamp = Date.parse(item?.published_at || "");
  return Number.isFinite(timestamp) ? timestamp : 0;
}

function sortByDate(items) {
  return [...items].sort((a, b) => articleDate(b) - articleDate(a));
}

function uniqueStrings(values, maximum = 20) {
  return [...new Set(values.map((value) => String(value || "").trim()).filter(Boolean))]
    .slice(0, maximum);
}

function articleUrls(item) {
  const sourceUrls = Array.isArray(item?.sources)
    ? item.sources.map((source) => source?.url)
    : [];
  return uniqueStrings([item?.url, item?.source_url, ...sourceUrls], 12);
}

function articleSourceNames(item) {
  const sourceNames = Array.isArray(item?.sources)
    ? item.sources.map((source) => source?.name)
    : [];
  return uniqueStrings([item?.source, ...sourceNames], 12);
}

function extractText(payload) {
  const parts = payload?.candidates?.[0]?.content?.parts || [];
  return parts.map((part) => part?.text || "").join("\n").trim();
}

function modelCandidates(env) {
  const configured = [env.GROUNDED_MODEL, env.GEMINI_MODEL]
    .map((value) => String(value || "").trim().replace(/^models\//, ""))
    .filter(Boolean)
    .filter((value) => !RETIRED_OR_BLOCKED_MODELS.has(value));
  return uniqueStrings([DEFAULT_MODEL, ...FALLBACK_MODELS, ...configured], 8);
}

function toolProfiles(useWebTools) {
  if (!useWebTools) {
    return [{ name: "none", tools: undefined }];
  }
  return [
    {
      name: "url_context_and_google_search",
      tools: [{ url_context: {} }, { google_search: {} }],
    },
    {
      name: "google_search",
      tools: [{ google_search: {} }],
    },
    {
      name: "url_context",
      tools: [{ url_context: {} }],
    },
  ];
}

function sleep(milliseconds) {
  return new Promise((resolve) => setTimeout(resolve, milliseconds));
}

function safeJson(text) {
  try {
    return JSON.parse(text);
  } catch {
    return null;
  }
}

function conciseError(status, payload, rawText) {
  const message = payload?.error?.message || rawText || `HTTP ${status}`;
  return String(message).replace(/\s+/g, " ").slice(0, 500);
}

function retryableStatus(status) {
  return status === 429 || status === 500 || status === 502 || status === 503 || status === 504;
}

async function callGemini(env, prompt, options = {}) {
  if (!env.GEMINI_API_KEY) {
    throw new Error("The GEMINI_API_KEY secret has not been configured in Cloudflare.");
  }

  const attempts = [];
  const models = options.model
    ? uniqueStrings([options.model, ...modelCandidates(env)], 8)
    : modelCandidates(env);
  const profiles = toolProfiles(options.useWebTools !== false);

  for (const model of models) {
    for (const profile of profiles) {
      const endpoint =
        `https://generativelanguage.googleapis.com/v1beta/models/${encodeURIComponent(model)}:generateContent`;
      const requestBody = {
        contents: [{ role: "user", parts: [{ text: prompt }] }],
        generationConfig: {
          maxOutputTokens: options.maxOutputTokens || 2200,
        },
      };
      if (profile.tools) requestBody.tools = profile.tools;

      for (let retry = 0; retry < 2; retry += 1) {
        let response;
        let rawText = "";
        try {
          response = await fetch(endpoint, {
            method: "POST",
            headers: {
              "Content-Type": "application/json",
              "x-goog-api-key": env.GEMINI_API_KEY,
            },
            body: JSON.stringify(requestBody),
          });
          rawText = await response.text();
        } catch (error) {
          attempts.push({
            model,
            tools: profile.name,
            status: "network_error",
            error: error instanceof Error ? error.message : String(error),
          });
          if (retry === 0) {
            await sleep(900);
            continue;
          }
          break;
        }

        const payload = safeJson(rawText) || {};
        if (!response.ok) {
          const error = conciseError(response.status, payload, rawText);
          attempts.push({ model, tools: profile.name, status: response.status, error });
          if (retry === 0 && retryableStatus(response.status)) {
            await sleep(response.status === 429 ? 1800 : 900);
            continue;
          }
          break;
        }

        const answer = extractText(payload);
        if (!answer) {
          attempts.push({
            model,
            tools: profile.name,
            status: "empty_response",
            error: payload?.promptFeedback?.blockReason || "Gemini returned no text.",
          });
          break;
        }

        if (typeof options.validateAnswer === "function" && !options.validateAnswer(answer)) {
          attempts.push({
            model,
            tools: profile.name,
            status: "unusable_answer",
            error: "The response did not meet the article-summary quality threshold.",
          });
          break;
        }

        return {
          answer,
          model,
          toolProfile: profile.name,
          payload,
          attempts,
        };
      }
    }
  }

  const last = attempts.at(-1);
  const detail = last
    ? `${last.model}/${last.tools}: ${last.status} ${last.error}`
    : "No Gemini request was attempted.";
  const error = new Error(`All Gemini attempts failed. ${detail}`);
  error.attempts = attempts;
  throw error;
}

async function fetchRepository() {
  const response = await fetch(`${NEWS_URL}?cache=${Date.now()}`, {
    headers: { Accept: "application/json" },
  });
  if (!response.ok) {
    throw new Error(`GitHub repository returned HTTP ${response.status}.`);
  }
  return response.json();
}

function selectionFromIds(items, requestedIds) {
  if (!Array.isArray(requestedIds)) return [];
  const ids = new Set(
    requestedIds
      .map((value) => String(value || "").trim())
      .filter(Boolean)
      .slice(0, MAX_REQUESTED_IDS),
  );
  return sortByDate(items.filter((item) => ids.has(String(item?.id || ""))));
}

function detectCompanies(question) {
  const text = normalize(question);
  return COMPANY_ALIASES
    .filter(([, aliases]) => aliases.some((alias) => text.includes(alias)))
    .map(([company]) => company);
}

function selectByQuestion(items, question) {
  const companies = detectCompanies(question);
  const terms = normalize(question)
    .replace(/[^a-z0-9\s-]/g, " ")
    .split(/\s+/)
    .filter((term) => term.length > 3)
    .slice(0, 15);

  const candidates = items.filter(
    (item) => !companies.length || companies.includes(item.company),
  );
  return [...(candidates.length ? candidates : items)]
    .map((item) => {
      const haystack = normalize(
        `${item.title} ${item.description} ${item.category} ${item.company}`,
      );
      let score = item.official_source ? 2 : 0;
      for (const term of terms) {
        if (normalize(item.title).includes(term)) score += 6;
        else if (haystack.includes(term)) score += 2;
      }
      score += Math.max(0, 4 - (Date.now() - articleDate(item)) / 864000000);
      return { item, score };
    })
    .sort((a, b) => b.score - a.score || articleDate(b.item) - articleDate(a.item))
    .slice(0, MAX_CHAT_ITEMS)
    .map(({ item }) => item);
}

function compactItem(item, index) {
  const description = String(item?.description || "")
    .replace(/<[^>]+>/g, " ")
    .replace(/\s+/g, " ")
    .trim()
    .slice(0, 1000);
  const urls = articleUrls(item);
  return [
    `[${index + 1}]`,
    `ID: ${item?.id || ""}`,
    `Company: ${item?.company || "Unknown"}`,
    `Title: ${item?.title || "Untitled"}`,
    `Category: ${item?.category || "Other"}`,
    `Published: ${item?.published_display || item?.published_at || "Unknown"}`,
    `Publishers: ${articleSourceNames(item).join(" | ") || "Unknown"}`,
    `Feed text: ${description || "Unavailable"}`,
    `URLs: ${urls.join(" | ") || "Unavailable"}`,
  ].join("\n");
}

function emailSummaryPrompt(item) {
  const urls = articleUrls(item);
  const sources = articleSourceNames(item);
  const feedText = String(item?.description || "")
    .replace(/<[^>]+>/g, " ")
    .replace(/\s+/g, " ")
    .trim()
    .slice(0, 1600);

  return `
You are preparing one factual competitor-intelligence article summary for a Quest Diagnostics email report.

RETRIEVAL REQUIREMENTS
1. Open and read the public URLs below when accessible.
2. Search the exact headline together with the company, publisher, and publication date.
3. Prefer the original company announcement, regulatory filing, investor material, journal paper, or named publisher article.
4. Cross-check duplicate coverage, but summarize the underlying event itself. Never discuss how many reports covered it.

SELECTED UPDATE
Company: ${item?.company || "Unknown"}
Category: ${item?.category || "Other"}
Exact headline: ${item?.title || "Untitled"}
Publication date hint: ${item?.published_display || item?.published_at || "Unknown"}
Publisher hints: ${sources.join(" | ") || "Unknown"}
Feed text: ${feedText || "Unavailable"}
Public URLs:
${urls.length ? urls.map((url) => `- ${url}`).join("\n") : "- No usable URL was captured; search by the exact headline."}

OUTPUT RULES
- When substantive content can be verified, write 170-260 words in two or three short paragraphs.
- Lead with what happened and summarize what the reporting actually says.
- Include every material reported fact: named parties; product, test, or service; indication or use case; regulatory status; geography; customers; timing; deal value and conditions; revenue, earnings, growth, margins, guidance, and segment data; study design, population, endpoints, and results; or leadership mandate, as applicable.
- Explain strategic significance only after the factual summary, tied directly to the disclosed facts.
- Do not mention article counts, coverage counts, retrieval, repository evidence, unavailable details, source names, URLs, citations, headline labels, category labels, ticker symbols, or legal suffixes.
- Never invent or estimate missing figures.
- If the pages are paywalled, inaccessible, irrelevant, or contain too little verified content, output exactly: CONTENT_UNAVAILABLE
- Return only the summary or CONTENT_UNAVAILABLE. No heading, preamble, bullets, or bibliography.
`.trim();
}

function usableEmailSummary(answer) {
  const text = String(answer || "").trim();
  if (!text || text.includes("CONTENT_UNAVAILABLE")) return false;
  const lower = text.toLowerCase();
  const forbidden = [
    "repository evidence",
    "available public report",
    "identified across",
    "separate reports",
    "unable to access",
    "cannot access",
    "insufficient information",
    "source article should be reviewed",
  ];
  if (forbidden.some((term) => lower.includes(term))) return false;
  const wordCount = text.split(/\s+/).filter(Boolean).length;
  return wordCount >= 100 && wordCount <= 330;
}

async function answerEmailArticle(env, body, repository) {
  const items = Array.isArray(repository.items) ? repository.items : [];
  const selected = selectionFromIds(items, body.article_ids);
  if (selected.length !== 1) {
    return {
      mode: "email_article_summary",
      content_verified: false,
      answer: "CONTENT_UNAVAILABLE",
      error: "Exactly one valid article_id is required.",
    };
  }

  const item = selected[0];
  try {
    const result = await callGemini(env, emailSummaryPrompt(item), {
      maxOutputTokens: 1800,
      useWebTools: true,
      validateAnswer: usableEmailSummary,
    });
    return {
      mode: "email_article_summary",
      article_id: item.id,
      content_verified: true,
      answer: result.answer.trim(),
      model: result.model,
      tool_profile: result.toolProfile,
      repository_updated: repository.generated_at_display || null,
      diagnostic_attempts: result.attempts,
    };
  } catch (error) {
    console.error("Email summary failed", error);
    return {
      mode: "email_article_summary",
      article_id: item.id,
      content_verified: false,
      answer: "CONTENT_UNAVAILABLE",
      error: error instanceof Error ? error.message : "Summary generation failed.",
      diagnostic_attempts: Array.isArray(error?.attempts) ? error.attempts : [],
      repository_updated: repository.generated_at_display || null,
    };
  }
}

function safeFilters(filters) {
  if (!filters || typeof filters !== "object") return {};
  return Object.fromEntries(
    ["search", "company", "category", "period"]
      .map((key) => [key, String(filters[key] || "").slice(0, 250)])
      .filter(([, value]) => value),
  );
}

function chatPrompt(mode, question, items, filters, repositoryUpdated) {
  const context = items.map(compactItem).join("\n\n");
  if (mode === "filtered_summary") {
    return `
Act as a senior clinical-laboratory competitive-intelligence analyst. Verify and deepen the selected updates before synthesizing them.

Create a concise executive synthesis of the filtered view. Start with one decisive takeaway, then give three to five strategic bullets and a short "What to watch" section. Focus on what changed, material facts, patterns, competitive consequences, customer impact, economics, and execution. Do not list every headline or repeat article counts. Distinguish facts from inference. Do not invent details.

Filters: ${JSON.stringify(filters)}
Repository updated: ${repositoryUpdated || "Unknown"}

SELECTED RECORDS
${context}
`.trim();
  }

  return `
Act as a concise human competitive-intelligence adviser for the clinical-laboratory market. Answer the user's question directly, verifying the underlying reporting. Focus on facts, strategic meaning, trade-offs, and what to watch. Do not automatically list headlines, publishers, dates, counts, or URLs. Mention names and figures only when they materially support the answer. Never invent details.

User question: ${question}
Filters: ${JSON.stringify(filters)}
Repository updated: ${repositoryUpdated || "Unknown"}

SELECTED RECORDS
${context}
`.trim();
}

async function answerChat(env, body, repository) {
  const mode = body.mode === "filtered_summary" ? "filtered_summary" : "chat";
  const question = String(body.question || "").trim();
  const allItems = Array.isArray(repository.items) ? repository.items : [];
  const byIds = selectionFromIds(allItems, body.article_ids);
  const selected = (byIds.length ? byIds : selectByQuestion(allItems, question)).slice(
    0,
    MAX_CHAT_ITEMS,
  );
  if (!selected.length) {
    return {
      mode,
      answer: "No relevant information was found in the current news repository.",
      selected_total: 0,
    };
  }

  const filters = safeFilters(body.filters);
  const result = await callGemini(
    env,
    chatPrompt(mode, question, selected, filters, repository.generated_at_display),
    {
      maxOutputTokens: mode === "filtered_summary" ? 1700 : 1100,
      useWebTools: true,
    },
  );
  return {
    mode,
    answer: result.answer,
    selected_total: selected.length,
    model: result.model,
    tool_profile: result.toolProfile,
    repository_updated: repository.generated_at_display || null,
  };
}

export default {
  async fetch(request, env) {
    const requestOrigin = request.headers.get("Origin");
    const origin = ALLOWED_ORIGINS.has(requestOrigin)
      ? requestOrigin
      : "https://atanubarik.github.io";

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
          gemini_configured: Boolean(env.GEMINI_API_KEY),
          preferred_model: DEFAULT_MODEL,
          configured_models: [env.GROUNDED_MODEL || null, env.GEMINI_MODEL || null],
          model_candidates: modelCandidates(env),
          capabilities: [
            "email_article_summary",
            "strategic_repository_chat",
            "strategic_filtered_summary",
            "google_search_deep_read",
            "url_context_deep_read",
            "model_and_tool_fallbacks",
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
      if (!(request.headers.get("Content-Type") || "").includes("application/json")) {
        return jsonResponse({ error: "Content-Type must be application/json." }, 415, origin);
      }
      const body = await request.json();
      const mode = String(body?.mode || "chat");
      if (!["chat", "filtered_summary", "email_article_summary"].includes(mode)) {
        return jsonResponse({ error: "Unsupported mode." }, 400, origin);
      }
      if (mode === "chat") {
        const question = String(body?.question || "").trim();
        if (question.length < 3 || question.length > 3000) {
          return jsonResponse(
            { error: "Please enter a question between 3 and 3,000 characters." },
            400,
            origin,
          );
        }
      }
      if (mode === "filtered_summary" && !Array.isArray(body?.article_ids)) {
        return jsonResponse({ error: "Filtered summaries require article_ids." }, 400, origin);
      }
      if (mode === "email_article_summary" && !Array.isArray(body?.article_ids)) {
        return jsonResponse({ error: "Email summaries require article_ids." }, 400, origin);
      }

      const repository = await fetchRepository();
      const result = mode === "email_article_summary"
        ? await answerEmailArticle(env, body, repository)
        : await answerChat(env, body, repository);
      return jsonResponse(result, 200, origin);
    } catch (error) {
      console.error(error);
      return jsonResponse(
        {
          error: error instanceof Error ? error.message : "Unexpected server error.",
          diagnostic_attempts: Array.isArray(error?.attempts) ? error.attempts : [],
        },
        500,
        origin,
      );
    }
  },
};
