/* Laboratory News AI - Cloudflare Worker
 * Required secret: GEMINI_API_KEY
 *
 * Summary pipeline:
 * 1) read publisher/article pages directly when possible;
 * 2) summarize retrieved article text with Gemini;
 * 3) if direct retrieval is unavailable, use Gemini URL Context + Google Search
 *    to find and read the exact headline before summarizing it.
 */

const ALLOWED_ORIGINS = new Set(["https://atanubarik.github.io"]);
const NEWS_URL = "https://raw.githubusercontent.com/AtanuBarik/laboratory-news-monitor/main/data/news.json";
const WORKER_VERSION = "2026-08-07.2";
const MODEL_CANDIDATES = ["gemini-3.6-flash", "gemini-3.5-flash-lite"];
const MAX_REQUESTED_IDS = 600;
const MAX_CHAT_ITEMS = 45;
const MAX_PAGE_CHARS = 30000;
const MIN_PAGE_CHARS = 450;

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

function normalize(value) { return String(value || "").toLowerCase(); }
function articleDate(item) {
  const value = Date.parse(item?.published_at || "");
  return Number.isFinite(value) ? value : 0;
}
function sortByDate(items) { return [...items].sort((a, b) => articleDate(b) - articleDate(a)); }
function uniqueStrings(values, maximum = 20) {
  return [...new Set(values.map((value) => String(value || "").trim()).filter(Boolean))].slice(0, maximum);
}
function sleep(milliseconds) { return new Promise((resolve) => setTimeout(resolve, milliseconds)); }
function safeJson(text) { try { return JSON.parse(text); } catch { return null; } }

function articleSourceNames(item) {
  const names = Array.isArray(item?.sources) ? item.sources.map((source) => source?.name) : [];
  return uniqueStrings([item?.source, ...names], 10);
}

function isUsefulArticleUrl(value) {
  try {
    const url = new URL(value);
    if (!["http:", "https:"].includes(url.protocol)) return false;
    if (url.hostname === "news.google.com") return true;
    return url.pathname && url.pathname !== "/";
  } catch { return false; }
}

function articleUrls(item) {
  const sourceUrls = Array.isArray(item?.sources) ? item.sources.map((source) => source?.url) : [];
  return uniqueStrings([item?.url, ...sourceUrls].filter(isUsefulArticleUrl), 8);
}

function extractText(payload) {
  const parts = payload?.candidates?.[0]?.content?.parts || [];
  return parts.map((part) => part?.text || "").join("\n").trim();
}

function retryable(status) { return [429, 500, 502, 503, 504].includes(status); }

async function callGemini(env, prompt, options = {}) {
  if (!env.GEMINI_API_KEY) throw new Error("GEMINI_API_KEY is not configured.");
  const attempts = [];
  const toolProfiles = options.webTools
    ? [
        { name: "url_context_and_google_search", tools: [{ url_context: {} }, { google_search: {} }] },
        { name: "google_search", tools: [{ google_search: {} }] },
      ]
    : [{ name: "none", tools: undefined }];

  for (const model of MODEL_CANDIDATES) {
    for (const profile of toolProfiles) {
      const endpoint = `https://generativelanguage.googleapis.com/v1beta/models/${encodeURIComponent(model)}:generateContent`;
      const body = {
        contents: [{ role: "user", parts: [{ text: prompt }] }],
        generationConfig: { maxOutputTokens: options.maxOutputTokens || 2200 },
      };
      if (profile.tools) body.tools = profile.tools;

      for (let retry = 0; retry < 2; retry += 1) {
        let response;
        let raw = "";
        try {
          response = await fetch(endpoint, {
            method: "POST",
            headers: { "Content-Type": "application/json", "x-goog-api-key": env.GEMINI_API_KEY },
            body: JSON.stringify(body),
          });
          raw = await response.text();
        } catch (error) {
          attempts.push({ api: "generateContent", model, tools: profile.name, status: "network_error", error: String(error) });
          if (retry === 0) { await sleep(900); continue; }
          break;
        }
        const payload = safeJson(raw) || {};
        if (!response.ok) {
          const error = String(payload?.error?.message || raw || `HTTP ${response.status}`).replace(/\s+/g, " ").slice(0, 500);
          attempts.push({ api: "generateContent", model, tools: profile.name, status: response.status, error });
          if (retry === 0 && retryable(response.status)) { await sleep(response.status === 429 ? 1800 : 900); continue; }
          break;
        }
        const answer = extractText(payload);
        if (!answer) {
          attempts.push({ api: "generateContent", model, tools: profile.name, status: "empty_response", error: payload?.promptFeedback?.blockReason || "No model text." });
          break;
        }
        if (typeof options.validate === "function" && !options.validate(answer)) {
          attempts.push({ api: "generateContent", model, tools: profile.name, status: "unusable_answer", error: `Rejected ${answer.split(/\s+/).length}-word response.`, sample: answer.slice(0, 240) });
          break;
        }
        return { answer, model, toolProfile: profile.name, attempts };
      }
    }
  }
  const error = new Error("All supported Gemini attempts failed.");
  error.attempts = attempts;
  throw error;
}

