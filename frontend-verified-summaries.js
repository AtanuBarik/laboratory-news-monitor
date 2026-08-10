(() => {
  'use strict';

  const RELEASE = '20260810a';
  const BRAND_PATTERN = new RegExp('chat' + 'gpt', 'gi');
  const summaryMap = new Map();
  let applying = false;
  let timer = 0;

  const clean = value => String(value || '').replace(/\s+/g, ' ').trim();
  const norm = value => clean(value).toLowerCase().replace(/[^a-z0-9]+/g, ' ').trim();
  const esc = value => String(value ?? '').replace(/[&<>"']/g, char => ({
    '&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;'
  }[char]));

  function injectStyles() {
    let style = document.getElementById('verifiedArticleSummaryStyles');
    if (!style) {
      style = document.createElement('style');
      style.id = 'verifiedArticleSummaryStyles';
      document.head.appendChild(style);
    }
    style.textContent = `
      body{font-size:16px}
      header p{font-size:1rem;line-height:1.55}
      .status{font-size:.92rem!important}
      input,select{font-size:.94rem!important}
      .metric span{font-size:.9rem!important}
      .panel-head p{font-size:.9rem!important;line-height:1.45}
      .hbar-label,.hbar-value{font-size:.9rem!important}
      .stack-total,.stack-label,.legend-item{font-size:.82rem!important}
      .section-title span{font-size:.9rem!important}
      .news-card h3{font-size:1.08rem!important;line-height:1.45!important}
      .meta{font-size:.91rem!important;line-height:1.45!important}
      .chip{font-size:.79rem!important}
      .context-list{display:none!important}
      .verified-article-summary{margin-top:2px;padding:14px 15px;border:1px solid #dbe7e1;border-radius:11px;background:#f8fbf9;color:#30483f;font-size:.98rem;line-height:1.68}
      .verified-article-summary strong{display:block;margin-bottom:7px;color:#065f46;font-size:.93rem}
      .verified-article-summary .evidence-note{margin-top:9px;padding-top:8px;border-top:1px solid #e2ebe6;color:#62766f;font-size:.82rem;line-height:1.45}
      footer{font-size:.9rem!important;line-height:1.5}
      .message{font-size:.95rem!important}.summary-meta{font-size:.84rem!important}
    `;
  }

  function scrubBranding(root = document.body) {
    if (!root) return;
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
    const nodes = [];
    while (walker.nextNode()) nodes.push(walker.currentNode);
    nodes.forEach(node => {
      if (!node.parentElement || /^(SCRIPT|STYLE|TEXTAREA|NOSCRIPT)$/i.test(node.parentElement.tagName)) return;
      BRAND_PATTERN.lastIndex = 0;
      if (BRAND_PATTERN.test(node.nodeValue || '')) {
        BRAND_PATTERN.lastIndex = 0;
        node.nodeValue = String(node.nodeValue || '').replace(BRAND_PATTERN, 'AI');
      }
      BRAND_PATTERN.lastIndex = 0;
    });
  }

  function removeNamedSynthesis() {
    const patterns = [/latest verified strategic synthesis/i,/what the newest signals mean competitively/i];
    const nodes = Array.from(document.querySelectorAll('section,article,div')).filter(node => {
      const value = clean(node.textContent);
      return value && value.length < 14000 && patterns.some(pattern => pattern.test(value));
    }).sort((a,b) => clean(a.textContent).length - clean(b.textContent).length);
    if (nodes[0]) nodes[0].closest('section,article')?.remove();
  }

  async function loadSummaries() {
    try {
      const response = await fetch(`data/news.json?v=${RELEASE}`, {cache:'no-store'});
      if (!response.ok) throw new Error(String(response.status));
      const payload = await response.json();
      (payload.items || []).forEach(item => {
        const summary = clean(item.article_summary || item.chatgpt_summary || '');
        if (!summary || !item.title) return;
        summaryMap.set(norm(item.title), {
          title:item.title,
          summary,
          retrieval:item.summary_retrieval_status || '',
        });
      });
    } catch (error) {
      console.error('Verified summaries could not be loaded:', error);
    }
  }

  function findRecord(card) {
    const title = norm(card.querySelector('h3')?.textContent);
    if (summaryMap.has(title)) return summaryMap.get(title);
    let best = null;
    let score = 0;
    for (const [key, record] of summaryMap.entries()) {
      if ((key.includes(title) || title.includes(key)) && Math.min(key.length, title.length) > score) {
        best = record;
        score = Math.min(key.length, title.length);
      }
    }
    return best;
  }

  function render() {
    if (!summaryMap.size) return;
    document.querySelectorAll('.news-card').forEach(card => {
      const record = findRecord(card);
      let box = card.querySelector('.verified-article-summary');
      if (!record) { box?.remove(); return; }
      if (!box) {
        box = document.createElement('div');
        box.className = 'verified-article-summary';
        const context = card.querySelector('.context-list');
        if (context) context.insertAdjacentElement('beforebegin', box);
        else card.appendChild(box);
      }
      const note = record.retrieval
        ? `<div class="evidence-note">Evidence basis: ${esc(record.retrieval)}</div>` : '';
      box.innerHTML = `<strong>Article summary</strong><div>${esc(record.summary)}</div>${note}`;
    });
  }

  function apply() {
    if (applying) return;
    applying = true;
    try {
      injectStyles();
      removeNamedSynthesis();
      render();
      scrubBranding(document.body);
      document.documentElement.dataset.verifiedSummaryRelease = RELEASE;
    } finally {
      applying = false;
    }
  }

  function schedule(delay = 0) { clearTimeout(timer); timer = setTimeout(apply, delay); }

  async function boot() {
    injectStyles();
    removeNamedSynthesis();
    scrubBranding(document.body);
    await loadSummaries();
    apply();
    const observer = new MutationObserver(mutations => {
      if (!applying && mutations.some(mutation => mutation.addedNodes?.length || mutation.type === 'characterData')) schedule(90);
    });
    observer.observe(document.body, {childList:true,subtree:true,characterData:true});
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot, {once:true});
  else boot();
})();
