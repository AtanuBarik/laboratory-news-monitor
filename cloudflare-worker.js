/*
 * Laboratory News AI - Cloudflare Worker
 *
 * Required secret:
 *   GEMINI_API_KEY
 *
 * The Worker first resolves and reads public publisher pages directly. Gemini
 * then summarizes the extracted article text without web tools. If direct page
 * reading is not possible, the Worker falls back to the current Interactions
 * API with URL Context and Google Search. This avoids making email delivery
 * dependent on Google News redirect URLs or a single grounding tool path.
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
const MAX_ARTICLE_URLS = 8;
const MAX_PAGE_CHARS = 28000;
const MIN_PAGE_CHARS = 500;

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
  return uniqueStrings([item?.url, ...sourceUrls, item?.source_url], MAX_ARTICLE_URLS);
}

function articleSourceNames(item) {
  const sourceNames = Array.isArray(item?.sources)
    ? item.sources.map((source) => source?.name)
    : [];
  return uniqueStrings([item?.source, ...sourceNames], 12);
}

function extractGenerateContentText(payload) {
  const parts = payload?.candidates?.[0]?.content?.parts || [];
  return parts.map((part) => part?.text || "").join("\n").trim();
}

function extractInteractionText(payload) {
  const textBlocks = [];
  for (const step of payload?.steps || []) {
    if (step?.type !== "model_output") continue;
    for (const block of step?.content || []) {
      if (block?.type === "text" && block?.text) textBlocks.push(block.text);
    }
  }
  return textBlocks.join("\n").trim();
}

function modelCandidates(env) {
  const configured = [env.GROUNDED_MODEL, env.GEMINI_MODEL]
    .map((value) => String(value || "").trim().replace(/^models\//, ""))
    .filter(Boolean)
    .filter((value) => !RETIRED_OR_BLOCKED_MODELS.has(value));
  return uniqueStrings([DEFAULT_MODEL, ...FALLBACK_MODELS, ...configured], 8);
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
  return String(message).replace(/\s+/g, " ").slice(0, 600);
}

function retryableStatus(status) {
  return status === 429 || status === 500 || status === 502 || status === 503 || status === 504;
}

async function callGenerateContent(env, prompt, options = {}) {
  if (!env.GEMINI_API_KEY) {
    throw new Error("The GEMINI_API_KEY secret has not been configured in Cloudflare.");
  }

  const attempts = [];
  for (const model of modelCandidates(env)) {
    const endpoint =
      `https://generativelanguage.googleapis.com/v1beta/models/${encodeURIComponent(model)}:generateContent`;
    const requestBody = {
      contents: [{ role: "user", parts: [{ text: prompt }] }],
      generationConfig: { maxOutputTokens: options.maxOutputTokens || 2200 },
    };

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
          api: "generateContent",
          model,
          status: "network_error",
          error: error instanceof Error ? error.message : String(error),
        });
        if (retry === 0) {
          await sleep(800);
          continue;
        }
        break;
      }

      const payload = safeJson(rawText) || {};
      if (!response.ok) {
        const error = conciseError(response.status, payload, rawText);
        attempts.push({ api: "generateContent", model, status: response.status, error });
        if (retry === 0 && retryableStatus(response.status)) {
          await sleep(response.status === 429 ? 1800 : 800);
          continue;
        }
        break;
      }

      const answer = extractGenerateContentText(payload);
      if (!answer) {
        attempts.push({
          api: "generateContent",
          model,
          status: "empty_response",
          error: payload?.promptFeedback?.blockReason || "Gemini returned no text.",
        });
        break;
      }
      if (typeof options.validateAnswer === "function" && !options.validateAnswer(answer)) {
        attempts.push({
          api: "generateContent",
          model,
          status: "unusable_answer",
          error: `Response length ${answer.split(/\s+/).filter(Boolean).length} words or prohibited wording.`,
          sample: answer.slice(0, 240),
        });
        break;
      }

      return { answer, model, api: "generateContent", attempts };
    }
  }

  const error = new Error("All generateContent attempts failed.");
  error.attempts = attempts;
  throw error;
}

async function callInteraction(env, prompt, options = {}) {
  if (!env.GEMINI_API_KEY) {
    throw new Error("The GEMINI_API_KEY secret has not been configured in Cloudflare.");
  }

  const attempts = [];
  const toolProfiles = options.useWebTools === false
    ? [{ name: "none", tools: undefined }]
    : [
        {
          name: "url_context_and_google_search",
          tools: [{ type: "url_context" }, { type: "google_search" }],
        },
        { name: "google_search", tools: [{ type: "google_search" }] },
        { name: "url_context", tools: [{ type: "url_context" }] },
      ];

  for (const model of modelCandidates(env)) {
    for (const profile of toolProfiles) {
      const requestBody = {
        model,
        store: false,
        input: prompt,
      };
      if (profile.tools) requestBody.tools = profile.tools;

      for (let retry = 0; retry < 2; retry += 1) {
        let response;
        let rawText = "";
        try {
          response = await fetch(
            "https://generativelanguage.googleapis.com/v1beta/interactions",
            {
              method: "POST",
              headers: {
                "Content-Type": "application/json",
                "x-goog-api-key": env.GEMINI_API_KEY,
              },
              body: JSON.stringify(requestBody),
            },
          );
          rawText = await response.text();
        } catch (error) {
          attempts.push({
            api: "interactions",
            model,
            tools: profile.name,
            status: "network_error",
            error: error instanceof Error ? error.message : String(error),
          });
          if (retry === 0) {
            await sleep(800);
            continue;
          }
          break;
        }

        const payload = safeJson(rawText) || {};
        if (!response.ok) {
          const error = conciseError(response.status, payload, rawText);
          attempts.push({
            api: "interactions",
            model,
            tools: profile.name,
            status: response.status,
            error,
          });
          if (retry === 0 && retryableStatus(response.status)) {
            await sleep(response.status === 429 ? 1800 : 800);
            continue;
          }
          break;
        }

        const answer = extractInteractionText(payload);
        if (!answer) {
          attempts.push({
            api: "interactions",
            model,
            tools: profile.name,
            status: "empty_response",
            error: "Interactions API returned no model text.",
          });
          break;
        }
        if (typeof options.validateAnswer === "function" && !options.validateAnswer(answer)) {
          attempts.push({
            api: "interactions",
            model,
            tools: profile.name,
            status: "unusable_answer",
            error: `Response length ${answer.split(/\s+/).filter(Boolean).length} words or prohibited wording.`,
            sample: answer.slice(0, 240),
          });
          break;
        }

        return {
          answer,
          model,
          api: "interactions",
          toolProfile: profile.name,
          payload,
          attempts,
        };
      }
    }
  }

  const error = new Error("All Interactions API attempts failed.");
  error.attempts = attempts;
  throw error;
}

async function fetchRepository() {
  const response = await fetch(`${NEWS_URL}?cache=${Date.now()}`, {
    headers: { Accept: "application/json" },
  });
  if (!response.ok) throw new Error(`GitHub repository returned HTTP ${response.status}.`);
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

function decodeHtmlEntities(value) {
  const named = {
    amp: "&",
    apos: "'",
    quot: '"',
    lt: "<",
    gt: ">",
    nbsp: " ",
    ndash: "–",
    mdash: "—",
    rsquo: "’",
    lsquo: "‘",
    rdquo: "”",
    ldquo: "“",
  };
  return String(value || "")
    .replace(/&#(\d+);/g, (_, number) => String.fromCodePoint(Number(number)))
    .replace(/&#x([0-9a-f]+);/gi, (_, number) => String.fromCodePoint(parseInt(number, 16)))
    .replace(/&([a-z]+);/gi, (match, name) => named[name.toLowerCase()] ?? match);
}

function extractJsonLdBodies(page) {
  const bodies = [];
  const bodyRegex = /"articleBody"\s*:\s*"((?:\\.|[^"\\])*)"/gi;
  let match;
  while ((match = bodyRegex.exec(page)) !== null) {
    try {
      const decoded = JSON.parse(`"${match[1]}"`);
      if (decoded.length >= MIN_PAGE_CHARS) bodies.push(decoded);
    } catch {
      // Ignore malformed JSON-LD blocks.
    }
  }
  return bodies;
}

function extractMetaDescription(page) {
  const patterns = [
    /<meta[^>]+(?:name|property)=["'](?:description|og:description|twitter:description)["'][^>]+content=["']([^"']+)["'][^>]*>/i,
    /<meta[^>]+content=["']([^"']+)["'][^>]+(?:name|property)=["'](?:description|og:description|twitter:description)["'][^>]*>/i,
  ];
  for (const pattern of patterns) {
    const match = page.match(pattern);
    if (match?.[1]) return decodeHtmlEntities(match[1]).replace(/\s+/g, " ").trim();
  }
  return "";
}

function htmlToReadableText(page) {
  const jsonLdBodies = extractJsonLdBodies(page);
  if (jsonLdBodies.length) {
    return [...new Set(jsonLdBodies)].join("\n\n").slice(0, MAX_PAGE_CHARS);
  }

  const metaDescription = extractMetaDescription(page);
  let text = page
    .replace(/<!--([\s\S]*?)-->/g, " ")
    .replace(/<(script|style|noscript|svg|canvas|iframe|head)[^>]*>[\s\S]*?<\/\1>/gi, " ")
    .replace(/<br\s*\/?\s*>/gi, "\n")
    .replace(/<\/(p|div|article|section|li|h[1-6])>/gi, "\n")
    .replace(/<[^>]+>/g, " ");
  text = decodeHtmlEntities(text)
    .replace(/[ \t]+/g, " ")
    .replace(/\n\s*\n+/g, "\n")
    .trim();
  if (metaDescription && !text.includes(metaDescription)) {
    text = `${metaDescription}\n${text}`;
  }
  return text.slice(0, MAX_PAGE_CHARS);
}

function isHttpUrl(value) {
  try {
    const url = new URL(value);
    return url.protocol === "https:" || url.protocol === "http:";
  } catch {
    return false;
  }
}

async function readPublicPage(url) {
  if (!isHttpUrl(url)) {
    return { inputUrl: url, status: "invalid_url", finalUrl: null, text: "" };
  }
  try {
    const response = await fetch(url, {
      redirect: "follow",
      headers: {
        Accept: "text/html,application/xhtml+xml,text/plain,application/json;q=0.9,*/*;q=0.5",
        "User-Agent":
          "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/151 Safari/537.36",
      },
    });
    const contentType = response.headers.get("content-type") || "";
    if (!response.ok) {
      return {
        inputUrl: url,
        status: `http_${response.status}`,
        finalUrl: response.url || null,
        text: "",
      };
    }
    if (!/(text\/html|application\/xhtml\+xml|text\/plain|application\/json)/i.test(contentType)) {
      return {
        inputUrl: url,
        status: `unsupported_${contentType.slice(0, 60)}`,
        finalUrl: response.url || null,
        text: "",
      };
    }
    const raw = await response.text();
    const text = contentType.includes("json")
      ? decodeHtmlEntities(raw).replace(/\s+/g, " ").slice(0, MAX_PAGE_CHARS)
      : htmlToReadableText(raw);
    return {
      inputUrl: url,
      finalUrl: response.url || url,
      status: text.length >= MIN_PAGE_CHARS ? "success" : "too_short",
      text: text.length >= MIN_PAGE_CHARS ? text : "",
      characters: text.length,
    };
  } catch (error) {
    return {
      inputUrl: url,
      status: "fetch_error",
      finalUrl: null,
      text: "",
      error: error instanceof Error ? error.message : String(error),
    };
  }
}