async function fetchRepository() {
  const response = await fetch(`${NEWS_URL}?cache=${Date.now()}`, { headers: { Accept: "application/json" } });
  if (!response.ok) throw new Error(`GitHub repository returned HTTP ${response.status}.`);
  return response.json();
}

function selectionFromIds(items, requestedIds) {
  if (!Array.isArray(requestedIds)) return [];
  const ids = new Set(requestedIds.map((value) => String(value || "").trim()).filter(Boolean).slice(0, MAX_REQUESTED_IDS));
  return sortByDate(items.filter((item) => ids.has(String(item?.id || ""))));
}

function decodeHtmlEntities(value) {
  const named = { amp: "&", apos: "'", quot: '"', lt: "<", gt: ">", nbsp: " ", ndash: "–", mdash: "—", rsquo: "’", lsquo: "‘", rdquo: "”", ldquo: "“" };
  return String(value || "")
    .replace(/&#(\d+);/g, (_, number) => String.fromCodePoint(Number(number)))
    .replace(/&#x([0-9a-f]+);/gi, (_, number) => String.fromCodePoint(parseInt(number, 16)))
    .replace(/&([a-z]+);/gi, (match, name) => named[name.toLowerCase()] ?? match);
}

function htmlToText(page) {
  const bodies = [];
  const bodyRegex = /"articleBody"\s*:\s*"((?:\\.|[^"\\])*)"/gi;
  let match;
  while ((match = bodyRegex.exec(page)) !== null) {
    try {
      const decoded = JSON.parse(`"${match[1]}"`);
      if (decoded.length >= MIN_PAGE_CHARS) bodies.push(decoded);
    } catch {}
  }
  if (bodies.length) return [...new Set(bodies)].join("\n\n").slice(0, MAX_PAGE_CHARS);
  let text = page
    .replace(/<!--([\s\S]*?)-->/g, " ")
    .replace(/<(script|style|noscript|svg|canvas|iframe|head|nav|footer)[^>]*>[\s\S]*?<\/\1>/gi, " ")
    .replace(/<br\s*\/?\s*>/gi, "\n")
    .replace(/<\/(p|div|article|section|li|h[1-6])>/gi, "\n")
    .replace(/<[^>]+>/g, " ");
  return decodeHtmlEntities(text).replace(/[ \t]+/g, " ").replace(/\n\s*\n+/g, "\n").trim().slice(0, MAX_PAGE_CHARS);
}

function titleTokens(item) {
  const stop = new Set(["the","and","for","with","from","that","this","into","labcorp","quest","diagnostics","arup","laboratories","mayo","clinic","sonic","healthcare"]);
  return normalize(item?.title).replace(/[^a-z0-9\s]/g, " ").split(/\s+/).filter((word) => word.length > 3 && !stop.has(word)).slice(0, 12);
}

function evidenceLooksRelevant(text, item) {
  const lower = normalize(text);
  const companyTerms = COMPANY_ALIASES.find(([company]) => company === item?.company)?.[1] || [];
  const companyMatch = companyTerms.some((term) => lower.includes(term));
  const tokens = titleTokens(item);
  const tokenMatches = tokens.filter((token) => lower.includes(token)).length;
  return companyMatch && tokenMatches >= Math.min(2, Math.max(1, tokens.length));
}

