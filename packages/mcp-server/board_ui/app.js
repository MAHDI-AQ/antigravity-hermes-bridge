/*
 * Read-only Swarm Board observer frontend.
 * Polls ./api/swarm with adaptive ETag cadence (1s active / 3s idle) and renders
 * Overview, Board, Members, Graph, and Raw tabs, or an empty state when no swarm is in scope.
 * Uses only relative ./ URLs so both root and Remote Control subpath proxies work.
 */
(() => {
  const ACTIVE_POLL_MS = 1000;
  const IDLE_POLL_MS = 3000;
  const POLL_MS = IDLE_POLL_MS;
  const $ = (id) => document.getElementById(id);
  const state = {
    conv: null,
    swarm: null,
    view: 'loading', // loading | board | empty
    etag: '',
    snap: null,
    me: null,
    seenSeqs: new Set(),
    firstLoad: true,
    hiddenAgents: new Set(),
    channel: '',
    tab: 'feed',
    tabChosen: false,
    graphView: 'hive',
    newEdgeKeys: new Set(),
    lastOk: 0,
    expanded: new Set(),
    rawKind: 'board',
    stick: true,
    feedWindow: 120,
    openAgentTabs: [],
    agentTabData: {},
    agentDetailsOpen: new Set(),
    agentPollTimer: 0,
    agentPollAbort: null,
    pollTimer: 0,
    polling: false,
    pollGen: 0,
    pollAbort: null,
    dirtyTabs: new Set(),
    pendingNewSeqs: new Set(),
    memberByName: new Map(),
    memberByLower: new Map(),
    memberGroupByLower: new Map(),
    msgBySeq: new Map(),
    msgById: new Map(),
    channelByNorm: new Map(),
  };
  if ('scrollRestoration' in history) history.scrollRestoration = 'manual';

  const LAST_HTML = new WeakMap();
  function setHtmlIfChanged(el, html) {
    if (!el) return false;
    const strHtml = String(html ?? '');
    if (LAST_HTML.get(el) === strHtml) return false;
    LAST_HTML.set(el, strHtml);
    el.dataset.lastRenderSig = `${strHtml.length}`;
    el.innerHTML = strHtml;
    return true;
  }

  const params = new URLSearchParams(location.search);
  const initialTab = params.get('tab');
  const urlTabChosen = !!(initialTab && document.querySelector(`.tab[data-tab="${CSS.escape(initialTab)}"]`));
  const currentConv = () => window.sidecar?.conversationId || params.get('conversationId') || '';

  async function getJson(path, { signal, timeoutMs = 8000 } = {}) {
    const f = window.sidecar?.fetch ? window.sidecar.fetch.bind(window.sidecar) : fetch;
    const ac = new AbortController();
    let rejectRace = null;
    const raceGuard = new Promise((_, reject) => { rejectRace = reject; });
    const onAbort = () => {
      const err = signal?.reason instanceof Error ? signal.reason : new Error(`${path}: aborted`);
      ac.abort(err);
      rejectRace?.(err);
    };
    if (signal) {
      if (signal.aborted) {
        throw (signal.reason instanceof Error ? signal.reason : new Error(`${path}: aborted`));
      }
      signal.addEventListener('abort', onAbort, { once: true });
    }
    const timer = setTimeout(() => {
      const err = new Error(`${path}: timeout`);
      ac.abort(err);
      rejectRace?.(err);
    }, timeoutMs);
    try {
      const res = await Promise.race([
        f(path, { method: 'GET', signal: ac.signal }),
        raceGuard,
      ]);
      if (res.status === 304) return { unchanged: true, etag: state.etag, swarm: state.swarm };
      if (!res.ok) throw new Error(`${path}: ${res.status}`);
      return await Promise.race([res.json(), raceGuard]);
    } finally {
      clearTimeout(timer);
      if (signal) signal.removeEventListener('abort', onAbort);
    }
  }

  // ---------- theme ----------
  // preload.js writes the host's tokens onto :root. Mark the root light/dark
  // from --background's luminance when injected, or prefers-color-scheme standalone.
  const lightMq = typeof window.matchMedia === 'function' ? window.matchMedia('(prefers-color-scheme: light)') : null;
  let lastHostBg = null;
  let schemeProbe = null;
  function parseColorRgb(raw) {
    const s = String(raw || '').trim();
    const hex3 = /^#([0-9a-f]{3})$/i.exec(s);
    if (hex3) {
      const h = hex3[1];
      return [parseInt(h[0] + h[0], 16) / 255, parseInt(h[1] + h[1], 16) / 255, parseInt(h[2] + h[2], 16) / 255];
    }
    const hex6 = /^#([0-9a-f]{6})(?:[0-9a-f]{2})?$/i.exec(s);
    if (hex6) {
      const h = hex6[1];
      return [parseInt(h.slice(0, 2), 16) / 255, parseInt(h.slice(2, 4), 16) / 255, parseInt(h.slice(4, 6), 16) / 255];
    }
    const rgb = /^rgba?\(\s*([\d.]+)\s*[,\s]\s*([\d.]+)\s*[,\s]\s*([\d.]+)/i.exec(s);
    if (rgb) {
      return [Number(rgb[1]) / 255, Number(rgb[2]) / 255, Number(rgb[3]) / 255];
    }
    return null;
  }
  function syncScheme() {
    const hostBg = getComputedStyle(document.documentElement).getPropertyValue('--background').trim();
    if (hostBg && hostBg === lastHostBg && document.documentElement.dataset.theme) return;
    lastHostBg = hostBg || null;
    let theme;
    if (!hostBg) {
      theme = lightMq?.matches ? 'light' : 'dark';
    } else {
      let rgb = parseColorRgb(hostBg);
      if (!rgb) {
        if (!schemeProbe) {
          schemeProbe = document.createElement('span');
          schemeProbe.style.cssText = 'position:absolute;visibility:hidden;pointer-events:none;color:var(--background, #0e0e10)';
        }
        if (!schemeProbe.isConnected) document.documentElement.appendChild(schemeProbe);
        const c = getComputedStyle(schemeProbe).color;
        schemeProbe.remove();
        const nums = (c.match(/[\d.]+/g) || []).map(Number);
        if (nums.length < 3) return;
        const scale = c.startsWith('color(') ? 1 : 255;
        rgb = nums.slice(0, 3).map((v) => v / scale);
      }
      const [r, g, b] = rgb;
      const lum = 0.2126 * r + 0.7152 * g + 0.0722 * b;
      theme = lum < 0.5 ? 'dark' : 'light';
    }
    if (document.documentElement.dataset.theme !== theme) document.documentElement.dataset.theme = theme;
  }
  let applyingTheme = false;
  let themeObserver = null;
  function applyTheme(vars) {
    if (applyingTheme) return;
    applyingTheme = true;
    try {
      if (vars && typeof vars === 'object') {
        for (const [k, v] of Object.entries(vars)) {
          if (typeof k === 'string' && k.startsWith('--') && typeof v === 'string') {
            document.documentElement.style.setProperty(k, v);
          }
        }
      }
      syncScheme();
      if (typeof postTheme === 'function') postTheme();
      themeObserver?.takeRecords();
    } finally {
      applyingTheme = false;
    }
  }
  syncScheme();
  if (lightMq) {
    const onMq = () => { lastHostBg = null; applyTheme(); };
    if (typeof lightMq.addEventListener === 'function') lightMq.addEventListener('change', onMq);
    else if (typeof lightMq.addListener === 'function') lightMq.addListener(onMq);
  }
  themeObserver = new MutationObserver(() => {
    if (applyingTheme) return;
    applyTheme();
  });
  themeObserver.observe(document.documentElement, { attributes: true, attributeFilter: ['style'] });
  window.addEventListener('message', (event) => {
    if (event.source !== window && event.source !== window.parent) return;
    if (event.data?.type === 'theme-change') {
      applyTheme(event.data?.payload?.cssVariables);
    }
  });

  // ---------- helpers ----------
  const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const STATUS_LABEL = { working: 'Active', waiting: 'Waiting', idle: 'Waiting', parked: 'Waiting', killed: 'Killed', failed: 'Failed' };
  const statusLabel = (st) => STATUS_LABEL[st] || st || '';
  const members = () => state.snap?.members || [];
  function rebuildIndexes(snap) {
    state.memberByName.clear();
    state.memberByLower.clear();
    state.memberGroupByLower.clear();
    state.msgBySeq.clear();
    state.msgById.clear();
    state.channelByNorm.clear();
    for (const m of snap?.members || []) {
      if (!m || !m.name) continue;
      state.memberByName.set(m.name, m);
      const low = m.name.toLowerCase();
      if (!state.memberByLower.has(low)) state.memberByLower.set(low, m);
      const match = low.match(/^(.+)-\d+$/);
      if (match) {
        const pfx = match[1];
        let grp = state.memberGroupByLower.get(pfx);
        if (!grp) { grp = []; state.memberGroupByLower.set(pfx, grp); }
        grp.push(m);
      }
    }
    for (const msg of snap?.messages || []) {
      if (!msg) continue;
      if (msg.seq != null) state.msgBySeq.set(Number(msg.seq), msg);
      const mid = msg.id ?? msg.messageId ?? msg.message_id;
      if (mid != null && mid !== '') state.msgById.set(String(mid), msg);
    }
    for (const c of Array.isArray(snap?.channels) ? snap.channels : []) {
      if (!c || !c.name) continue;
      state.channelByNorm.set(String(c.name).replace(/^#/, '').trim().toLowerCase(), c);
    }
  }
  const memberOf = (name) => state.memberByName.get(name) || members().find((m) => m.name === name);
  const memberByLower = (lower) => state.memberByLower.get(lower);
  const convIdOf = (m) => m?.conversationId || m?.conversation_id || '';
  const senderOf = (m) => m?.agent || m?.sender || '';
  const textOf = (m) => m?.text || m?.summary || '';
  const msgTypeOf = (m) => m?.type || m?.kind || 'post';
  const replyToOf = (m) => m?.replyTo ?? m?.reply_to ?? null;
  function resolveReplySeq(replyTo) {
    if (replyTo == null || replyTo === '') return null;
    if (typeof replyTo === 'number') {
      return Number.isInteger(replyTo) && replyTo > 0 ? replyTo : null;
    }
    const s = String(replyTo).trim();
    if (!s) return null;
    if (/^#?\d+$/.test(s)) {
      const n = Number(s.startsWith('#') ? s.slice(1) : s);
      return Number.isInteger(n) && n > 0 ? n : null;
    }
    const hit = state.msgById.get(s);
    const seq = Number(hit?.seq);
    return Number.isInteger(seq) && seq > 0 ? seq : null;
  }
  const channelMentionsOf = (m) => m?.channelMentions || m?.channel_mentions || [];
  const viaChannelOf = (m) => m?.viaChannel || m?.via_channel || [];

  const parseTime = (t) => (t ? Date.parse(String(t).replace(' ', 'T')) : NaN);
  function normalizeTsMs(raw) {
    if (raw == null || raw === '' || typeof raw === 'boolean') return NaN;
    if (typeof raw === 'number') {
      if (!Number.isFinite(raw) || raw <= 0) return NaN;
      return raw < 1e11 ? Math.round(raw * 1000) : Math.round(raw);
    }
    const s = String(raw).trim();
    if (!s) return NaN;
    if (/^\d+(?:\.\d+)?$/.test(s)) {
      const n = Number(s);
      if (!Number.isFinite(n) || n <= 0) return NaN;
      return n < 1e11 ? Math.round(n * 1000) : Math.round(n);
    }
    const parsed = parseTime(s);
    return Number.isFinite(parsed) && parsed > 0 ? parsed : NaN;
  }
  function normalizeAgentStatus(raw, killed = false) {
    if (killed) return 'killed';
    const s = String(raw || '').trim().toLowerCase().replace(/^(?:task_state_|member_state_)/, '');
    if (s === 'working' || s === 'active' || s === 'running' || s === 'busy' || s === 'in_progress') return 'working';
    if (s === 'killed' || s === 'retired' || s === 'stopped' || s === 'canceled' || s === 'cancelled' || s === 'terminated') return 'killed';
    if (s === 'failed' || s === 'error' || s === 'errored') return 'failed';
    return 'waiting';
  }
  function formatCurrentToolLabel(ct) {
    if (ct == null || ct === '') return '';
    if (typeof ct === 'string') return ct.trim().slice(0, 180);
    if (typeof ct === 'object') {
      const name = String(ct.name || ct.toolName || ct.tool || '').trim();
      const prev = String(ct.preview || ct.argsPreview || ct.target || '').trim();
      if (name && prev) return `${/^running\s+/i.test(name) ? name : `Running ${name}`}(${prev})`.slice(0, 180);
      if (name) return (/^running\s+/i.test(name) ? name : `Running ${name}`).slice(0, 180);
      if (prev) return prev.slice(0, 180);
    }
    return '';
  }
  const msgMs = (m) => (Number.isFinite(m?.ts) ? m.ts : normalizeTsMs(m?.timestampMs ?? m?.timestamp ?? m?.time));
  const isoTime = (ms) => (Number.isFinite(ms) ? new Date(ms).toISOString() : '');
  const sameDay = (a, b) =>
    a.getFullYear() === b.getFullYear() && a.getMonth() === b.getMonth() && a.getDate() === b.getDate();

  const clockFormat = new Intl.DateTimeFormat(undefined, { hour: 'numeric', minute: '2-digit' });
  const dayClockFormat = new Intl.DateTimeFormat(undefined, { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' });
  const fullFormat = new Intl.DateTimeFormat(undefined, { dateStyle: 'medium', timeStyle: 'medium' });

  function fmtClock(ms) {
    if (!Number.isFinite(ms)) return '';
    const d = new Date(ms);
    return (sameDay(d, new Date()) ? clockFormat : dayClockFormat).format(d);
  }
  const fmtFull = (ms) => (Number.isFinite(ms) ? fullFormat.format(ms) : '');
  function fmtDur(ms) {
    const s = Math.max(0, Math.round((ms || 0) / 1000));
    if (s < 60) return `${s}s`;
    if (s < 3600) return `${Math.floor(s / 60)}m ${String(s % 60).padStart(2, '0')}s`;
    return `${Math.floor(s / 3600)}h ${String(Math.floor((s % 3600) / 60)).padStart(2, '0')}m`;
  }
  const plural = (n, one, many = `${one}s`) => `${n} ${n === 1 ? one : many}`;

  const ICON = {
    play: '<svg class="icon" width="16" height="16" viewBox="0 0 16 16" aria-hidden="true"><path class="fill" d="M5 3.6v8.8a.5.5 0 0 0 .76.43l7-4.4a.5.5 0 0 0 0-.86l-7-4.4A.5.5 0 0 0 5 3.6z"/></svg>',
    pause: '<svg class="icon" width="16" height="16" viewBox="0 0 16 16" aria-hidden="true"><rect class="fill" x="4" y="3.5" width="2.75" height="9" rx=".75"/><rect class="fill" x="9.25" y="3.5" width="2.75" height="9" rx=".75"/></svg>',
    arrow: '<svg class="icon arrow" width="12" height="12" viewBox="0 0 16 16" aria-hidden="true"><path d="M3 8h9.5M9 4.5 12.5 8 9 11.5"/></svg>',
    open: '<svg class="icon" width="16" height="16" viewBox="0 0 16 16" aria-hidden="true"><path d="M7 3.5H4.5a1 1 0 0 0-1 1v7a1 1 0 0 0 1 1h7a1 1 0 0 0 1-1V9"/><path d="M9.5 3.5h3v3M12.5 3.5 7.5 8.5"/></svg>',
    search: '<svg class="icon" width="16" height="16" viewBox="0 0 16 16" aria-hidden="true"><circle cx="7" cy="7" r="4.25"/><path d="m10.25 10.25 3.25 3.25"/></svg>',
    inbox: '<svg class="icon" width="16" height="16" viewBox="0 0 16 16" aria-hidden="true"><path d="M2.5 9.5 4 4a1 1 0 0 1 1-.75h6a1 1 0 0 1 1 .75l1.5 5.5v2.5a1 1 0 0 1-1 1h-9a1 1 0 0 1-1-1z"/><path d="M2.5 9.5h3l1 1.5h3l1-1.5h3"/></svg>',
  };

  function collapseRecipientGroups(names, roster = members()) {
    const set = new Set(names);
    const groups = new Map();
    for (const mem of roster) {
      const match = mem.name.match(/^(.+)-\d+$/);
      if (!match) continue;
      const prefix = match[1];
      if (!groups.has(prefix)) groups.set(prefix, []);
      groups.get(prefix).push(mem.name);
    }
    const collapsed = new Set();
    const out = [];
    for (const n of names) {
      const match = n.match(/^(.+)-\d+$/);
      const prefix = match?.[1];
      const group = prefix ? groups.get(prefix) : null;
      if (group && group.length >= 2 && group.every((g) => set.has(g))) {
        if (!collapsed.has(prefix)) {
          collapsed.add(prefix);
          out.push(prefix);
        }
      } else {
        out.push(n);
      }
    }
    return out;
  }

  function recipientsHtml(m) {
    const sender = senderOf(m);
    const roster = members();
    const names = collapseRecipientGroups((m.mentions || []).filter((n) => n !== sender), roster);
    const parts = names.map((n) => {
      const hit = state.memberByName.get(n)
        || (state.memberGroupByLower.get(n.toLowerCase()) || [])[0]
        || null;
      return hit
        ? `<button type="button" class="msg-mention-btn" data-feed-member="${esc(hit.name)}" title="Open @${esc(hit.name)}">@${esc(n)}</button>`
        : `<span>@${esc(n)}</span>`;
    });
    for (const c of channelMentionsOf(m)) {
      const ch = normChan(c);
      parts.push(`<button type="button" class="msg-mention-btn channel" data-channel="${esc(ch)}" title="Show only #${esc(ch)}">@#${esc(ch)}</button>`);
    }
    if (m.all) parts.push('<span>@all</span>');
    return parts.length ? `<span class="msg-to">${ICON.arrow}${parts.join('')}</span>` : '';
  }

  function transformTextNodes(html, fn) {
    let skipPre = 0, skipA = 0, skipBtn = 0;
    return String(html).replace(/(<[^>]*>)|([^<]+)/g, (m, tag, text) => {
      if (tag) {
        if (/^<pre\b/i.test(tag)) skipPre++;
        else if (/^<\/pre\b/i.test(tag)) skipPre = Math.max(0, skipPre - 1);
        else if (/^<a\b/i.test(tag)) skipA++;
        else if (/^<\/a\b/i.test(tag)) skipA = Math.max(0, skipA - 1);
        else if (/^<button\b/i.test(tag)) skipBtn++;
        else if (/^<\/button\b/i.test(tag)) skipBtn = Math.max(0, skipBtn - 1);
        return tag;
      }
      return (skipPre || skipA || skipBtn) ? text : fn(text);
    });
  }

  const MENTION_RE = /(^|[^\p{L}\p{N}_-])@([\p{L}\p{N}_-]+)/gu;
  const CHANNEL_MENTION_RE = /(^|[^\p{L}\p{N}_-])@#([a-z][a-z0-9-]{0,23})(?![\p{L}\p{N}_-])/giu;
  function highlightChannelMentions(html) {
    if (!hasChannels()) return html;
    return transformTextNodes(html, (seg) => seg.replace(CHANNEL_MENTION_RE, (all, pre, name) => {
      const c = state.channelByNorm.get(name.toLowerCase());
      if (!c) return all;
      const tip = c.archived ? `#${normChan(c.name)} is archived: this addresses nobody` : `Everyone in #${normChan(c.name)}`;
      return `${pre}<button type="button" class="mention mention-btn channel${c.archived ? ' archived' : ''}" data-channel="${esc(normChan(c.name))}" title="${esc(tip)}">@#${name}</button>`;
    }));
  }
  function highlightMentions(html) {
    html = highlightChannelMentions(html);
    return transformTextNodes(html, (seg) => seg.replace(MENTION_RE, (all, pre, name) => {
      const lower = name.toLowerCase();
      if (lower === 'all') return `${pre}<span class="mention all">@${name}</span>`;
      const m = memberByLower(lower);
      if (m) {
        return `${pre}<button type="button" class="mention mention-btn" data-feed-member="${esc(m.name)}" title="${esc(m.name)} · ${esc(statusLabel(m.status))}">@${name}</button>`;
      }
      const group = state.memberGroupByLower.get(lower);
      if (group && group.length) {
        const tip = group.map((mem) => mem.name).join(', ');
        return `${pre}<button type="button" class="mention mention-btn" data-feed-member="${esc(group[0].name)}" title="${esc(tip)}">@${name}</button>`;
      }
      return all;
    }));
  }

  const CITATION_RE = /(^|[^\p{L}\p{N}_&#/-])#([1-9]\d{0,8})(?![\p{L}\p{N}_-])/gu;
  function highlightCitations(html) {
    return transformTextNodes(html, (seg) => seg.replace(CITATION_RE, (all, pre, digits) => {
      const seq = Number(digits);
      if (!Number.isInteger(seq) || seq <= 0) return all;
      const target = state.msgBySeq.get(seq);
      const tip = target ? `Go to #${seq} (${senderOf(target)})` : `Go to #${seq}`;
      return `${pre}<button type="button" class="ref seq-ref" data-seq-ref="${seq}" data-ov-seq="${seq}" data-testid="swarm-seq-ref" title="${esc(tip)}">#${seq}</button>`;
    }))
      .replace(/<code>(<button\b[^>]*\bseq-ref\b[^>]*>#[0-9]+<\/button>)<\/code>/g, '<code class="seq-ref-only">$1</code>');
  }

  function safeHttpsHref(raw) {
    if (typeof raw !== 'string') return '';
    const trimmed = raw.trim();
    if (!/^https:\/\//i.test(trimmed)) return '';
    try {
      const u = new URL(trimmed);
      if (u.protocol !== 'https:' || u.username || u.password) return '';
      return u.href;
    } catch {
      return '';
    }
  }

  function md(rawSrc) {
    const rawStr = String(rawSrc || '').replace(/\x00/g, '').replace(/\r\n?/g, '\n');
    const src = rawStr.length > 120000 ? `${rawStr.slice(0, 120000)}\n\n… (truncated)` : rawStr;
    const lines = src.split('\n');
    let out = '', inCode = false, codeLang = '', code = [];
    const listStack = [];
    let bqLines = [];

    const inline = (rawText) => {
      const tokens = [];
      const hold = (html) => {
        const idx = tokens.length;
        tokens.push(html);
        return `\x00M${idx}\x00`;
      };
      let t = String(rawText || '');
      t = t.replace(/`([^`\n]+)`/g, (_, c) => hold(`<code>${esc(c)}</code>`));
      t = t.replace(/\[([^\]]+)\]\(([^)\s]+)\)/g, (full, label, url) => {
        const href = safeHttpsHref(url);
        if (!href) return full;
        return hold(`<a class="ag-md-link" href="${esc(href)}" target="_blank" rel="noopener noreferrer">${esc(label)}</a>`);
      });
      t = t.replace(/https:\/\/[^\s<>"'`]+/g, (rawUrl) => {
        let u = rawUrl;
        let trail = '';
        while (u.length > 8 && /[.,;:!?\]}]/.test(u[u.length - 1])) {
          trail = u[u.length - 1] + trail;
          u = u.slice(0, -1);
        }
        while (u.endsWith(')') && (u.match(/\(/g) || []).length < (u.match(/\)/g) || []).length) {
          trail = ')' + trail;
          u = u.slice(0, -1);
        }
        const href = safeHttpsHref(u);
        if (!href) return rawUrl;
        return hold(`<a class="ag-md-link" href="${esc(href)}" target="_blank" rel="noopener noreferrer">${esc(u)}</a>`) + trail;
      });
      let html = esc(t)
        .replace(/~~([^~\n]+)~~/g, '<del>$1</del>')
        .replace(/\*\*([^*\n]+)\*\*/g, '<strong>$1</strong>')
        .replace(/(^|[\s(])\*([^*\n]+)\*(?=[\s).,;:!?]|$)/g, '$1<em>$2</em>');
      html = html.replace(/\x00M(\d+)\x00/g, (_, idx) => tokens[Number(idx)] ?? '');
      return highlightCitations(highlightMentions(html));
    };

    const closeLists = () => {
      while (listStack.length) {
        const top = listStack.pop();
        if (top.hasOpenLi) out += '</li>';
        out += `</${top.type}>`;
      }
    };
    const flushBq = () => {
      if (!bqLines.length) return;
      out += `<blockquote>${bqLines.map((l) => (l.trim() ? `<p>${inline(l)}</p>` : '')).join('')}</blockquote>`;
      bqLines = [];
    };
    const emitCodeBlock = () => {
      const lang = codeLang ? esc(codeLang) : 'text';
      out += `<div class="ag-codeblock" data-testid="ag-codeblock">` +
        `<div class="ag-codehead"><span class="ag-codelang">${lang}</span>` +
        `<button type="button" class="ghost-btn sm ag-codecopy" data-copy-code>Copy</button></div>` +
        `<pre><code>${esc(code.join('\n'))}</code></pre></div>`;
      code = [];
      codeLang = '';
      inCode = false;
    };
    const splitRow = (line) => {
      let s = line.trim();
      if (s.startsWith('|')) s = s.slice(1);
      if (s.endsWith('|') && !s.endsWith('\\|')) s = s.slice(0, -1);
      return s.split(/(?<!\\)\|/).map((c) => c.trim().replace(/\\\|/g, '|'));
    };
    const isTableSep = (line) => /^\s*\|?(?:\s*:?-{3,}:?\s*\|)+\s*:?-{3,}:?\s*\|?\s*$/.test(line)
      || /^\s*\|\s*:?-{3,}:?\s*\|\s*$/.test(line);

    for (let i = 0; i < lines.length; i++) {
      const line = lines[i];
      const fence = line.match(/^\s{0,3}```(.*)$/);
      if (fence) {
        if (inCode) {
          emitCodeBlock();
        } else {
          flushBq();
          closeLists();
          inCode = true;
          codeLang = fence[1].trim().split(/\s+/)[0] || '';
        }
        continue;
      }
      if (inCode) {
        code.push(line);
        continue;
      }

      const bq = line.match(/^\s{0,3}>\s?(.*)$/);
      if (bq) {
        closeLists();
        bqLines.push(bq[1]);
        continue;
      }
      flushBq();

      if (i + 1 < lines.length && line.includes('|') && isTableSep(lines[i + 1])) {
        closeLists();
        const headers = splitRow(line);
        const aligns = splitRow(lines[i + 1]).map((c) => (
          c.startsWith(':') && c.endsWith(':') ? 'center' : (c.endsWith(':') ? 'right' : 'left')
        ));
        i += 1;
        const rows = [];
        while (i + 1 < lines.length && lines[i + 1].includes('|') && lines[i + 1].trim() && !/^\s{0,3}```/.test(lines[i + 1])) {
          i += 1;
          rows.push(splitRow(lines[i]));
        }
        const ths = headers.map((h, idx) => `<th style="text-align:${aligns[idx] || 'left'}">${inline(h)}</th>`).join('');
        const trs = rows.map((r) => `<tr>${headers.map((_, idx) => `<td style="text-align:${aligns[idx] || 'left'}">${inline(r[idx] || '')}</td>`).join('')}</tr>`).join('');
        out += `<div class="ag-table-wrap"><table class="ag-table"><thead><tr>${ths}</tr></thead>${trs ? `<tbody>${trs}</tbody>` : ''}</table></div>`;
        continue;
      }

      if (/^\s{0,3}(?:[-*_]\s*){3,}$/.test(line)) {
        closeLists();
        out += '<hr>';
        continue;
      }

      const h = line.match(/^\s{0,3}(#{1,6})\s+(.*)$/);
      if (h) {
        closeLists();
        const lvl = Math.min(6, h[1].length);
        out += `<h${lvl}>${inline(h[2])}</h${lvl}>`;
        continue;
      }

      const li = line.match(/^(\s*)([-*+]|\d+\.)\s+(.*)$/);
      if (li) {
        const indent = li[1].replace(/\t/g, '  ').length;
        const type = /\d+\./.test(li[2]) ? 'ol' : 'ul';
        while (listStack.length && indent < listStack[listStack.length - 1].indent) {
          const top = listStack.pop();
          if (top.hasOpenLi) out += '</li>';
          out += `</${top.type}>`;
        }
        if (!listStack.length || indent > listStack[listStack.length - 1].indent) {
          out += `<${type}>`;
          listStack.push({ type, indent, hasOpenLi: false });
        } else if (listStack[listStack.length - 1].type !== type) {
          const top = listStack.pop();
          if (top.hasOpenLi) out += '</li>';
          out += `</${top.type}>`;
          out += `<${type}>`;
          listStack.push({ type, indent, hasOpenLi: false });
        } else if (listStack[listStack.length - 1].hasOpenLi) {
          out += '</li>';
          listStack[listStack.length - 1].hasOpenLi = false;
        }
        out += `<li>${inline(li[3])}`;
        listStack[listStack.length - 1].hasOpenLi = true;
        continue;
      }

      closeLists();
      if (line.trim()) out += `<p>${inline(line)}</p>`;
    }
    if (inCode) emitCodeBlock();
    flushBq();
    closeLists();
    return out;
  }

  const canOpenConversation = () => !!window.sidecar?.ui?.toggleConversation;
  function isAntigravityMember(m, name = '') {
    const rt = String(m?.runtime || '').trim().toLowerCase();
    if (rt === 'antigravity') return true;
    const role = m ? roleOf(m) : '';
    if (role === 'coordinator' || role === 'primary') return true;
    const n = String(name || m?.name || m?.rawName || '').trim().toLowerCase();
    const leadLow = String(window.SwarmHive?.leadName || '').trim().toLowerCase();
    return n === 'coordinator' || (leadLow && n === leadLow);
  }
  function openMember(name, subSessionId = '') {
    const leadName = window.SwarmHive?.leadName || 'coordinator';
    const target = name === 'coordinator' ? leadName : name;
    const m = memberOf(target) || memberByLower(String(target || '').toLowerCase());
    if (m && m.local === false) {
      const rCid = convIdOf(m);
      if (rCid) return openConversation(rCid);
      toast(`${m.name} runs on ${m.host?.label || 'another machine'}; open it there.`);
      return;
    }
    if (!subSessionId && isAntigravityMember(m, target)) {
      const cid = convIdOf(m) || (target === leadName || target === 'coordinator' ? coordinatorConv() : '');
      if (isValidConvId(cid) && canOpenConversation()) {
        openConversation(cid);
        return;
      }
    }
    if (m || target) {
      openAgentTab(m ? m.name : target, subSessionId);
    }
  }
  function openConversation(convId) {
    if (!convId) return;
    const remote = members().find((m) => convIdOf(m) === convId && m.local === false);
    if (remote) {
      toast(`${remote.name} runs on ${remote.host?.label || 'another machine'}; open it there.`);
      return;
    }
    if (window.sidecar?.ui?.toggleConversation)
      window.sidecar.ui.toggleConversation(convId);
  }

  function targetsOf(m, expandAll, roster = members()) {
    const sender = senderOf(m);
    if (isRoleWorker(sender, roster)) return { direct: new Set(), viaAll: new Set() };
    const direct = new Set([...(m.mentions || []), ...viaChannelOf(m)].filter((n) => n !== sender && !isRoleWorker(n, roster)));
    const viaAll = new Set();
    if (m.all && expandAll) {
      for (const mem of roster)
        if (mem.name !== sender && !direct.has(mem.name) && !isRoleWorker(mem, roster)) viaAll.add(mem.name);
    }
    return { direct, viaAll };
  }

  // ---------- data loading ----------
  let agentsMarkup = '';
  const filterMarkup = new Map();
  function resetSwarm(id, { keepPoll = false } = {}) {
    if (!keepPoll) {
      state.pollGen++;
      state.pollAbort?.abort();
    }
    stopAgentPoll();
    state.openAgentTabs = [];
    state.agentTabData = {};
    state.agentDetailsOpen.clear();
    renderAgentTabs();
    setHtmlIfChanged($('agent-header'), '');
    if ($('agent-committed')) $('agent-committed').innerHTML = '';
    if ($('agent-live-tail')) {
      $('agent-live-tail').innerHTML = '';
      $('agent-live-tail').classList.add('hidden');
    }
    if ($('agent-live-foot')) {
      setHtmlIfChanged($('agent-live-foot'), '');
      $('agent-live-foot').classList.add('hidden');
    }
    $('agent-pane')?.classList.remove('has-live-foot');
    if (typeof state.tab === 'string' && state.tab.startsWith('agent:')) {
      selectTab('feed');
    }
    state.swarm = id;
    state.etag = '';
    state.snap = null;
    state.seenSeqs.clear();
    state.hiddenAgents.clear();
    state.channel = '';
    state.expanded.clear();
    state.dirtyTabs.clear();
    state.pendingNewSeqs.clear();
    rebuildIndexes(null);
    feedMarkup = '';
    agentsMarkup = '';
    filterMarkup.clear();
    setHtmlIfChanged($('channel-tabs'), '');
    setHtmlIfChanged($('f-channel'), '');
    setHtmlIfChanged($('telemetry-strip'), '');
    setHtmlIfChanged($('ov-status'), '');
    setHtmlIfChanged($('ov-system'), '');
    setHtmlIfChanged($('ov-notice'), '');
    setHtmlIfChanged($('ov-ack'), '');
    setHtmlIfChanged($('raw-meta'), '');
    setHtmlIfChanged($('replay-label'), '');
    setHtmlIfChanged($('replay-caption'), '');
    if (typeof graphCache !== 'undefined') graphCache.acc = null;
    state.firstLoad = true;
    state.stick = true;
    $('f-agent').value = '';
    $('f-mention').value = '';
    if ($('f-kind')) $('f-kind').value = '';
    $('f-search').value = '';
    replayStop();
    closeGifModal();
    frameTeardown();
    frame.failed = null;
    state.tabChosen = urlTabChosen;
  }

  function syncTabsOverflow() {
    const nav = document.querySelector('.tabs');
    if (!nav) return;
    const maxScroll = Math.max(0, nav.scrollWidth - nav.clientWidth);
    const hasOverflow = maxScroll > 1;
    nav.classList.toggle('overflow-left', hasOverflow && nav.scrollLeft > 1);
    nav.classList.toggle('overflow-right', hasOverflow && nav.scrollLeft < maxScroll - 1);
  }

  function showView(view) {
    if (view === state.view) return;
    state.view = view;
    $('loading').classList.toggle('hidden', view !== 'loading');
    $('board').classList.toggle('hidden', view !== 'board');
    $('empty').classList.toggle('hidden', view !== 'empty');
    if (view !== 'board') frameTeardown();
    syncWatchdog();
    if (view === 'board' && state.snap) {
      const flushed = flushActiveTab();
      syncTabsOverflow();
      if (state.tab === 'feed' && !flushed) {
        measureClamps();
        if ($('f-autoscroll').checked && state.stick) scrollToLatest();
        updateJump();
      }
    }
  }

  function isSwarmActive(snap) {
    if (!snap || !snap.swarm) return false;
    if ((snap.telemetry?.activeWorkers || 0) > 0) return true;
    const ms = Array.isArray(snap.members) ? snap.members : [];
    return ms.some((m) => !m.killed && (
      m.status === 'working' ||
      Boolean(m.currentTool && m.currentTool.name) ||
      (Array.isArray(m.subagents) && m.subagents.some((s) => s && s.status === 'working'))
    ));
  }

  function showEmpty(conv) {
    const strip = $('telemetry-strip');
    if (strip) {
      strip.classList.add('hidden');
      setHtmlIfChanged(strip, '');
    }
    $('empty-body').innerHTML = conv ?
        'Swarms started in this conversation appear here automatically, with their board, ' +
            'members and @mention graph.' :
        'Open this pane from a conversation to see its swarm. Swarms started there ' +
            'appear here automatically.';
    showView('empty');
  }

  function setLive(ok) {
    const live = $('live');
    const stale = !ok;
    live.classList.toggle('stale', stale);
    const label = stale ? 'Reconnecting' : 'Live';
    if ($('live-label').textContent !== label) $('live-label').textContent = label;
    live.title = stale ? 'Lost contact with the swarm sidecar; retrying' : 'Updates automatically';
  }

  function schedulePoll(ms) {
    clearTimeout(state.pollTimer);
    if (document.hidden) return;
    state.pollTimer = setTimeout(poll, ms);
  }

  async function poll() {
    const conv = currentConv();
    if (state.polling) {
      if (conv === state.conv) return;
      state.pollAbort?.abort();
    }
    if (conv !== state.conv) {
      state.conv = conv;
      resetSwarm(null);
    }
    const gen = ++state.pollGen;
    const ac = new AbortController();
    state.pollAbort = ac;
    state.polling = true;
    let delay = isSwarmActive(state.snap) ? ACTIVE_POLL_MS : IDLE_POLL_MS;
    let transientErr = false;
    try {
      const q = new URLSearchParams({ etag: state.etag });
      if (conv) q.set('conversationId', conv);
      const r = await getJson(`./api/swarm?${q}`, { signal: ac.signal, timeoutMs: 8000 });
      if (gen !== state.pollGen || conv !== currentConv()) return;
      if (!r || r.online === false || r.transientError || r.error || (r.status && r.status >= 500)) {
        transientErr = true;
        return;
      }
      state.lastOk = Date.now();
      const swarmId = r.swarm !== null ? (r.swarm || r.id || r.meta?.swarmId || null) : null;
      if (!swarmId) {
        if (r.online === true || r.swarm === null) {
          if (state.swarm) resetSwarm(null, { keepPoll: true });
          state.me = null;
          showEmpty(r?.conversationId || conv);
          delay = IDLE_POLL_MS;
        }
        return;
      }
      if (swarmId !== state.swarm) resetSwarm(swarmId, { keepPoll: true });
      if (!r.unchanged) {
        state.me = r.member || null;
        ingest({ ...r, swarm: swarmId, id: swarmId });
        state.etag = r.etag || '';
      } else if (r.member) {
        state.me = r.member;
      }
      delay = isSwarmActive(state.snap) ? ACTIVE_POLL_MS : IDLE_POLL_MS;
      showView('board');
    } catch (e) {
      if (gen === state.pollGen) {
        transientErr = true;
        console.warn(e);
      }
    } finally {
      if (gen === state.pollGen) {
        state.polling = false;
        state.pollAbort = null;
        setLive(!transientErr && Date.now() - state.lastOk <= POLL_MS * 4);
        schedulePoll(delay);
      }
    }
  }

  document.addEventListener('visibilitychange', () => {
    if (document.hidden) {
      clearTimeout(state.pollTimer);
      clearTimeout(state.agentPollTimer);
      clearTimeout(bootstrapTimer);
      bootstrapTimer = 0;
    } else {
      poll();
      if (ov.bootstrapPending && !ov.bootstrap && !ov.bootstrapLoading) {
        ov.bootstrapPending = false;
        loadBootstrap();
      }
      if (typeof state.tab === 'string' && state.tab.startsWith('agent:')) {
        const aName = state.tab.slice(6);
        if (state.openAgentTabs.includes(aName)) fetchAgentHistory(aName, { incremental: true });
      }
    }
  });

  function flushActiveTab(newSet = null) {
    if (newSet && newSet.size) {
      for (const seq of newSet) state.pendingNewSeqs.add(seq);
    }
    if (!state.snap || state.view !== 'board') return false;
    const tab = state.tab;
    if (tab === 'feed') {
      if (!state.dirtyTabs.has('feed') && !state.pendingNewSeqs.size) return false;
      const mergedNew = new Set(state.pendingNewSeqs);
      state.pendingNewSeqs.clear();
      state.dirtyTabs.delete('feed');
      renderAgents();
      renderFilters();
      renderChannels();
      renderFeed(mergedNew);
      return true;
    } else if (tab === 'members') {
      if (!state.dirtyTabs.has('members')) return false;
      state.dirtyTabs.delete('members');
      renderMembers();
      return true;
    } else if (tab === 'graph') {
      if (!state.dirtyTabs.has('graph')) return false;
      state.dirtyTabs.delete('graph');
      renderGraph();
      return true;
    } else if (tab === 'raw') {
      if (!state.dirtyTabs.has('raw')) return false;
      state.dirtyTabs.delete('raw');
      renderRaw();
      return true;
    } else if (tab === 'overview') {
      if (!state.dirtyTabs.has('overview')) return false;
      state.dirtyTabs.delete('overview');
      renderOverview();
      return true;
    }
    return false;
  }

  function ingest(snap) {
    const newSeqs = [];
    const activeSeqs = new Set();
    for (const m of snap.messages || []) {
      activeSeqs.add(m.seq);
      if (!state.seenSeqs.has(m.seq)) {
        newSeqs.push(m.seq);
        state.seenSeqs.add(m.seq);
      }
    }
    for (const seq of state.seenSeqs) {
      if (!activeSeqs.has(seq)) state.seenSeqs.delete(seq);
    }
    for (const seq of state.expanded) {
      if (!activeSeqs.has(seq)) state.expanded.delete(seq);
    }
    state.snap = snap;
    rebuildIndexes(snap);
    const rawUnseen = Array.isArray(snap.unseen)
      ? snap.unseen
      : (state.me && snap.unseenBy && Array.isArray(snap.unseenBy[state.me]) ? snap.unseenBy[state.me] : null);
    state.unseen = Array.isArray(rawUnseen) ? new Set(rawUnseen) : null;
    const newSet = state.firstLoad ? new Set() : new Set(newSeqs);
    state.newEdgeKeys = new Set();
    if (!state.firstLoad) {
      for (const m of snap.messages || []) {
        if (!newSet.has(m.seq)) continue;
        const { direct, viaAll } = targetsOf(m, true);
        const sender = senderOf(m);
        for (const t of [...direct, ...viaAll])
          state.newEdgeKeys.add(`${sender}>${t}`);
      }
    }
    if (snap.view?.html && !ov.bootstrap && !ov.bootstrapLoading) {
      loadBootstrap();
    }
    ov.model = overviewOn() && snap.view?.json ? sanitizeView(snap.view.json, snap) : null;
    for (const t of TABS) state.dirtyTabs.add(t);
    for (const seq of newSet) state.pendingNewSeqs.add(seq);
    renderMeta();
    renderOverviewBadge();
    autoSelectTab();
    flushActiveTab();
    if (newSet.size) {
      const live = $('live');
      live.classList.add('flash');
      setTimeout(() => live.classList.remove('flash'), 600);
    }
    state.firstLoad = false;
  }

  // ---------- rendering ----------
  {
    window.SwarmHive.hooks = {
      members: () => selectTab('members', false, true),
      member: (name, subSessionId = '') => openMember(name, subSessionId),
    };
  }

  const fmtTokCompact = (n) => {
    const v = Number(n) || 0;
    if (v < 1000) return String(Math.round(v));
    if (v < 100000) return `${(v / 1000).toFixed(1)}k`;
    if (v < 1000000) return `${Math.round(v / 1000)}k`;
    return `${(v / 1000000).toFixed(2)}M`;
  };
  const fmtTpsCompact = (v) => {
    const n = Number(v) || 0;
    return n < 10 ? `${n.toFixed(1)} t/s` : `${Math.round(n)} t/s`;
  };
  const fmtCostCompact = (c) => {
    if (c == null) return '';
    const n = Number(c);
    if (!Number.isFinite(n) || n <= 0) return '';
    return n < 0.01 ? `$${n.toFixed(3)}` : `$${n.toFixed(2)}`;
  };

  function renderTelemetryStrip() {
    const strip = $('telemetry-strip');
    if (!strip) return;
    const s = state.snap;
    const t = s && s.telemetry && typeof s.telemetry === 'object' ? s.telemetry : null;
    if (!t) {
      strip.classList.add('hidden');
      setHtmlIfChanged(strip, '');
      return;
    }
    const active = Number(t.activeWorkers) || 0;
    const waiting = Number(t.waitingWorkers) || 0;
    const subs = Number(t.subagentCount) || 0;
    const steps = Number(t.totalSteps) || 0;
    const tools = Number(t.totalToolCalls) || 0;
    const processed = Number(t.totalProcessedTokens ?? t.totalTokens) || 0;
    const pure = Number(t.pureInputTokens ?? t.newTokens ?? t.totalNewTokens) || 0;
    const cached = Number(t.cachedInputTokens ?? t.cacheReadTokens ?? t.totalCacheRead) || 0;
    const outTok = Number(t.outputTokens) || 0;
    const rawRate = t.cacheHitRate != null
      ? Number(t.cacheHitRate)
      : ((pure + cached) > 0 ? cached / (pure + cached) : 0);
    const cachePct = rawRate > 0 ? (rawRate <= 1 ? Math.round(rawRate * 100) : Math.round(rawRate)) : 0;
    const tps = Number(t.liveTps) > 0 ? Number(t.liveTps) : (Number(t.avgTps) || 0);
    const cost = t.totalCostUsd != null ? fmtCostCompact(t.totalCostUsd) : '';
    const lowCacheAlert = !!t.lowCacheHitAlert;
    const compactionWarn = Number(t.compactionWarningCount) || 0;
    if (!active && !steps && !tools && !processed && !pure && !tps && !cost && !lowCacheAlert && !compactionWarn) {
      strip.classList.add('hidden');
      setHtmlIfChanged(strip, '');
      return;
    }
    const items = [];
    if (active > 0) {
      items.push(`<span class="ts-item ts-active"><span class="ts-dot" aria-hidden="true"></span>${active} active</span>`);
    } else if (waiting > 0) {
      items.push(`<span class="ts-item">${waiting} waiting</span>`);
    }
    if (subs > 0) items.push(`<span class="ts-item">${subs} sub</span>`);
    if (steps > 0) items.push(`<span class="ts-item">${steps} ${steps === 1 ? 'step' : 'steps'}</span>`);
    if (tools > 0) items.push(`<span class="ts-item">${tools} ${tools === 1 ? 'tool' : 'tools'}</span>`);
    if (processed > 0) items.push(`<span class="ts-item" title="Total billable processed tokens">${fmtTokCompact(processed)} proc</span>`);
    if (pure > 0) items.push(`<span class="ts-item" title="Uncached pure input tokens">${fmtTokCompact(pure)} pure</span>`);
    if (cached > 0) items.push(`<span class="ts-item" title="Cached input tokens">${fmtTokCompact(cached)} cached</span>`);
    if (outTok > 0) items.push(`<span class="ts-item" title="Output tokens">${fmtTokCompact(outTok)} out</span>`);
    if (cachePct > 0) items.push(`<span class="ts-item" title="Prompt cache hit rate">${cachePct}% cache</span>`);
    if (tps > 0) items.push(`<span class="ts-item">${esc(fmtTpsCompact(tps))}</span>`);
    if (cost) items.push(`<span class="ts-item">${esc(cost)}</span>`);
    if (lowCacheAlert) items.push(`<span class="ts-item ts-warn" title="Prompt cache hit rate is below 25%">Low cache</span>`);
    if (compactionWarn > 0) items.push(`<span class="ts-item ts-danger" title="Workers approaching context compaction threshold">${compactionWarn} CTX warn</span>`);
    const html = items.join('');
    setHtmlIfChanged(strip, html);
    strip.classList.toggle('hidden', !html);
  }

  function renderMeta() {
    const s = state.snap;
    const allMembers = s.members || [];
    const team = allMembers.filter((m) => !isRoleWorker(m));
    const visibleTeam = team.filter((m) => !m.killed);
    const title = s.title || s.meta?.title || 'Untitled swarm';
    const dir = s.dir || s.meta?.workspace || '';
    const head = Number.isFinite(s.head) ? s.head : ((s.messages || []).length ? s.messages[s.messages.length - 1].seq : 0);
    $('swarm-title').textContent = title;
    $('swarm-title').title = `${title}\nswarm ${s.id || s.swarm || ''}\n${dir}`;
    setHtmlIfChanged($('swarm-meta'), '');
    $('tab-count-feed').textContent = head ? String(head) : '';
    $('tab-count-members').textContent = visibleTeam.length ? String(visibleTeam.length) : '';
    if (state.openAgentTabs.length) renderAgentTabs();
    syncTabsOverflow();
    renderTelemetryStrip();
  }

  function renderAgents() {
    const html = members().map((m) => {
      const hidden = state.hiddenAgents.has(m.name);
      const role = roleOf(m);
      const posts = Number(m.posts) || 0;
      const tip = `${m.name}${m.name === state.me ? ' (you)' : ''} · ${role}${m.model ? ' · ' + m.model : ''} · ${statusLabel(m.status)} · ${plural(posts, 'post')}` +
          `\n${hidden ? 'Hidden from the board; click to show' : 'Click to hide from the board'}`;
      return `<button type="button" class="chip ${hidden ? 'dim' : ''} ${m.killed ? 'killed' : ''}" data-agent="${esc(m.name)}" data-testid="swarm-member-chip" ` +
          `aria-pressed="${hidden ? 'false' : 'true'}" title="${esc(tip)}">` +
          `<span class="chip-name">${esc(m.name)}${m.name === state.me ? ' (you)' : ''}</span>` +
          `<span class="chip-count">${posts}</span></button>`;
    }).join('');
    if (html !== agentsMarkup) {
      agentsMarkup = html;
      $('agents').innerHTML = html;
    }
  }

  function renderFilters() {
    const names = members().map((m) => m.name);
    const msgs = state.snap.messages || [];
    const posters = [...new Set([...names, ...msgs.map((m) => senderOf(m)).filter(Boolean)])];
    const kinds = [...new Set(['post', 'decision', 'handoff', 'escalation', 'blocker', ...msgs.filter((m) => !isControl(m)).map((m) => msgTypeOf(m)).filter(Boolean)])];
    const fill = (id, first, opts) => {
      const sel = $(id);
      if (!sel) return;
      const prev = sel.value;
      const html = `<option value="">${first}</option>` + opts.map(([v, l]) => `<option value="${esc(v)}">${esc(l)}</option>`).join('');
      if (filterMarkup.get(id) !== html) {
        if (document.activeElement === sel) return;
        filterMarkup.set(id, html);
        sel.innerHTML = html;
      }
      const next = opts.some(([v]) => v === prev) ? prev : '';
      if (sel.value !== next) sel.value = next;
      sel.classList.toggle('set', !!sel.value);
    };
    fill('f-agent', 'From anyone', posters.map((n) => [n, `From ${n}`]));
    fill('f-mention', 'To anyone', [['@none', 'To no one'], ['@all', 'To @all'], ...names.map((n) => [n, `To ${n}`])]);
    fill('f-kind', 'All kinds', kinds.map((k) => [k, `Kind: ${k}`]));
  }

  // ---------- channels ----------
  const normChan = (c) => String(c || '').replace(/^#/, '').trim().toLowerCase();
  const isDefaultChan = (c) => {
    const n = normChan(c);
    return !n || n === 'main' || n === 'general';
  };
  const chanOf = (m) => {
    const n = normChan(m?.channel);
    return n || 'main';
  };
  const isControl = (m) => !!(m?.control && typeof m.control === 'object');
  const swarmChannels = () => (Array.isArray(state.snap?.channels) ? state.snap.channels : []);
  const nonDefaultChannels = () => swarmChannels().filter((c) => !isDefaultChan(c.name));
  const hasChannels = () => nonDefaultChannels().length > 0;

  function seesNow(m) {
    if (isControl(m)) return false;
    return !(state.unseen && state.unseen.has(m.seq));
  }

  function renderChannels() {
    const chans = nonDefaultChannels();
    const strip = $('channel-tabs');
    const wrap = $('f-channel-wrap');
    const on = chans.length > 0;
    strip.classList.toggle('hidden', !on);
    wrap.classList.toggle('hidden', !on);
    if (!on) {
      state.channel = '';
      setHtmlIfChanged(strip, '');
      const sel = $('f-channel');
      if (sel) {
        if (sel.value !== '') sel.value = '';
        sel.classList.remove('set');
      }
      return;
    }
    const baseChan = swarmChannels().find((c) => normChan(c.name) === 'general') ? 'general' : 'main';
    const names = [baseChan, ...chans.map((c) => normChan(c.name))];
    if (state.channel && !names.includes(state.channel)) state.channel = '';
    const counts = new Map();
    for (const m of state.snap.messages || []) {
      const ch = isDefaultChan(m.channel) ? baseChan : normChan(m.channel);
      counts.set(ch, (counts.get(ch) || 0) + 1);
    }
    const tab = (value, label, tip, extra = '') => {
      const sel = state.channel === value;
      return `<button type="button" role="tab" class="channel-tab ${sel ? 'on' : ''} ${extra}" data-channel="${esc(value)}" ` +
          `aria-selected="${sel}" tabindex="${sel ? 0 : -1}" title="${esc(tip)}" data-testid="swarm-channel-tab">${label}</button>`;
    };
    const byName = new Map(chans.map((c) => [normChan(c.name), c]));
    const html = tab('', 'All', 'Posts in every channel') + names.map((n) => {
      const c = byName.get(n);
      const count = counts.get(n) || 0;
      const tip = n === baseChan ? `${baseChan}: the whole swarm reads it` :
        `${n}${c?.topic ? ` · ${c.topic}` : ''}${c?.created_by || c?.createdBy ? `\ncreated by ${c.created_by || c.createdBy}` : ''}${c?.archived ? '\narchived' : ''}`;
      return tab(n, `#${esc(n)}${count ? `<span class="chip-count">${count}</span>` : ''}`, tip, c?.archived ? 'archived' : '');
    }).join('');
    setHtmlIfChanged(strip, html);
    const sel = $('f-channel');
    const opts = `<option value="">All channels</option>` +
        names.map((n) => `<option value="${esc(n)}">#${esc(n)}${byName.get(n)?.archived ? ' (archived)' : ''}</option>`).join('');
    if (document.activeElement !== sel) {
      setHtmlIfChanged(sel, opts);
      if (sel.value !== state.channel) sel.value = state.channel;
    }
    sel.classList.toggle('set', !!state.channel);
  }

  function setChannel(value) {
    state.channel = normChan(value);
    renderChannels();
    renderFeed();
  }

  function channelChipHtml(m) {
    if (isDefaultChan(m.channel)) return '';
    const ch = normChan(m.channel);
    return `<button type="button" class="msg-channel" data-channel="${esc(ch)}" data-testid="swarm-message-channel" ` +
        `title="Posted in #${esc(ch)}; click to show only #${esc(ch)}">#${esc(ch)}</button>`;
  }

  function filtersActive() {
    return !!($('f-agent').value || $('f-mention').value || $('f-kind')?.value || $('f-search').value.trim() || state.hiddenAgents.size || state.channel);
  }

  function updateFilterToggle() {
    const hidden = !!($('f-agent').value || $('f-mention').value || $('f-kind')?.value || state.hiddenAgents.size || state.channel);
    const t = $('f-toggle');
    t.classList.toggle('active', hidden);
    t.title = hidden ? 'Filters (active)' : 'Filters';
  }
  function setFiltersOpen(open) {
    $('filters').classList.toggle('hidden', !open);
    $('f-toggle').setAttribute('aria-expanded', String(open));
  }

  function visibleMessages() {
    const s = state.snap;
    const from = $('f-agent').value;
    const to = $('f-mention').value;
    const kindFilter = $('f-kind')?.value || '';
    const q = $('f-search').value.trim().toLowerCase();
    const ch = state.channel;
    return (s.messages || []).filter((m) => {
      const mChan = isDefaultChan(m.channel) ? (ch === 'general' ? 'general' : 'main') : normChan(m.channel);
      if (ch && mChan !== ch) return false;
      const sender = senderOf(m);
      if (state.hiddenAgents.has(sender)) return false;
      if (from && sender !== from) return false;
      if (kindFilter && (isControl(m) ? 'control' : msgTypeOf(m)) !== kindFilter) return false;
      if (to === '@none' && ((m.mentions || []).length || m.all || channelMentionsOf(m).length)) return false;
      if (to === '@all' && !m.all) return false;
      if (to && to[0] !== '@' && !(m.all || (m.mentions || []).includes(to) || viaChannelOf(m).includes(to))) return false;
      if (q) {
        const hay = [
          `#${m.seq}`,
          sender,
          msgTypeOf(m),
          mChan,
          ...(m.mentions || []),
          ...viaChannelOf(m),
          ...channelMentionsOf(m).map((c) => `#${normChan(c)}`),
          ...(Array.isArray(m.files) ? m.files : []),
          m.summary || '',
          textOf(m),
        ].join('\n').toLowerCase();
        if (!hay.includes(q)) return false;
      }
      return true;
    });
  }

  const nearBottom = () => {
    const f = $('feed');
    return !f || (f.scrollTop + f.clientHeight >= f.scrollHeight - 80);
  };
  const feedShown = () => state.view === 'board' && state.tab === 'feed';
  function scrollToLatest(behavior = 'auto') {
    state.stick = true;
    const f = $('feed');
    if (!f) return;
    if (behavior === 'smooth' && typeof f.scrollTo === 'function') {
      f.scrollTo({ top: f.scrollHeight, behavior: 'smooth' });
    } else {
      f.scrollTop = f.scrollHeight;
    }
  }
  function keepPinned() {
    if (feedShown() && state.stick && $('f-autoscroll').checked) scrollToLatest();
  }

  function controlHtml(m) {
    const t = msgMs(m);
    const sender = senderOf(m);
    const text = String(textOf(m) || '').replace(/^\[channel\]\s*/, '');
    return `<div class="msg msg-system" data-seq="${m.seq}" data-testid="swarm-control-message" role="note">` +
        `<span class="sys-text" title="${esc(`#${m.seq} · ${sender} · ${fmtFull(t)}`)}">${esc(text)}</span>` +
        `<time class="ts" datetime="${esc(isoTime(t))}">${esc(fmtClock(t))}</time></div>`;
  }

  function stripLeadingMentions(text) {
    const t = String(text || '');
    const rest = t.replace(/^(\s*@#?[\p{L}\p{N}_.-]+[,:]?)+\s*/u, '');
    return rest || t;
  }

  function messageHtml(m, { unreadByMe = false } = {}) {
    if (isControl(m)) return controlHtml(m);
    const sender = senderOf(m);
    const bodyText = textOf(m);
    const kind = msgTypeOf(m);
    const replyTo = replyToOf(m);
    const replySeq = resolveReplySeq(replyTo);
    const targets = [...(m.mentions || [])].filter((n) => n !== sender);
    const open = state.expanded.has(m.seq);
    const onRoster = !!memberOf(sender);
    const t = msgMs(m);
    const tsTitle = esc(`#${m.seq} · ${fmtFull(t)}`);
    const dt = esc(isoTime(t));
    const cls = ['msg', unreadByMe ? 'unread-by-me' : ''].filter(Boolean).join(' ');
    const color = window.SwarmHive?.colorOf ? window.SwarmHive.colorOf(sender) : '#8a8f98';
    const avatar = window.SwarmHive?.avatarHtml ? window.SwarmHive.avatarHtml(sender) :
        `<span class="msg-av" style="--av:${esc(color)}" aria-hidden="true">${esc((sender || '?').charAt(0).toUpperCase())}</span>`;
    const kindPill = kind && kind !== 'post' ? `<span class="msg-kind ${esc(kind)}">${esc(kind)}</span>` : '';
    const replyPill = replySeq
      ? `<button type="button" class="ref seq-ref" data-seq-ref="${replySeq}" title="In reply to #${replySeq}">↩ #${replySeq}</button>`
      : (typeof replyTo === 'string' && replyTo.trim() && !/^#?\d+$/.test(replyTo.trim())
        ? '<span class="msg-note">↩ reply</span>'
        : '');
    const authorHtml = onRoster
      ? `<button type="button" class="msg-author msg-author-btn" data-feed-member="${esc(sender)}" title="Open @${esc(sender)}">@${esc(sender)}</button>`
      : `<span class="msg-author" title="Not on the roster">@${esc(sender)}</span>`;
    const head = `<header class="msg-head">
            <span class="msg-who">
              ${authorHtml}
              ${channelChipHtml(m)}
              ${kindPill}
              ${recipientsHtml(m)}
              ${replyPill}
              ${!onRoster ? '<span class="msg-note">not on roster</span>' : ''}
            </span>
            <time class="ts" datetime="${dt}" title="${tsTitle}">#${m.seq} · ${esc(fmtClock(t))}</time>
          </header>`;
    return `<article class="${cls}" data-seq="${m.seq}" data-testid="swarm-message"` +
        ` aria-label="${esc(`${sender}${targets.length || m.all ? ' to ' + [...targets, ...(m.all ? ['all'] : [])].join(', ') : ''}, ${fmtClock(t)}`)}">
          ${avatar}${head}
          <div class="msg-body md ${open ? 'open' : ''}">${md(targets.length || m.all ? stripLeadingMentions(bodyText) : bodyText)}</div>
          <button type="button" class="msg-more hidden" data-testid="swarm-message-more" aria-expanded="${open}">${open ? 'Show less' : 'Show more'}</button>
      </article>`;
  }

  let suppressScrollSync = false;
  function measureClamps() {
    if (!feedShown()) return;
    const feed = $('feed');
    if (!feed) return;
    const bodies = [...feed.querySelectorAll('.msg-body')];
    if (!bodies.length) return;
    const prevTop = feed.scrollTop;
    suppressScrollSync = true;
    try {
      const open = bodies.map((body) => body.classList.contains('open'));
      for (const body of bodies) {
        body.classList.add('clamped');
        body.classList.remove('open');
      }
      const overflows = bodies.map((body) => body.scrollHeight > body.clientHeight + 4);
      bodies.forEach((body, i) => {
        body.classList.toggle('open', open[i]);
        body.classList.toggle('clamped', overflows[i]);
        body.nextElementSibling?.classList.toggle('hidden', !overflows[i]);
      });
      if (feed.scrollTop !== prevTop) feed.scrollTop = prevTop;
    } finally {
      requestAnimationFrame(() => { suppressScrollSync = false; });
    }
  }

  let feedMarkup = '';
  function renderFeed(newSet = new Set()) {
    const feed = $('feed');
    const wasNearBottom = nearBottom();
    const prevScrollTop = feed.scrollTop;
    const myMember = state.me ? memberOf(state.me) : null;
    const myCursor = myMember ? (myMember.lastReadSeq ?? myMember.last_read_seq ?? null) : null;
    const allMsgs = visibleMessages();
    const winSize = Math.max(120, Number(state.feedWindow) || 120);
    const hiddenCount = Math.max(0, allMsgs.length - winSize);
    const msgs = hiddenCount > 0 ? allMsgs.slice(-winSize) : allMsgs;
    const olderBtn = hiddenCount > 0
      ? `<div class="feed-older-wrap"><button type="button" class="ghost-btn sm feed-older-btn" data-show-older data-testid="swarm-show-older">Show older (${hiddenCount} more)</button></div>`
      : '';
    let dividerDone = false;
    const html = olderBtn + msgs.map((m) => {
      const unreadByMe = myCursor !== null && m.seq > myCursor && senderOf(m) !== state.me && seesNow(m);
      let pre = '';
      if (unreadByMe && !dividerDone) {
        dividerDone = true;
        pre = `<div class="divider" role="separator" data-testid="swarm-unread-divider">New</div>`;
      }
      return pre + messageHtml(m, { unreadByMe });
    }).join('');
    updateFilterToggle();
    const markup = allMsgs.length ? html : ((state.snap.messages || []).length ?
      `<div class="feed-empty" data-testid="swarm-feed-empty">${ICON.search}
          <div class="feed-empty-title">No posts match</div>
          <div>Try a different search or member filter.</div>
          <button type="button" class="ghost-btn" data-clear-filters data-testid="swarm-clear-filters">Clear filters</button></div>` :
      `<div class="feed-empty" data-testid="swarm-feed-empty">${ICON.inbox}
          <div class="feed-empty-title">No posts yet</div>
          <div>Posts to the swarm board will appear here as members talk.</div></div>`);
    const shouldStick = $('f-autoscroll').checked && state.tab === 'feed' &&
        (state.firstLoad || state.stick || (newSet.size && wasNearBottom));
    if (markup !== feedMarkup) {
      feedMarkup = markup;
      suppressScrollSync = true;
      feed.innerHTML = markup;
      if (state.view === 'board' && state.tab === 'feed') measureClamps();
      else requestAnimationFrame(() => { suppressScrollSync = false; });
      if (!shouldStick && prevScrollTop > 0 && feed.scrollTop !== prevScrollTop) {
        feed.scrollTop = prevScrollTop;
      }
    }
    for (const seq of newSet) feed.querySelector(`.msg[data-seq="${seq}"]`)?.classList.add('new');
    if (shouldStick) {
      scrollToLatest();
    }
    updateJump();
  }

  function updateJump() {
    const f = $('feed');
    const onFeed = state.tab === 'feed' && state.view === 'board';
    const scrollable = f && f.scrollHeight > f.clientHeight * 1.3;
    const showLatest = onFeed && !nearBottom() && scrollable;
    const showTop = onFeed && f && f.scrollTop > 400;
    $('jump-latest').classList.toggle('hidden', !showLatest);
    $('jump-top')?.classList.toggle('hidden', !showTop);
    if (typeof updateAgentJump === 'function') {
      updateAgentJump(state.tab.startsWith('agent:') ? state.tab.slice(6) : '');
    }
  }

  function renderMembers() {
    window.SwarmHive.update(state.snap, $('members'), { view: 'cards' });
  }

  const pillWidth = (id, maxW) => {
    const full = Math.max(72, 54 + Math.ceil(String(id).length * 6.8));
    return id === 'coordinator' ? Math.min(Math.max(maxW, 136), full) : Math.min(maxW, full);
  };

  function layout(nodes, width) {
    const nodeH = 28;
    const clampX = (x, w) => Math.max(w / 2 + 8, Math.min(width - w / 2 - 8, x));
    if (nodes.length <= 16) {
      const pos = new Map();
      const n = nodes.length;
      const maxPillW = Math.max(76, Math.min(180, Math.floor(width * 0.44)));
      const w = Math.min(maxPillW, Math.max(76, width / Math.max(4, Math.ceil(n / 2) + 1) - 10));
      const rx = Math.max(24, Math.min(width * 0.42, width / 2 - w / 2 - 18));
      const ry = n <= 2 ? Math.min(60, Math.max(36, rx * 0.6)) : Math.min(250, Math.max(96, Math.min(n * 20, rx * 0.78 + 44)));
      const cx = width / 2, cy = ry + nodeH / 2 + 20;
      nodes.forEach((node, i) => {
        const a = -Math.PI / 2 + (2 * Math.PI * i) / n;
        const pw = pillWidth(node.id, w);
        pos.set(node.id, { x: clampX(cx + rx * Math.cos(a), pw), y: cy + ry * Math.sin(a), w: pw, h: nodeH });
      });
      return { pos, height: Math.ceil(cy + ry + nodeH / 2 + 20) };
    }
    const rows = new Map();
    for (const n of nodes) {
      if (!rows.has(n.row)) rows.set(n.row, []);
      rows.get(n.row).push(n);
    }
    const pos = new Map();
    const minNodeW = Math.min(114, Math.max(76, width - 24)), lineH = 64, rowGap = 40;
    const perLine = Math.max(1, Math.floor((width - 16) / (minNodeW + 12)));
    let y = 32;
    for (const [, list] of [...rows.entries()].sort((a, b) => a[0] - b[0])) {
      for (let i = 0; i < list.length; i += perLine) {
        const line = list.slice(i, i + perLine);
        const gap = width / (line.length + 1);
        const w = Math.min(168, Math.max(minNodeW, gap - 12));
        line.forEach((n, j) => {
          const pw = pillWidth(n.id, Math.max(72, Math.min(w, gap - 8)));
          pos.set(n.id, { x: clampX(gap * (j + 1), pw), y: y + (line.length > 3 && j % 2 ? 14 : 0), w: pw, h: nodeH });
        });
        y += lineH;
      }
      y += rowGap;
    }
    return { pos, height: Math.ceil(y - rowGap + 14) };
  }

  // ---------- graph + replay ----------
  const graphMsgs = () => {
    const roster = state.snap?.members || [];
    return (state.snap?.messages || []).filter((m) => !isControl(m) && !isRoleWorker(senderOf(m), roster));
  };
  const replay = { idx: null, playing: false, timer: null, animatedSeq: null };

  function edgeGeom(a, b) {
    const dx = b.x - a.x, dy = b.y - a.y, len = Math.hypot(dx, dy) || 1;
    const nx = -dy / len, ny = dx / len, bend = 24;
    const mx = (a.x + b.x) / 2 + nx * bend, my = (a.y + b.y) / 2 + ny * bend;
    const trim = (p, q) => {
      const tx = q.x - p.x, ty = q.y - p.y, l = Math.hypot(tx, ty) || 1;
      const ux = tx / l, uy = ty / l;
      const toBorder = Math.min(ux ? p.w / 2 / Math.abs(ux) : Infinity, uy ? p.h / 2 / Math.abs(uy) : Infinity);
      const t = Math.min(0.45 * l, toBorder + 4);
      return { x: p.x + ux * t, y: p.y + uy * t };
    };
    const s1 = trim(a, { x: mx, y: my }), e1 = trim(b, { x: mx, y: my });
    return { d: `M${s1.x},${s1.y} Q${mx},${my} ${e1.x},${e1.y}`, start: s1, mx, my, end: e1 };
  }

  function createGraphAccumulator(s, all, { expandAll, hideInactive, width }) {
    const roster = s.members || [];
    const memberNames = new Set(roster.map((m) => m.name));
    const outsiders = [...new Set(all.map((m) => senderOf(m)))].filter((n) => n && !memberNames.has(n) && !isRoleWorker(n, roster));
    const allNodes = [
      ...roster.filter((m) => !isRoleWorker(m, roster)).map((m) => ({ id: m.name, row: m.role === 'coordinator' ? 0 : 1, m })),
      ...outsiders.map((n) => ({ id: n, row: 2, m: null })),
    ];
    const isKilled = (n) => Boolean(n.m?.killed || n.m?.status === 'killed');

    const active = new Set(['coordinator']);
    const edges = new Map();
    const unaddressed = new Map();
    const broadcasts = new Map();
    const bump = (k, field) => { const e = edges.get(k) || { direct: 0, viaAll: 0 }; e[field]++; edges.set(k, e); };
    let cursor = 0;

    let cachedLayoutKey = null;
    let cachedLayout = null;
    const layoutFor = (nodes) => {
      const key = hideInactive ? nodes.map((n) => n.id).join('\0') : '*';
      if (key !== cachedLayoutKey) {
        cachedLayoutKey = key;
        cachedLayout = layout(nodes, width);
      }
      return cachedLayout;
    };

    const advanceTo = (upto) => {
      const target = Math.max(cursor, Math.min(upto, all.length));
      for (; cursor < target; cursor++) {
        const m = all[cursor];
        const sender = senderOf(m);
        if (isRoleWorker(sender, roster)) continue;
        active.add(sender);
        const { direct, viaAll } = targetsOf(m, expandAll, roster);
        if (m.all) broadcasts.set(sender, (broadcasts.get(sender) || 0) + 1);
        if (!direct.size && !viaAll.size && !m.all && !(m.mentions || []).some((n) => n !== sender))
          unaddressed.set(sender, (unaddressed.get(sender) || 0) + 1);
        for (const t of direct) {
          active.add(t);
          bump(`${sender}>${t}`, 'direct');
        }
        for (const t of viaAll) {
          if (!hideInactive) active.add(t);
          bump(`${sender}>${t}`, 'viaAll');
        }
      }
    };

    const snapshot = ({ replaying }) => {
      const current = replaying && cursor > 0 ? all[cursor - 1] : null;
      const curSender = current ? senderOf(current) : '';
      const currentDirect = current && !isRoleWorker(curSender, roster)
        ? new Set([curSender, ...targetsOf(current, false, roster).direct])
        : new Set();
      const nodes = hideInactive
        ? allNodes.filter((n) => currentDirect.has(n.id) || (active.has(n.id) && !isKilled(n)))
        : allNodes;
      const { pos, height } = layoutFor(nodes);

      const isActive = (id) => !replaying || active.has(id);
      let curTargets = new Set();
      if (current) {
        const { direct, viaAll } = targetsOf(current, expandAll, roster);
        curTargets = new Set([...direct, ...viaAll]);
      }
      const isCur = (from, to) => !!current && from === curSender && curTargets.has(to);

      let max = 1;
      for (const [k, e] of edges) {
        const [from, to] = k.split('>');
        if (pos.has(from) && pos.has(to)) {
          const total = e.direct + e.viaAll;
          if (total > max) max = total;
        }
      }

      return {
        nodes, pos, height, edges, unaddressed, broadcasts,
        current, curTargets, max, isActive, isCur, replaying, upto: cursor,
      };
    };

    return { advanceTo, snapshot };
  }

  const graphCache = { acc: null, snapRef: null, msgLen: 0, lastSeq: 0, expandAll: false, hideInactive: false, width: 0, upto: 0 };
  function buildGraphState(s, all, { replaying, upto, expandAll, hideInactive, width }) {
    const lastSeq = all.length ? all[all.length - 1].seq : 0;
    const canReuse = graphCache.acc &&
      graphCache.snapRef === s &&
      graphCache.msgLen === all.length &&
      graphCache.lastSeq === lastSeq &&
      graphCache.expandAll === expandAll &&
      graphCache.hideInactive === hideInactive &&
      graphCache.width === width &&
      upto >= graphCache.upto;
    const acc = canReuse ? graphCache.acc : createGraphAccumulator(s, all, { expandAll, hideInactive, width });
    acc.advanceTo(upto);
    graphCache.acc = acc;
    graphCache.snapRef = s;
    graphCache.msgLen = all.length;
    graphCache.lastSeq = lastSeq;
    graphCache.expandAll = expandAll;
    graphCache.hideInactive = hideInactive;
    graphCache.width = width;
    graphCache.upto = upto;
    return acc.snapshot({ replaying });
  }

  function renderGraph() {
    const s = state.snap;
    if (!s) return;
    const hiveOn = state.graphView !== 'mentions' && !!window.SwarmHive?.map;
    $('tab-graph').classList.toggle('hive-on', hiveOn);
    $('hive').hidden = !hiveOn;
    document.querySelectorAll('.gview-btn').forEach((b) => {
      const on = (b.dataset.gview === 'hive') === hiveOn;
      b.classList.toggle('on', on);
      b.setAttribute('aria-pressed', String(on));
    });
    const all = graphMsgs();
    const replaying = replay.idx !== null;
    if (replaying && replay.idx > all.length) replay.idx = all.length;
    const upto = replaying ? replay.idx : all.length;
    if (hiveOn) {
      const cur = replaying && upto > 0 ? all[upto - 1] : null;
      window.SwarmHive.map(s, $('hive'), { seq: replaying ? (cur ? cur.seq : 0) : null });
      renderReplayBar(all, upto, cur);
      return;
    }
    const svg = $('graph');
    const measuredW = Math.floor(svg.clientWidth || $('tab-graph')?.clientWidth || 0);
    const width = Math.max(180, measuredW || 480);
    const expandAll = $('g-all').checked;
    const hideInactive = $('g-hide-inactive')?.checked || false;
    const g = buildGraphState(s, all, { replaying, upto, expandAll, hideInactive, width });
    const { nodes, pos, height, edges, unaddressed, broadcasts, current, curTargets, max, isActive, isCur } = g;

    svg.setAttribute('viewBox', `0 0 ${width} ${height}`);
    svg.style.height = 'auto';
    svg.style.aspectRatio = `${width} / ${height}`;

    const animateCur = !!current && current.seq !== replay.animatedSeq;
    replay.animatedSeq = current ? current.seq : null;
    const newEdgeKeys = state.newEdgeKeys;
    state.newEdgeKeys = new Set();

    let edgesSvg = '', countsSvg = '', dots = '', curIdx = 0;
    for (const [k, e] of edges) {
      const [from, to] = k.split('>');
      const a = pos.get(from), b = pos.get(to);
      if (!a || !b) continue;
      const n = e.direct + e.viaAll;
      const { d, mx, my, end } = edgeGeom(a, b);
      const w = 1 + (3 * n) / max;
      const cur = isCur(from, to);
      const pulse = replaying ? cur && animateCur : newEdgeKeys.has(k);
      const id = cur ? `cur-edge-${curIdx++}` : '';
      edgesSvg += `<path ${id ? `id="${id}"` : ''} class="edge ${e.direct ? '' : 'via-all'} ${pulse ? 'pulse' : ''} ${cur ? 'cur' : ''} ${replaying && !cur ? 'past' : ''}" d="${d}" ` +
        `stroke-width="${w.toFixed(2)}" marker-end="url(#arrow)"><title>${esc(from)} → ${esc(to)}: ${e.direct} direct` +
        `${e.viaAll ? `, ${e.viaAll} via @all` : ''}</title></path>`;
      if (n > 1) countsSvg += `<text class="count" x="${mx}" y="${my + 3}" text-anchor="middle">${n}</text>`;
      if (id && animateCur) {
        dots += `<circle class="msg-dot" r="4.5"><animateMotion class="dot-anim" dur="0.9s" begin="indefinite" fill="freeze"><mpath href="#${id}"/></animateMotion></circle>`;
      } else if (id) {
        dots += `<circle class="msg-dot" r="4.5" cx="${end.x}" cy="${end.y}"/>`;
      }
    }
    const defs = '<marker id="arrow" viewBox="0 0 10 10" refX="8.5" refY="5" markerUnits="userSpaceOnUse" markerWidth="8" markerHeight="8" orient="auto-start-reverse">' +
        '<path class="arrowhead" d="M1,1.2 L9,5 L1,8.8 Q2.6,5 1,1.2 z"/></marker>';
    let nodesSvg = '';
    for (const n of nodes) {
      const p = pos.get(n.id);
      const status = n.m?.status || '';
      const beeSrc = window.SwarmHive?.beeSrc ? window.SwarmHive.beeSrc(n.id) : '';
      const beeX = p.x - p.w / 2 + 8;
      const dotX = beeSrc ? beeX + 22 : p.x - p.w / 2 + 14;
      const dotY = beeSrc ? p.y - 7 : p.y;
      const labelX = beeSrc ? beeX + 28 : status ? dotX + 9 : p.x - p.w / 2 + 12;
      const maxChars = Math.max(4, Math.floor((p.x + p.w / 2 - 10 - labelX) / 6.8));
      const label = n.id.length > maxChars ? n.id.slice(0, maxChars - 1) + '…' : n.id;
      const hot = current && (n.id === senderOf(current) || curTargets.has(n.id));
      const ring = unaddressed.get(n.id);
      const bcast = expandAll ? 0 : broadcasts.get(n.id) || 0;
      const bw = 30 + 6 * String(bcast).length;
      const cid = convIdOf(n.m);
      const clickHint = isAntigravityMember(n.m, n.id) && canOpenConversation() && isValidConvId(cid)
        ? 'Click to open in Antigravity'
        : 'Click to open agent tab';
      const tip = n.m
        ? `${n.id} · ${n.m.role}${n.m.model ? ' · ' + n.m.model : ''} · ${statusLabel(status)} · ${n.m.steps || 0} steps${n.m.toolCalls ? ` · ${n.m.toolCalls} tools` : ''}${n.m.tokens?.total ? ` · ${fmtTokCompact(n.m.tokens.total)} tok` : ''}${n.m.tps ? ` · ${fmtTpsCompact(n.m.tps)}` : ''} · ${n.m.posts || 0} posts · ${n.m.unread || 0} unread${ring ? ` · ${ring} unaddressed` : ''}${broadcasts.get(n.id) ? ` · ${broadcasts.get(n.id)} @all` : ''}${n.m.activity && status === 'working' ? `\n${n.m.activity}` : ''}${n.m.host ? `\non ${n.m.host.label}` : ''}\n${clickHint}`
        : `${n.id} (not on the roster)`;
      const cls = ['node', isActive(n.id) ? '' : 'inactive', hot ? 'hot' : '', status === 'killed' ? 'killed' : '', n.m ? '' : 'outsider'].filter(Boolean).join(' ');
      nodesSvg += `<g class="${cls}" data-member="${esc(n.id)}" data-conv="${esc(cid)}">` +
        `<title>${esc(tip)}</title>` +
        (ring ? `<rect class="ring" x="${p.x - p.w / 2 - 4}" y="${p.y - p.h / 2 - 4}" width="${p.w + 8}" height="${p.h + 8}" rx="${p.h / 2 + 4}"/>` : '') +
        `<rect class="node-box" x="${p.x - p.w / 2}" y="${p.y - p.h / 2}" width="${p.w}" height="${p.h}" rx="${p.h / 2}"/>` +
        (beeSrc ? `<image class="node-bee" href="${beeSrc}" x="${beeX}" y="${p.y - 9}" width="22" height="18"/>` : '') +
        (status ? `<circle class="status-dot ${esc(status)}" cx="${dotX}" cy="${dotY}" r="${beeSrc ? 3 : 3.25}"/>` : '') +
        `<text class="node-label" x="${labelX}" y="${p.y + 4}">${esc(label)}</text>` +
        (bcast ? `<g class="bcast"><rect x="${p.x + p.w / 2 - bw + 6}" y="${p.y - p.h / 2 - 7}" width="${bw}" height="14" rx="7"/>` +
          `<text x="${p.x + p.w / 2 - bw / 2 + 6}" y="${p.y - p.h / 2 + 3}" text-anchor="middle">@all ${bcast}</text></g>` : '') +
        `</g>`;
    }
    svg.innerHTML = `<defs>${defs}</defs>${edgesSvg}${nodesSvg}${countsSvg}${dots}`;
    svg.querySelectorAll('.dot-anim').forEach((a) => a.beginElement?.());
    renderReplayBar(all, upto, current);
  }

  function renderReplayBar(all, upto, current) {
    const slider = $('replay-slider');
    slider.max = String(all.length);
    slider.value = String(upto);
    slider.style.setProperty('--pct', `${all.length ? (100 * upto) / all.length : 100}%`);
    const play = $('replay-play');
    const playIcon = replay.playing ? 'pause' : 'play';
    if (play.dataset.icon !== playIcon) {
      play.innerHTML = ICON[playIcon];
      play.dataset.icon = playIcon;
      play.setAttribute('aria-label', replay.playing ? 'Pause replay' : 'Play replay');
    }
    const liveBtn = $('replay-live');
    liveBtn.classList.toggle('on', replay.idx === null);
    liveBtn.setAttribute('aria-pressed', String(replay.idx === null));
    $('replay-start').disabled = replay.idx === 0;
    $('replay-back').disabled = upto <= 0;
    $('replay-fwd').disabled = replay.idx === null || upto >= all.length;
    const t0 = all.length ? msgMs(all[0]) : 0;
    const label = $('replay-label');
    const labelHtml = replay.idx === null
      ? `<b>${plural(all.length, 'post')}</b>` + (all.length > 1 ? ` over ${esc(fmtDur(msgMs(all[all.length - 1]) - t0))}` : '')
      : `<b>${upto}</b> of ${all.length}` + (current ? ` · #${current.seq} at ${esc(fmtClock(msgMs(current)))} (+${esc(fmtDur(msgMs(current) - t0))})` : '');
    setHtmlIfChanged(label, labelHtml);
    const cap = $('replay-caption');
    if (!current) {
      cap.classList.add('hidden');
      setHtmlIfChanged(cap, '');
      return;
    }
    cap.classList.remove('hidden');
    const capHtml = `<div class="msg-head">
          <span class="msg-who">
            <span class="msg-author">${esc(senderOf(current))}</span>
            ${recipientsHtml(current)}
          </span>
          <span class="ts" title="${esc(`#${current.seq} · ${fmtFull(msgMs(current))}`)}">${esc(fmtClock(msgMs(current)))}</span>
        </div>
        <div class="msg-body md open">${md(textOf(current))}</div>`;
    setHtmlIfChanged(cap, capHtml);
    sizeReplayCaption();
  }

  function sizeReplayCaption() {
    const cap = $('replay-caption');
    const body = cap?.querySelector('.msg-body');
    if (!body) return;
    const tab = $('tab-graph');
    const capPad = parseFloat(getComputedStyle(cap).paddingBottom) || 0;
    const tabBottom = tab ? tab.getBoundingClientRect().bottom : window.innerHeight;
    const top = body.getBoundingClientRect().top;
    cap.style.setProperty('--caption-room', `${Math.max(120, Math.floor(tabBottom - top - capPad - 16))}px`);
  }

  function replayDelay(all, idx) {
    const speed = parseFloat($('replay-speed').value) || 1;
    if ($('replay-timing').value === 'even' || idx <= 0 || idx >= all.length)
      return 1400 / speed;
    const gap = msgMs(all[idx]) - msgMs(all[idx - 1]);
    return Math.min(4000, Math.max(250, (gap || 0) / 60 / speed));
  }

  function replayStop() {
    replay.playing = false;
    clearTimeout(replay.timer);
    replay.timer = null;
  }

  function replayTick() {
    const all = graphMsgs();
    if (replay.idx === null || replay.idx >= all.length) {
      replayStop();
      renderGraph();
      return;
    }
    replay.idx += 1;
    renderGraph();
    if (replay.idx >= all.length) {
      replayStop();
      renderGraph();
      return;
    }
    replay.timer = setTimeout(replayTick, replayDelay(all, replay.idx));
  }

  function replaySeek(idx) {
    replayStop();
    replay.idx = Math.max(0, Math.min(idx, graphMsgs().length));
    renderGraph();
  }

  function replayToggle() {
    if (replay.playing) {
      replayStop();
      renderGraph();
      return;
    }
    const n = graphMsgs().length;
    if (!n) return;
    if (replay.idx === null || replay.idx >= n) replay.idx = 0;
    replay.playing = true;
    replay.timer = setTimeout(replayTick, 300);
    renderGraph();
  }

  function replayLive() {
    replayStop();
    replay.idx = null;
    renderGraph();
  }

  // ---------- graph GIF export ----------
  let exportingGif = false;
  let activeGifModalUrl = null;

  function isDownloadSandboxed() {
    if (window.self === window.top) return false;
    let cur = window;
    while (cur !== cur.parent) {
      try {
        const fe = cur.frameElement;
        if (!fe) return true;
        if (fe.hasAttribute('sandbox') && !fe.sandbox?.contains('allow-downloads')) {
          return true;
        }
        cur = cur.parent;
      } catch {
        return true;
      }
    }
    return false;
  }

  function closeGifModal() {
    const modal = $('gif-modal');
    if (modal) modal.classList.add('hidden');
    const img = $('gif-modal-img');
    if (img) img.removeAttribute('src');
    const open = $('gif-modal-open');
    if (open) open.removeAttribute('href');
    if (activeGifModalUrl) {
      try { URL.revokeObjectURL(activeGifModalUrl); } catch {}
      activeGifModalUrl = null;
    }
  }

  function showGifModal(blob, filename, title, metaText) {
    closeGifModal();
    const url = URL.createObjectURL(blob);
    activeGifModalUrl = url;
    const modal = $('gif-modal');
    if (!modal) {
      URL.revokeObjectURL(url);
      activeGifModalUrl = null;
      return false;
    }
    const titleEl = $('gif-modal-title');
    if (titleEl) titleEl.textContent = filename;
    const metaEl = $('gif-modal-meta');
    if (metaEl) metaEl.textContent = metaText;
    const img = $('gif-modal-img');
    if (img) {
      img.src = url;
      img.alt = `${title} graph replay`;
    }
    const open = $('gif-modal-open');
    if (open) open.href = url;
    modal.classList.remove('hidden');
    $('gif-modal-close')?.focus();
    return true;
  }

  function resolveGraphTheme() {
    const probe = document.createElement('span');
    probe.style.cssText = 'position:absolute;visibility:hidden;pointer-events:none';
    document.documentElement.appendChild(probe);
    const read = (expr, fallback) => {
      probe.style.color = '';
      probe.style.color = expr;
      return getComputedStyle(probe).color || fallback;
    };
    const fallbackFont = 'system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif';
    probe.style.fontFamily = `var(--sw-font, ${fallbackFont})`;
    const computedFont = getComputedStyle(probe).fontFamily;
    const font = computedFont && !computedFont.includes('var(') ? computedFont : fallbackFont;
    const theme = {
      bg: read('var(--sw-bg, #0e0e10)', '#0e0e10'),
      fg: read('var(--sw-fg, #eceef2)', '#eceef2'),
      fg2: read('var(--sw-fg-2, #c7cbd4)', '#c7cbd4'),
      muted: read('var(--sw-fg-muted, #8e95a2)', '#8e95a2'),
      faint: read('var(--sw-fg-faint, #646b78)', '#646b78'),
      fill: read('var(--sw-fill, rgba(236,238,242,0.08))', 'rgba(236,238,242,0.08)'),
      line: read('var(--sw-line, rgba(236,238,242,0.08))', 'rgba(236,238,242,0.08)'),
      track: read('color-mix(in srgb, var(--sw-fg, #eceef2) 12%, transparent)', 'rgba(236,238,242,0.12)'),
      success: read('var(--sw-success, #7aae66)', '#7aae66'),
      nodeBorder: read('color-mix(in srgb, var(--sw-fg, #eceef2) 22%, transparent)', 'rgba(236,238,242,0.22)'),
      ringStroke: read('color-mix(in srgb, var(--sw-fg, #eceef2) 30%, transparent)', 'rgba(236,238,242,0.30)'),
      bcastBorder: read('color-mix(in srgb, var(--sw-fg, #eceef2) 18%, transparent)', 'rgba(236,238,242,0.18)'),
      arrow: read('color-mix(in srgb, var(--sw-fg, #eceef2) 45%, transparent)', 'rgba(236,238,242,0.45)'),
      font,
    };
    probe.remove();
    return theme;
  }

  function roundRectPath(ctx, x, y, w, h, r) {
    const rad = Math.min(r, w / 2, h / 2);
    ctx.beginPath();
    ctx.moveTo(x + rad, y);
    ctx.arcTo(x + w, y, x + w, y + h, rad);
    ctx.arcTo(x + w, y + h, x, y + h, rad);
    ctx.arcTo(x, y + h, x, y, rad);
    ctx.arcTo(x, y, x + w, y, rad);
    ctx.closePath();
  }

  function fitCanvasText(ctx, text, maxW) {
    const s = String(text || '');
    if (maxW <= 0) return '';
    if (ctx.measureText(s).width <= maxW) return s;
    const ell = '…';
    const ellW = ctx.measureText(ell).width;
    let lo = 0, hi = s.length;
    while (lo < hi) {
      const mid = (lo + hi + 1) >>> 1;
      if (ctx.measureText(s.slice(0, mid)).width + ellW <= maxW) lo = mid;
      else hi = mid - 1;
    }
    return lo > 0 ? s.slice(0, lo) + ell : '';
  }

  function formatGifRecipients(m, roster = members()) {
    const sender = senderOf(m);
    const names = collapseRecipientGroups((m.mentions || []).filter((n) => n !== sender), roster);
    const parts = names.map((n) => `@${n}`);
    for (const c of channelMentionsOf(m)) parts.push(`@#${normChan(c)}`);
    if (m.all) parts.push('@all');
    return parts.join(' ');
  }

  function snippetOfPost(m) {
    const sender = senderOf(m);
    const hasTargets = ((m.mentions || []).some((n) => n !== sender)) || m.all;
    const bodyText = textOf(m);
    const raw = hasTargets ? stripLeadingMentions(bodyText) : String(bodyText || '');
    return raw.replace(/\s+/g, ' ').trim();
  }

  function drawGraphFrame(ctx, {
    width, height, headerH, footerH, graphOffsetY, scale,
    theme, title, g, roster, expandAll, totalPosts, memberCount, totalDurMs,
    dotT, stepLabel, isOverview,
  }) {
    ctx.save();
    ctx.scale(scale, scale);

    ctx.fillStyle = theme.bg;
    ctx.fillRect(0, 0, width, height);

    const padX = 16;
    ctx.font = `500 11px ${theme.font}`;
    ctx.textBaseline = 'middle';
    ctx.textAlign = 'right';
    ctx.fillStyle = theme.muted;
    const stepW = ctx.measureText(stepLabel).width;
    ctx.fillText(stepLabel, width - padX, headerH / 2);

    ctx.font = `600 13px ${theme.font}`;
    ctx.textAlign = 'left';
    ctx.fillStyle = theme.fg;
    const maxTitleW = Math.max(40, width - padX * 2 - stepW - 14);
    ctx.fillText(fitCanvasText(ctx, title, maxTitleW), padX, headerH / 2);

    ctx.strokeStyle = theme.line;
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(0, headerH - 0.5);
    ctx.lineTo(width, headerH - 0.5);
    ctx.stroke();

    ctx.save();
    ctx.translate(0, graphOffsetY);
    const { nodes, pos, edges, unaddressed, broadcasts, current, curTargets, max, isActive, isCur, replaying } = g;

    const countLabels = [];
    const movingDots = [];
    for (const [k, e] of edges) {
      const [from, to] = k.split('>');
      const a = pos.get(from), b = pos.get(to);
      if (!a || !b) continue;
      const n = e.direct + e.viaAll;
      const geom = edgeGeom(a, b);
      const w = 1 + (3 * n) / max;
      const cur = isCur(from, to);

      ctx.save();
      ctx.beginPath();
      ctx.moveTo(geom.start.x, geom.start.y);
      ctx.quadraticCurveTo(geom.mx, geom.my, geom.end.x, geom.end.y);
      ctx.strokeStyle = theme.fg;
      ctx.lineWidth = w;
      ctx.lineCap = 'round';
      if (!e.direct) ctx.setLineDash([4, 4]);
      ctx.globalAlpha = cur ? 0.85 : (replaying ? 0.12 : (e.direct ? 0.28 : 0.18));
      ctx.stroke();
      ctx.restore();

      const ang = Math.atan2(geom.end.y - geom.my, geom.end.x - geom.mx);
      ctx.save();
      ctx.translate(geom.end.x, geom.end.y);
      ctx.rotate(ang);
      ctx.beginPath();
      ctx.moveTo(-6, -3.04);
      ctx.lineTo(0.4, 0);
      ctx.lineTo(-6, 3.04);
      ctx.quadraticCurveTo(-4.72, 0, -6, -3.04);
      ctx.closePath();
      if (cur) {
        ctx.fillStyle = theme.fg;
        ctx.globalAlpha = 0.85;
      } else {
        ctx.fillStyle = theme.arrow;
        ctx.globalAlpha = replaying ? 0.4 : 1;
      }
      ctx.fill();
      ctx.restore();

      if (n > 1) countLabels.push({ x: geom.mx, y: geom.my, text: String(n) });
      if (cur && dotT !== null) movingDots.push(geom);
    }

    for (const n of nodes) {
      const p = pos.get(n.id);
      const status = n.m?.status || '';
      const dotX = p.x - p.w / 2 + 14;
      const labelX = status ? dotX + 9 : p.x - p.w / 2 + 12;
      const maxChars = Math.max(4, Math.floor((p.x + p.w / 2 - 10 - labelX) / 6.8));
      const label = n.id.length > maxChars ? n.id.slice(0, maxChars - 1) + '…' : n.id;
      const hot = !!current && (n.id === senderOf(current) || curTargets.has(n.id));
      const ring = unaddressed.get(n.id);
      const bcast = expandAll ? 0 : broadcasts.get(n.id) || 0;
      const killed = status === 'killed';
      const outsider = !n.m;

      ctx.save();
      if (!isActive(n.id)) ctx.globalAlpha = 0.2;

      if (ring) {
        ctx.save();
        roundRectPath(ctx, p.x - p.w / 2 - 4, p.y - p.h / 2 - 4, p.w + 8, p.h + 8, p.h / 2 + 4);
        ctx.strokeStyle = theme.ringStroke;
        ctx.lineWidth = 1;
        ctx.setLineDash([2, 2.5]);
        ctx.stroke();
        ctx.restore();
      }

      roundRectPath(ctx, p.x - p.w / 2, p.y - p.h / 2, p.w, p.h, p.h / 2);
      ctx.fillStyle = theme.bg;
      ctx.fill();
      if (hot) {
        ctx.fillStyle = theme.fill;
        ctx.fill();
      }
      ctx.save();
      ctx.strokeStyle = hot ? theme.fg : theme.nodeBorder;
      ctx.lineWidth = hot ? 1.5 : 1;
      if (outsider) ctx.setLineDash([3, 2]);
      else if (killed) ctx.setLineDash([2, 2]);
      ctx.stroke();
      ctx.restore();

      if (status) {
        ctx.beginPath();
        ctx.arc(dotX, p.y, 3.25, 0, Math.PI * 2);
        if (status === 'working') {
          ctx.fillStyle = theme.success;
          ctx.fill();
        } else if (status === 'waiting' || status === 'idle' || status === 'parked') {
          ctx.strokeStyle = theme.faint;
          ctx.lineWidth = 1.5;
          ctx.stroke();
        } else if (status === 'killed') {
          ctx.fillStyle = theme.faint;
          ctx.fill();
        }
      }

      ctx.font = `500 12px ${theme.font}`;
      ctx.textAlign = 'left';
      ctx.textBaseline = 'middle';
      ctx.fillStyle = killed ? theme.muted : theme.fg;
      ctx.fillText(label, labelX, p.y + 0.5);
      if (killed) {
        const tw = ctx.measureText(label).width;
        ctx.strokeStyle = theme.muted;
        ctx.lineWidth = 1;
        ctx.beginPath();
        ctx.moveTo(labelX, p.y + 0.5);
        ctx.lineTo(labelX + tw, p.y + 0.5);
        ctx.stroke();
      }

      if (bcast) {
        const bw = 30 + 6 * String(bcast).length;
        const bx = p.x + p.w / 2 - bw + 6;
        const by = p.y - p.h / 2 - 7;
        roundRectPath(ctx, bx, by, bw, 14, 7);
        ctx.fillStyle = theme.bg;
        ctx.fill();
        ctx.strokeStyle = theme.bcastBorder;
        ctx.lineWidth = 1;
        ctx.stroke();
        ctx.font = `600 9px ${theme.font}`;
        ctx.textAlign = 'center';
        ctx.textBaseline = 'middle';
        ctx.fillStyle = theme.muted;
        ctx.fillText(`@all ${bcast}`, bx + bw / 2, by + 7.5);
      }

      ctx.restore();
    }

    if (countLabels.length) {
      ctx.font = `500 10px ${theme.font}`;
      ctx.textAlign = 'center';
      ctx.textBaseline = 'middle';
      ctx.lineWidth = 4;
      ctx.lineJoin = 'round';
      ctx.strokeStyle = theme.bg;
      ctx.fillStyle = theme.muted;
      for (const c of countLabels) {
        ctx.strokeText(c.text, c.x, c.y + 0.5);
        ctx.fillText(c.text, c.x, c.y + 0.5);
      }
    }

    for (const geom of movingDots) {
      const t = dotT;
      const u = 1 - t;
      const dx = u * u * geom.start.x + 2 * u * t * geom.mx + t * t * geom.end.x;
      const dy = u * u * geom.start.y + 2 * u * t * geom.my + t * t * geom.end.y;
      if (t < 1) {
        ctx.save();
        ctx.beginPath();
        ctx.arc(dx, dy, 7.5, 0, Math.PI * 2);
        ctx.fillStyle = theme.fg;
        ctx.globalAlpha = 0.16;
        ctx.fill();
        ctx.restore();
      }
      ctx.beginPath();
      ctx.arc(dx, dy, 4.5, 0, Math.PI * 2);
      ctx.fillStyle = theme.fg;
      ctx.fill();
      ctx.strokeStyle = theme.bg;
      ctx.lineWidth = 2;
      ctx.stroke();
    }
    ctx.restore();

    const footY = height - footerH;
    const pct = totalPosts > 0 ? Math.min(1, g.upto / totalPosts) : (isOverview ? 1 : 0);
    ctx.fillStyle = theme.track;
    ctx.fillRect(0, footY, width, 2);
    if (pct > 0) {
      ctx.fillStyle = theme.fg2;
      ctx.fillRect(0, footY, Math.round(width * pct), 2);
    }

    const capY = footY + 2 + (footerH - 2) / 2;
    ctx.textBaseline = 'middle';
    ctx.textAlign = 'left';
    let x = padX;
    const maxRight = width - padX;
    if (current) {
      const sender = `@${senderOf(current)}`;
      ctx.font = `600 11.5px ${theme.font}`;
      ctx.fillStyle = theme.fg;
      ctx.fillText(sender, x, capY);
      x += ctx.measureText(sender).width;

      const recips = formatGifRecipients(current, roster);
      const snippet = snippetOfPost(current);
      const rest = [recips ? ` → ${recips}` : '', snippet ? ` · ${snippet}` : ''].join('');
      if (rest && x < maxRight - 10) {
        ctx.font = `400 11.5px ${theme.font}`;
        ctx.fillStyle = theme.muted;
        ctx.fillText(fitCanvasText(ctx, rest, maxRight - x), x, capY);
      }
    } else if (isOverview) {
      ctx.font = `400 11.5px ${theme.font}`;
      ctx.fillStyle = theme.muted;
      const summary = `${plural(totalPosts, 'post')} · ${plural(memberCount, 'member')}` +
        (totalDurMs > 0 ? ` · ${fmtDur(totalDurMs)}` : '');
      ctx.fillText(fitCanvasText(ctx, summary, maxRight - x), x, capY);
    } else {
      ctx.font = `400 11.5px ${theme.font}`;
      ctx.fillStyle = theme.muted;
      ctx.fillText(fitCanvasText(ctx, `Swarm start · ${plural(memberCount, 'member')}`, maxRight - x), x, capY);
    }

    ctx.restore();
  }

  function gifFilename(title) {
    const slug = String(title || 'swarm')
      .toLowerCase()
      .replace(/[^a-z0-9]+/g, '-')
      .replace(/^-+|-+$/g, '')
      .slice(0, 48);
    return `${slug || 'swarm'}-graph.gif`;
  }

  async function exportGraphGif() {
    if (exportingGif) return;
    const s = state.snap;
    if (!s || !window.SwarmGif?.createEncoder) return;

    const btn = $('graph-export-gif');
    const lbl = $('graph-export-gif-label');
    exportingGif = true;
    if (btn) btn.disabled = true;
    if (lbl) lbl.textContent = 'Exporting…';

    let popup = null;
    try {
      const expandAll = $('g-all').checked;
      const hideInactive = $('g-hide-inactive')?.checked || false;
      const speed = parseFloat($('replay-speed').value) || 1;
      const roster = (s.members || []).map((m) => ({ ...m }));
      const snap = { ...s, members: roster };
      const all = (s.messages || []).filter((m) => !isControl(m) && !isRoleWorker(senderOf(m), roster));
      const svgWidth = $('graph')?.clientWidth || 0;
      const logicalW = Math.min(720, Math.max(520, Math.round(svgWidth || 640)));
      const title = s.title || s.meta?.title || 'Untitled swarm';
      const filename = gifFilename(title);
      const theme = resolveGraphTheme();
      const graphOpts = { expandAll, hideInactive, width: logicalW };

      const MAX_REPLAY_STEPS = 200;
      const stepKs = [];
      if (all.length <= MAX_REPLAY_STEPS) {
        for (let k = 1; k <= all.length; k++) stepKs.push(k);
      } else {
        for (let i = 0; i < MAX_REPLAY_STEPS; i++) {
          const k = Math.max(1, Math.min(all.length, Math.round(1 + (i * (all.length - 1)) / (MAX_REPLAY_STEPS - 1))));
          if (stepKs[stepKs.length - 1] !== k) stepKs.push(k);
        }
      }

      const dotTs = all.length <= 25 ? [0.08, 0.30, 0.56, 0.80, 1.0] :
                    all.length <= 60 ? [0.18, 0.58, 1.0] :
                    all.length <= 150 ? [0.45, 1.0] : [1.0];

      const planAcc = createGraphAccumulator(snap, all, graphOpts);
      const plan = [];
      let maxGraphH = 0;
      const memberCount = roster.filter((m) => !isRoleWorker(m, roster)).length;

      if (all.length === 0) {
        const overview = planAcc.snapshot({ replaying: false });
        if (overview.nodes.length === 0) {
          toast('Nothing to export yet.');
          return;
        }
        maxGraphH = overview.height;
        plan.push({ k: 0, isOverview: true, dotT: null, stepLabel: 'Overview', delayMs: 1000 });
      } else {
        const startSnap = planAcc.snapshot({ replaying: true });
        maxGraphH = startSnap.height;
        plan.push({
          k: 0,
          isOverview: false,
          dotT: null,
          stepLabel: `0 / ${all.length}`,
          delayMs: Math.round(Math.min(800, Math.max(220, 600 / speed))),
        });

        for (let idx = 0; idx < stepKs.length; idx++) {
          const k = stepKs[idx];
          planAcc.advanceTo(k);
          const g = planAcc.snapshot({ replaying: true });
          if (g.height > maxGraphH) maxGraphH = g.height;
          const cur = g.current;
          const rawDelay = replayDelay(all, k);
          const postMs = Math.round(Math.min(2200, Math.max(240, rawDelay)));
          const stepLabel = `${k} / ${all.length} · #${cur.seq}`;
          let hasCurEdge = false;
          if (dotTs.length > 1) {
            for (const [key] of g.edges) {
              const [from, to] = key.split('>');
              if (g.isCur(from, to) && g.pos.has(from) && g.pos.has(to)) {
                hasCurEdge = true;
                break;
              }
            }
          }
          if (hasCurEdge) {
            const travelTotal = Math.min(Math.round(postMs * 0.58), 520);
            const subDelay = Math.max(60, Math.round(travelTotal / (dotTs.length - 1)));
            for (let i = 0; i < dotTs.length - 1; i++) {
              plan.push({ k, isOverview: false, dotT: dotTs[i], stepLabel, delayMs: subDelay });
            }
            const holdDelay = Math.max(140, postMs - subDelay * (dotTs.length - 1));
            plan.push({ k, isOverview: false, dotT: 1.0, stepLabel, delayMs: holdDelay });
          } else {
            plan.push({ k, isOverview: false, dotT: 1.0, stepLabel, delayMs: postMs });
          }
        }

        planAcc.advanceTo(all.length);
        const overview = planAcc.snapshot({ replaying: false });
        if (overview.height > maxGraphH) maxGraphH = overview.height;
        plan.push({
          k: all.length,
          isOverview: true,
          dotT: null,
          stepLabel: 'Overview',
          delayMs: Math.round(Math.min(2000, Math.max(900, 1800 / Math.min(speed, 2)))),
        });
      }

      const sandboxed = isDownloadSandboxed();
      if (sandboxed) {
        try {
          popup = window.open('', '_blank');
        } catch {
          popup = null;
        }
        if (popup) {
          try {
            const doc = popup.document;
            doc.title = `Exporting ${filename}…`;
            doc.body.style.cssText =
              `margin:0;min-height:100vh;display:grid;place-items:center;` +
              `background:${theme.bg};color:${theme.fg};font:13px/1.5 ${theme.font};`;
            doc.body.textContent = `Rendering ${filename}…`;
          } catch {
            try { popup.close(); } catch {}
            popup = null;
          }
        }
      }

      const headerH = 36;
      const footerH = 34;
      const padY = 8;
      const logicalH = Math.ceil(headerH + padY * 2 + maxGraphH + footerH);
      const scale = logicalH > 900 ? Math.max(0.65, 900 / logicalH) : 1;
      const canvasW = Math.max(1, Math.round(logicalW * scale));
      const canvasH = Math.max(1, Math.round(logicalH * scale));

      const canvas = document.createElement('canvas');
      canvas.width = canvasW;
      canvas.height = canvasH;
      const ctx = canvas.getContext('2d', { willReadFrequently: true });
      if (!ctx) throw new Error('Canvas 2D context unavailable');

      const totalDurMs = all.length > 1 ? Math.max(0, (msgMs(all[all.length - 1]) || 0) - (msgMs(all[0]) || 0)) : 0;

      const renderDescriptor = (f, g) => {
        drawGraphFrame(ctx, {
          width: logicalW,
          height: logicalH,
          headerH,
          footerH,
          graphOffsetY: headerH + padY + (maxGraphH - g.height) / 2,
          scale,
          theme,
          title,
          g,
          roster,
          expandAll,
          totalPosts: all.length,
          memberCount,
          totalDurMs,
          dotT: f.dotT,
          stepLabel: f.stepLabel,
          isOverview: f.isOverview,
        });
        return ctx.getImageData(0, 0, canvasW, canvasH).data;
      };

      const sampleIdxSet = new Set([0, plan.length - 1]);
      const movingIdx = plan.findIndex((f) => f.dotT !== null && f.dotT < 1);
      if (movingIdx >= 0) sampleIdxSet.add(movingIdx);
      const sampleCap = Math.min(plan.length, 10);
      for (let i = 1; i < sampleCap - 1; i++) {
        sampleIdxSet.add(Math.round((i * (plan.length - 1)) / (sampleCap - 1)));
      }
      const sampleIndices = [...sampleIdxSet].sort((a, b) => a - b);
      const sampleAcc = createGraphAccumulator(snap, all, graphOpts);
      const sampleFrames = [];
      for (const idx of sampleIndices) {
        const f = plan[idx];
        sampleAcc.advanceTo(f.k);
        const g = sampleAcc.snapshot({ replaying: !f.isOverview });
        sampleFrames.push(new Uint8Array(renderDescriptor(f, g)));
      }

      const enc = window.SwarmGif.createEncoder({
        width: canvasW,
        height: canvasH,
        sampleFrames,
        repeat: 0,
        useDelta: plan.length > 1,
      });

      let bytes;
      if (plan.length === 1) {
        enc.addFrame(sampleFrames[0], plan[0].delayMs);
        sampleFrames.length = 0;
        bytes = enc.finish();
      } else {
        sampleFrames.length = 0;
        const streamAcc = createGraphAccumulator(snap, all, graphOpts);
        let curK = -1;
        let curOverview = null;
        let curG = null;
        let lastYield = performance.now();

        for (let i = 0; i < plan.length; i++) {
          const f = plan[i];
          if (f.k !== curK || f.isOverview !== curOverview) {
            streamAcc.advanceTo(f.k);
            curG = streamAcc.snapshot({ replaying: !f.isOverview });
            curK = f.k;
            curOverview = f.isOverview;
          }
          const rgba = renderDescriptor(f, curG);
          enc.addFrame(rgba, f.delayMs);

          if (i + 1 < plan.length && performance.now() - lastYield >= 16) {
            if (lbl) lbl.textContent = `Exporting ${Math.round(((i + 1) / plan.length) * 100)}%`;
            await new Promise((r) => setTimeout(r, 0));
            lastYield = performance.now();
          }
        }
        bytes = enc.finish();
      }

      const blob = new Blob([bytes], { type: 'image/gif' });
      const metaSummary = `${plural(plan.length, 'frame')}, ${fmtBytes(bytes.length)}`;
      if (!sandboxed) {
        const url = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        a.download = filename;
        document.body.appendChild(a);
        a.click();
        a.remove();
        setTimeout(() => URL.revokeObjectURL(url), 10000);
        toast(`Exported ${filename} (${metaSummary})`);
      } else {
        let deliveredInPopup = false;
        if (popup && !popup.closed) {
          try {
            const doc = popup.document;
            const urlApi = popup.URL || URL;
            const popupUrl = urlApi.createObjectURL(blob);
            doc.title = filename;
            doc.body.textContent = '';
            doc.body.style.cssText =
              `margin:0;min-height:100vh;display:flex;flex-direction:column;align-items:center;` +
              `justify-content:center;gap:12px;padding:20px;box-sizing:border-box;` +
              `background:${theme.bg};color:${theme.fg};font:13px/1.5 ${theme.font};`;

            const img = doc.createElement('img');
            img.src = popupUrl;
            img.alt = `${title} graph replay`;
            img.style.cssText =
              `max-width:100%;height:auto;border-radius:8px;border:1px solid ${theme.line};display:block;`;

            const bar = doc.createElement('div');
            bar.style.cssText = 'display:flex;align-items:center;gap:12px;flex-wrap:wrap;justify-content:center;';

            const link = doc.createElement('a');
            link.href = popupUrl;
            link.download = filename;
            link.textContent = `Download ${filename}`;
            link.style.cssText =
              `display:inline-flex;align-items:center;padding:6px 12px;border-radius:6px;` +
              `background:${theme.fg};color:${theme.bg};text-decoration:none;font-weight:500;font-size:12px;`;

            const meta = doc.createElement('span');
            meta.textContent = `${plural(plan.length, 'frame')} · ${fmtBytes(bytes.length)}`;
            meta.style.cssText = `color:${theme.muted};font-size:12px;`;

            bar.append(link, meta);
            doc.body.append(img, bar);
            popup.addEventListener('pagehide', () => {
              try { urlApi.revokeObjectURL(popupUrl); } catch {}
            }, { once: true });
            if (typeof popup.setTimeout === 'function') {
              popup.setTimeout(() => link.click(), 0);
            } else {
              link.click();
            }
            deliveredInPopup = true;
            toast(`Exported ${filename} (${metaSummary})`);
          } catch {
            try { popup.close(); } catch {}
          }
        }
        if (!deliveredInPopup) {
          const metaText = `${plural(plan.length, 'frame')} · ${fmtBytes(bytes.length)}`;
          if (showGifModal(blob, filename, title, metaText)) {
            toast(`GIF ready (${metaSummary}) — right-click image to save.`);
          } else {
            toast('Could not download GIF from embedded pane.');
          }
        }
      }
    } catch (e) {
      if (popup && !popup.closed) {
        try { popup.close(); } catch {}
      }
      console.warn('[swarm_board] GIF export failed:', e);
      toast('Could not export GIF.');
    } finally {
      exportingGif = false;
      if (btn) btn.disabled = false;
      if (lbl) lbl.textContent = 'Export GIF';
    }
  }

  function fmtBytes(n) {
    if (n < 1024) return `${n} B`;
    if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
    return `${(n / 1024 / 1024).toFixed(1)} MB`;
  }

  function renderRaw() {
    const s = state.snap;
    const rawObj = s.raw && typeof s.raw === 'object' ? s.raw : {};
    const fallbackBoard = () => JSON.stringify({
      meta: s.meta || null,
      plan: s.plan || null,
      channels: s.channels || [],
      members: s.members || [],
      messages: s.messages || [],
      claims: s.claims || [],
    }, null, 2);
    const hasMeta = typeof rawObj.meta === 'string' || !!s.meta;
    const hasMembers = typeof rawObj.members === 'string' || Array.isArray(s.members);
    const hasView = typeof rawObj.view === 'string' && rawObj.view.length > 0;
    const archiveCount = Number(s.archiveCount ?? s.meta?.archiveCount ?? 0) || 0;
    const hasArchive = (typeof rawObj.archive === 'string' && rawObj.archive.length > 0) || archiveCount > 0;
    document.querySelectorAll('.raw-btn').forEach((b) => {
      const k = b.dataset.raw;
      if (k === 'meta') b.classList.toggle('hidden', !hasMeta);
      else if (k === 'members') b.classList.toggle('hidden', !hasMembers);
      else if (k === 'view') b.classList.toggle('hidden', !hasView);
      else if (k === 'archive') b.classList.toggle('hidden', !hasArchive);
    });
    if (state.rawKind === 'view' && !hasView) state.rawKind = 'board';
    if (state.rawKind === 'meta' && !hasMeta) state.rawKind = 'board';
    if (state.rawKind === 'members' && !hasMembers) state.rawKind = 'board';
    if (state.rawKind === 'archive' && !hasArchive) state.rawKind = 'board';

    let text = '';
    if (state.rawKind === 'board') {
      text = typeof rawObj.board === 'string' ? rawObj.board : fallbackBoard();
    } else if (state.rawKind === 'meta') {
      text = typeof rawObj.meta === 'string' ? rawObj.meta : JSON.stringify(s.meta || {}, null, 2);
    } else if (state.rawKind === 'members') {
      text = typeof rawObj.members === 'string' ? rawObj.members : JSON.stringify(s.members || [], null, 2);
    } else if (state.rawKind === 'view') {
      text = typeof rawObj.view === 'string' ? rawObj.view : '';
    } else if (state.rawKind === 'archive') {
      text = typeof rawObj.archive === 'string'
        ? rawObj.archive
        : (archiveCount > 0 ? `Archived messages: ${archiveCount} (stored in board_archive.jsonl)` : '');
    }
    const file = { board: 'board.json', meta: 'meta.json', members: 'members.json', view: 'view.json', archive: 'board_archive.jsonl' }[state.rawKind] || '';
    const lines = text ? text.split('\n').filter(Boolean).length : 0;
    const rawDir = String(state.rawKind === 'view' ? s.view?.dir || s.dir : (s.dir || s.meta?.workspace || '')).replace(/[\\/]+$/, '');
    const sep = rawDir.includes('\\') && !rawDir.includes('/') ? '\\' : '/';
    const path = rawDir ? `${rawDir}${sep}${file}` : file;
    setHtmlIfChanged($('raw-meta'), `<span class="path" title="${esc(path)}"><span dir="ltr">${esc(path)}</span></span>` +
        `<span class="size">· ${esc(fmtBytes(text.length))}${lines ? ` · ${plural(lines, 'line')}` : ''}</span>`);
    state.rawIsEmpty = !text;
    const shown = text || '(empty)';
    if ($('raw').textContent !== shown) $('raw').textContent = shown;
    if ($('raw-copy')) $('raw-copy').disabled = state.rawIsEmpty;
    document.querySelectorAll('.raw-btn').forEach((b) => {
      const on = b.dataset.raw === state.rawKind;
      b.classList.toggle('on', on);
      b.setAttribute('aria-pressed', String(on));
    });
  }

  // ---------- overview ----------
  const ROLE_WORKER_RE = /^(?:narrator|monitor)(?:-\d+)?$/;
  const roleOf = (m) => {
    if (!m || typeof m !== 'object') return '';
    if (m.role && m.role !== 'worker') return m.role;
    const match = String(m.name ?? '').match(/^(narrator|monitor)(?:-\d+)?$/);
    return match ? match[1] : m.role || '';
  };
  const isRoleWorker = (m, roster = members()) => {
    if (typeof m === 'string') {
      const list = Array.isArray(roster) ? roster : members();
      m = list.find((mem) => mem.name === m) || { name: m };
    }
    if (!m || typeof m !== 'object') return false;
    const role = String(m.role || '').toLowerCase();
    return Boolean(
      m.roleWorker ||
      m.narrator ||
      role === 'narrator' ||
      role === 'monitor' ||
      ROLE_WORKER_RE.test(String(m.name ?? ''))
    );
  };
  const WATCHDOG_MS = 3000;
  const FRAME_MSG_MAX = 8 * 1024;
  const ITEM_ID_RE = /^[a-z0-9-]{1,40}$/;
  const STALE_LAG = 10;
  const STALE_IDLE_MS = 60 * 1000;
  const WS_STATES = ['active', 'blocked', 'review', 'queued', 'done', 'failed'];
  const ATT_KINDS = ['decision', 'failure', 'review', 'stuck', 'quota', 'error', 'fyi'];
  const SEVERITIES = ['high', 'med', 'low'];
  const FRAME_CSP = "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; " +
      "img-src data:; font-src data:; connect-src 'none'; frame-src 'none'; object-src 'none'; " +
      "form-action 'none'; base-uri 'none'";
  const FRAME_INBOUND = new Set(['READY', 'RENDERED', 'ERROR', 'NAVIGATE', 'FEEDBACK_INTENT']);
  const THEME_TOKENS = [
    ['--background', '--sw-bg'], ['--foreground', '--sw-fg'], ['--card', '--sw-card'],
    ['--card-foreground', '--sw-fg'], ['--card-border', '--sw-card-border'], ['--border', '--sw-border'],
    ['--primary', '--sw-primary'], ['--primary-foreground', '--sw-primary-fg'],
    ['--secondary', '--sw-secondary'], ['--secondary-foreground', '--sw-fg-2'],
    ['--muted', '--sw-muted'], ['--muted-foreground', '--sw-fg-muted'],
    ['--accent', '--sw-fill'], ['--accent-foreground', '--sw-fg'], ['--error', '--sw-error'],
    ['--placeholder', '--sw-fg-faint'], ['--code-foreground', '--sw-fg'], ['--ring', '--sw-ring'],
    ['--font-family', '--sw-font'], ['--editor-font-family', '--sw-mono'],
  ];
  // Non-secret UI toggle preference only (never store auth tokens here).
  const PLAIN_KEY = 'swarm_board.narrator_plain';
  function lsGet(k) {
    try {
      return localStorage.getItem(k);
    } catch {
      return null;
    }
  }
  function lsSet(k, v) {
    try {
      if (v == null) localStorage.removeItem(k); else localStorage.setItem(k, v);
    } catch {
      // Storage can be unavailable in the host's iframe; the toggle still works for this load.
    }
  }
  const prefs = { plain: params.get('narrator') !== 'frame' && lsGet(PLAIN_KEY) !== '0' };

  const str = (v, max) => (typeof v === 'string' ? v : typeof v === 'number' ? String(v) : '').slice(0, max).trim();
  const list = (v, max) => (Array.isArray(v) ? v.slice(0, max).filter((x) => x && typeof x === 'object') : []);
  const posInt = (v) => (Number.isInteger(v) && v > 0 ? v : 0);

  function hostAllowed(host, snap) {
    const h = host.toLowerCase();
    return Array.isArray(snap.urlHosts) && snap.urlHosts.some((d) => h === d || h.endsWith(`.${d}`));
  }

  function checkAction(a, snap) {
    if (!a || typeof a !== 'object' || !snap) return null;
    const label = str(a.label, 60);
    switch (a.type) {
      case 'open_conv': {
        const m = typeof a.target === 'string' ? snap.members.find((x) => x.name === a.target) : null;
        return convIdOf(m) ? { type: 'open_conv', target: m.name, label: label || `Open ${m.name}` } : null;
      }
      case 'board_post': {
        const seq = typeof a.target === 'string' && /^\d{1,9}$/.test(a.target) ? Number(a.target) : a.target;
        return posInt(seq) && seq <= snap.head ? { type: 'board_post', target: seq, label: label || `#${seq}` } : null;
      }
      case 'url': {
        if (typeof a.target !== 'string' || a.target.length > 2048) return null;
        let u;
        try {
          u = new URL(a.target);
        } catch {
          return null;
        }
        if (u.protocol !== 'https:' || u.username || u.password || !hostAllowed(u.hostname, snap)) return null;
        return { type: 'url', target: u.href, label: label || u.hostname };
      }
      case 'file': {
        const files = snap.allowedFiles || {};
        if (typeof a.target !== 'string' || !Object.hasOwn(files, a.target)) return null;
        return { type: 'file', target: a.target, label: label || a.target.split('/').pop() };
      }
      default:
        return null;
    }
  }

  function cleanRefs(refs, snap) {
    const out = [];
    if (!Array.isArray(refs)) return out;
    for (const r of refs.slice(0, 12)) {
      if (typeof r === 'string') {
        const m = r.trim().match(/^#?(\d{1,9})$/);
        if (m) {
          const seq = Number(m[1]);
          if (posInt(seq)) out.push({ seq, unverified: seq > snap.head });
        }
        continue;
      }
      if (!r || typeof r !== 'object') continue;
      if (posInt(r.seq)) out.push({ seq: r.seq, unverified: r.seq > snap.head });
      else if (typeof r.member === 'string' && r.member) {
        const name = str(r.member, 60);
        out.push({ member: name, unverified: !snap.members.some((m) => m.name === name) });
      }
    }
    return out;
  }

  function sanitizeView(v, snap) {
    if (!v || typeof v !== 'object' || Array.isArray(v)) return null;
    const ids = new Set();
    const itemId = (x) => {
      const id = typeof x === 'string' && ITEM_ID_RE.test(x) ? x : '';
      if (id) ids.add(id);
      return id;
    };
    const actions = (as) => list(as, 6).map((a) => checkAction(a, snap)).filter(Boolean);
    const base = (it) => {
      const refs = cleanRefs(it.refs, snap);
      return { id: itemId(it.id), refs, uncited: refs.length === 0, actions: actions(it.actions) };
    };
    const rank = (arr, x) => (arr.includes(x) ? arr.indexOf(x) : arr.length);
    const attention = list(v.attention, 30).map((a) => ({
      ...base(a),
      kind: ATT_KINDS.includes(a.kind) ? a.kind : 'fyi',
      severity: SEVERITIES.includes(a.severity) ? a.severity : 'low',
      title: str(a.title, 200),
      why: str(a.why, 400),
    })).filter((a) => a.title);
    attention.sort((a, b) => rank(ATT_KINDS, a.kind) - rank(ATT_KINDS, b.kind) ||
        rank(SEVERITIES, a.severity) - rank(SEVERITIES, b.severity));
    const workstreams = list(v.workstreams, 40).map((w) => {
      const rawAssignees = Array.isArray(w.assignees) ? w.assignees : (Array.isArray(w.owners) ? w.owners : (w.assignee ? [w.assignee] : []));
      return {
        ...base(w),
        title: str(w.title || w.task, 160),
        assignees: rawAssignees.slice(0, 8).map((o) => str(o, 60)).filter(Boolean),
        state: WS_STATES.includes(w.state) ? w.state : 'queued',
        note: str(w.note || w.evidence, 300),
      };
    }).filter((w) => w.title);
    const textItems = (arr) => list(arr, 30).map((m) => ({ ...base(m), text: str(m.text || m.event || m.risk, 300) })).filter((m) => m.text);
    const phase = v.phase && typeof v.phase === 'object' ? {
      label: str(v.phase.label, 40),
      index: posInt(v.phase.index),
      total: posInt(v.phase.total),
    } : (v.now && typeof v.now === 'object' && typeof v.now.phase === 'string' ? {
      label: str(v.now.phase, 40),
      index: 1,
      total: 1,
    } : null);
    const ack = v.feedback_ack && typeof v.feedback_ack === 'object' ? {
      through_seq: posInt(v.feedback_ack.through_seq),
      text: str(v.feedback_ack.text, 300),
    } : null;
    const headlineFallback = typeof v.goal === 'string' ? v.goal : (v.goal?.title || v.now?.summary || v.now?.phase || '');
    const runningSummary = str(v.running_summary || v.runningSummary || v.summary || '', 1200);
    const threads = list(v.threads, 24).map((t) => ({
      id: itemId(t.id) || str(t.title, 48),
      title: str(t.title, 80),
      goal: str(t.goal, 200),
      now: str(t.now, 200),
      state: str(t.state, 24),
      workstreams: list(t.workstreams, 40).map((w) => str(w, 60)).filter(Boolean),
      attention: list(t.attention, 20).map((a) => str(a, 60)).filter(Boolean),
    })).filter((t) => t.title);
    const urlConv = currentConv();
    const cleanFrame = (f) => {
      if (typeof f === 'string') {
        const p = str(f, 300);
        if (!p) return null;
        const q = new URLSearchParams({ path: p, seq: '0' });
        if (urlConv) q.set('conversationId', urlConv);
        return { path: p, seq: 0, caption: '', src: `./api/frame?${q}` };
      }
      if (!f || typeof f !== 'object') return null;
      const p = str(f.path || f.file || '', 300);
      const seq = posInt(f.seq) || 0;
      const caption = str(f.caption || f.title || '', 160);
      let src = '';
      if (p) {
        const q = new URLSearchParams({ path: p, seq: String(seq) });
        if (urlConv) q.set('conversationId', urlConv);
        src = `./api/frame?${q}`;
      } else if (typeof f.src === 'string' && /^\.\/api\/frame\?/.test(f.src)) {
        src = str(f.src, 400);
      }
      if (!p && !src) return null;
      return { path: p, seq, caption, src };
    };
    const rawReelFrames = Array.isArray(v.reel?.frames) ? v.reel.frames : (Array.isArray(v.reel) ? v.reel : []);
    const reelFrames = list(rawReelFrames, 40).map(cleanFrame).filter(Boolean);
    const rawScreenshots = Array.isArray(v.screenshots) ? v.screenshots : [];
    const screenshots = list(rawScreenshots, 40).map(cleanFrame).filter(Boolean);
    return {
      ids,
      view: {
        version: posInt(v.version) || 1,
        as_of_seq: posInt(v.as_of_seq ?? v.board?.head),
        updated_at: str(v.updated_at || v.generatedAt, 40),
        headline: str(v.headline || headlineFallback, 300),
        running_summary: runningSummary,
        phase: phase?.label ? phase : null,
        attention,
        workstreams,
        threads,
        reel: reelFrames.length ? { frames: reelFrames } : null,
        screenshots: screenshots.length ? screenshots : reelFrames,
        milestones: textItems(v.milestones),
        risks: textItems(v.risks),
        feedback_ack: ack?.text ? ack : null,
      },
    };
  }

  // ----- overview state -----
  const ov = {
    model: null,
    bootstrap: null,
    bootstrapLoading: false,
    bootstrapPending: false,
  };
  let bootstrapTimer = 0;
  const narratorOf = () => state.snap?.narrator || null;
  const liveNarrator = () => !!narratorOf() && !['absent', 'killed'].includes(state.snap.narratorState);
  const viewExists = () => !!state.snap?.view?.exists;
  const rawViewExists = () => viewExists() || (typeof state.snap?.raw?.view === 'string' && state.snap.raw.view.length > 0);
  const builtinOn = () => state.snap?.builtinOverview === true;
  const hasOverview = () => builtinOn() || (liveNarrator() && !!ov.model);
  const overviewOn = () => builtinOn() || viewExists();
  const graphOn = () => state.snap?.graphTab !== false;
  const rawOn = () => state.snap?.rawTab !== false;
  const isValidConvId = (id) => typeof id === 'string' && /^[A-Za-z0-9][A-Za-z0-9_-]{2,127}$/.test(id.trim());
  const primaryConv = () => {
    const s = state.snap;
    const cid = String(s?.primaryConversationId || s?.meta?.primaryConversationId || '').trim();
    return isValidConvId(cid) ? cid : '';
  };
  const coordinatorConv = () => {
    const s = state.snap;
    const m = (s?.members || []).find((x) => x.role === 'coordinator');
    return convIdOf(m) || s?.meta?.coordinatorConversationId || '';
  };

  function autoSelectTab() {
    const on = overviewOn();
    $('tabbtn-overview').classList.toggle('hidden', !on);
    $('tabbtn-graph').classList.toggle('hidden', !graphOn());
    $('tabbtn-raw').classList.toggle('hidden', !rawOn());
    $('raw-btn-view')?.classList.toggle('hidden', !rawViewExists());
    syncTabsOverflow();
    if (!rawViewExists() && state.rawKind === 'view') {
      state.rawKind = 'board';
      state.dirtyTabs.add('raw');
    }
    if ((!on && state.tab === 'overview') || (!graphOn() && state.tab === 'graph') ||
        (!rawOn() && state.tab === 'raw')) {
      selectTab('feed');
      return;
    }
    if (state.tabChosen) return;
    const want = hasOverview() ? 'overview' : 'feed';
    if (state.tab !== want) selectTab(want);
  }

  function noticeHtml() {
    const s = state.snap;
    const n = s?.narrator;
    if (!s) return '';
    if (s.narratorState === 'absent') return 'No narrator in this swarm. Ask the coordinator to add one.';
    if (s.narratorState === 'killed') {
      return `The narrator (<b>${esc(n.name)}</b>) was stopped. Ask the coordinator to add a new one ` +
          `(<code>${esc(nextNarratorName())}</code>).`;
    }
    if (!ov.model) {
      if (s.view?.jsonError === 'too_large') return 'The narrator\'s view.json is over 64 KB, so it isn\'t shown.';
      if (s.view?.jsonError === 'invalid') return 'The narrator\'s view.json isn\'t valid JSON yet.';
      return 'The narrator hasn\'t published an overview yet.';
    }
    return '';
  }

  function nextNarratorName() {
    let max = 0;
    for (const m of members()) {
      const g = m.name.match(/^narrator(?:-(\d+))?$/);
      if (g) max = Math.max(max, g[1] ? Number(g[1]) : 1);
    }
    return `narrator-${max + 1}`;
  }

  const ago = (ms) => (Number.isFinite(ms) && ms > 0 ? `${fmtDur(Date.now() - ms)} ago` : '');
  const STATE_BADGE = {
    working: ['updating', 'The narrator is updating the overview'],
    waiting: ['watching', 'The narrator is waiting for new board posts'],
    killed: ['stopped', 'The narrator was stopped; the overview no longer updates'],
  };

  function renderOverviewBadge() {
    const s = state.snap;
    if (!s) return;
    const sys = s.systemAttention || [];
    const coord = (s.members || []).find((m) => m.role === 'coordinator' && !m.killed);
    const msgs = s.messages || [];
    const lastMsg = msgs[msgs.length - 1];
    const swarmDone = !!coord && senderOf(lastMsg) === coord.name && /^\[done\]/i.test(String(textOf(lastMsg) || '').trim());
    const coordSys = coord ? sys.filter((it) => it.member === coord.name).length : 0;
    const want = swarmDone ? 0 : coordSys + (coord?.status === 'waiting' && coord.replied && coord.replyAsk ? 1 : 0);
    const badge = $('tab-badge-overview');
    if (badge) {
      const next = want ? String(want) : '';
      if (badge.textContent !== next) badge.textContent = next;
      badge.classList.toggle('hidden', !want);
      syncTabsOverflow();
    }
  }

  function renderOverviewChrome() {
    const s = state.snap;
    if (!s || !overviewOn()) return;
    const n = s.narrator;
    const v = ov.model?.view;
    let status = '';
    if (n) {
      const [label, tip] = s.narratorState === 'paused'
        ? [`paused: ${n.reason === 'quota' ? 'quota' : 'error'}`, `The narrator's last step failed: ${n.detail}`]
        : STATE_BADGE[s.narratorState] || ['', ''];
      const read = Math.max(v?.as_of_seq || 0, n.last_read_seq || n.lastReadSeq || 0);
      const lag = Math.max(0, (s.head || 0) - read);
      const idle = Date.now() - (n.transcriptMtime || 0);
      const stale = !!v && s.narratorState !== 'working' &&
          (lag >= STALE_LAG || (lag > 0 && idle > STALE_IDLE_MS));
      const updated = ago(s.view?.jsonMtime);
      status = `<span class="ov-who" title="${esc(`${n.name} · ${convIdOf(n)}`)}">◉ ${esc(n.name)}</span>` +
          (label ? `<span class="badge ${esc(s.narratorState)}" data-testid="swarm-overview-badge" title="${esc(tip)}">${esc(label)}</span>` : '') +
          (stale ? `<span class="badge stale" data-testid="swarm-overview-stale" title="${esc(`${plural(lag, 'post')} since the view was updated`)}">stale</span>` : '') +
          '<span class="spacer"></span>' +
          `<span class="ov-fresh" data-testid="swarm-overview-freshness" title="${esc(`Narrator read up to #${read} of ${s.head || 0}`)}">` +
          `read #${read}${updated ? ` · updated ${esc(updated)}` : ''}</span>`;
    }
    setHtmlIfChanged($('ov-status'), status);
    $('ov-status').classList.toggle('hidden', !status);

    const sys = s.systemAttention || [];
    const sysHtml = sys.map((it) => {
      const since = it.kind === 'stuck' && it.since ? ` for ${fmtDur(Date.now() - it.since)}` : '';
      const memName = it.member && memberOf(it.member) ? it.member : '';
      const cid = it.conversationId || it.conversation_id || '';
      const openBtn = memName
        ? `<button type="button" class="ghost-btn sm" data-open-member="${esc(memName)}">Open</button>`
        : (canOpenConversation() && cid ? `<button type="button" class="ghost-btn sm" data-open="${esc(cid)}">Open</button>` : '');
      return `<div class="ov-sys ${esc(it.kind)}" role="listitem" data-testid="swarm-overview-system-item" title="${esc(it.detail || '')}">` +
          `<span class="ov-sys-icon" aria-hidden="true">⚠</span>` +
          `<span class="ov-sys-text"><b>system:</b> ${esc(it.text)}${esc(since)}</span>` +
          openBtn +
          `</div>`;
    }).join('');
    setHtmlIfChanged($('ov-system'), sysHtml);
    $('ov-system').classList.toggle('hidden', !sysHtml);

    const notice = noticeHtml();
    const lastGood = ov.model && s.view?.jsonError ?
      'Showing the last good view.json; the current file is ' + (s.view.jsonError === 'too_large' ? 'over 64 KB.' : 'not valid JSON.') : '';
    const noticeText = notice || lastGood;
    setHtmlIfChanged($('ov-notice'), noticeText);
    $('ov-notice').classList.toggle('hidden', !noticeText);

    const hasCoord = !!(s.members || []).find((m) => m.role === 'coordinator' || m.name === 'coordinator');
    $('ov-footer').classList.toggle('hidden', !n && !ov.model && !hasCoord);
    $('ov-open-coord').classList.toggle('hidden', !hasCoord && !coordinatorConv());
    const ack = v?.feedback_ack;
    const ackHtml = ack ? `Last change: “${esc(ack.text)}”${ack.through_seq ? ` <button type="button" class="ref" data-ov-seq="${ack.through_seq}">#${ack.through_seq}</button>` : ''}` : '';
    setHtmlIfChanged($('ov-ack'), ackHtml);
    $('ov-ack').classList.toggle('hidden', !ackHtml);

    renderOverviewBadge();
  }

  function plainReason() {
    const view = state.snap?.view;
    if (!view?.html) return view?.htmlError === 'too_large' ? 'view.html is over 64 KB' : 'no view.html yet';
    if (frame.failed && frame.failed.htmlKey === view.htmlKey) return `the narrator's view failed (${frame.failed.reason})`;
    if (!ov.bootstrap) return 'loading the narrator view';
    return '';
  }

  function renderOverview() {
    const s = state.snap;
    if (!s) return;
    ov.model = overviewOn() && s.view?.json ? sanitizeView(s.view.json, s) : null;
    renderOverviewChrome();
    const label = $('ov-label');
    if (!ov.model) {
      frameTeardown();
      label.classList.add('hidden');
      $('ov-frame').classList.add('hidden');
      $('ov-plain').classList.remove('hidden');
      $('tab-overview').classList.add('clean');
      const safeSnap = s.view ? { ...s, view: { ...s.view, json: null } } : s;
      window.SwarmHive.update(safeSnap, $('ov-plain'), { view: 'overview' });
      return;
    }
    const reason = plainReason();
    const plain = prefs.plain || !!reason;
    label.classList.remove('hidden');
    const mode = $('ov-mode');
    const canFrame = !!s.view?.html;
    mode.textContent = plain ? (frame.failed && canFrame ? 'Retry narrator\'s page' : 'Narrator\'s page') : 'Overview';
    mode.setAttribute('aria-pressed', String(plain));
    mode.classList.toggle('hidden', plain && !canFrame);
    label.querySelector('.ov-label-text').textContent = plain ?
      `overview · from the narrator's notes${reason && !prefs.plain ? ` · ${reason}` : ''}` : 'narrator\'s page · AI-generated';
    $('ov-frame').classList.toggle('hidden', plain);
    $('ov-plain').classList.toggle('hidden', !plain);
    $('tab-overview').classList.toggle('clean', plain);
    if (plain) {
      frameTeardown();
      const safeSnap = { ...s, view: { ...s.view, json: ov.model.view } };
      window.SwarmHive.update(safeSnap, $('ov-plain'), { view: 'overview' });
    } else {
      frameSync();
    }
  }

  function performAction(raw) {
    const a = checkAction(raw, state.snap);
    if (!a) {
      toast('That link isn\'t allowed here.');
      return;
    }
    if (a.type === 'open_conv') {
      const m = memberOf(a.target);
      if (m) {
        openMember(m.name);
      } else {
        toast(`Opening ${a.target}'s conversation needs the app.`);
      }
    } else if (a.type === 'board_post') {
      goToPost(a.target);
    } else if (a.type === 'url') {
      window.open(a.target, '_blank', 'noopener,noreferrer');
    } else if (a.type === 'file') {
      const uri = state.snap.allowedFiles[a.target];
      if (window.sidecar?.ui?.toggleAuxPane) window.sidecar.ui.toggleAuxPane({ tabType: 'fileView', uri });
      else toast('Opening files needs the app.');
    }
  }

  function clearFilters() {
    $('f-agent').value = '';
    $('f-mention').value = '';
    if ($('f-kind')) $('f-kind').value = '';
    if ($('f-channel')) {
      $('f-channel').value = '';
      $('f-channel').classList.remove('set');
    }
    $('f-search').value = '';
    $('f-agent').classList.remove('set');
    $('f-mention').classList.remove('set');
    $('f-kind')?.classList.remove('set');
    state.hiddenAgents.clear();
    state.channel = '';
    renderAgents();
    renderChannels();
    renderFeed();
  }

  function goToPost(seq) {
    const s = state.snap;
    if (!s || !posInt(seq)) return;
    if (!s.messages?.some((m) => m.seq === seq)) return;
    state.stick = false;
    selectTab('feed', false, true);
    let el = $('feed').querySelector(`[data-seq="${seq}"]`);
    if (!el && filtersActive()) {
      clearFilters();
      el = $('feed').querySelector(`[data-seq="${seq}"]`);
    }
    if (!el) {
      const allMsgs = visibleMessages();
      const idx = allMsgs.findIndex((m) => m.seq === seq);
      if (idx >= 0) {
        state.feedWindow = Math.max(state.feedWindow, allMsgs.length - idx + 20);
        renderFeed();
        el = $('feed').querySelector(`[data-seq="${seq}"]`);
      }
    }
    if (!el) return;
    state.stick = false;
    el.scrollIntoView({ block: 'center' });
    el.classList.remove('target');
    void el.offsetWidth;
    el.classList.add('target');
    setTimeout(() => el.classList.remove('target'), 1800);
    updateJump();
  }

  async function feedbackIntent(id) {
    if (typeof id !== 'string' || !ITEM_ID_RE.test(id) || !ov.model?.ids.has(id)) return;
    const text = `user feedback on card ${id}: `;
    const copied = await copyText(text);
    const prim = primaryConv();
    const coord = memberOf(window.SwarmHive?.leadName || 'coordinator');
    if (coord && coord.local === false) {
      toast(`${copied ? `Copied “${text.trim()}”. Paste it` : `Tell it “${text.trim()} …”`} to the coordinator on ${coord.host?.label || 'its machine'}.`);
      return;
    }
    if (canOpenConversation() && prim) openConversation(prim);
    else openAgentTab(window.SwarmHive?.leadName || 'coordinator');
    toast(copied ? `Copied “${text.trim()}”. Paste it to the coordinator and say what you want changed.` :
      `Tell the coordinator: “${text.trim()} …”`);
  }

  async function copyText(text) {
    try {
      await navigator.clipboard.writeText(text);
      return true;
    } catch {
      // Clipboard API may be blocked inside iframe.
    }
    try {
      const ta = document.createElement('textarea');
      ta.value = text;
      ta.style.cssText = 'position:fixed;top:0;left:0;opacity:0';
      document.body.appendChild(ta);
      ta.select();
      const ok = document.execCommand('copy');
      ta.remove();
      return ok;
    } catch {
      return false;
    }
  }

  let toastTimer = 0;
  function toast(msg) {
    const t = $('toast');
    t.textContent = msg;
    t.classList.remove('hidden');
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => t.classList.add('hidden'), 3500);
  }

  // ----- NarratorFrameHost: optional sandboxed frame when view.html is present -----
  const frame = {
    el: null,
    htmlKey: '',
    loads: 0,
    ready: false,
    sent: '',
    awaiting: '',
    timer: 0,
    failed: null,
    spent: false,
  };

  function themeTokens() {
    const cs = getComputedStyle(document.documentElement);
    const out = {};
    for (const [name, local] of THEME_TOKENS) {
      const v = (cs.getPropertyValue(name).trim() || cs.getPropertyValue(local).trim());
      if (v && v.length <= 200 && !/[;{}<>\\]|url\(|expression/i.test(v)) out[name] = v;
    }
    return out;
  }
  const scheme = () => (document.documentElement.dataset.theme === 'light' ? 'light' : 'dark');

  function framePayload() {
    const s = state.snap;
    return {
      view: ov.model.view,
      meta: {
        head: s.head,
        narrator: s.narrator?.name || '',
        members: (s.members || []).filter((m) => !isRoleWorker(m)).map((m) => ({ name: m.name, role: roleOf(m), status: m.status })),
      },
    };
  }

  function buildSrcdoc(html, payload) {
    const tokens = themeTokens();
    const css = `:root{${Object.entries(tokens).map(([k, v]) => `${k}:${v}`).join(';')};color-scheme:${scheme()}}` +
        'html,body{margin:0;background:var(--background);color:var(--foreground);font-family:var(--font-family,system-ui,sans-serif)}';
    const data = JSON.stringify(payload).replace(/</g, '\\u003c').replace(/\u2028/g, '\\u2028').replace(/\u2029/g, '\\u2029');
    const boot = ov.bootstrap.replace(/<\/(script)/gi, '<\\/$1');
    return `<!DOCTYPE html><html><head><meta charset="utf-8">` +
        `<meta http-equiv="Content-Security-Policy" content="${FRAME_CSP}">` +
        `<meta name="viewport" content="width=device-width, initial-scale=1">` +
        `<style>${css}</style>` +
        `<script type="application/json" id="narrator-data">${data}</script>` +
        `<script>${boot}</script>\n` + html;
  }

  function frameSync() {
    const view = state.snap.view;
    if (frame.el && frame.htmlKey === view.htmlKey) {
      postData();
      return;
    }
    frameTeardown();
    const payload = framePayload();
    const f = document.createElement('iframe');
    f.setAttribute('sandbox', 'allow-scripts');
    f.setAttribute('referrerpolicy', 'no-referrer');
    f.setAttribute('title', 'Narrator view (AI-generated)');
    f.setAttribute('data-testid', 'swarm-overview-iframe');
    f.className = 'ov-iframe';
    f.addEventListener('load', () => {
      if (frame.el !== f) return;
      frame.loads++;
      if (frame.loads === 1) {
        if (!frame.ready) arm('READY');
      } else {
        frameFail('it navigated away');
      }
    });
    f.srcdoc = buildSrcdoc(view.html, payload);
    frame.el = f;
    frame.htmlKey = view.htmlKey;
    frame.loads = 0;
    frame.ready = false;
    frame.sent = JSON.stringify(payload);
    $('ov-frame').replaceChildren(f);
  }

  function frameTeardown() {
    clearTimeout(frame.timer);
    frame.awaiting = '';
    if (frame.el) frame.el.remove();
    frame.el = null;
    frame.htmlKey = '';
    frame.ready = false;
    frame.sent = '';
  }

  function frameFail(reason) {
    if (!frame.el) return;
    frame.failed = { htmlKey: frame.htmlKey, reason };
    console.warn(`[swarm_board] narrator view failed: ${reason}`);
    frameTeardown();
    renderOverview();
  }

  function postToFrame(msg) {
    try {
      frame.el?.contentWindow?.postMessage(msg, '*');
    } catch {}
  }

  function postData() {
    if (!frame.el || !frame.ready || !ov.model) return;
    const payload = framePayload();
    const json = JSON.stringify(payload);
    if (json === frame.sent) return;
    frame.sent = json;
    postToFrame({ type: 'DATA_UPDATE', ...payload });
    arm('RENDERED');
  }

  function postTheme() {
    if (frame.el && frame.ready) postToFrame({ type: 'THEME', tokens: themeTokens(), scheme: scheme() });
  }

  const frameVisible = () => !!frame.el && state.view === 'board' && state.tab === 'overview' && !document.hidden;
  function arm(what) {
    frame.awaiting = what;
    syncWatchdog();
  }
  function syncWatchdog() {
    clearTimeout(frame.timer);
    if (!frame.awaiting || !frameVisible()) return;
    const what = frame.awaiting;
    frame.timer = setTimeout(() => frameFail(`no ${what} within ${WATCHDOG_MS / 1000}s`), WATCHDOG_MS);
  }
  function disarm(what) {
    if (frame.awaiting !== what) return;
    frame.awaiting = '';
    clearTimeout(frame.timer);
  }
  document.addEventListener('visibilitychange', syncWatchdog);

  const GATE_OVERLAP_MS = 150;
  const GATE_SETTLE_MS = 100;
  const GATE_SPAM_MS = 250;
  let parentInputAt = -Infinity;
  let lastGatedMsgAt = -Infinity;
  for (const type of ['pointerdown', 'mousedown', 'touchstart', 'keydown']) {
    document.addEventListener(type, () => { parentInputAt = performance.now(); }, { capture: true, passive: true });
  }
  function gate(onAllow, onDeny) {
    const receivedAt = performance.now();
    const spam = receivedAt - lastGatedMsgAt < GATE_SPAM_MS;
    lastGatedMsgAt = receivedAt;
    const ua = navigator.userActivation;
    const el = frame.el;
    if (spam || !ua?.isActive || frame.spent || !el || document.activeElement !== el ||
        receivedAt - parentInputAt <= GATE_OVERLAP_MS) {
      setTimeout(onDeny, 0);
      return;
    }
    frame.spent = true;
    const release = () => {
      if (navigator.userActivation.isActive) setTimeout(release, 200);
      else frame.spent = false;
    };
    setTimeout(() => {
      setTimeout(release, 200);
      const ok = parentInputAt < receivedAt && frame.el === el &&
          document.activeElement === el && navigator.userActivation.isActive;
      (ok ? onAllow : onDeny)();
    }, GATE_SETTLE_MS);
  }

  window.addEventListener('message', (e) => {
    if (!frame.el || e.source !== frame.el.contentWindow) return;
    const d = e.data;
    if (!d || typeof d !== 'object' || !FRAME_INBOUND.has(d.type)) return;
    let size = Infinity;
    try {
      size = JSON.stringify(d).length;
    } catch {
      return;
    }
    if (size > FRAME_MSG_MAX) return;
    switch (d.type) {
      case 'READY':
        if (frame.ready) return;
        frame.ready = true;
        disarm('READY');
        arm('RENDERED');
        postTheme();
        postData();
        return;
      case 'RENDERED': {
        disarm('RENDERED');
        const h = Number(d.height);
        if (Number.isFinite(h) && h > 0) frame.el.style.height = `${Math.min(20000, Math.max(120, Math.ceil(h)))}px`;
        return;
      }
      case 'ERROR':
        frameFail(`error: ${str(d.message, 500).split('\n')[0].slice(0, 160) || 'unknown'}`);
        return;
      case 'NAVIGATE': {
        const action = d.action;
        gate(() => performAction(action), () => toast('Click a link in the view to open it.'));
        return;
      }
      case 'FEEDBACK_INTENT': {
        const target = d.target_id;
        gate(() => feedbackIntent(target), () => {});
        return;
      }
    }
  });

  async function loadBootstrap() {
    if (ov.bootstrapLoading || ov.bootstrap) return;
    if (document.hidden) {
      ov.bootstrapPending = true;
      return;
    }
    ov.bootstrapLoading = true;
    ov.bootstrapPending = false;
    clearTimeout(bootstrapTimer);
    bootstrapTimer = 0;
    try {
      const f = window.sidecar?.fetch ? window.sidecar.fetch.bind(window.sidecar) : fetch;
      const res = await f('./narrator_bootstrap.js', { method: 'GET' });
      if (!res.ok) throw new Error(`narrator_bootstrap.js: ${res.status}`);
      ov.bootstrap = await res.text();
      if (state.snap) {
        if (state.tab === 'overview') renderOverview();
        else state.dirtyTabs.add('overview');
      }
    } catch (e) {
      console.warn(e);
      if (document.hidden) {
        ov.bootstrapPending = true;
      } else {
        bootstrapTimer = setTimeout(loadBootstrap, IDLE_POLL_MS);
      }
    } finally {
      ov.bootstrapLoading = false;
    }
  }
  loadBootstrap();

  // ---------- dynamic live agent history tabs ----------
  function ensureAgentData(name) {
    if (!state.agentTabData[name]) {
      state.agentTabData[name] = {
        messages: [],
        messagesVersion: 0,
        subagents: [],
        selectedSubagent: '',
        telemetry: null,
        live: null,
        hasMore: false,
        totalCount: 0,
        lastId: 0,
        limit: 100,
        loading: false,
        error: null,
        stick: true,
        hasNewWhileScrolled: false,
        reqGen: 0,
        lastCommittedSig: '',
        lastLiveSig: '',
        committedInputKey: '',
        committedDeferred: false,
        cachedCommittedToolIds: null,
        cachedLiveReasoning: null,
        cachedLiveTranscript: null,
        seenDKeys: new Set(),
        prevTab: state.tab && !state.tab.startsWith('agent:') ? state.tab : 'members',
      };
    }
    return state.agentTabData[name];
  }

  function effectiveAgentStatus(name, data, m) {
    if (m?.killed) return 'killed';
    const boardSt = m?.status ? normalizeAgentStatus(m.status, !!m?.killed) : '';
    if (boardSt === 'killed' || boardSt === 'failed') return boardSt;
    const tel = data?.telemetry || {};
    const baseSt = normalizeAgentStatus(tel.state || m?.status || 'waiting', !!m?.killed);
    if (baseSt === 'killed' || baseSt === 'failed') return baseSt;
    const subId = String(data?.selectedSubagent || '').trim();
    if (subId) {
      const subs = (Array.isArray(data?.subagents) && data.subagents.length ? data.subagents : m?.subagents) || [];
      const hit = subs.find((s) => s && String(s.sessionId || s.id || '') === subId);
      if (hit) return normalizeAgentStatus(hit.state || hit.status || 'waiting', false);
    }
    return baseSt;
  }

  function updateAgentJump(name) {
    const btn = $('agent-jump-live');
    const dot = $('agent-jump-dot');
    const af = $('agent-feed');
    if (!btn) return;
    const onAgent = !!name && state.tab === `agent:${name}` && state.view === 'board';
    const data = onAgent ? state.agentTabData[name] : null;
    const scrollable = !!(af && af.scrollHeight > af.clientHeight + 60);
    const show = Boolean(onAgent && data && !data.stick && scrollable);
    btn.classList.toggle('hidden', !show);
    if (dot) dot.classList.toggle('hidden', !(show && data?.hasNewWhileScrolled));
  }

  function renderAgentTabs() {
    const nav = document.querySelector('.tabs');
    if (!nav) return;
    nav.querySelectorAll('.tab-agent').forEach((el) => {
      const name = el.dataset.agentTab;
      if (!state.openAgentTabs.includes(name)) el.remove();
    });
    for (const name of state.openAgentTabs) {
      let btn = nav.querySelector(`.tab-agent[data-agent-tab="${CSS.escape(name)}"]`);
      const m = memberOf(name);
      const st = normalizeAgentStatus(m?.status || 'waiting', !!m?.killed);
      const tabKey = `agent:${name}`;
      const on = state.tab === tabKey;
      if (!btn) {
        btn = document.createElement('button');
        btn.type = 'button';
        btn.role = 'tab';
        btn.className = 'tab tab-agent';
        btn.dataset.tab = tabKey;
        btn.dataset.agentTab = name;
        btn.setAttribute('aria-controls', 'tab-agent');
        btn.setAttribute('data-testid', `swarm-tab-agent-${name}`);
        btn.innerHTML = `<span class="tab-agent-dot ${esc(st)}" aria-hidden="true"></span>` +
          `<span class="tab-agent-name">${esc(name)}</span>` +
          `<span role="button" tabindex="0" class="tab-close" data-close-agent="${esc(name)}" aria-label="Close ${esc(name)} tab" title="Close">×</span>`;
        nav.appendChild(btn);
      } else {
        const dot = btn.querySelector('.tab-agent-dot');
        if (dot) dot.className = `tab-agent-dot ${st}`;
      }
      btn.classList.toggle('active', on);
      btn.setAttribute('aria-selected', String(on));
      btn.tabIndex = on ? 0 : -1;
    }
    syncTabsOverflow();
  }

  function stopAgentPoll() {
    clearTimeout(state.agentPollTimer);
    state.agentPollTimer = 0;
    if (state.agentPollAbort) {
      state.agentPollAbort.abort();
      state.agentPollAbort = null;
    }
  }

  function scheduleAgentPoll(name) {
    stopAgentPoll();
    if (state.tab !== `agent:${name}` || !state.openAgentTabs.includes(name) || document.hidden) return;
    const m = memberOf(name);
    const data = state.agentTabData[name];
    const st = effectiveAgentStatus(name, data, m);
    const intervalMs = st === 'working' ? 750 : 3500;
    state.agentPollTimer = setTimeout(() => {
      if (state.tab === `agent:${name}` && state.openAgentTabs.includes(name)) {
        fetchAgentHistory(name, { incremental: true });
      }
    }, intervalMs);
  }

  async function fetchAgentHistory(name, { incremental = false, older = false } = {}) {
    const conv = currentConv();
    if (!conv || !name) return;
    const data = ensureAgentData(name);
    const reqSub = String(data.selectedSubagent || '');
    const myGen = ++data.reqGen;
    const af = $('agent-feed');
    let olderRestore = null;
    let beforeId = 0;
    if (older) {
      if (af) olderRestore = { prevTop: af.scrollTop, prevHeight: af.scrollHeight };
      if (data.messages.length) {
        let minId = Infinity;
        for (const x of data.messages) {
          const nid = Number(x?.id);
          if (Number.isFinite(nid) && nid > 0 && nid < minId) minId = nid;
        }
        if (Number.isFinite(minId) && minId > 1) beforeId = minId;
      }
      if (!beforeId) data.limit = Math.min(300, (data.limit || 100) + 100);
      incremental = false;
    }
    if (state.agentPollAbort) state.agentPollAbort.abort();
    const ac = new AbortController();
    state.agentPollAbort = ac;
    if (!data.messages.length) data.loading = true;
    data.error = null;
    if (state.tab === `agent:${name}` && !data.messages.length) renderAgentPane(name);

    const qs = new URLSearchParams({
      conversationId: conv,
      member: name,
      limit: String(beforeId > 0 ? 100 : (data.limit || 100)),
    });
    if (beforeId > 0) qs.set('before_id', String(beforeId));
    else if (incremental && data.lastId > 0) qs.set('after_id', String(data.lastId));
    if (reqSub) qs.set('subagent_session_id', reqSub);

    try {
      const res = await getJson(`./api/member/history?${qs.toString()}`, { signal: ac.signal, timeoutMs: 8000 });
      if (ac.signal.aborted || myGen !== data.reqGen || String(data.selectedSubagent || '') !== reqSub || currentConv() !== conv) {
        return;
      }
      data.loading = false;
      const incoming = Array.isArray(res.messages) ? res.messages : [];
      let addedNew = false;
      if ((incremental && data.lastId > 0) || beforeId > 0) {
        if (incoming.length) {
          const seen = new Set(data.messages.map((x) => x.id));
          for (const msg of incoming) {
            if (msg && !seen.has(msg.id)) {
              seen.add(msg.id);
              data.messages.push(msg);
              addedNew = true;
            }
          }
          if (addedNew) {
            data.messages.sort((a, b) => (Number(a.id) || 0) - (Number(b.id) || 0));
            data.messagesVersion = (data.messagesVersion || 0) + 1;
          }
        }
      } else {
        const seen = new Set();
        const deduped = [];
        for (const msg of incoming) {
          if (msg && !seen.has(msg.id)) {
            seen.add(msg.id);
            deduped.push(msg);
          }
        }
        data.messages = deduped;
        data.messagesVersion = (data.messagesVersion || 0) + 1;
      }
      if (typeof res.hasMore === 'boolean') data.hasMore = res.hasMore;
      if (typeof res.totalCount === 'number') data.totalCount = res.totalCount;
      let maxMsgId = 0;
      for (const x of data.messages) {
        const nid = Number(x?.id) || 0;
        if (nid > maxMsgId) maxMsgId = nid;
      }
      const resLastId = (typeof res.lastId === 'number' && res.lastId > 0) ? res.lastId : 0;
      if (incremental || beforeId > 0) {
        data.lastId = Math.max(data.lastId || 0, resLastId, maxMsgId);
      } else {
        data.lastId = resLastId || maxMsgId;
      }
      if (Array.isArray(res.subagents)) data.subagents = res.subagents;
      const m = memberOf(name);
      data.telemetry = {
        tokens: res.tokens || null,
        tps: res.tps ?? null,
        tpsEstimated: !!res.tpsEstimated,
        currentTool: res.currentTool ?? null,
        state: normalizeAgentStatus(res.state || '', !!m?.killed),
        model: res.model || '',
        workspace: res.workspace || res.worktreePath || res.workdir || '',
      };
      const nextLive = (!m?.killed && !reqSub && res.live && typeof res.live === 'object') ? res.live : null;
      const prevLiveSig = JSON.stringify(data.live || null);
      const nextLiveSig = JSON.stringify(nextLive);
      data.live = nextLive;
      if (!data.stick && (addedNew || (nextLive && prevLiveSig !== nextLiveSig))) {
        data.hasNewWhileScrolled = true;
      }
      if (state.tab === `agent:${name}`) renderAgentPane(name, { olderRestore });
    } catch (err) {
      if (ac.signal.aborted || myGen !== data.reqGen || String(data.selectedSubagent || '') !== reqSub || currentConv() !== conv) {
        return;
      }
      data.loading = false;
      data.error = err?.message || 'Failed to load history';
      if (state.tab === `agent:${name}`) renderAgentPane(name);
    } finally {
      if (state.agentPollAbort === ac) state.agentPollAbort = null;
      if (myGen === data.reqGen && state.tab === `agent:${name}` && currentConv() === conv) {
        scheduleAgentPoll(name);
      }
    }
  }

  function openAgentTab(name, subSessionId = '') {
    if (!name) return;
    const data = ensureAgentData(name);
    if (!state.tab.startsWith('agent:')) data.prevTab = state.tab;
    if (subSessionId) {
      const subStr = String(subSessionId);
      if (data.selectedSubagent !== subStr) {
        data.selectedSubagent = subStr;
        data.messages = [];
        data.lastId = 0;
        data.live = null;
        data.lastCommittedSig = '';
        data.lastLiveSig = '';
        data.seenDKeys?.clear();
        data.stick = true;
        data.hasNewWhileScrolled = false;
      }
    }
    if (!state.openAgentTabs.includes(name)) state.openAgentTabs.push(name);
    renderAgentTabs();
    selectTab(`agent:${name}`, false, true);
    document.querySelector(`.tabs .tab-agent[data-agent-tab="${CSS.escape(name)}"]`)?.scrollIntoView({ block: 'nearest', inline: 'nearest' });
  }

  function closeAgentTab(name) {
    const idx = state.openAgentTabs.indexOf(name);
    if (idx === -1) return;
    const data = state.agentTabData[name];
    state.openAgentTabs.splice(idx, 1);
    delete state.agentTabData[name];
    const wasActive = state.tab === `agent:${name}`;
    if (wasActive) stopAgentPoll();
    renderAgentTabs();
    if (wasActive) {
      const fallback = (data?.prevTab && TABS.includes(data.prevTab)) ? data.prevTab : 'members';
      selectTab(fallback, false, true);
    }
  }

  function parseToolArgsPreview(rawArgs) {
    if (rawArgs == null) return '';
    let obj = rawArgs;
    if (typeof rawArgs === 'string') {
      const s = rawArgs.trim();
      if (!s) return '';
      if (s.length > 200000) return s.slice(0, 90);
      try { obj = JSON.parse(s); } catch { return s.slice(0, 90); }
    }
    if (obj && typeof obj === 'object' && !Array.isArray(obj)) {
      const keys = Object.keys(obj);
      if (!keys.length) return '';
      const prefKey = keys.find((k) => /^(?:path|file_path|filePath|TargetFile|command|cmd|query|pattern|url|name)$/i.test(k)) || keys[0];
      const firstVal = obj[prefKey];
      const valStr = typeof firstVal === 'string' ? firstVal : JSON.stringify(firstVal);
      const preview = `${prefKey}=${valStr}`;
      return preview.length > 100 ? `${preview.slice(0, 97)}...` : preview;
    }
    const strVal = JSON.stringify(obj);
    return strVal.length > 100 ? `${strVal.slice(0, 97)}...` : strVal;
  }

  const CAP_DETAIL_TEXT = 16000;
  const CAP_DETAIL_LINES = 400;
  function formatJsonOrText(val) {
    if (val == null) return '';
    let out = '';
    let origLen = 0;
    if (typeof val === 'string') {
      origLen = val.length;
      if (origLen > CAP_DETAIL_TEXT) {
        out = val.slice(0, CAP_DETAIL_TEXT);
      } else {
        const t = val.trim();
        if ((t.startsWith('{') && t.endsWith('}')) || (t.startsWith('[') && t.endsWith(']'))) {
          try {
            out = JSON.stringify(JSON.parse(t), null, 2);
          } catch {
            out = val;
          }
        } else {
          out = val;
        }
      }
    } else {
      try {
        out = JSON.stringify(val, null, 2);
      } catch {
        out = String(val);
      }
      origLen = out.length;
    }
    let truncated = origLen > CAP_DETAIL_TEXT && out.length >= CAP_DETAIL_TEXT;
    if (out.length > CAP_DETAIL_TEXT) {
      out = out.slice(0, CAP_DETAIL_TEXT);
      truncated = true;
    }
    const lines = out.split('\n');
    if (lines.length > CAP_DETAIL_LINES) {
      out = lines.slice(0, CAP_DETAIL_LINES).join('\n');
      truncated = true;
    }
    return truncated ? `${out}\n… (truncated, ${fmtBytes(origLen)} total)` : out;
  }

  const TOOL_HUMAN_LABELS = {
    read_file: 'Read',
    write_file: 'Write',
    write_to_file: 'Write',
    create_file: 'Create file',
    edit_file: 'Edit',
    patch: 'Patch',
    patch_file: 'Patch',
    apply_patch: 'Apply patch',
    replace_file_content: 'Edit',
    multi_replace_file_content: 'Edit',
    str_replace_editor: 'Edit',
    delete_file: 'Delete file',
    terminal: 'Run terminal',
    bash: 'Run terminal',
    run_command: 'Run terminal',
    exec: 'Run terminal',
    search_files: 'Search',
    grep: 'Search',
    grep_search: 'Search',
    ripgrep: 'Search',
    find_files: 'Find files',
    find_by_name: 'Find files',
    list_dir: 'List directory',
    ls: 'List directory',
    post_message: 'Post to board',
    read_board: 'Read board',
    swarm_wait: 'Wait on board',
    delegate_task: 'Delegate subagent',
    web_search: 'Web search',
    search_web: 'Web search',
    web_fetch: 'Fetch URL',
    fetch_url: 'Fetch URL',
  };
  function humanToolLabel(tcName) {
    const key = String(tcName || '').trim().toLowerCase();
    return TOOL_HUMAN_LABELS[key] || '';
  }

  function contentToText(rawContent) {
    if (rawContent == null) return '';
    if (typeof rawContent === 'string') return rawContent;
    if (Array.isArray(rawContent)) {
      return rawContent.map((p) => {
        if (typeof p === 'string') return p;
        if (p && typeof p === 'object') return String(p.text ?? p.content ?? '');
        return '';
      }).filter(Boolean).join('\n');
    }
    if (typeof rawContent === 'object') {
      return String(rawContent.text ?? rawContent.content ?? '');
    }
    return String(rawContent);
  }

  function unwrapUserPrompt(rawContent) {
    const text = contentToText(rawContent).replace(/\r\n?/g, '\n');
    const sysStart = text.match(/^\s*\[SYSTEM\s*\/\s*ROLE\]\s*\n([\s\S]*)$/i);
    if (sysStart) {
      const rest = sysStart[1];
      const splitLast = rest.match(/^([\s\S]*)\n+\[TASK\]\s*(?:\n([\s\S]*))?$/i);
      if (splitLast) {
        const systemBrief = splitLast[1].trim();
        const taskText = String(splitLast[2] || '').trim();
        return { systemBrief, taskText: taskText || '(See System / Role brief)' };
      }
      const leadTask = rest.match(/^\[TASK\]\s*(?:\n([\s\S]*))?$/i);
      if (leadTask) {
        const taskText = String(leadTask[1] || '').trim();
        return { systemBrief: '', taskText: taskText || '(No task text)' };
      }
    }
    const taskOnly = text.match(/^\s*\[TASK\]\s*(?:\n([\s\S]*))?$/i);
    if (taskOnly) {
      const taskText = String(taskOnly[1] || '').trim();
      return { systemBrief: '', taskText: taskText || '(No task text)' };
    }
    return { systemBrief: '', taskText: text };
  }

  function resolveAllowedFileUri(rawPath) {
    if (!rawPath || typeof rawPath !== 'string') return null;
    if (rawPath.includes('\0') || rawPath.includes('%') || rawPath.startsWith('\\\\')) return null;
    const norm = rawPath.trim().replace(/\\/g, '/').replace(/\/+$/, '');
    if (!norm || norm.split('/').includes('..')) return null;
    const allowed = state.snap?.allowedFiles;
    if (!allowed || typeof allowed !== 'object') return null;
    const isSafeUri = (u) => typeof u === 'string' && /^file:\/\/\//i.test(u) && !u.includes('..') && !/%2e/i.test(u);
    const trimUriSlash = (u) => (u.length > 8 && u.endsWith('/') ? u.slice(0, -1) : u);
    const normWin = (p) => String(p || '').replace(/\\/g, '/').replace(/\/+$/, '').replace(/^[A-Z]:/, (d) => d.toLowerCase());

    if (Object.hasOwn(allowed, rawPath) && isSafeUri(allowed[rawPath])) return allowed[rawPath];
    if (Object.hasOwn(allowed, norm) && isSafeUri(allowed[norm])) return allowed[norm];

    const allowedEntries = Object.entries(allowed)
      .filter(([, u]) => isSafeUri(u))
      .map(([k, u]) => ({ keyNorm: normWin(k), uri: trimUriSlash(u) }));

    const candidateRoots = [
      state.snap?.workspace,
      state.snap?.meta?.workspace,
      state.snap?.dir,
      state.snap?.view?.dir,
    ].map((r) => String(r || '').trim()).filter(Boolean);

    const authorizedRoots = [];
    for (const r of candidateRoots) {
      const nr = normWin(r);
      if (!nr) continue;
      const hit = allowedEntries.find((e) => e.keyNorm === nr);
      if (hit && !authorizedRoots.some((x) => x.normRoot === nr)) {
        authorizedRoots.push({ normRoot: nr, uriRoot: hit.uri });
      }
    }
    const primaryRoot = authorizedRoots[0] || null;

    const encodeRelUri = (uriRoot, rel) => {
      const parts = rel.split('/').filter(Boolean);
      if (!parts.length || parts.some((p) => p === '..' || p === '.')) return null;
      const encoded = parts.map((p) => encodeURIComponent(p)).join('/');
      return `${uriRoot}/${encoded}`;
    };

    const isAbs = /^(?:\/|[a-zA-Z]:\/)/.test(norm);
    if (!isAbs) {
      const rel = norm.replace(/^\.\//, '');
      if (!rel) return null;
      if (Object.hasOwn(allowed, rel) && isSafeUri(allowed[rel])) return allowed[rel];
      if (primaryRoot) {
        const built = encodeRelUri(primaryRoot.uriRoot, rel);
        return built && isSafeUri(built) ? built : null;
      }
      return null;
    }

    const nNorm = normWin(norm);
    for (const e of allowedEntries) {
      if (e.keyNorm === nNorm) return e.uri;
    }
    for (const root of authorizedRoots) {
      if (nNorm === root.normRoot) return root.uriRoot;
      if (nNorm.startsWith(`${root.normRoot}/`)) {
        const rel = norm.slice(root.normRoot.length + 1);
        const built = encodeRelUri(root.uriRoot, rel);
        return built && isSafeUri(built) ? built : null;
      }
    }
    return null;
  }

  const FILE_MUTATE_RE = /^(?:edit_file|write_file|write_to_file|create_file|patch|patch_file|apply_patch|str_replace_editor|replace_file_content|multi_replace_file_content|delete_file)$/i;
  function countLines(strVal) {
    if (typeof strVal !== 'string' || !strVal.length) return 0;
    const norm = strVal.replace(/\r\n?/g, '\n');
    const lines = norm.split('\n');
    return norm.endsWith('\n') ? Math.max(1, lines.length - 1) : lines.length;
  }
  const normSlashPath = (p) => String(p || '').trim().replace(/\\/g, '/').replace(/\/{2,}/g, '/').replace(/\/+$/, '');
  const cmpPathKey = (p) => normSlashPath(p).replace(/^[A-Z]:/, (d) => d.toLowerCase());
  function workspaceRootsFor(name, data) {
    const m = memberOf(name);
    const primary = [
      m?.worktreePath,
      m?.workspace,
      m?.workdir,
      data?.telemetry?.workspace,
      state.snap?.workspace,
      state.snap?.meta?.workspace,
    ].map((r) => normSlashPath(r)).filter(Boolean);
    const list = primary.length
      ? primary
      : [state.snap?.dir, state.snap?.view?.dir].map((r) => normSlashPath(r)).filter(Boolean);
    const seen = new Set();
    const out = [];
    for (const r of list) {
      const cmp = cmpPathKey(r).toLowerCase();
      if (!seen.has(cmp)) {
        seen.add(cmp);
        out.push({ raw: r, cmp, isWin: /^[a-z]:\//i.test(r) });
      }
    }
    return out;
  }
  function normalizeFileForTurn(rawPath, roots) {
    if (!rawPath || typeof rawPath !== 'string') return null;
    if (rawPath.includes('\0') || rawPath.includes('%') || rawPath.startsWith('\\\\')) return null;
    const norm = normSlashPath(rawPath);
    if (!norm) return null;
    const segs = norm.split('/').filter(Boolean);
    if (!segs.length || segs.includes('..')) return null;
    const isAbs = /^(?:\/|[a-zA-Z]:\/)/.test(norm);
    const normCmp = cmpPathKey(norm);
    const normCmpLower = normCmp.toLowerCase();
    const isScratch = /(?:^|\/)(?:appdata\/local\/)?\.?hermes\/(?:cache|scratch)(?:\/|$)/i.test(normCmpLower);
    if (isScratch && !roots.some((r) => normCmpLower === r.cmp || normCmpLower.startsWith(`${r.cmp}/`))) {
      return null;
    }
    if (isAbs) {
      for (const r of roots) {
        const hit = r.isWin
          ? (normCmpLower === r.cmp || normCmpLower.startsWith(`${r.cmp}/`))
          : (normCmp === r.raw || normCmp.startsWith(`${r.raw}/`));
        if (hit) {
          const rel = norm.slice(r.raw.length).replace(/^\/+/, '');
          if (!rel) return null;
          return { key: r.isWin ? rel.toLowerCase() : rel, path: rel };
        }
      }
      if (roots.length > 0) return null;
      return { key: normCmpLower, path: norm };
    }
    const rel = norm.replace(/^\.\//, '');
    if (!rel) return null;
    const winWorkspace = roots.some((r) => r.isWin) || rawPath.includes('\\');
    return { key: winWorkspace ? rel.toLowerCase() : rel, path: rel };
  }
  function extractToolFileChange(tcName, tcArgs, resMsg) {
    if (!FILE_MUTATE_RE.test(String(tcName || '').trim())) return null;
    let obj = tcArgs;
    if (typeof tcArgs === 'string') {
      const s = tcArgs.trim();
      if (!s || s.length > 200000) return null;
      try { obj = JSON.parse(s); } catch { return null; }
    }
    if (!obj || typeof obj !== 'object' || Array.isArray(obj)) return null;
    const rawPath = obj.path || obj.file_path || obj.filePath || obj.TargetFile || obj.file || obj.filename || obj.target;
    if (typeof rawPath !== 'string' || !rawPath.trim()) return null;
    let added = 0;
    let deleted = 0;
    if (Array.isArray(obj.ReplacementChunks)) {
      for (const ch of obj.ReplacementChunks) {
        if (!ch || typeof ch !== 'object') continue;
        deleted += countLines(ch.TargetContent ?? ch.old_string ?? '');
        added += countLines(ch.ReplacementContent ?? ch.new_string ?? '');
      }
    } else if (obj.old_string != null || obj.new_string != null || obj.TargetContent != null || obj.ReplacementContent != null) {
      deleted = countLines(String(obj.old_string ?? obj.TargetContent ?? ''));
      added = countLines(String(obj.new_string ?? obj.ReplacementContent ?? ''));
    } else if (typeof obj.content === 'string' || typeof obj.CodeContent === 'string') {
      added = countLines(String(obj.content ?? obj.CodeContent ?? ''));
    } else if (typeof obj.patch === 'string' || typeof obj.diff === 'string') {
      const lines = String(obj.patch ?? obj.diff).replace(/\r\n?/g, '\n').split('\n');
      for (const l of lines) {
        if (l.startsWith('+') && !l.startsWith('+++')) added++;
        else if (l.startsWith('-') && !l.startsWith('---')) deleted++;
      }
    }
    if (!added && !deleted && resMsg?.content && typeof resMsg.content === 'string' && resMsg.content.length < 4096) {
      try {
        const rObj = JSON.parse(resMsg.content);
        if (rObj && typeof rObj === 'object') {
          added = Math.max(0, Number(rObj.added ?? rObj.additions) || 0);
          deleted = Math.max(0, Number(rObj.deleted ?? rObj.deletions ?? rObj.removed) || 0);
        }
      } catch { /* ignore */ }
    }
    return { path: rawPath.trim(), added, deleted };
  }

  function formatWorkedLabel(startMs, endMs) {
    if (Number.isFinite(startMs) && Number.isFinite(endMs)) {
      const dur = Math.max(0, endMs - startMs);
      if (dur < 1000) return 'Worked for <1s';
      return `Worked for ${fmtDur(dur)}`;
    }
    return 'Worked';
  }

  function renderToolCardHtml(name, dKey, tcName, tcArgs, resMsg, { st = 'waiting', durationMs = NaN } = {}) {
    const isOpen = state.agentDetailsOpen.has(dKey);
    const argPrev = parseToolArgsPreview(tcArgs);
    const outStr = resMsg ? formatJsonOrText(resMsg.content ?? resMsg.result ?? resMsg.output ?? '') : '';
    const isErr = Boolean(resMsg && (/^error[:\s]/i.test(outStr.trim()) || resMsg.status === 'error' || resMsg.isError));
    const stLabel = resMsg ? (isErr ? 'error' : 'ok') : (st === 'working' ? 'running' : 'stopped');
    const stCls = resMsg ? (isErr ? 'error' : 'ok') : (st === 'working' ? 'running' : '');
    const human = humanToolLabel(tcName);
    const durStr = Number.isFinite(durationMs) && durationMs >= 0 ? (durationMs < 1000 ? `${Math.round(durationMs)}ms` : fmtDur(durationMs)) : '';
    return `<details class="ag-tool-card" data-ag-dkey="${esc(dKey)}"${isOpen ? ' open' : ''}>
      <summary>
        ${human ? `<span class="ag-tool-label">${esc(human)}</span>` : ''}
        <span class="ag-tool-name">${esc(tcName)}</span>
        ${argPrev ? `<span class="ag-tool-prev">${esc(argPrev)}</span>` : '<span class="ag-tool-prev"></span>'}
        ${durStr ? `<span class="ag-tool-dur">${esc(durStr)}</span>` : ''}
        <span class="ag-tool-status ${stCls}">${stLabel}</span>
      </summary>
      <div class="ag-tool-detail">
        ${tcArgs ? `<div><div class="ag-tool-sec-lbl">Arguments</div><pre class="ag-tool-pre">${esc(formatJsonOrText(tcArgs))}</pre></div>` : ''}
        ${resMsg ? `<div><div class="ag-tool-sec-lbl">Result</div><pre class="ag-tool-pre">${esc(outStr || '(empty)')}</pre></div>` : ''}
      </div>
    </details>`;
  }

  function groupAgentTimeline(messages, { name, st, data = null }) {
    const toolResultsById = new Map();
    let lastUserIdx = -1;
    for (let idx = 0; idx < messages.length; idx++) {
      const msg = messages[idx];
      if (!msg) continue;
      if (msg.role === 'user') lastUserIdx = idx;
      if (msg.role === 'tool' && msg.toolCallId) {
        toolResultsById.set(msg.toolCallId, msg);
      }
    }
    const subKey = data?.selectedSubagent || 'main';
    const liveWgKey = `live-wg:${name}:${subKey}`;
    const liveThinkKey = `live-think:${name}:${subKey}`;
    const wsRoots = workspaceRootsFor(name, data);
    const pairedToolIds = new Set();
    const committedToolCallIds = new Set();
    const timeline = [];
    let curWork = null;
    let turnFiles = new Map();
    let turnKey = '0';
    let prevEndMs = NaN;

    const recordFileChange = (fc) => {
      if (!fc) return;
      const scoped = normalizeFileForTurn(fc.path, wsRoots);
      if (!scoped) return;
      const prev = turnFiles.get(scoped.key) || { path: scoped.path, added: 0, deleted: 0 };
      prev.added += fc.added;
      prev.deleted += fc.deleted;
      turnFiles.set(scoped.key, prev);
    };

    const flushWork = () => {
      if (!curWork) return;
      const dKey = `wg:${name}:${curWork.firstId}`;
      if (curWork.inCurrentTurn && data?.seenDKeys && !data.seenDKeys.has(dKey)) {
        if (state.agentDetailsOpen.has(liveWgKey)) state.agentDetailsOpen.add(dKey);
        data.seenDKeys.add(dKey);
      }
      const isOpen = state.agentDetailsOpen.has(dKey);
      const startMs = (curWork.tools === 0 && Number.isFinite(curWork.minMs) && Number.isFinite(curWork.maxMs)
        && curWork.minMs === curWork.maxMs && Number.isFinite(curWork.prevEndMs) && curWork.prevEndMs < curWork.maxMs)
        ? curWork.prevEndMs
        : curWork.minMs;
      const label = formatWorkedLabel(startMs, curWork.maxMs);
      const tip = `${plural(curWork.steps, 'step')}${curWork.tools ? ` · ${plural(curWork.tools, 'tool call')}` : ''}`;
      const html = `<details class="ag-work-group" data-ag-dkey="${esc(dKey)}"${isOpen ? ' open' : ''}>
        <summary title="${esc(tip)}"><span>${esc(label)}</span><span class="ag-work-chev" aria-hidden="true"></span></summary>
        <div class="ag-work-rail">${curWork.parts.join('')}</div>
      </details>`;
      const sig = `${curWork.parts.length}|${startMs}|${curWork.maxMs}|${curWork.parts.join('\0').length}|${curWork.lastSig}`;
      timeline.push({ key: `wg:${curWork.firstId}`, sig, html });
      curWork = null;
    };

    const flushFiles = () => {
      if (!turnFiles.size) return;
      const list = [...turnFiles.values()];
      const totalAdd = list.reduce((a, f) => a + f.added, 0);
      const totalDel = list.reduce((a, f) => a + f.deleted, 0);
      const dKey = `files:${name}:${turnKey}`;
      const isOpen = state.agentDetailsOpen.has(dKey);
      const rows = list.map((f) => {
        const uri = resolveAllowedFileUri(f.path);
        const shortName = f.path.replace(/\\/g, '/').split('/').slice(-2).join('/') || f.path;
        const pathEl = uri
          ? `<button type="button" class="ag-fc-path ag-fc-open" data-ag-file="${esc(f.path)}" title="Open ${esc(f.path)}">${esc(shortName)}</button>`
          : `<span class="ag-fc-path" title="${esc(f.path)}">${esc(shortName)}</span>`;
        return `<li class="ag-fc-row">
          ${pathEl}
          ${f.added > 0 ? `<span class="ag-fc-add">+${f.added}</span>` : ''}
          ${f.deleted > 0 ? `<span class="ag-fc-del">-${f.deleted}</span>` : ''}
        </li>`;
      }).join('');
      const html = `<details class="ag-files-changed" data-ag-dkey="${esc(dKey)}" data-testid="ag-files-changed"${isOpen ? ' open' : ''}>
        <summary>
          <span class="ag-fc-count">${list.length} ${list.length === 1 ? 'file' : 'files'} changed</span>
          ${totalAdd > 0 ? `<span class="ag-fc-add">+${totalAdd}</span>` : ''}
          ${totalDel > 0 ? `<span class="ag-fc-del">-${totalDel}</span>` : ''}
          <span class="ag-fc-chev" aria-hidden="true"></span>
        </summary>
        <ul class="ag-fc-list">${rows}</ul>
      </details>`;
      const sig = list.map((f) => `${f.path}:${f.added}:${f.deleted}`).join('|');
      timeline.push({ key: `fc:${turnKey}`, sig, html });
      turnFiles = new Map();
    };

    const ensureWork = (id, tsMs, inCurrentTurn) => {
      if (!curWork) {
        curWork = { firstId: id, minMs: tsMs, maxMs: tsMs, prevEndMs, inCurrentTurn, steps: 0, tools: 0, parts: [], lastSig: '' };
      } else {
        if (inCurrentTurn) curWork.inCurrentTurn = true;
        if (Number.isFinite(tsMs)) {
          curWork.minMs = Number.isFinite(curWork.minMs) ? Math.min(curWork.minMs, tsMs) : tsMs;
          curWork.maxMs = Number.isFinite(curWork.maxMs) ? Math.max(curWork.maxMs, tsMs) : tsMs;
        }
      }
      return curWork;
    };

    for (let idx = 0; idx < messages.length; idx++) {
      const msg = messages[idx];
      if (!msg) continue;
      const inCurrentTurn = idx > lastUserIdx;
      if (msg.compacted) {
        flushWork();
        const cmpHtml = '<div class="ag-compacted" role="separator"><span>Context compacted</span></div>';
        timeline.push({ key: `cmp:${msg.id}`, sig: '1', html: cmpHtml });
      }
      const tsMs = normalizeTsMs(msg.timestampMs ?? msg.timestamp ?? msg.ts);
      const tsStr = Number.isFinite(tsMs) ? fmtClock(tsMs) : '';
      const tsTitle = Number.isFinite(tsMs) ? fmtFull(tsMs) : '';

      if (msg.role === 'system') {
        flushWork();
        const dKey = `sys:${name}:${msg.id}`;
        const isOpen = state.agentDetailsOpen.has(dKey);
        const sysHtml = `<details class="ag-sys" data-ag-dkey="${esc(dKey)}"${isOpen ? ' open' : ''}>
          <summary>System / Role brief${tsStr ? ` · <span title="${esc(tsTitle)}">${esc(tsStr)}</span>` : ''}</summary>
          <div class="ag-sys-body md">${md(msg.content || '')}</div>
        </details>`;
        timeline.push({ key: `sys:${msg.id}`, sig: `${msg.id}|${String(msg.content || '').length}`, html: sysHtml });
        if (Number.isFinite(tsMs)) prevEndMs = tsMs;
        continue;
      }

      if (msg.role === 'tool') {
        if (msg.toolCallId && pairedToolIds.has(msg.toolCallId)) continue;
        const w = ensureWork(msg.id, tsMs, inCurrentTurn);
        w.tools += 1;
        const dKey = `tool:${name}:${msg.id}`;
        w.parts.push(renderToolCardHtml(name, dKey, msg.toolName || 'tool', '', msg, { st }));
        w.lastSig = `t:${msg.id}`;
        if (Number.isFinite(tsMs)) prevEndMs = tsMs;
        continue;
      }

      if (msg.role === 'user') {
        flushWork();
        flushFiles();
        turnKey = String(msg.id);
        const { systemBrief, taskText } = unwrapUserPrompt(msg.content || '');
        const sysDKey = `usys:${name}:${msg.id}`;
        const sysOpen = state.agentDetailsOpen.has(sysDKey);
        const sysDisclosure = systemBrief
          ? `<details class="ag-sys ag-sys-inline" data-ag-dkey="${esc(sysDKey)}"${sysOpen ? ' open' : ''}>
              <summary>System / Role brief</summary>
              <div class="ag-sys-body md">${md(systemBrief)}</div>
            </details>`
          : '';
        const uHtml = `<div class="ag-user-wrap" data-testid="ag-user-prompt">
          ${sysDisclosure}
          <div class="ag-user-pill md">${md(taskText)}</div>
          ${tsStr ? `<div class="ag-user-meta"><span class="ag-msg-ts" title="${esc(tsTitle)}">${esc(tsStr)}</span></div>` : ''}
        </div>`;
        timeline.push({ key: `u:${msg.id}`, sig: `${msg.id}|${taskText.length}|${systemBrief.length}`, html: uHtml });
        if (Number.isFinite(tsMs)) prevEndMs = tsMs;
        continue;
      }

      // assistant
      const tcs = Array.isArray(msg.toolCalls) ? msg.toolCalls : [];
      const hasWork = Boolean(msg.reasoning || tcs.length > 0);
      let stepEndMs = tsMs;
      if (hasWork) {
        const w = ensureWork(msg.id, tsMs, inCurrentTurn);
        w.steps += 1;
        if (msg.reasoning) {
          const dKey = `think:${name}:${msg.id}`;
          if (inCurrentTurn && data?.seenDKeys && !data.seenDKeys.has(dKey)) {
            if (state.agentDetailsOpen.has(liveThinkKey)) state.agentDetailsOpen.add(dKey);
            data.seenDKeys.add(dKey);
          }
          const isOpen = state.agentDetailsOpen.has(dKey);
          const rTok = Number(msg.reasoningTokens) || 0;
          w.parts.push(`<details class="ag-think" data-ag-dkey="${esc(dKey)}"${isOpen ? ' open' : ''}>
            <summary>Thought process${rTok > 0 ? ` · ${fmtTokCompact(rTok)} tok` : ''}</summary>
            <div class="ag-think-body md">${md(msg.reasoning)}</div>
          </details>`);
        }
        for (let i = 0; i < tcs.length; i++) {
          const tc = tcs[i] || {};
          const tcId = tc.id || tc.toolCallId || '';
          if (tcId) committedToolCallIds.add(tcId);
          const tcName = tc.name || tc.function?.name || 'tool';
          const tcArgs = tc.arguments ?? tc.function?.arguments ?? tc.args ?? '';
          const resMsg = tcId ? toolResultsById.get(tcId) : null;
          let tcDur = NaN;
          if (resMsg) {
            if (tcId) pairedToolIds.add(tcId);
            const resMs = normalizeTsMs(resMsg.timestampMs ?? resMsg.timestamp ?? resMsg.ts);
            if (Number.isFinite(resMs)) {
              if (Number.isFinite(tsMs)) tcDur = Math.max(0, resMs - tsMs);
              w.maxMs = Number.isFinite(w.maxMs) ? Math.max(w.maxMs, resMs) : resMs;
              stepEndMs = Number.isFinite(stepEndMs) ? Math.max(stepEndMs, resMs) : resMs;
            }
          }
          recordFileChange(extractToolFileChange(tcName, tcArgs, resMsg));
          w.tools += 1;
          const dKey = `tc:${name}:${msg.id}:${tcId || i}`;
          if (tcId && data?.seenDKeys && !data.seenDKeys.has(dKey)) {
            if (state.agentDetailsOpen.has(`live-tc:${name}:${tcId}`)) state.agentDetailsOpen.add(dKey);
            data.seenDKeys.add(dKey);
          }
          w.parts.push(renderToolCardHtml(name, dKey, tcName, tcArgs, resMsg, { st, durationMs: tcDur }));
        }
        w.lastSig = `a:${msg.id}:${w.tools}:${String(msg.reasoning || '').length}`;
      }

      if (msg.content && String(msg.content).trim()) {
        flushWork();
        const rawReply = String(msg.content);
        const rHtml = `<div class="ag-reply-wrap" data-ag-msg-id="${esc(msg.id)}">
          <div class="ag-reply md">${md(rawReply)}</div>
          <div class="ag-reply-foot">
            <button type="button" class="ghost-btn sm ag-reply-copy" data-copy-reply>Copy</button>
            ${tsStr ? `<span class="ag-msg-ts" title="${esc(tsTitle)}">${esc(tsStr)}</span>` : ''}
          </div>
        </div>`;
        timeline.push({ key: `r:${msg.id}`, sig: `${msg.id}|${rawReply.length}|${tsStr}`, html: rHtml });
      }
      if (Number.isFinite(stepEndMs)) prevEndMs = stepEndMs;
    }

    flushWork();
    flushFiles();
    return { timeline, committedToolCallIds };
  }

  function reconcileCommitted(committedEl, timeline) {
    let deferred = false;
    const existingByKey = new Map();
    for (const child of [...committedEl.children]) {
      if (child.dataset?.agKey) existingByKey.set(child.dataset.agKey, child);
      else child.remove();
    }
    const desiredKeys = new Set(timeline.map((b) => b.key));
    for (const [k, node] of existingByKey) {
      if (!desiredKeys.has(k)) {
        if (hasSelectionInside(node)) deferred = true;
        else node.remove();
      }
    }
    let cursor = committedEl.firstElementChild;
    for (const item of timeline) {
      let node = existingByKey.get(item.key);
      if (!node) {
        const tmp = document.createElement('div');
        tmp.innerHTML = item.html;
        node = tmp.firstElementChild;
        if (!node) continue;
        node.dataset.agKey = item.key;
        node.dataset.agSig = item.sig;
        committedEl.insertBefore(node, cursor);
        continue;
      }
      if (node.dataset.agSig !== item.sig) {
        if (hasSelectionInside(node)) {
          deferred = true;
        } else {
          const wasCursor = cursor === node;
          const tmp = document.createElement('div');
          tmp.innerHTML = item.html;
          const fresh = tmp.firstElementChild;
          if (fresh) {
            fresh.dataset.agKey = item.key;
            fresh.dataset.agSig = item.sig;
            committedEl.replaceChild(fresh, node);
            node = fresh;
            if (wasCursor) cursor = fresh;
          }
        }
      }
      if (node !== cursor) {
        committedEl.insertBefore(node, cursor);
      } else {
        cursor = cursor.nextElementSibling;
      }
    }
    return deferred;
  }

  function stripCommittedPrefix(rawLive, committedList) {
    let rem = String(rawLive || '').trim();
    if (!rem || !committedList.length) return rem;
    if (committedList[committedList.length - 1] === rem) return '';
    let i = 0;
    while (i < committedList.length && !rem.startsWith(committedList[i])) i++;
    while (i < committedList.length && rem) {
      const chunk = committedList[i];
      i++;
      if (!chunk) continue;
      if (rem === chunk) return '';
      if (rem.startsWith(chunk) && /^\s/.test(rem.slice(chunk.length))) {
        rem = rem.slice(chunk.length).trimStart();
      }
    }
    if (rem && committedList.includes(rem)) return '';
    return rem;
  }

  function hasSelectionInside(el) {
    if (!el) return false;
    const sel = window.getSelection?.();
    if (!sel || sel.isCollapsed || !sel.rangeCount) return false;
    return el.contains(sel.anchorNode) || el.contains(sel.focusNode);
  }

  const fmtModAgent = (m) => {
    let s = String(m || '').trim();
    if (!s) return '';
    if (s.includes('/')) s = s.split('/').pop();
    return s.replace(/-(?:dropdown|high|medium|low)$/i, '');
  };

  function buildLiveTailSpec(name, data, st, committedToolCallIds) {
    if (st !== 'working') return null;
    const live = data.live && typeof data.live === 'object' ? data.live : null;
    const tel = data.telemetry || {};
    const m = memberOf(name);
    const curLabel = formatCurrentToolLabel(live?.currentTool ?? tel.currentTool) || (m?.activity ? String(m.activity) : '');
    const trace = Array.isArray(live?.toolTrace) ? live.toolTrace : [];
    const liveTools = trace.filter((tc) => {
      const tcId = tc?.toolCallId || tc?.id || '';
      return !tcId || !committedToolCallIds.has(tcId);
    });
    const elapsedMs = Number(live?.elapsedMs);
    const durSuffix = Number.isFinite(elapsedMs) && elapsedMs > 0 ? ` · ${fmtDur(elapsedMs)}` : '';
    const summaryText = curLabel
      ? `Working${durSuffix} · ${curLabel}`
      : `Working${durSuffix || '…'}`;

    let lastUserIdx = -1;
    for (let i = data.messages.length - 1; i >= 0; i--) {
      if (data.messages[i]?.role === 'user') { lastUserIdx = i; break; }
    }
    const turnAssts = data.messages.slice(lastUserIdx + 1).filter((msg) => msg?.role === 'assistant');
    const committedTexts = turnAssts.map((msg) => String(msg.content || '').trim()).filter(Boolean);
    const committedReasonings = turnAssts.map((msg) => String(msg.reasoning || '').trim()).filter(Boolean);

    const dedupedReasoning = stripCommittedPrefix(live?.reasoning, committedReasonings);
    const dedupedTranscript = stripCommittedPrefix(live?.transcript, committedTexts);

    const liveParts = [];
    if (dedupedReasoning) {
      const rKey = `live-think:${name}:${data.selectedSubagent || 'main'}`;
      const rOpen = state.agentDetailsOpen.has(rKey);
      let reasoningHtml;
      if (data.cachedLiveReasoning && data.cachedLiveReasoning.raw === dedupedReasoning) {
        reasoningHtml = data.cachedLiveReasoning.html;
      } else {
        reasoningHtml = md(dedupedReasoning);
        data.cachedLiveReasoning = { raw: dedupedReasoning, html: reasoningHtml };
      }
      liveParts.push({
        key: rKey,
        sig: `${dedupedReasoning.length}|${dedupedReasoning.slice(-40)}`,
        html: `<details class="ag-think" data-ag-dkey="${esc(rKey)}"${rOpen ? ' open' : ''}>
          <summary>Thought process</summary>
          <div class="ag-think-body md">${reasoningHtml}</div>
        </details>`,
      });
    } else {
      data.cachedLiveReasoning = null;
    }
    for (let i = 0; i < liveTools.length; i++) {
      const tc = liveTools[i] || {};
      const tcId = tc.toolCallId || tc.id || String(i);
      const dKey = `live-tc:${name}:${tcId}`;
      const tcArgs = tc.arguments ?? tc.preview ?? '';
      const hasRes = tc.status === 'ok' || tc.status === 'error' || tc.result != null || tc.output != null;
      const resMsg = hasRes ? { content: tc.result ?? tc.output ?? '', status: tc.status } : null;
      const html = renderToolCardHtml(name, dKey, tc.name || 'tool', tcArgs, resMsg, {
        st: 'working',
        durationMs: Number.isFinite(Number(tc.durationMs)) ? Number(tc.durationMs) : NaN,
      });
      liveParts.push({
        key: dKey,
        sig: `${tc.status || ''}|${tc.durationMs || ''}|${String(tcArgs).length}|${String(tc.result ?? tc.output ?? '').length}`,
        html,
      });
    }

    const wgKey = `live-wg:${name}:${data.selectedSubagent || 'main'}`;
    const wgOpen = state.agentDetailsOpen.has(wgKey);
    let transcriptHtml = '';
    if (dedupedTranscript) {
      if (data.cachedLiveTranscript && data.cachedLiveTranscript.raw === dedupedTranscript) {
        transcriptHtml = data.cachedLiveTranscript.html;
      } else {
        transcriptHtml = md(dedupedTranscript);
        data.cachedLiveTranscript = { raw: dedupedTranscript, html: transcriptHtml };
      }
    } else {
      data.cachedLiveTranscript = null;
    }
    const transcriptSig = dedupedTranscript ? `${dedupedTranscript.length}|${dedupedTranscript.slice(-64)}` : '';
    return { wgKey, wgOpen, summaryText, liveParts, transcriptHtml, transcriptSig };
  }

  function reconcileLiveTail(liveTail, spec) {
    let wgEl = liveTail.querySelector(':scope > details.ag-live-work');
    if (wgEl && wgEl.dataset.agDkey !== spec.wgKey) {
      liveTail.innerHTML = '';
      wgEl = null;
    }
    if (!wgEl) {
      const tmp = document.createElement('div');
      tmp.innerHTML = `<details class="ag-work-group ag-live-work" data-ag-dkey="${esc(spec.wgKey)}"${spec.wgOpen ? ' open' : ''}>
        <summary><span class="live-dot" aria-hidden="true"></span><span class="animate-shimmer-text">${esc(spec.summaryText)}</span></summary>
        <div class="ag-work-rail"></div>
      </details>`;
      wgEl = tmp.firstElementChild;
      liveTail.prepend(wgEl);
    } else {
      if (wgEl.open !== spec.wgOpen && state.agentDetailsOpen.has(spec.wgKey)) {
        wgEl.open = true;
      }
      const shim = wgEl.querySelector(':scope > summary .animate-shimmer-text');
      if (shim && shim.textContent !== spec.summaryText) {
        shim.textContent = spec.summaryText;
      }
    }
    const summaryEl = wgEl.querySelector(':scope > summary');
    let chev = summaryEl?.querySelector('.ag-work-chev');
    if (spec.liveParts.length && summaryEl && !chev) {
      chev = document.createElement('span');
      chev.className = 'ag-work-chev';
      chev.setAttribute('aria-hidden', 'true');
      summaryEl.appendChild(chev);
    } else if (!spec.liveParts.length && chev) {
      chev.remove();
    }

    let rail = wgEl.querySelector(':scope > .ag-work-rail');
    if (spec.liveParts.length) {
      if (!rail) {
        rail = document.createElement('div');
        rail.className = 'ag-work-rail';
        wgEl.appendChild(rail);
      }
      const existing = new Map();
      for (const ch of [...rail.children]) {
        if (ch.dataset?.agKey) existing.set(ch.dataset.agKey, ch);
        else ch.remove();
      }
      const wanted = new Set(spec.liveParts.map((p) => p.key));
      for (const [k, node] of existing) {
        if (!wanted.has(k)) node.remove();
      }
      let cursor = rail.firstElementChild;
      for (const part of spec.liveParts) {
        let node = existing.get(part.key);
        if (!node) {
          const tmp = document.createElement('div');
          tmp.innerHTML = part.html;
          node = tmp.firstElementChild;
          if (!node) continue;
          node.dataset.agKey = part.key;
          node.dataset.agSig = part.sig;
          rail.insertBefore(node, cursor);
          continue;
        }
        if (node.dataset.agSig !== part.sig && !hasSelectionInside(node)) {
          const tmp = document.createElement('div');
          tmp.innerHTML = part.html;
          const fresh = tmp.firstElementChild;
          if (fresh) {
            const wasOpen = node.open;
            const nSum = node.querySelector(':scope > summary');
            const fSum = fresh.querySelector(':scope > summary');
            if (nSum && fSum && nSum.innerHTML !== fSum.innerHTML) nSum.innerHTML = fSum.innerHTML;
            const nBody = node.querySelector(':scope > .ag-think-body, :scope > .ag-tool-detail');
            const fBody = fresh.querySelector(':scope > .ag-think-body, :scope > .ag-tool-detail');
            if (nBody && fBody && nBody.innerHTML !== fBody.innerHTML) nBody.innerHTML = fBody.innerHTML;
            node.open = wasOpen || state.agentDetailsOpen.has(part.key);
            node.dataset.agSig = part.sig;
          }
        }
        if (node !== cursor) rail.insertBefore(node, cursor);
        else cursor = cursor.nextElementSibling;
      }
    } else if (rail) {
      rail.remove();
    }

    let replyEl = liveTail.querySelector(':scope > .ag-live-reply');
    if (spec.transcriptHtml) {
      if (!replyEl) {
        replyEl = document.createElement('div');
        replyEl.className = 'ag-reply ag-live-reply md';
        replyEl.dataset.agSig = spec.transcriptSig;
        replyEl.innerHTML = spec.transcriptHtml;
        liveTail.appendChild(replyEl);
      } else if (replyEl.dataset.agSig !== spec.transcriptSig && !hasSelectionInside(replyEl)) {
        replyEl.dataset.agSig = spec.transcriptSig;
        replyEl.innerHTML = spec.transcriptHtml;
      }
    } else if (replyEl && !hasSelectionInside(replyEl)) {
      replyEl.remove();
    }
  }

  function renderAgentPane(name, { olderRestore = null } = {}) {
    const header = $('agent-header');
    const feed = $('agent-feed');
    const foot = $('agent-live-foot');
    if (!header || !feed || !foot) return;

    let committed = $('agent-committed');
    let liveTail = $('agent-live-tail');
    if (!committed || !liveTail) {
      feed.innerHTML = '<div id="agent-committed" class="agent-committed" data-testid="swarm-agent-committed"></div>' +
        '<div id="agent-live-tail" class="agent-live-tail hidden" data-testid="swarm-agent-live-tail" aria-live="polite"></div>';
      committed = $('agent-committed');
      liveTail = $('agent-live-tail');
    }

    const data = ensureAgentData(name);
    const m = memberOf(name);
    const tel = data.telemetry || {};
    const st = effectiveAgentStatus(name, data, m);
    const subId = String(data.selectedSubagent || '').trim();
    const paneKey = `${name}:${subId}`;
    if (committed.dataset.agPaneOwner !== paneKey) {
      committed.dataset.agPaneOwner = paneKey;
      data.lastCommittedSig = '';
      data.lastLiveSig = '';
      data.committedInputKey = '';
      data.committedDeferred = false;
      data.cachedCommittedToolIds = null;
      data.cachedLiveReasoning = null;
      data.cachedLiveTranscript = null;
    }
    const subList = (Array.isArray(data.subagents) && data.subagents.length ? data.subagents : m?.subagents) || [];
    const subObj = subId ? (subList.find((s) => s && String(s.sessionId || s.id || '') === subId) || null) : null;
    const snapSub = subId && Array.isArray(m?.subagents)
      ? (m.subagents.find((s) => s && String(s.sessionId || s.id || '') === subId) || null)
      : null;
    const fallbackRec = subId ? (subObj || snapSub) : m;
    const hasKeys = (o) => Boolean(o && typeof o === 'object' && Object.keys(o).length > 0);
    const model = tel.model || fallbackRec?.model || '';
    const tok = hasKeys(tel.tokens) ? tel.tokens : (hasKeys(fallbackRec?.tokens) ? fallbackRec.tokens : (subId && hasKeys(snapSub?.tokens) ? snapSub.tokens : {}));
    const processed = Number(tok.totalProcessed ?? tok.total) || 0;
    const pure = Number(tok.pureInput ?? tok.uncachedInput ?? tok.input) || 0;
    const cached = Number(tok.cachedInput ?? tok.cacheRead) || 0;
    const outTok = Number(tok.output) || 0;
    const ctxUsed = Number(tok.contextUsed) || 0;
    const ctxLimit = Number(tok.contextLimit) || 0;
    let ctxPct = Number(tok.contextPct) || 0;
    if (ctxPct <= 0 && ctxUsed > 0 && ctxLimit > 0) ctxPct = (ctxUsed / ctxLimit) * 100;
    const ctxCls = ctxPct >= 85 ? 'crit' : (ctxPct >= 70 ? 'warn' : '');
    const tps = Number((tel.tps != null && tel.tps > 0) ? tel.tps : (fallbackRec?.tps ?? snapSub?.tps ?? tel.tps)) || 0;
    const tpsEst = !!(tel.tps != null && tel.tps > 0 ? tel.tpsEstimated : (fallbackRec?.tpsEstimated ?? snapSub?.tpsEstimated ?? tel.tpsEstimated));
    const color = window.SwarmHive?.colorOf ? window.SwarmHive.colorOf(name) : '#8a8f98';
    const avatar = window.SwarmHive?.avatarHtml ? window.SwarmHive.avatarHtml(name)
      : `<span class="msg-av" style="--av:${esc(color)}" aria-hidden="true">${esc(name.charAt(0).toUpperCase())}</span>`;

    const subPills = subList.length
      ? `<div class="agent-sub-pills" role="group" aria-label="Session switcher">
          <button type="button" class="agent-sub-pill ${!data.selectedSubagent ? 'active' : ''}" data-sub-id="">Main</button>
          ${subList.map((s) => {
            const sid = String(s.sessionId || s.id || '');
            const snapMatch = Array.isArray(m?.subagents) ? m.subagents.find((x) => x && String(x.sessionId || x.id || '') === sid) : null;
            const lbl = String(s.label || s.name || s.title || snapMatch?.name || sid.slice(0, 12));
            const sSt = normalizeAgentStatus(s.state || s.status || snapMatch?.status || 'waiting', false);
            const stepsVal = Number(s.steps ?? snapMatch?.steps) || 0;
            const sTokObj = hasKeys(s.tokens) ? s.tokens : snapMatch?.tokens;
            const sTok = sTokObj ? (Number(sTokObj.totalProcessed ?? sTokObj.total) || ((Number(sTokObj.pureInput ?? sTokObj.uncachedInput ?? sTokObj.input) || 0) + (Number(sTokObj.output) || 0))) : 0;
            const sTps = Number(s.tps ?? snapMatch?.tps) || 0;
            const metaBits = [];
            if (stepsVal > 0) metaBits.push(String(stepsVal));
            if (sTok > 0) metaBits.push(fmtTokCompact(sTok));
            if (sTps > 0) metaBits.push(fmtTpsCompact(sTps));
            const metaStr = metaBits.length ? ` · ${metaBits.join(' · ')}` : '';
            return `<button type="button" class="agent-sub-pill ${data.selectedSubagent === sid ? 'active' : ''}" data-sub-id="${esc(sid)}" title="${esc(sid)}"><span class="agent-sub-dot ${esc(sSt)}" aria-hidden="true"></span><span>${esc(lbl)}${esc(metaStr)}</span></button>`;
          }).join('')}
        </div>`
      : '';

    const isCoord = name === (window.SwarmHive?.leadName || 'coordinator') || m?.role === 'coordinator';
    const primCid = isCoord && canOpenConversation() ? primaryConv() : '';
    const headHtml = `
      <div class="agent-head-top">
        <div class="agent-head-id">
          ${avatar}
          <span class="agent-head-name">${esc(name)}</span>
          <span class="agent-status-chip ${esc(st)}">${esc(statusLabel(st))}</span>
          ${model ? `<span class="agent-model-badge" title="${esc(model)}">${esc(fmtModAgent(model))}</span>` : ''}
        </div>
        <div class="agent-head-actions">
          ${primCid ? `<button type="button" class="ghost-btn sm" data-ag-open-primary="${esc(primCid)}" title="Open Primary Chat (${esc(primCid)})">Open Primary Chat</button>` : ''}
          ${data.hasMore ? '<button type="button" class="ghost-btn sm" data-ag-older>Show older</button>' : ''}
          <button type="button" class="ghost-btn sm" data-ag-refresh>Refresh</button>
          <button type="button" class="ghost-btn sm" data-ag-close="${esc(name)}">Close</button>
        </div>
      </div>
      <div class="agent-head-metrics">
        ${(ctxPct > 0 || (ctxUsed > 0 && ctxLimit > 0)) ? `
          <span class="agent-ctx-wrap">
            <span class="agent-ctx-bar"><span class="agent-ctx-fill ${ctxCls}" style="width:${Math.min(100, Math.max(0, Math.round(ctxPct)))}%"></span></span>
            <span class="agent-ctx-label ${ctxCls}">CTX ${ctxUsed > 0 && ctxLimit > 0 ? `${fmtTokCompact(ctxUsed)}/${fmtTokCompact(ctxLimit)} ` : ''}${Math.round(ctxPct)}%</span>
          </span>` : ''}
        ${processed > 0 ? `<span>${fmtTokCompact(processed)} proc</span>` : ''}
        ${pure > 0 ? `<span>${fmtTokCompact(pure)} pure</span>` : ''}
        ${cached > 0 ? `<span>${fmtTokCompact(cached)} cached</span>` : ''}
        ${outTok > 0 ? `<span>${fmtTokCompact(outTok)} out</span>` : ''}
        ${tps > 0 ? `<span>${tpsEst ? '~' : ''}${esc(fmtTpsCompact(tps))}</span>` : ''}
      </div>
      ${subPills}`;
    setHtmlIfChanged(header, headHtml);

    const wasNearBottom = feed.scrollTop + feed.clientHeight >= feed.scrollHeight - 60;
    const prevTop = feed.scrollTop;
    let committedChanged = false;

    let committedToolIds = new Set();
    if (data.loading && !data.messages.length) {
      const loadHtml = '<div class="ag-empty">Loading agent history…</div>';
      if (data.lastCommittedSig !== 'loading') {
        data.lastCommittedSig = 'loading';
        data.committedInputKey = '';
        data.cachedCommittedToolIds = null;
        committed.innerHTML = loadHtml;
        committedChanged = true;
      }
    } else if (data.error && !data.messages.length) {
      const errSig = `err:${data.error}`;
      if (data.lastCommittedSig !== errSig) {
        data.lastCommittedSig = errSig;
        data.committedInputKey = '';
        data.cachedCommittedToolIds = null;
        committed.innerHTML = `<div class="ag-empty">${esc(data.error)}</div>`;
        committedChanged = true;
      }
    } else if (!data.messages.length) {
      if (data.lastCommittedSig !== 'empty') {
        data.lastCommittedSig = 'empty';
        data.committedInputKey = '';
        data.cachedCommittedToolIds = null;
        committed.innerHTML = '<div class="ag-empty">No session history recorded yet.</div>';
        committedChanged = true;
      }
    } else {
      const afCount = state.snap?.allowedFiles ? Object.keys(state.snap.allowedFiles).length : 0;
      const committedInputKey = `${paneKey}|v${data.messagesVersion || 0}|n${data.messages.length}|l${data.lastId || 0}|s${st}|af${afCount}`;
      if (data.committedInputKey === committedInputKey && !data.committedDeferred && data.cachedCommittedToolIds) {
        committedToolIds = data.cachedCommittedToolIds;
      } else {
        const { timeline, committedToolCallIds } = groupAgentTimeline(data.messages, { name, st, data });
        committedToolIds = committedToolCallIds;
        data.cachedCommittedToolIds = committedToolCallIds;
        const cSig = timeline.map((b) => `${b.key}=${b.sig}`).join('\n');
        if (data.lastCommittedSig !== cSig) {
          const deferred = reconcileCommitted(committed, timeline);
          data.committedDeferred = deferred;
          if (!deferred) {
            data.lastCommittedSig = cSig;
            data.committedInputKey = committedInputKey;
          }
          committedChanged = true;
        } else {
          data.committedDeferred = false;
          data.committedInputKey = committedInputKey;
        }
      }
    }

    const tailSpec = buildLiveTailSpec(name, data, st, committedToolIds);
    let tailChanged = false;
    if (tailSpec) {
      const nextTailSig = JSON.stringify({
        s: tailSpec.summaryText,
        p: tailSpec.liveParts.map((x) => [x.key, x.sig]),
        t: tailSpec.transcriptSig,
      });
      if (data.lastLiveSig !== nextTailSig) {
        data.lastLiveSig = nextTailSig;
        reconcileLiveTail(liveTail, tailSpec);
        tailChanged = true;
      }
      liveTail.classList.remove('hidden');
    } else {
      data.lastLiveSig = '';
      if (!liveTail.classList.contains('hidden')) liveTail.classList.add('hidden');
      if (liveTail.innerHTML !== '' && !hasSelectionInside(liveTail)) liveTail.innerHTML = '';
    }

    if (olderRestore) {
      feed.scrollTop = olderRestore.prevTop + Math.max(0, feed.scrollHeight - olderRestore.prevHeight);
    } else if ((data.stick || wasNearBottom) && !hasSelectionInside(feed)) {
      if (committedChanged || tailChanged || prevTop === 0) {
        feed.scrollTop = feed.scrollHeight;
      }
    } else if (prevTop > 0) {
      feed.scrollTop = prevTop;
    }

    const showFoot = st === 'working' && !m?.killed;
    if (showFoot) {
      const curTool = formatCurrentToolLabel(tel.currentTool ?? data.live?.currentTool) || (m?.activity ? String(m.activity) : 'Thinking…');
      const footHtml = `<span class="live-dot" aria-hidden="true"></span>` +
        `<span class="ag-live-tool">${esc(curTool)}</span>` +
        `${tps > 0 ? `<span class="ag-live-tps">${tpsEst ? '~' : ''}${esc(fmtTpsCompact(tps))}</span>` : ''}`;
      setHtmlIfChanged(foot, footHtml);
      foot.classList.remove('hidden');
    } else {
      foot.classList.add('hidden');
      setHtmlIfChanged(foot, '');
    }
    $('agent-pane')?.classList.toggle('has-live-foot', showFoot);
    updateAgentJump(name);
  }

  // ---------- events ----------
  $('tab-overview').addEventListener('click', (e) => {
    const openMem = e.target.closest('[data-open-member]');
    if (openMem) {
      window.SwarmHive?.hooks?.member?.(openMem.dataset.openMember, openMem.dataset.openSubagent || '');
      return;
    }
    const open = e.target.closest('[data-open]');
    if (open) return openConversation(open.dataset.open);
    const seq = e.target.closest('[data-ov-seq]');
    if (seq) {
      if (seq.dataset.ovSeq) goToPost(Number(seq.dataset.ovSeq));
      return;
    }
  });
  $('ov-mode').addEventListener('click', () => {
    const toFrame = prefs.plain || !!frame.failed;
    prefs.plain = !toFrame;
    frame.failed = null;
    lsSet(PLAIN_KEY, prefs.plain ? null : '0');
    renderOverview();
  });
  $('ov-open-coord').addEventListener('click', () => {
    const coordName = window.SwarmHive?.leadName || 'coordinator';
    openMember(coordName);
  });

  const TABS = ['overview', 'feed', 'members', 'graph', 'raw'];
  function selectTab(tab, focus = false, byUser = false) {
    if (byUser) state.tabChosen = true;
    const isAgent = typeof tab === 'string' && tab.startsWith('agent:');
    const agentName = isAgent ? tab.slice(6) : '';
    if (!isAgent) stopAgentPoll();

    document.querySelectorAll('.tab').forEach((x) => {
      const on = x.dataset.tab === tab;
      x.classList.toggle('active', on);
      x.setAttribute('aria-selected', String(on));
      x.tabIndex = on ? 0 : -1;
      if (on && focus) x.focus();
    });
    const switched = byUser && tab !== state.tab;
    const targetBodyId = isAgent ? 'tab-agent' : `tab-${tab}`;
    document.querySelectorAll('.tab-body').forEach((x) => {
      const on = x.id === targetBodyId;
      x.classList.toggle('active', on);
      x.classList.toggle('tab-fade', on && switched);
    });
    state.tab = tab;
    if (isAgent) {
      renderAgentPane(agentName);
      fetchAgentHistory(agentName, { incremental: !!state.agentTabData[agentName]?.messages?.length });
    } else if (state.snap) {
      const wasDirty = state.dirtyTabs.has(tab);
      flushActiveTab();
      if (!wasDirty && state.view === 'board') {
        if (tab === 'graph') renderGraph();
        else if (tab === 'overview') renderOverview();
        else if (tab === 'members') renderMembers();
        else if (tab === 'feed') measureClamps();
      }
    }
    syncWatchdog();
    updateJump();
  }
  const tabsNav = document.querySelector('.tabs');
  tabsNav.addEventListener('click', (e) => {
    const closeBtn = e.target.closest('[data-close-agent]');
    if (closeBtn) {
      e.stopPropagation();
      closeAgentTab(closeBtn.dataset.closeAgent);
      return;
    }
    const t = e.target.closest('.tab[data-tab]');
    if (t) selectTab(t.dataset.tab, false, true);
  });
  tabsNav.addEventListener('scroll', syncTabsOverflow, { passive: true });
  tabsNav.addEventListener('wheel', (e) => {
    if (e.ctrlKey) return;
    const maxScroll = tabsNav.scrollWidth - tabsNav.clientWidth;
    if (maxScroll <= 1) return;
    if (Math.abs(e.deltaY) <= Math.abs(e.deltaX) || e.deltaY === 0) return;
    e.preventDefault();
    const scale = e.deltaMode === 1 ? 24 : (e.deltaMode === 2 ? Math.max(80, tabsNav.clientWidth * 0.85) : 1);
    const delta = e.deltaY * scale;
    if (typeof tabsNav.scrollBy === 'function') {
      tabsNav.scrollBy({ left: delta, behavior: 'instant' });
    } else {
      tabsNav.scrollLeft += delta;
    }
    syncTabsOverflow();
  }, { passive: false });
  tabsNav.addEventListener('keydown', (e) => {
    const closeBtn = e.target.closest('[data-close-agent]');
    if (closeBtn && (e.key === 'Enter' || e.key === ' ')) {
      e.preventDefault();
      e.stopPropagation();
      closeAgentTab(closeBtn.dataset.closeAgent);
      return;
    }
    const tabs = [...document.querySelectorAll('.tabs .tab')]
      .filter((el) => !el.classList.contains('hidden'))
      .map((el) => el.dataset.tab);
    if (!tabs.length) return;
    const i = tabs.indexOf(state.tab);
    let next = null;
    if (e.key === 'ArrowRight') next = tabs[(i + 1) % tabs.length];
    else if (e.key === 'ArrowLeft') next = tabs[(i - 1 + tabs.length) % tabs.length];
    else if (e.key === 'Home') next = tabs[0];
    else if (e.key === 'End') next = tabs[tabs.length - 1];
    if (!next) return;
    e.preventDefault();
    e.stopPropagation();
    selectTab(next, true, true);
  });

  $('agent-header')?.addEventListener('click', (e) => {
    if (!state.tab.startsWith('agent:')) return;
    const name = state.tab.slice(6);
    const primBtn = e.target.closest('[data-ag-open-primary]');
    if (primBtn && isValidConvId(primBtn.dataset.agOpenPrimary)) {
      return openConversation(primBtn.dataset.agOpenPrimary);
    }
    const closeBtn = e.target.closest('[data-ag-close]');
    if (closeBtn) return closeAgentTab(closeBtn.dataset.agClose);
    if (e.target.closest('[data-ag-refresh]')) return fetchAgentHistory(name, { incremental: false });
    if (e.target.closest('[data-ag-older]')) return fetchAgentHistory(name, { older: true });
    const subBtn = e.target.closest('[data-sub-id]');
    if (subBtn) {
      const data = ensureAgentData(name);
      data.selectedSubagent = subBtn.dataset.subId || '';
      data.messages = [];
      data.lastId = 0;
      data.live = null;
      data.lastCommittedSig = '';
      data.lastLiveSig = '';
      data.seenDKeys?.clear();
      data.stick = true;
      data.hasNewWhileScrolled = false;
      fetchAgentHistory(name, { incremental: false });
    }
  });
  $('agent-feed')?.addEventListener('toggle', (e) => {
    const d = e.target.closest('details[data-ag-dkey]');
    if (!d) return;
    if (d.open) state.agentDetailsOpen.add(d.dataset.agDkey);
    else state.agentDetailsOpen.delete(d.dataset.agDkey);
  }, true);
  $('agent-feed')?.addEventListener('click', async (e) => {
    const copyCodeBtn = e.target.closest('[data-copy-code]');
    if (copyCodeBtn) {
      const codeEl = copyCodeBtn.closest('.ag-codeblock')?.querySelector('pre code');
      const ok = await copyText(codeEl?.textContent || '');
      copyCodeBtn.textContent = ok ? 'Copied' : 'Failed';
      setTimeout(() => { copyCodeBtn.textContent = 'Copy'; }, 1200);
      return;
    }
    const copyReplyBtn = e.target.closest('[data-copy-reply]');
    if (copyReplyBtn) {
      const replyEl = copyReplyBtn.closest('.ag-reply-wrap')?.querySelector('.ag-reply');
      const ok = await copyText(replyEl?.innerText || replyEl?.textContent || '');
      copyReplyBtn.textContent = ok ? 'Copied' : 'Failed';
      setTimeout(() => { copyReplyBtn.textContent = 'Copy'; }, 1200);
      return;
    }
    const fileBtn = e.target.closest('[data-ag-file]');
    if (fileBtn) {
      const uri = resolveAllowedFileUri(fileBtn.dataset.agFile || '');
      if (uri && window.sidecar?.ui?.toggleAuxPane) {
        window.sidecar.ui.toggleAuxPane({ tabType: 'fileView', uri });
      } else {
        toast('Opening files needs the app.');
      }
      return;
    }
  });
  $('agent-jump-live')?.addEventListener('click', () => {
    if (!state.tab.startsWith('agent:')) return;
    const name = state.tab.slice(6);
    const data = state.agentTabData[name];
    const af = $('agent-feed');
    if (data) {
      data.stick = true;
      data.hasNewWhileScrolled = false;
    }
    if (af) {
      if (typeof af.scrollTo === 'function') af.scrollTo({ top: af.scrollHeight, behavior: 'smooth' });
      else af.scrollTop = af.scrollHeight;
    }
    updateAgentJump(name);
  });
  $('agent-feed')?.addEventListener('scroll', () => {
    if (!state.tab.startsWith('agent:')) return;
    const name = state.tab.slice(6);
    const data = state.agentTabData[name];
    const af = $('agent-feed');
    if (data && af) {
      data.stick = af.scrollTop + af.clientHeight >= af.scrollHeight - 60;
      if (data.stick) data.hasNewWhileScrolled = false;
      updateAgentJump(name);
    }
  }, { passive: true });

  ['f-agent', 'f-mention', 'f-kind'].forEach((id) => $(id)?.addEventListener('input', () => {
    $(id).classList.toggle('set', !!$(id).value);
    if (state.snap) renderFeed();
  }));
  let searchTimer = 0;
  $('f-search').addEventListener('input', () => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(() => {
      if (state.snap) renderFeed();
    }, 120);
  });
  $('f-search').addEventListener('keydown', (e) => {
    if (e.key !== 'Enter') return;
    const raw = $('f-search').value.trim();
    const match = /^#?(\d+)$/.exec(raw);
    if (match) {
      const seq = Number(match[1]);
      if (posInt(seq) && state.snap?.messages?.some((m) => m.seq === seq)) {
        e.preventDefault();
        clearTimeout(searchTimer);
        goToPost(seq);
      }
    }
  });
  $('f-toggle').addEventListener('click', () => setFiltersOpen($('filters').classList.contains('hidden')));
  $('f-channel').addEventListener('input', () => state.snap && setChannel($('f-channel').value));
  $('channel-tabs').addEventListener('click', (e) => {
    const t = e.target.closest('[data-channel]');
    if (t && state.snap) setChannel(t.dataset.channel);
  });
  $('channel-tabs').addEventListener('keydown', (e) => {
    if (!state.snap || !['ArrowRight', 'ArrowLeft'].includes(e.key)) return;
    const tabs = [...$('channel-tabs').querySelectorAll('[data-channel]')];
    const i = tabs.findIndex((t) => t.dataset.channel === state.channel);
    const next = tabs[(i + (e.key === 'ArrowRight' ? 1 : tabs.length - 1)) % tabs.length];
    e.preventDefault();
    setChannel(next.dataset.channel);
    $('channel-tabs').querySelector(`[data-channel="${CSS.escape(next.dataset.channel)}"]`)?.focus();
  });
  $('agents').addEventListener('click', (e) => {
    const c = e.target.closest('[data-agent]');
    if (!c) return;
    const name = c.dataset.agent;
    if (state.hiddenAgents.has(name)) state.hiddenAgents.delete(name); else state.hiddenAgents.add(name);
    renderAgents();
    renderFeed();
    $('agents').querySelector(`[data-agent="${CSS.escape(name)}"]`)?.focus();
  });
  $('feed').addEventListener('click', async (e) => {
    const copyCodeBtn = e.target.closest('[data-copy-code]');
    if (copyCodeBtn) {
      const codeEl = copyCodeBtn.closest('.ag-codeblock')?.querySelector('pre code');
      const ok = await copyText(codeEl?.textContent || '');
      copyCodeBtn.textContent = ok ? 'Copied' : 'Failed';
      setTimeout(() => { copyCodeBtn.textContent = 'Copy'; }, 1200);
      return;
    }
    if (e.target.closest('[data-show-older]')) {
      const feed = $('feed');
      const prevH = feed.scrollHeight;
      const prevTop = feed.scrollTop;
      state.feedWindow = (Number(state.feedWindow) || 120) + 120;
      state.stick = false;
      renderFeed();
      feed.scrollTop = prevTop + (feed.scrollHeight - prevH);
      return;
    }
    if (e.target.closest('[data-clear-filters]')) {
      clearFilters();
      return;
    }
    const openMem = e.target.closest('[data-feed-member], [data-open-member]');
    if (openMem) {
      window.SwarmHive?.hooks?.member?.(openMem.dataset.feedMember || openMem.dataset.openMember, openMem.dataset.openSubagent || '');
      return;
    }
    const chip = e.target.closest('.msg-channel[data-channel], .msg-mention-btn[data-channel], .mention-btn[data-channel]');
    if (chip) {
      setChannel(chip.dataset.channel);
      return;
    }
    const seqRef = e.target.closest('[data-seq-ref], [data-ov-seq]');
    if (seqRef) {
      const targetSeq = Number(seqRef.dataset.seqRef || seqRef.dataset.ovSeq);
      if (targetSeq) goToPost(targetSeq);
      return;
    }
    let more = e.target.closest('.msg-more');
    if (!more) {
      const row = e.target.closest('.feed .msg');
      if (!row || e.target.closest('a, button, input, pre, code, details, summary, [data-seq-ref], [data-feed-member], [data-open-member]')) return;
      if (String(window.getSelection?.() || '').length) return;
      const btn = row.querySelector('.msg-more');
      if (!btn || btn.classList.contains('hidden')) return;
      more = btn;
    }
    const body = more.previousElementSibling;
    const seq = Number(more.closest('.msg').dataset.seq);
    const open = body.classList.toggle('open');
    more.textContent = open ? 'Show less' : 'Show more';
    more.setAttribute('aria-expanded', String(open));
    if (open) state.expanded.add(seq); else state.expanded.delete(seq);
    if (open) state.stick = false;
    updateJump();
  });
  $('jump-latest').addEventListener('click', () => scrollToLatest('smooth'));
  $('jump-top')?.addEventListener('click', () => {
    state.stick = false;
    const f = $('feed');
    if (f && typeof f.scrollTo === 'function') f.scrollTo({ top: 0, behavior: 'smooth' });
    else if (f) f.scrollTop = 0;
  });
  $('f-autoscroll').addEventListener('input', () => {
    if ($('f-autoscroll').checked && feedShown()) scrollToLatest();
  });
  $('feed').addEventListener('scroll', () => {
    const f = $('feed');
    if (feedShown() && !suppressScrollSync) state.stick = nearBottom();
    $('tab-feed').querySelector('.toolbar').classList.toggle('stuck', f.scrollTop > 0);
    updateJump();
  }, { passive: true });
  let lastFeedW = 0;
  let feedResizeRaf = 0;
  new ResizeObserver(() => {
    const f = $('feed');
    if (!f || !feedShown()) return;
    const w = Math.floor(f.clientWidth);
    if (Math.abs(w - lastFeedW) >= 4) {
      lastFeedW = w;
      cancelAnimationFrame(feedResizeRaf);
      feedResizeRaf = requestAnimationFrame(() => {
        feedResizeRaf = 0;
        measureClamps();
        keepPinned();
        updateJump();
      });
    } else {
      keepPinned();
    }
  }).observe($('feed'));
  document.querySelectorAll('.gview-btn').forEach((b) => b.addEventListener('click', () => {
    state.graphView = b.dataset.gview;
    renderGraph();
  }));
  $('graph').addEventListener('click', (e) => {
    const g = e.target.closest('.node');
    if (!g) return;
    const mem = g.dataset.member;
    if (mem && memberOf(mem)) {
      window.SwarmHive?.hooks?.member?.(mem);
    } else if (g.dataset.conv) {
      openConversation(g.dataset.conv);
    }
  });
  $('replay-caption').addEventListener('click', async (e) => {
    const copyCodeBtn = e.target.closest('[data-copy-code]');
    if (copyCodeBtn) {
      const codeEl = copyCodeBtn.closest('.ag-codeblock')?.querySelector('pre code');
      const ok = await copyText(codeEl?.textContent || '');
      copyCodeBtn.textContent = ok ? 'Copied' : 'Failed';
      setTimeout(() => { copyCodeBtn.textContent = 'Copy'; }, 1200);
      return;
    }
    const openMem = e.target.closest('[data-feed-member], [data-open-member]');
    if (openMem) {
      window.SwarmHive?.hooks?.member?.(openMem.dataset.feedMember || openMem.dataset.openMember);
      return;
    }
    const seqRef = e.target.closest('[data-seq-ref], [data-ov-seq]');
    if (seqRef) {
      const targetSeq = Number(seqRef.dataset.seqRef || seqRef.dataset.ovSeq);
      if (targetSeq) goToPost(targetSeq);
    }
  });
  document.querySelectorAll('.raw-btn').forEach((b) => b.addEventListener('click', () => {
    state.rawKind = b.dataset.raw;
    if (state.snap) renderRaw();
  }));
  $('raw-copy').addEventListener('click', async () => {
    if (state.rawIsEmpty) return toast('Nothing to copy.');
    const label = $('raw-copy-label');
    const ok = await copyText($('raw').textContent);
    label.textContent = ok ? 'Copied' : 'Copy failed';
    setTimeout(() => { label.textContent = 'Copy'; }, 1200);
  });
  $('g-all').addEventListener('input', () => state.snap && renderGraph());
  $('g-hide-inactive')?.addEventListener('input', () => state.snap && renderGraph());
  $('replay-play').addEventListener('click', () => state.snap && replayToggle());
  $('replay-start').addEventListener('click', () => state.snap && replaySeek(0));
  $('replay-back').addEventListener('click', () => state.snap && replaySeek((replay.idx ?? graphMsgs().length) - 1));
  $('replay-fwd').addEventListener('click', () => state.snap && replaySeek((replay.idx ?? graphMsgs().length) + 1));
  $('replay-live').addEventListener('click', () => state.snap && replayLive());
  $('replay-slider').addEventListener('input', (e) => state.snap && replaySeek(parseInt(e.target.value, 10)));
  $('graph-export-gif')?.addEventListener('click', () => state.snap && exportGraphGif());
  $('gif-modal-close')?.addEventListener('click', closeGifModal);
  $('gif-modal')?.addEventListener('click', (e) => {
    if (e.target === $('gif-modal')) closeGifModal();
  });
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && $('gif-modal') && !$('gif-modal').classList.contains('hidden')) {
      e.preventDefault();
      closeGifModal();
      return;
    }
    if (state.tab !== 'graph' || !state.snap || e.target.matches('input, select, textarea, button, [role="tab"]')) return;
    if (e.key === ' ') { e.preventDefault(); replayToggle(); }
    else if (e.key === 'ArrowLeft') replaySeek((replay.idx ?? graphMsgs().length) - 1);
    else if (e.key === 'ArrowRight') replaySeek((replay.idx ?? graphMsgs().length) + 1);
  });
  let resizeRaf = 0;
  let lastBoardW = 0;
  let lastBoardH = 0;
  let lastGraphW = 0;
  function scheduleLayoutReflow() {
    cancelAnimationFrame(resizeRaf);
    resizeRaf = requestAnimationFrame(() => {
      resizeRaf = 0;
      if (!state.snap || state.view !== 'board') return;
      if (state.tab === 'graph') {
        renderGraph();
      } else if (state.tab === 'feed') {
        measureClamps();
        keepPinned();
        updateJump();
      } else if (typeof state.tab === 'string' && state.tab.startsWith('agent:')) {
        const name = state.tab.slice(6);
        const data = state.agentTabData[name];
        const af = $('agent-feed');
        if (data?.stick && af && !hasSelectionInside(af)) {
          af.scrollTop = af.scrollHeight;
        }
        updateAgentJump(name);
      }
    });
  }
  window.addEventListener('resize', scheduleLayoutReflow, { passive: true });
  if (typeof ResizeObserver === 'function') {
    const boardEl = $('board');
    if (boardEl) {
      new ResizeObserver(() => {
        if (state.view !== 'board') return;
        const w = Math.floor(boardEl.clientWidth);
        const h = Math.floor(boardEl.clientHeight);
        if (w < 40) return;
        if (Math.abs(w - lastBoardW) >= 4 || Math.abs(h - lastBoardH) >= 4) {
          lastBoardW = w;
          lastBoardH = h;
          scheduleLayoutReflow();
        }
      }).observe(boardEl);
    }
    const graphEl = $('graph');
    if (graphEl) {
      new ResizeObserver(() => {
        if (state.view !== 'board' || state.tab !== 'graph' || state.graphView !== 'mentions') return;
        const w = Math.floor(graphEl.clientWidth);
        if (w < 40) return;
        if (Math.abs(w - lastGraphW) >= 4) {
          lastGraphW = w;
          scheduleLayoutReflow();
        }
      }).observe(graphEl);
    }
    if (tabsNav) {
      new ResizeObserver(syncTabsOverflow).observe(tabsNav);
    }
  }
  document.fonts?.ready?.then(() => {
    if (state.snap && feedShown()) measureClamps();
  });

  if (urlTabChosen) selectTab(initialTab, false, true);
  if (params.get('replay'))
    replay.idx = parseInt(params.get('replay'), 10) || 0;
  $('replay-play').innerHTML = ICON.play;
  $('replay-play').dataset.icon = 'play';
  poll();
  setInterval(() => {
    if (!state.snap || state.view !== 'board' || document.hidden) return;
    renderMeta();
    if (state.tab === 'members') renderMembers();
    if (state.tab === 'overview') renderOverviewChrome();
  }, 5000);
})();