async function directArticleEvidence(item) {
  const urls = articleUrls(item);
  const results = await Promise.all(urls.slice(0, 5).map((url) => readPublicPage(url)));
  const successful = results.filter((result) => result.status === "success" && result.text);
  const seen = new Set();
  const evidence = [];
  for (const result of successful) {
    const fingerprint = result.text.slice(0, 800).toLowerCase();
    if (seen.has(fingerprint)) continue;
    seen.add(fingerprint);
    evidence.push({
      url: result.finalUrl || result.inputUrl,
      text: result.text,
    });
  }
  return { evidence, retrievals: results };
}

function emailSummaryPrompt(item, directEvidence = []) {
  const urls = articleUrls(item);
  const sources = articleSourceNames(item);
  const feedText = String(item?.description || "")
    .replace(/<[^>]+>/g, " ")
    .replace(/\s+/g, " ")
    .trim()
    .slice(0, 1800);
  const evidenceText = directEvidence.length
    ? directEvidence
        .map(
          (entry, index) =>
            `DIRECTLY RETRIEVED PAGE ${index + 1}\nURL: ${entry.url}\nCONTENT:\n${entry.text}`,
        )
        .join("\n\n")
    : "No publisher page text was directly retrieved. Use URL Context and Google Search to find and verify the exact event.";

  return `
You are preparing one factual competitor-intelligence article summary for a Quest Diagnostics email report.

SELECTED UPDATE
Company: ${item?.company || "Unknown"}
Category: ${item?.category || "Other"}
Exact headline: ${item?.title || "Untitled"}
Publication date hint: ${item?.published_display || item?.published_at || "Unknown"}
Publisher hints: ${sources.join(" | ") || "Unknown"}
Feed text: ${feedText || "Unavailable"}
Known public URLs:
${urls.length ? urls.map((url) => `- ${url}`).join("\n") : "- None"}

SOURCE MATERIAL
${evidenceText}

OUTPUT RULES
- Write 150-240 words in two or three short paragraphs.
- Summarize what the article or filing actually says, not the existence of coverage.
- Lead with the event and include all material facts that are present: named parties; test, product, or service; indication; regulatory status; customers; geography; timing; transaction value and conditions; revenue, earnings, growth, margins, guidance, and segment data; study design, population, endpoints and findings; or leadership scope.
- For financial reporting, include the key figures and period comparisons present in the source material.
- For M&A or partnerships, include parties, terms, assets/capabilities, timing and rationale present in the source material.
- Explain strategic significance only after the facts and tie it directly to those facts.
- Do not mention article counts, coverage counts, retrieval, repository evidence, source names, URLs, citations, ticker symbols or legal suffixes.
- Do not invent or estimate missing information.
- If the material does not contain enough substantive facts to create a reliable summary, output exactly: CONTENT_UNAVAILABLE
- Return only the summary or CONTENT_UNAVAILABLE.
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
    "most important follow-up is to confirm",
  ];
  if (forbidden.some((term) => lower.includes(term))) return false;
  const wordCount = text.split(/\s+/).filter(Boolean).length;
  return wordCount >= 80 && wordCount <= 330;
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
  const diagnostics = [];
  const direct = await directArticleEvidence(item);
  diagnostics.push({
    stage: "direct_page_retrieval",
    successful_pages: direct.evidence.length,
    retrievals: direct.retrievals.map((result) => ({
      input_url: result.inputUrl,
      final_url: result.finalUrl,
      status: result.status,
      characters: result.characters || 0,
      error: result.error || null,
    })),
  });

  if (direct.evidence.length) {
    try {
      const result = await callGenerateContent(env, emailSummaryPrompt(item, direct.evidence), {
        maxOutputTokens: 1800,
        validateAnswer: usableEmailSummary,
      });
      return {
        mode: "email_article_summary",
        article_id: item.id,
        content_verified: true,
        answer: result.answer.trim(),
        model: result.model,
        api: result.api,
        evidence_mode: "direct_page_text",
        repository_updated: repository.generated_at_display || null,
        diagnostic_attempts: [...diagnostics, ...result.attempts],
      };
    } catch (error) {
      diagnostics.push(...(Array.isArray(error?.attempts) ? error.attempts : []));
    }
  }

  try {
    const result = await callInteraction(env, emailSummaryPrompt(item, []), {
      useWebTools: true,
      validateAnswer: usableEmailSummary,
    });
    return {
      mode: "email_article_summary",
      article_id: item.id,
      content_verified: true,
      answer: result.answer.trim(),
      model: result.model,
      api: result.api,
      tool_profile: result.toolProfile,
      evidence_mode: "interactions_web_tools",
      repository_updated: repository.generated_at_display || null,
      diagnostic_attempts: [...diagnostics, ...result.attempts],
    };
  } catch (error) {
    diagnostics.push(...(Array.isArray(error?.attempts) ? error.attempts : []));
    console.error("Email summary failed", error);
    return {
      mode: "email_article_summary",
      article_id: item.id,
      content_verified: false,
      answer: "CONTENT_UNAVAILABLE",
      error: error instanceof Error ? error.message : "Summary generation failed.",
      diagnostic_attempts: diagnostics,
      repository_updated: repository.generated_at_display || null,
    };
  }
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
Act as a senior clinical-laboratory competitive-intelligence analyst. Create a concise executive synthesis of the filtered view. Start with one decisive takeaway, then give three to five strategic bullets and a short "What to watch" section. Focus on what changed, material facts, patterns, competitive consequences, customer impact, economics and execution. Do not list every headline or repeat article counts. Distinguish facts from inference and do not invent details.

Filters: ${JSON.stringify(filters)}
Repository updated: ${repositoryUpdated || "Unknown"}

SELECTED RECORDS
${context}
`.trim();
  }
  return `
Act as a concise human competitive-intelligence adviser for the clinical-laboratory market. Answer the user's question directly. Focus on facts, strategic meaning, trade-offs and what to watch. Do not automatically list headlines, publishers, dates, counts or URLs. Mention names and figures only when they materially support the answer. Never invent details.

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
  const result = await callInteraction(
    env,
    chatPrompt(mode, question, selected, filters, repository.generated_at_display),
    { useWebTools: true },
  );
  return {
    mode,
    answer: result.answer,
    selected_total: selected.length,
    model: result.model,
    api: result.api,
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
          model_candidates: modelCandidates(env),
          summary_pipeline: [
            "direct_publisher_page_read",
            "gemini_generateContent_without_web_tools",
            "interactions_url_context_and_google_search_fallback",
          ],
          capabilities: [
            "email_article_summary",
            "direct_publisher_page_read",
            "interactions_api",
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