async function readPublicPage(url, item) {
  try {
    const response = await fetch(url, {
      redirect: "follow",
      headers: { Accept: "text/html,application/xhtml+xml,text/plain;q=0.9,*/*;q=0.4", "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/151 Safari/537.36" },
    });
    if (!response.ok) return { inputUrl: url, finalUrl: response.url || null, status: `http_${response.status}`, text: "" };
    const type = response.headers.get("content-type") || "";
    if (!/(text\/html|application\/xhtml\+xml|text\/plain)/i.test(type)) return { inputUrl: url, finalUrl: response.url || null, status: "unsupported_content", text: "" };
    const text = type.includes("html") ? htmlToText(await response.text()) : (await response.text()).slice(0, MAX_PAGE_CHARS);
    if (text.length < MIN_PAGE_CHARS) return { inputUrl: url, finalUrl: response.url || url, status: "too_short", characters: text.length, text: "" };
    if (!evidenceLooksRelevant(text, item)) return { inputUrl: url, finalUrl: response.url || url, status: "not_matching_story", characters: text.length, text: "" };
    return { inputUrl: url, finalUrl: response.url || url, status: "success", characters: text.length, text };
  } catch (error) {
    return { inputUrl: url, finalUrl: null, status: "fetch_error", error: error instanceof Error ? error.message : String(error), text: "" };
  }
}

async function directArticleEvidence(item) {
  const urls = articleUrls(item);
  const retrievals = await Promise.all(urls.slice(0, 5).map((url) => readPublicPage(url, item)));
  const evidence = [];
  const seen = new Set();
  for (const result of retrievals) {
    if (result.status !== "success" || !result.text) continue;
    const fingerprint = result.text.slice(0, 1000).toLowerCase();
    if (seen.has(fingerprint)) continue;
    seen.add(fingerprint);
    evidence.push({ url: result.finalUrl || result.inputUrl, text: result.text });
  }
  return { evidence, retrievals };
}

function emailSummaryPrompt(item, evidence = []) {
  const urls = articleUrls(item);
  const sourceNames = articleSourceNames(item);
  const feedText = String(item?.description || "").replace(/<[^>]+>/g, " ").replace(/\s+/g, " ").trim().slice(0, 1500);
  const evidenceText = evidence.length
    ? evidence.map((entry, index) => `RETRIEVED ARTICLE ${index + 1}\n${entry.text}`).join("\n\n")
    : "No page text was directly retrieved. Search the web for the exact headline and company, open the relevant result, and summarize only the matching event.";
  return `You are writing one factual competitor-intelligence summary for a Quest Diagnostics email report.\n\nSELECTED EVENT\nCompany: ${item?.company || "Unknown"}\nCategory: ${item?.category || "Other"}\nExact headline: ${item?.title || "Untitled"}\nPublication date: ${item?.published_display || item?.published_at || "Unknown"}\nPublisher hints: ${sourceNames.join(" | ") || "Unknown"}\nFeed text: ${feedText || "Unavailable"}\nCaptured URLs:\n${urls.map((url) => `- ${url}`).join("\n") || "- None"}\n\nSOURCE MATERIAL\n${evidenceText}\n\nOUTPUT RULES\n- Write 150-250 words in two or three short paragraphs.\n- Summarize what the underlying article, filing, announcement, study or report actually says.\n- Include the material facts and numbers that are disclosed. Financials must include key revenue/earnings/growth/margin/guidance or segment figures when reported. M&A/partnerships must include parties, terms/value, capabilities, timing/conditions and rationale when reported. Products/services must include the offering, intended use, regulatory status, customer/geographic scope and differentiation when reported. Clinical/R&D must include study design/population/endpoints/results when reported. Leadership/organizational updates must state the exact change, timing and remit.\n- Strategic meaning may be one concluding sentence and must be tied directly to the reported facts.\n- Never discuss how many sources covered the story or how retrieval worked.\n- Do not include source names, URLs, ticker symbols, legal suffixes, headings or citations in the answer.\n- Never invent or estimate missing facts.\n- If you cannot verify that the material is about this exact event, return exactly CONTENT_UNAVAILABLE.\n- Return only the summary or CONTENT_UNAVAILABLE.`;
}

function usableEmailSummary(answer) {
  const text = String(answer || "").trim();
  if (!text || text.includes("CONTENT_UNAVAILABLE")) return false;
  const lower = text.toLowerCase();
  if (["repository evidence","identified across","separate reports","unable to access","cannot access","most important follow-up is to confirm"].some((term) => lower.includes(term))) return false;
  const words = text.split(/\s+/).filter(Boolean).length;
  return words >= 80 && words <= 330;
}

async function answerEmailArticle(env, body, repository) {
  const items = Array.isArray(repository.items) ? repository.items : [];
  const selected = selectionFromIds(items, body.article_ids);
  if (selected.length !== 1) return { mode: "email_article_summary", content_verified: false, answer: "CONTENT_UNAVAILABLE", error: "Exactly one valid article_id is required." };
  const item = selected[0];
  const diagnostics = [];
  const direct = await directArticleEvidence(item);
  diagnostics.push({ stage: "direct_page_retrieval", successful_pages: direct.evidence.length, retrievals: direct.retrievals.map((r) => ({ input_url: r.inputUrl, final_url: r.finalUrl, status: r.status, characters: r.characters || 0, error: r.error || null })) });

  if (direct.evidence.length) {
    try {
      const result = await callGemini(env, emailSummaryPrompt(item, direct.evidence), { webTools: false, maxOutputTokens: 1900, validate: usableEmailSummary });
      return { mode: "email_article_summary", article_id: item.id, content_verified: true, answer: result.answer.trim(), model: result.model, tool_profile: result.toolProfile, evidence_mode: "direct_page_text", diagnostic_attempts: [...diagnostics, ...result.attempts] };
    } catch (error) { diagnostics.push(...(Array.isArray(error?.attempts) ? error.attempts : [])); }
  }

  try {
    const result = await callGemini(env, emailSummaryPrompt(item, []), { webTools: true, maxOutputTokens: 1900, validate: usableEmailSummary });
    return { mode: "email_article_summary", article_id: item.id, content_verified: true, answer: result.answer.trim(), model: result.model, tool_profile: result.toolProfile, evidence_mode: "gemini_url_context_and_search", diagnostic_attempts: [...diagnostics, ...result.attempts] };
  } catch (error) {
    diagnostics.push(...(Array.isArray(error?.attempts) ? error.attempts : []));
    return { mode: "email_article_summary", article_id: item.id, content_verified: false, answer: "CONTENT_UNAVAILABLE", error: error instanceof Error ? error.message : "Summary generation failed.", diagnostic_attempts: diagnostics };
  }
}

function detectCompanies(question) {
  const text = normalize(question);
  return COMPANY_ALIASES.filter(([, aliases]) => aliases.some((alias) => text.includes(alias))).map(([company]) => company);
}

function selectByQuestion(items, question) {
  const companies = detectCompanies(question);
  const terms = normalize(question).replace(/[^a-z0-9\s-]/g, " ").split(/\s+/).filter((term) => term.length > 3).slice(0, 15);
  const base = items.filter((item) => !companies.length || companies.includes(item.company));
  return [...(base.length ? base : items)].map((item) => {
    const haystack = normalize(`${item.title} ${item.description} ${item.category} ${item.company}`);
    let score = item.official_source ? 3 : 0;
    for (const term of terms) score += normalize(item.title).includes(term) ? 6 : haystack.includes(term) ? 2 : 0;
    score += Math.max(0, 4 - (Date.now() - articleDate(item)) / 864000000);
    return { item, score };
  }).sort((a,b) => b.score-a.score || articleDate(b.item)-articleDate(a.item)).slice(0, MAX_CHAT_ITEMS).map(({item}) => item);
}

function compactItem(item, index) {
  return `[${index + 1}]\nCompany: ${item?.company || "Unknown"}\nTitle: ${item?.title || "Untitled"}\nCategory: ${item?.category || "Other"}\nPublished: ${item?.published_display || item?.published_at || "Unknown"}\nFeed text: ${String(item?.description || "").replace(/<[^>]+>/g," ").replace(/\s+/g," ").slice(0,1200)}\nURLs: ${articleUrls(item).join(" | ")}`;
}

async function answerChat(env, body, repository) {
  const mode = body.mode === "filtered_summary" ? "filtered_summary" : "chat";
  const question = String(body.question || "").trim();
  const allItems = Array.isArray(repository.items) ? repository.items : [];
  const byIds = selectionFromIds(allItems, body.article_ids);
  const selected = (byIds.length ? byIds : selectByQuestion(allItems, question)).slice(0, MAX_CHAT_ITEMS);
  if (!selected.length) return { mode, answer: "No relevant information was found in the current news repository.", selected_total: 0 };
  const context = selected.map(compactItem).join("\n\n");
  const prompt = mode === "filtered_summary"
    ? `Act as a senior clinical-laboratory competitive-intelligence analyst. Deep-read the selected developments using Google Search when helpful. Give one decisive takeaway, 3-5 strategic bullets, and a short What to watch section. Focus on material facts, patterns, competitive consequences, customers, economics and execution; do not list every headline or count. Never invent details.\n\nSELECTED RECORDS\n${context}`
    : `Act as a concise human competitive-intelligence adviser. Answer the user's question directly and strategically. Use Google Search to verify/deepen the selected developments when useful. Keep the response short and decision-oriented; do not automatically list headlines, sources, dates, counts or URLs. Never invent details.\n\nQuestion: ${question}\n\nSELECTED RECORDS\n${context}`;
  const result = await callGemini(env, prompt, { webTools: true, maxOutputTokens: mode === "filtered_summary" ? 1700 : 1100 });
  return { mode, answer: result.answer, selected_total: selected.length, model: result.model, tool_profile: result.toolProfile, repository_updated: repository.generated_at_display || null };
}

export default {
  async fetch(request, env) {
    const requestOrigin = request.headers.get("Origin");
    const origin = ALLOWED_ORIGINS.has(requestOrigin) ? requestOrigin : "https://atanubarik.github.io";
    if (request.method === "OPTIONS") return new Response(null, { status: 204, headers: corsHeaders(origin) });
    if (request.method === "GET") return jsonResponse({ ok: true, service: "Laboratory News AI", worker_version: WORKER_VERSION, gemini_configured: Boolean(env.GEMINI_API_KEY), preferred_model: MODEL_CANDIDATES[0], model_candidates: MODEL_CANDIDATES, summary_pipeline: ["direct_publisher_page_read", "gemini_generate_content", "gemini_url_context_and_google_search_fallback"], capabilities: ["email_article_summary", "direct_publisher_page_read", "generate_content_search_fallback", "strategic_repository_chat", "strategic_filtered_summary", "model_and_tool_fallbacks"] }, 200, origin);
    if (request.method !== "POST") return jsonResponse({ error: "Method not allowed." }, 405, origin);
    if (requestOrigin && !ALLOWED_ORIGINS.has(requestOrigin)) return jsonResponse({ error: "Origin not allowed." }, 403, origin);
    try {
      if (!(request.headers.get("Content-Type") || "").includes("application/json")) return jsonResponse({ error: "Content-Type must be application/json." }, 415, origin);
      const body = await request.json();
      const mode = String(body?.mode || "chat");
      if (!["chat", "filtered_summary", "email_article_summary"].includes(mode)) return jsonResponse({ error: "Unsupported mode." }, 400, origin);
      if (mode === "chat") {
        const question = String(body?.question || "").trim();
        if (question.length < 3 || question.length > 3000) return jsonResponse({ error: "Please enter a question between 3 and 3,000 characters." }, 400, origin);
      }
      if (["filtered_summary", "email_article_summary"].includes(mode) && !Array.isArray(body?.article_ids)) return jsonResponse({ error: "This mode requires article_ids." }, 400, origin);
      const repository = await fetchRepository();
      const result = mode === "email_article_summary" ? await answerEmailArticle(env, body, repository) : await answerChat(env, body, repository);
      return jsonResponse(result, 200, origin);
    } catch (error) {
      return jsonResponse({ error: error instanceof Error ? error.message : "Unexpected server error.", diagnostic_attempts: Array.isArray(error?.attempts) ? error.attempts : [] }, 500, origin);
    }
  },
};
