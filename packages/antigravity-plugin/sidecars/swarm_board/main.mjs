/**
 * @fileoverview Hermes Swarm read-only observer sidecar backend (1:1 session-bound).
 *
 * Uses Antigravity `SidecarApp` (`app.page`, `app.api`, `app.sendMessage`) cleanly
 * with zero HTTP listener hijacking, zero Python subprocess fallbacks, and zero
 * mutation/POST proxy routes. Serves ONLY:
 *   - `GET /` (board_ui/index.html with relative `./preload.js`)
 *   - Static UI assets from `board_ui/` (`styles.css`, `hive.css`, `app.js`,
 *     `hive.js`, `gif.js`, `channels.mjs`) plus `/preload.js` via `SidecarApp`
 *   - `GET /api/swarm` (ETag-aware, strictly 1:1 `conversationId` scoped)
 *   - `GET /api/events` (read-only buffered SSE snapshot/update, strictly 1:1 `conversationId` scoped;
 *     `sidecar_sdk` buffers `Response.body` and calls `res.end()`, so persistent chunked SSE streaming is not supported)
 *   - `GET /api/frame` (proxying `GET /board/frame` with strict `conversationId` + path validation)
 *   - `GET /api/member/history` (proxying `GET /board/member/history` with strict 1:1 `conversationId` + `member` validation)
 *
 * `agent_permissions.allows_user_interactions: true` in `sidecar.json` is used
 * strictly by `wakeup_bridge.mjs` via `app.sendMessage`.
 */

import { existsSync, readFileSync, realpathSync, statSync } from 'node:fs';
import { homedir } from 'node:os';
import { dirname, extname, join, resolve } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { createCursorStore, createWakeupBridge, defaultCursorFilePath } from './wakeup_bridge.mjs';

const __dirname = dirname(fileURLToPath(import.meta.url));
const DEFAULT_BOARD_URL = 'http://127.0.0.1:9910';
const BOARD_FETCH_TIMEOUT_MS = 6000;
const MAX_FRAME_BYTES = 8 * 1024 * 1024;
const MAX_SWARM_JSON_BYTES = 16 * 1024 * 1024;
const MAX_CONV_CACHE_ENTRIES = 100;
const CONV_CACHE_TTL_MS = 6 * 60 * 60 * 1000;
const HOST_RE = /^[a-z0-9-]+(?:\.[a-z0-9-]+)+$/i;
const DEFAULT_URL_HOSTS = ['docs.google.com'];
const ALLOWED_FRAME_EXTS = new Set(['.png', '.jpg', '.jpeg', '.webp', '.gif']);
const ALLOWED_FRAME_MIMES = new Set([
  'image/png',
  'image/jpeg',
  'image/webp',
  'image/gif',
]);

const STATIC_ASSETS = {
  '/styles.css': ['styles.css', 'text/css; charset=utf-8'],
  '/hive.css': ['hive.css', 'text/css; charset=utf-8'],
  '/app.js': ['app.js', 'application/javascript; charset=utf-8'],
  '/hive.js': ['hive.js', 'application/javascript; charset=utf-8'],
  '/gif.js': ['gif.js', 'application/javascript; charset=utf-8'],
  '/channels.mjs': ['channels.mjs', 'application/javascript; charset=utf-8'],
  '/narrator_bootstrap.js': ['narrator_bootstrap.js', 'text/plain; charset=utf-8'],
};

const lastSwarmByConv = new Map(); // conv -> { value, ts }
const lastGoodSwarmByConv = new Map(); // cacheKey -> { value, ts }
const tokenFileCache = new Map(); // filePath -> { mtimeMs, token }

function setConvCache(map, key, value) {
  if (!key) return;
  const now = Date.now();
  if (map.has(key)) map.delete(key);
  map.set(key, { value, ts: now });
  for (const [k, entry] of map.entries()) {
    if (now - entry.ts > CONV_CACHE_TTL_MS || map.size > MAX_CONV_CACHE_ENTRIES) {
      map.delete(k);
    } else {
      break;
    }
  }
}

function getConvCache(map, key) {
  const entry = map.get(key);
  if (!entry) return null;
  if (Date.now() - entry.ts > CONV_CACHE_TTL_MS) {
    map.delete(key);
    return null;
  }
  return entry.value;
}

async function readResponseBytesWithCap(res, maxBytes) {
  const clHeader = res.headers?.get?.('content-length');
  if (clHeader !== null && clHeader !== undefined && clHeader !== '') {
    const cl = Number(clHeader);
    if (Number.isFinite(cl) && cl > maxBytes) {
      try { await res.body?.cancel?.(); } catch {}
      const err = new Error(`Response Content-Length ${cl} exceeds cap ${maxBytes}`);
      err.code = 'PAYLOAD_TOO_LARGE';
      throw err;
    }
  }
  if (res.body && typeof res.body.getReader === 'function') {
    const reader = res.body.getReader();
    const chunks = [];
    let total = 0;
    try {
      while (true) {
        const { value, done } = await reader.read();
        if (done) break;
        const buf = Buffer.isBuffer(value) ? value : Buffer.from(value);
        total += buf.length;
        if (total > maxBytes) {
          try { await reader.cancel(); } catch {}
          const err = new Error(`Response stream exceeded ${maxBytes} bytes`);
          err.code = 'PAYLOAD_TOO_LARGE';
          throw err;
        }
        chunks.push(buf);
      }
      return Buffer.concat(chunks, total);
    } catch (err) {
      try { await reader.cancel(); } catch {}
      throw err;
    }
  }
  if (typeof res.arrayBuffer === 'function') {
    const buf = Buffer.from(await res.arrayBuffer());
    if (buf.length > maxBytes) {
      const err = new Error(`Response buffer ${buf.length} exceeds cap ${maxBytes}`);
      err.code = 'PAYLOAD_TOO_LARGE';
      throw err;
    }
    return buf;
  }
  return null;
}

function escapeHtml(str) {
  return String(str ?? '').replace(/[&<>"']/g, (c) => ({
    '&': '&amp;',
    '<': '&lt;',
    '>': '&gt;',
    '"': '&quot;',
    "'": '&#39;',
  }[c]));
}

/**
 * Load `sidecar_sdk` via Antigravity's module resolution first, falling back
 * to `~/.gemini/antigravity/lib/node_modules/sidecar_sdk/index.mjs` for
 * standalone Node execution and unit tests.
 */
export async function loadSidecarSdk() {
  try {
    return await import('sidecar_sdk');
  } catch {
    const candidates = [
      process.env.HERMES_SWARM_SIDECAR_SDK_PATH,
      resolve(__dirname, '../../../../../antigravity/lib/node_modules/sidecar_sdk/index.mjs'),
      join(homedir(), '.gemini', 'antigravity', 'lib', 'node_modules', 'sidecar_sdk', 'index.mjs'),
    ].filter(Boolean);
    for (const candidate of candidates) {
      if (existsSync(candidate)) {
        return await import(pathToFileURL(candidate).href);
      }
    }
    throw new Error('Unable to locate sidecar_sdk/index.mjs');
  }
}

/** Determine whether this module was executed directly as the process entrypoint. */
export function isEntrypointModule(metaUrl = import.meta.url, argv1 = process.argv[1]) {
  if (!argv1) return false;
  const targetHref = pathToFileURL(resolve(argv1)).href;
  if (metaUrl === targetHref) return true;
  return process.platform === 'win32' && metaUrl.toLowerCase() === targetHref.toLowerCase();
}

/** Probe whether an OS PID is currently alive. */
export function isPidAlive(pid) {
  const num = typeof pid === 'number' ? pid : Number(String(pid ?? '').trim());
  if (!Number.isInteger(num) || num <= 0) return false;
  try {
    process.kill(num, 0);
    return true;
  } catch (err) {
    return err?.code === 'EPERM';
  }
}

/** Normalize Windows, POSIX, `file://`, and WSL `/mnt/<drive>/` paths for comparison. */
export function normalizeWorkspacePath(rawPath) {
  if (!rawPath || typeof rawPath !== 'string') return '';
  let text = rawPath.trim();
  if (!text) return '';
  if (text.startsWith('file://')) {
    try {
      text = fileURLToPath(text);
    } catch {
      text = text.replace(/^file:\/+/, '');
    }
  }
  const wsl = /^[/\\]mnt[/\\]([a-zA-Z])([/\\].*)?$/.exec(text);
  if (wsl) {
    text = `${wsl[1].toUpperCase()}:${wsl[2] || '/'}`;
  }
  text = text.replace(/\\/g, '/').replace(/\/+$/, '');
  if (/^\/[a-zA-Z]:(?:\/|$)/.test(text)) {
    text = text.slice(1);
  }
  if (/^[a-zA-Z]:/.test(text) || process.platform === 'win32') {
    return text.toLowerCase();
  }
  return text;
}

/** Case-aware (on Windows) realpath containment check inside `rootDir`. */
export function isPathWithinRoot(candidatePath, rootDir) {
  if (!candidatePath || !rootDir) return false;
  try {
    const rootReal = realpathSync(rootDir);
    const candReal = realpathSync(candidatePath);
    const normRoot = normalizeWorkspacePath(rootReal);
    const normCand = normalizeWorkspacePath(candReal);
    const prefix = normRoot.endsWith('/') ? normRoot : `${normRoot}/`;
    return normCand === normRoot || normCand.startsWith(prefix);
  } catch {
    return false;
  }
}

/** Validate a frame query `path` before proxying upstream `/board/frame`. */
export function validateFramePath(rawPath) {
  if (typeof rawPath !== 'string') return '';
  const cleaned = rawPath.trim();
  if (!cleaned || cleaned.length > 4096) return '';
  if (/[\0%]/.test(cleaned)) return '';
  const segments = cleaned.split(/[\\/]/);
  if (segments.some((seg) => seg === '..')) return '';
  const ext = extname(cleaned).toLowerCase();
  if (!ALLOWED_FRAME_EXTS.has(ext)) return '';
  return cleaned;
}

/** Load allowed `https:` hosts from `url_hosts.json`, merged with defaults. */
export function loadUrlHosts(filePath = join(__dirname, 'url_hosts.json')) {
  const out = [...DEFAULT_URL_HOSTS];
  const seen = new Set(out);
  try {
    if (filePath && existsSync(filePath)) {
      const parsed = JSON.parse(readFileSync(filePath, 'utf-8'));
      if (Array.isArray(parsed)) {
        for (const item of parsed) {
          if (typeof item !== 'string') continue;
          const host = item.trim().toLowerCase();
          if (host && HOST_RE.test(host) && !seen.has(host)) {
            seen.add(host);
            out.push(host);
          }
        }
      }
    }
  } catch {
    // Fall back to default hosts if url_hosts.json is missing or invalid.
  }
  return out;
}

function hasExplicitFeatureFlag(raw) {
  if (raw === undefined || raw === null) return false;
  const text = String(raw).trim().toLowerCase();
  if (!text || text.startsWith('${flag:')) return false;
  return text === 'true' || text === '1' || text === 'false' || text === '0';
}

function parseFeatureFlag(raw, defaultValue = true) {
  if (raw === undefined || raw === null) return defaultValue;
  const text = String(raw).trim().toLowerCase();
  if (!text || text.startsWith('${flag:')) return defaultValue;
  if (text === 'false' || text === '0' || text === 'off' || text === 'no') return false;
  if (text === 'true' || text === '1' || text === 'on' || text === 'yes') return true;
  return defaultValue;
}

/**
 * Resolve loopback internal bearer token from:
 *   1. `HERMES_A2A_HTTP_TOKEN` env var
 *   2. `HERMES_A2A_TOKEN_FILE` env var (or explicit tokenFile path)
 *   3. `<stateDir>/internal_token` written by `hermes-a2a-mcp`
 */
export function resolveHttpToken(env = process.env, stateDir = '', explicitTokenFile = '') {
  const envToken = String(env?.HERMES_A2A_HTTP_TOKEN || '').trim();
  if (envToken) return envToken;

  const candidates = [];
  const envFile = String(env?.HERMES_A2A_TOKEN_FILE || '').trim();
  if (envFile) candidates.push(resolve(envFile));
  if (explicitTokenFile) candidates.push(resolve(explicitTokenFile));
  if (stateDir) candidates.push(join(stateDir, 'internal_token'));

  for (const filePath of candidates) {
    try {
      if (filePath && existsSync(filePath)) {
        const token = readFileSync(filePath, 'utf-8').trim();
        if (token) return token;
      }
    } catch {
      // Ignore unreadable token file and try next candidate.
    }
  }
  return '';
}

/** Resolve sidecar configuration from environment variables. */
export function createSidecarConfig(env = process.env, options = {}) {
  const platform = options.platform || process.platform;
  const rawBridgeOverride = String(env.HERMES_A2A_BRIDGE_DIR || '').trim();
  const bridgeDir = rawBridgeOverride
    ? resolve(rawBridgeOverride)
    : join(homedir(), 'Tools', 'hermes-a2a-mcp');
  if (rawBridgeOverride && !existsSync(bridgeDir)) {
    throw new Error(`Configured HERMES_A2A_BRIDGE_DIR does not exist: ${bridgeDir}`);
  }

  const uiDir = join(bridgeDir, 'board_ui');
  const rawStateOverride = String(env.HERMES_A2A_STATE_DIR || '').trim();
  const stateDir = rawStateOverride ? resolve(rawStateOverride) : join(bridgeDir, 'state');
  const registryPath = join(stateDir, 'registry.json');
  const tokenFile = String(env.HERMES_A2A_TOKEN_FILE || '').trim() || join(stateDir, 'internal_token');
  const rawBoardUrl = String(env.HERMES_A2A_BOARD_URL || '').trim();
  const fallbackBoardUrl = (rawBoardUrl || DEFAULT_BOARD_URL).replace(/\/+$/, '');
  const urlHosts = Array.isArray(options.urlHosts)
    ? options.urlHosts.map((h) => String(h || '').trim().toLowerCase()).filter((h) => HOST_RE.test(h))
    : loadUrlHosts(join(__dirname, 'url_hosts.json'));

  return {
    platform,
    bridgeDir,
    uiDir,
    stateDir,
    registryPath,
    tokenFile,
    fallbackBoardUrl,
    envHttpToken: String(env?.HERMES_A2A_HTTP_TOKEN || '').trim(),
    hasEnvHttpToken: Boolean(String(env?.HERMES_A2A_HTTP_TOKEN || '').trim()),
    httpToken: resolveHttpToken(env, stateDir, tokenFile),
    urlHosts,
    flags: {
      builtinOverview: parseFeatureFlag(env.SWARM_OVERVIEW, true),
      graphTab: parseFeatureFlag(env.SWARM_GRAPH_TAB, true),
      rawTab: parseFeatureFlag(env.SWARM_RAW_TAB, true),
    },
    explicitFlags: {
      builtinOverview: hasExplicitFeatureFlag(env.SWARM_OVERVIEW),
      graphTab: hasExplicitFeatureFlag(env.SWARM_GRAPH_TAB),
      rawTab: hasExplicitFeatureFlag(env.SWARM_RAW_TAB),
    },
    conversationId: String(env.ANTIGRAVITY_CONVERSATION_ID || '').trim(),
    projectId: String(env.ANTIGRAVITY_PROJECT_ID || '').trim(),
    workspace: String(
      env.ANTIGRAVITY_WORKSPACE_PATH || env.ANTIGRAVITY_WORKSPACE || options.workspace || '',
    ).trim(),
    appDataDirs: Array.isArray(options.appDataDirs) ? options.appDataDirs : undefined,
    isPidAlive: options.isPidAlive || isPidAlive,
  };
}

/**
 * Resolve dynamic upstream bearer token, re-reading `tokenFile` when `mtimeMs`
 * changes (or on `forceRefresh` after a 401) whenever `HERMES_A2A_HTTP_TOKEN`
 * was not explicitly pinned via environment variable.
 */
export function resolveDynamicHttpToken(config, { forceRefresh = false } = {}) {
  const envToken = String(config?.envHttpToken || '').trim();
  if (envToken) return envToken;
  if (config?.hasEnvHttpToken === true && config?.httpToken) return config.httpToken;

  const candidates = [];
  if (config?.tokenFile) candidates.push(resolve(config.tokenFile));
  if (config?.stateDir) candidates.push(join(config.stateDir, 'internal_token'));

  for (const filePath of candidates) {
    try {
      if (!filePath || !existsSync(filePath)) continue;
      const mtimeMs = Number(statSync(filePath).mtimeMs) || 0;
      const cached = tokenFileCache.get(filePath);
      if (!forceRefresh && cached && cached.mtimeMs === mtimeMs && cached.token) {
        return cached.token;
      }
      const token = readFileSync(filePath, 'utf-8').trim();
      if (token) {
        tokenFileCache.set(filePath, { mtimeMs, token });
        return token;
      }
    } catch {
      // Ignore unreadable token file and try next candidate.
    }
  }
  return String(config?.httpToken || '').trim();
}

/** Build server-side upstream headers (token is never exposed to the browser). */
export function buildUpstreamHeaders(config, extra = {}, options = {}) {
  const headers = { ...extra };
  const token = resolveDynamicHttpToken(config, options);
  if (token) {
    headers.Authorization = `Bearer ${token}`;
  }
  return headers;
}

/** Join upstream base URL, pathname, and query string cleanly. */
export function buildTargetUrl(upstreamBaseUrl, pathname, search = '') {
  const base = String(upstreamBaseUrl || DEFAULT_BOARD_URL).replace(/\/+$/, '');
  const cleanPath = pathname.startsWith('/') ? pathname : `/${pathname}`;
  return `${base}${cleanPath}${search || ''}`;
}

/** Extract trusted discovery hints (`conversationId` + host `workspace`, never `?swarm=`). */
export function extractDiscoveryHints(queryOrData = {}, config = {}) {
  const q = queryOrData && typeof queryOrData === 'object' ? queryOrData : {};
  const conversationId = String(
    q.conversationId || q.conversation_id || config.conversationId || '',
  ).trim();
  const workspace = String(config.workspace || '').trim();
  return { conversationId, workspace };
}

function normalizeRegistryEntry(raw) {
  if (!raw || typeof raw !== 'object') return null;
  const httpUrl = String(raw.httpUrl || raw.http_url || raw.url || '').trim().replace(/\/+$/, '');
  if (!/^https?:\/\//i.test(httpUrl)) return null;

  const workspaces = [];
  for (const candidate of [
    raw.workspacePath,
    raw.workspace_path,
    raw.workspace,
    raw.workdir,
    raw.workspaceRoot,
    raw.workspace_root,
    raw.projectDir,
    raw.project_dir,
  ]) {
    if (typeof candidate === 'string' && candidate.trim()) {
      workspaces.push(candidate.trim());
    }
  }
  if (Array.isArray(raw.workspaces)) {
    for (const candidate of raw.workspaces) {
      if (typeof candidate === 'string' && candidate.trim()) {
        workspaces.push(candidate.trim());
      }
    }
  }

  const conversationIds = new Set();
  for (const field of [
    raw.conversationId,
    raw.conversation_id,
    raw.primaryConversationId,
    raw.coordinatorConversationId,
  ]) {
    if (typeof field === 'string' && field.trim()) {
      conversationIds.add(field.trim());
    }
  }
  if (Array.isArray(raw.conversationIds)) {
    for (const c of raw.conversationIds) {
      if (typeof c === 'string' && c.trim()) conversationIds.add(c.trim());
    }
  }

  const tsRaw = raw.updatedAt || raw.updated_at || raw.lastSeenAt || raw.startedAt || raw.createdAt || '';
  const updatedAtMs = Date.parse(String(tsRaw)) || 0;

  return {
    httpUrl,
    pid: raw.pid ?? raw.processId ?? null,
    conversationIds: [...conversationIds],
    workspaces,
    updatedAtMs,
  };
}

/** Read active HTTP server entries from `<STATE_DIR>/registry.json`. */
export function readRegistryEntries(registryPath) {
  if (!registryPath) return [];
  let data;
  try {
    if (!existsSync(registryPath)) return [];
    const text = readFileSync(registryPath, 'utf-8').trim();
    data = text ? JSON.parse(text) : null;
  } catch {
    return [];
  }
  if (!data || typeof data !== 'object') return [];

  const entries = [];
  const pushFromContainer = (container) => {
    if (!container) return;
    if (Array.isArray(container)) {
      for (const item of container) {
        const norm = normalizeRegistryEntry(item);
        if (norm) entries.push(norm);
      }
      return;
    }
    if (typeof container === 'object') {
      for (const item of Object.values(container)) {
        const norm = normalizeRegistryEntry(item);
        if (norm) entries.push(norm);
      }
    }
  };

  if (Array.isArray(data)) {
    pushFromContainer(data);
    return entries;
  }

  let matchedContainer = false;
  for (const bucket of ['servers', 'instances', 'entries']) {
    if (data[bucket] !== undefined) {
      matchedContainer = true;
      pushFromContainer(data[bucket]);
    }
  }
  if (!matchedContainer) {
    const direct = normalizeRegistryEntry(data);
    if (direct) entries.push(direct);
    else pushFromContainer(data);
  }
  return entries;
}

/** Resolve the upstream HTTP server URL from `registry.json` with PID liveness verification. */
export function resolveUpstreamUrl(queryOrData = {}, config = createSidecarConfig()) {
  const hints = extractDiscoveryHints(queryOrData, config);
  const normHintWs = normalizeWorkspacePath(hints.workspace);
  const pidAlive = config.isPidAlive || isPidAlive;

  const liveEntries = readRegistryEntries(config.registryPath).filter((entry) => pidAlive(entry.pid));
  if (liveEntries.length === 0) {
    return config.fallbackBoardUrl || DEFAULT_BOARD_URL;
  }

  const scored = liveEntries.map((entry, idx) => {
    const convMatch = Boolean(
      hints.conversationId && entry.conversationIds.includes(hints.conversationId),
    );
    const wsMatch = Boolean(
      normHintWs && entry.workspaces.some((w) => normalizeWorkspacePath(w) === normHintWs),
    );

    let rank = 0;
    if (convMatch) {
      rank = 20 + (wsMatch ? 2 : 0);
    } else if (wsMatch) {
      rank = 10;
    }

    return { entry, rank, idx };
  });

  scored.sort((a, b) => {
    if (b.rank !== a.rank) return b.rank - a.rank;
    if (b.entry.updatedAtMs !== a.entry.updatedAtMs) return b.entry.updatedAtMs - a.entry.updatedAtMs;
    return a.idx - b.idx;
  });

  return scored[0].entry.httpUrl;
}

/** Return all live upstream HTTP URLs from `registry.json` (or `[fallbackBoardUrl]`). */
export function resolveAllUpstreamUrls(config = createSidecarConfig()) {
  const pidAlive = config.isPidAlive || isPidAlive;
  const liveEntries = readRegistryEntries(config.registryPath).filter((entry) => pidAlive(entry.pid));
  const urls = [];
  const seen = new Set();
  for (const entry of liveEntries) {
    if (entry.httpUrl && !seen.has(entry.httpUrl)) {
      seen.add(entry.httpUrl);
      urls.push(entry.httpUrl);
    }
  }
  if (urls.length === 0) {
    urls.push(config.fallbackBoardUrl || DEFAULT_BOARD_URL);
  }
  return urls;
}

/** Ensure `./preload.js` is injected as a relative path for Desktop and Remote Control parity. */
export function injectPreloadScript(html) {
  if (!html) return html;
  if (/src=["']\.\/preload\.js["']/.test(html)) return html;
  if (/src=["']\/preload\.js["']/.test(html)) {
    return html.replace(/src=(["'])\/preload\.js\1/g, 'src=$1./preload.js$1');
  }
  if (html.includes('</head>')) {
    return html.replace('</head>', '  <script src="./preload.js"></script>\n</head>');
  }
  return `<script src="./preload.js"></script>\n${html}`;
}

/** Verify whether a returned swarm payload belongs strictly to the requested `conversationId`. */
export function isSwarmScopedToRequest(swarmObj, { conversationId = '' } = {}) {
  if (!swarmObj || typeof swarmObj !== 'object') return false;
  const scopedConv = String(conversationId || '').trim();
  if (!scopedConv) return false;

  const meta = swarmObj.meta && typeof swarmObj.meta === 'object' ? swarmObj.meta : {};
  const hasConcreteSwarm = Boolean(
    swarmObj.swarm || swarmObj.swarmId || swarmObj.id || meta.swarmId || meta.id,
  );
  const convSet = new Set();
  for (const field of [
    swarmObj.primaryConversationId,
    swarmObj.coordinatorConversationId,
    swarmObj.conversationId,
    meta.primaryConversationId,
    meta.coordinatorConversationId,
    meta.conversationId,
  ]) {
    if (typeof field === 'string' && field.trim()) convSet.add(field.trim());
  }
  for (const list of [swarmObj.conversationIds, meta.conversationIds]) {
    if (Array.isArray(list)) {
      for (const c of list) {
        if (typeof c === 'string' && c.trim()) convSet.add(c.trim());
      }
    }
  }

  if (!hasConcreteSwarm) {
    const hasMessagesOrTasks = (
      (Array.isArray(swarmObj.messages) && swarmObj.messages.length > 0) ||
      (Array.isArray(swarmObj.tasks) && swarmObj.tasks.length > 0)
    );
    if (hasMessagesOrTasks) {
      return convSet.has(scopedConv);
    }
    return convSet.size === 0 || convSet.has(scopedConv);
  }

  return convSet.has(scopedConv);
}

/** Serve `board_ui/index.html` with relative `./preload.js`. */
export async function readHtmlPage(config, ResponseClass = null, appConfig = {}) {
  const idx = join(config.uiDir, 'index.html');
  let html = '';
  let status = 200;

  if (existsSync(idx)) {
    html = injectPreloadScript(readFileSync(idx, 'utf-8'));
  } else {
    const upstream = resolveUpstreamUrl(
      { conversationId: appConfig?.conversationId || config.conversationId || '' },
      config,
    );
    const uiUrl = buildTargetUrl(upstream, '/board/ui');
    try {
      const firstHeaders = buildUpstreamHeaders(config);
      let res = await fetch(uiUrl, {
        headers: firstHeaders,
        signal: AbortSignal.timeout(3000),
      });
      if (res.status === 401 && !config?.hasEnvHttpToken) {
        const retryHeaders = buildUpstreamHeaders(config, {}, { forceRefresh: true });
        if (retryHeaders.Authorization && retryHeaders.Authorization !== firstHeaders.Authorization) {
          try { await res.body?.cancel?.(); } catch {}
          res = await fetch(uiUrl, {
            headers: retryHeaders,
            signal: AbortSignal.timeout(3000),
          });
        }
      }
      if (!res.ok) {
        try { await res.body?.cancel?.(); } catch {}
        status = res.status >= 500 ? 502 : res.status;
        html = `<h1>Hermes Swarm UI unavailable</h1><pre>Upstream returned HTTP ${escapeHtml(status)}</pre>`;
      } else {
        status = res.status;
        html = injectPreloadScript(await res.text());
      }
    } catch (err) {
      status = 503;
      html = `<h1>Hermes Swarm UI unavailable</h1><pre>${escapeHtml(err?.message || err)}</pre>`;
    }
  }

  if (ResponseClass) {
    return new ResponseClass(html, {
      status,
      contentType: 'text/html; charset=utf-8',
      headers: { 'Cache-Control': 'no-cache' },
    });
  }
  return { status, contentType: 'text/html; charset=utf-8', body: html };
}

/** Serve a static file from `board_ui/` with realpath containment verification. */
export function readStaticAsset(filename, contentType, config, ResponseClass) {
  const candidate = resolve(config.uiDir, filename);
  if (!isPathWithinRoot(candidate, config.uiDir)) {
    return new ResponseClass('Not found', { status: 404, contentType: 'text/plain' });
  }
  try {
    if (!statSync(candidate).isFile()) {
      return new ResponseClass('Not found', { status: 404, contentType: 'text/plain' });
    }
    const buf = readFileSync(candidate);
    return new ResponseClass(buf, {
      status: 200,
      contentType,
      headers: { 'Cache-Control': 'no-cache' },
    });
  } catch {
    return new ResponseClass('Not found', { status: 404, contentType: 'text/plain' });
  }
}

/** `GET /api/swarm` handler: ETag-aware, read-only, strictly 1:1 `conversationId` scoped. */
export async function handleGetSwarm(
  data = {},
  config = createSidecarConfig(),
  appConfig = {},
  fetchImpl = fetch,
) {
  const conv = String(
    data?.conversationId || data?.conversation_id || appConfig?.conversationId || config?.conversationId || '',
  ).trim();
  if (!conv) {
    return { conversationId: null, swarm: null, member: null };
  }

  const requestedEtag = String(data?.etag || '').trim();
  const upstreamBase = resolveUpstreamUrl({ conversationId: conv }, config);
  const cacheKey = conv || upstreamBase;
  const qs = new URLSearchParams({ conversationId: conv });
  if (requestedEtag) qs.set('etag', requestedEtag);
  const targetUrl = buildTargetUrl(upstreamBase, '/board', `?${qs.toString()}`);

  try {
    const reqHeaders = {
      Accept: 'application/json',
      ...(requestedEtag ? { 'If-None-Match': requestedEtag } : {}),
    };
    const firstHeaders = buildUpstreamHeaders(config, reqHeaders);
    let res = await fetchImpl(targetUrl, {
      headers: firstHeaders,
      signal: AbortSignal.timeout(BOARD_FETCH_TIMEOUT_MS),
    });
    if (res.status === 401 && !config?.hasEnvHttpToken) {
      const retryHeaders = buildUpstreamHeaders(config, reqHeaders, { forceRefresh: true });
      if (retryHeaders.Authorization && retryHeaders.Authorization !== firstHeaders.Authorization) {
        try { await res.body?.cancel?.(); } catch {}
        res = await fetchImpl(targetUrl, {
          headers: retryHeaders,
          signal: AbortSignal.timeout(BOARD_FETCH_TIMEOUT_MS),
        });
      }
    }

    if (res.status === 304) {
      try { await res.body?.cancel?.(); } catch {}
      const cachedSwarm = getConvCache(lastSwarmByConv, conv);
      return {
        online: true,
        transientError: false,
        conversationId: conv,
        ...(cachedSwarm ? { swarm: cachedSwarm } : {}),
        unchanged: true,
        etag: res.headers.get('etag') || requestedEtag,
      };
    }

    if (!res.ok) {
      try { await res.body?.cancel?.(); } catch {}
      console.warn(`[swarm_board] GET /board returned HTTP ${res.status} for conversationId=${conv}`);
      const isTransientHttp = res.status >= 500;
      const lastGood = isTransientHttp ? getConvCache(lastGoodSwarmByConv, cacheKey) : null;
      if (lastGood) {
        return {
          ...lastGood,
          online: false,
          transientError: true,
          unchanged: true,
          status: res.status,
          error: `Upstream /board returned HTTP ${res.status}`,
          conversationId: conv,
        };
      }
      return {
        online: false,
        transientError: isTransientHttp,
        status: res.status,
        error: `Upstream /board returned HTTP ${res.status}`,
        conversationId: conv,
        swarm: null,
        member: null,
        members: [],
        messages: [],
      };
    }

    let payload;
    const rawBytes = await readResponseBytesWithCap(res, MAX_SWARM_JSON_BYTES);
    if (rawBytes !== null) {
      payload = JSON.parse(rawBytes.toString('utf-8'));
    } else {
      payload = await res.json();
    }
    if (!payload || typeof payload !== 'object') {
      lastGoodSwarmByConv.delete(cacheKey);
      return {
        online: true,
        transientError: false,
        conversationId: conv,
        swarm: null,
        member: null,
        members: [],
        messages: [],
      };
    }

    if (payload.unchanged === true) {
      const meta = payload.meta && typeof payload.meta === 'object' ? payload.meta : {};
      const hasAnyConvId = Boolean(
        payload.primaryConversationId
        || payload.coordinatorConversationId
        || payload.conversationId
        || meta.primaryConversationId
        || meta.coordinatorConversationId
        || meta.conversationId
        || (Array.isArray(payload.conversationIds) && payload.conversationIds.length > 0)
        || (Array.isArray(meta.conversationIds) && meta.conversationIds.length > 0)
      );
      if (hasAnyConvId) {
        if (!isSwarmScopedToRequest(payload, { conversationId: conv })) {
          lastGoodSwarmByConv.delete(cacheKey);
          return {
            online: true,
            transientError: false,
            conversationId: conv,
            swarm: null,
            member: null,
            members: [],
            messages: [],
          };
        }
      } else {
        const cachedSwarm = getConvCache(lastSwarmByConv, conv);
        const payloadSwarm = String(payload.swarm || payload.swarmId || payload.id || meta.swarmId || meta.id || '').trim();
        if (!cachedSwarm || !payloadSwarm || cachedSwarm !== payloadSwarm) {
          lastGoodSwarmByConv.delete(cacheKey);
          return {
            online: true,
            transientError: false,
            conversationId: conv,
            swarm: null,
            member: null,
            members: [],
            messages: [],
          };
        }
      }
      const { swarms: _ignoredSwarms, ...rest } = payload;
      return {
        ...rest,
        online: true,
        transientError: false,
        conversationId: conv,
      };
    }

    if (!isSwarmScopedToRequest(payload, { conversationId: conv })) {
      lastGoodSwarmByConv.delete(cacheKey);
      return {
        online: true,
        transientError: false,
        conversationId: conv,
        swarm: null,
        member: null,
        members: [],
        messages: [],
      };
    }

    const { swarms: _ignoredSwarms, ...rest } = payload;
    const swarmId = rest.swarm || rest.id || rest.meta?.swarmId || null;

    const memberVal = rest.member ?? null;
    const headerEtag = res.headers.get('etag') || '';
    const resolvedEtag = rest.etag
      || (swarmId && headerEtag && !headerEtag.includes('|') ? `${swarmId}|${memberVal || ''}|${headerEtag}` : headerEtag)
      || undefined;

    const mergedUrlHosts = [
      ...new Set([
        ...(Array.isArray(rest.urlHosts) ? rest.urlHosts : []),
        ...(Array.isArray(config?.urlHosts) ? config.urlHosts : DEFAULT_URL_HOSTS),
      ]),
    ];

    const flags = config?.flags || { builtinOverview: true, graphTab: true, rawTab: true };
    const explicit = config?.explicitFlags || {};
    const builtinOverview = explicit.builtinOverview
      ? flags.builtinOverview
      : (rest.builtinOverview !== undefined ? rest.builtinOverview : flags.builtinOverview);
    const graphTab = explicit.graphTab
      ? flags.graphTab
      : (rest.graphTab !== undefined ? rest.graphTab : flags.graphTab);
    const rawTab = explicit.rawTab
      ? flags.rawTab
      : (rest.rawTab !== undefined ? rest.rawTab : flags.rawTab);

    const allowedFiles = (
      rest.allowedFiles && typeof rest.allowedFiles === 'object' && !Array.isArray(rest.allowedFiles)
    ) ? rest.allowedFiles : {};

    const result = {
      ...rest,
      ...(resolvedEtag ? { etag: resolvedEtag } : {}),
      urlHosts: mergedUrlHosts,
      allowedFiles,
      builtinOverview,
      graphTab,
      rawTab,
      online: true,
      transientError: false,
      conversationId: conv,
      member: memberVal,
    };

    if (swarmId) {
      setConvCache(lastSwarmByConv, conv, String(swarmId));
      setConvCache(lastGoodSwarmByConv, cacheKey, result);
    } else {
      lastSwarmByConv.delete(conv);
      lastGoodSwarmByConv.delete(cacheKey);
    }

    return result;
  } catch (err) {
    const isTimeout = err?.name === 'TimeoutError'
      || err?.name === 'AbortError'
      || /timeout/i.test(String(err?.message || ''));
    const status = err?.code === 'PAYLOAD_TOO_LARGE' ? 502 : (isTimeout ? 504 : 503);
    const errorMsg = err?.message ? String(err.message) : 'Upstream /board request failed';
    const lastGood = getConvCache(lastGoodSwarmByConv, cacheKey);
    if (lastGood) {
      return {
        ...lastGood,
        online: false,
        transientError: true,
        unchanged: true,
        status,
        error: errorMsg,
        conversationId: conv,
      };
    }
    return {
      online: false,
      transientError: true,
      status,
      error: errorMsg,
      conversationId: conv,
      swarm: null,
      member: null,
      members: [],
      messages: [],
    };
  }
}

/**
 * `GET /api/events` handler: read-only, strictly 1:1 `conversationId` scoped SSE response.
 * Note: `SidecarApp` (`sidecar_sdk/index.mjs`) buffers `Response.body` and immediately calls
 * `res.end()`, so this returns a valid single-burst SSE payload (`text/event-stream`) rather
 * than a long-lived chunked stream.
 */
export async function handleGetEvents(
  data = {},
  config = createSidecarConfig(),
  appConfig = {},
  ResponseClass = null,
  fetchImpl = fetch,
) {
  const payload = await handleGetSwarm(data, config, appConfig, fetchImpl);
  const head = Number.isFinite(Number(payload?.head)) ? Number(payload.head) : 0;
  const eventName = !payload?.swarm
    ? 'empty'
    : payload?.unchanged
      ? 'unchanged'
      : (data?.etag ? 'update' : 'snapshot');
  const retryMs = payload?.swarm ? 1000 : 3000;
  const sseBody =
    `retry: ${retryMs}\n` +
    `id: ${head}\n` +
    `event: ${eventName}\n` +
    `data: ${JSON.stringify(payload)}\n\n`;
  const headers = {
    'Cache-Control': 'no-cache',
    'X-Accel-Buffering': 'no',
  };
  if (ResponseClass) {
    return new ResponseClass(sseBody, {
      status: 200,
      contentType: 'text/event-stream; charset=utf-8',
      headers,
    });
  }
  return {
    status: 200,
    contentType: 'text/event-stream; charset=utf-8',
    headers,
    body: sseBody,
  };
}

/** `GET /api/frame` handler: proxies `GET /board/frame` with strict `conversationId` + path validation. */
export async function handleGetFrame(
  data = {},
  config = createSidecarConfig(),
  appConfig = {},
  ResponseClass,
  fetchImpl = fetch,
) {
  const notFound = () => new ResponseClass('Not found', { status: 404, contentType: 'text/plain' });

  const conv = String(
    data?.conversationId || data?.conversation_id || appConfig?.conversationId || config?.conversationId || '',
  ).trim();
  const cleanPath = validateFramePath(data?.path);
  if (!conv || !cleanPath) return notFound();

  const upstreamBase = resolveUpstreamUrl({ conversationId: conv }, config);
  const qs = new URLSearchParams({ conversationId: conv, path: cleanPath });
  const rawSeq = String(data?.seq ?? '').trim();
  if (rawSeq && /^\d+$/.test(rawSeq)) {
    qs.set('seq', rawSeq);
  }

  try {
    const frameUrl = buildTargetUrl(upstreamBase, '/board/frame', `?${qs.toString()}`);
    const firstHeaders = buildUpstreamHeaders(config);
    let res = await fetchImpl(frameUrl, {
      headers: firstHeaders,
      signal: AbortSignal.timeout(5000),
    });
    if (res.status === 401 && !config?.hasEnvHttpToken) {
      const retryHeaders = buildUpstreamHeaders(config, {}, { forceRefresh: true });
      if (retryHeaders.Authorization && retryHeaders.Authorization !== firstHeaders.Authorization) {
        try { await res.body?.cancel?.(); } catch {}
        res = await fetchImpl(frameUrl, {
          headers: retryHeaders,
          signal: AbortSignal.timeout(5000),
        });
      }
    }
    if (!res.ok) {
      try { await res.body?.cancel?.(); } catch {}
      console.warn(
        `[swarm_board] GET /board/frame returned HTTP ${res.status} for conversationId=${conv} path=${cleanPath}`,
      );
      const status = res.status === 401 || res.status === 403
        ? res.status
        : (res.status >= 500 ? 502 : 404);
      const msg = res.status === 401
        ? 'Unauthorized'
        : (res.status === 403 ? 'Forbidden' : (res.status >= 500 ? 'Upstream error' : 'Not found'));
      return new ResponseClass(msg, {
        status,
        contentType: 'text/plain',
      });
    }
    const rawMime = String(res.headers.get('content-type') || '').split(';')[0].trim().toLowerCase();
    if (!ALLOWED_FRAME_MIMES.has(rawMime)) {
      try { await res.body?.cancel?.(); } catch {}
      return notFound();
    }

    const buf = await readResponseBytesWithCap(res, MAX_FRAME_BYTES);
    if (!buf || buf.length === 0 || buf.length > MAX_FRAME_BYTES) return notFound();

    return new ResponseClass(buf, {
      status: 200,
      contentType: rawMime,
      headers: { 'Cache-Control': 'private, max-age=3600' },
    });
  } catch (err) {
    if (err?.code === 'PAYLOAD_TOO_LARGE') {
      return new ResponseClass('Payload too large', { status: 413, contentType: 'text/plain' });
    }
    return new ResponseClass('Frame unavailable', { status: 503, contentType: 'text/plain' });
  }
}

/** `GET /api/member/history` handler: proxies `GET /board/member/history` with strict 1:1 `conversationId` + `member` validation. */
export async function handleGetMemberHistory(
  data = {},
  config = createSidecarConfig(),
  appConfig = {},
  ResponseClass = null,
  fetchImpl = fetch,
) {
  const jsonReply = (obj, status = 200) => {
    if (typeof ResponseClass === 'function') {
      return new ResponseClass(JSON.stringify(obj), {
        status,
        contentType: 'application/json; charset=utf-8',
      });
    }
    return obj;
  };

  const conv = String(
    data?.conversationId || data?.conversation_id || appConfig?.conversationId || config?.conversationId || '',
  ).trim();
  const member = String(data?.member || data?.agent || '').trim();
  if (!conv || !member) {
    return jsonReply({ ok: false, error: 'conversationId and member are required' }, 400);
  }

  const upstreamBase = resolveUpstreamUrl({ conversationId: conv }, config);
  const qs = new URLSearchParams({ conversationId: conv, member });

  const afterId = data?.after_id ?? data?.afterId;
  if (afterId !== undefined && afterId !== null && String(afterId).trim() !== '') {
    qs.set('after_id', String(afterId).trim());
  }
  const rawLimit = String(data?.limit ?? '').trim();
  if (rawLimit && /^\d+$/.test(rawLimit)) {
    qs.set('limit', rawLimit);
  }
  const subagentSessionId = data?.subagent_session_id ?? data?.subagentSessionId;
  if (subagentSessionId !== undefined && subagentSessionId !== null && String(subagentSessionId).trim() !== '') {
    qs.set('subagent_session_id', String(subagentSessionId).trim());
  }

  try {
    const historyUrl = buildTargetUrl(upstreamBase, '/board/member/history', `?${qs.toString()}`);
    const firstHeaders = buildUpstreamHeaders(config);
    let res = await fetchImpl(historyUrl, {
      headers: firstHeaders,
      signal: AbortSignal.timeout(BOARD_FETCH_TIMEOUT_MS),
    });
    if (res.status === 401 && !config?.hasEnvHttpToken) {
      const retryHeaders = buildUpstreamHeaders(config, {}, { forceRefresh: true });
      if (retryHeaders.Authorization && retryHeaders.Authorization !== firstHeaders.Authorization) {
        try { await res.body?.cancel?.(); } catch {}
        res = await fetchImpl(historyUrl, {
          headers: retryHeaders,
          signal: AbortSignal.timeout(BOARD_FETCH_TIMEOUT_MS),
        });
      }
    }
    if (!res.ok) {
      try { await res.body?.cancel?.(); } catch {}
      console.warn(
        `[swarm_board] GET /board/member/history returned HTTP ${res.status} for conversationId=${conv} member=${member}`,
      );
      const status = res.status === 401 || res.status === 403
        ? res.status
        : (res.status >= 500 ? 502 : res.status);
      return jsonReply(
        {
          ok: false,
          status: res.status,
          error: `Upstream /board/member/history returned HTTP ${res.status}`,
          conversationId: conv,
          member,
          events: [],
        },
        status,
      );
    }

    let payload;
    const rawBytes = await readResponseBytesWithCap(res, MAX_SWARM_JSON_BYTES);
    if (rawBytes !== null) {
      payload = JSON.parse(rawBytes.toString('utf-8'));
    } else {
      payload = await res.json();
    }
    if (!payload || typeof payload !== 'object' || !isSwarmScopedToRequest(payload, { conversationId: conv })) {
      return jsonReply(
        {
          ok: false,
          error: 'Member history payload failed conversation scope validation',
          conversationId: conv,
          member,
          events: [],
        },
        403,
      );
    }

    return jsonReply(payload, 200);
  } catch (err) {
    if (err?.code === 'PAYLOAD_TOO_LARGE') {
      return jsonReply({ ok: false, error: 'Payload too large', conversationId: conv, member, events: [] }, 413);
    }
    return jsonReply(
      { ok: false, error: err?.message || 'Member history unavailable', conversationId: conv, member, events: [] },
      503,
    );
  }
}

/** Boot `SidecarApp`, register clean GET-only routes, and start the host-local wakeup bridge. */
export async function startSidecar(env = process.env, options = {}) {
  const config = createSidecarConfig(env, options);
  const { SidecarApp, Response } = await loadSidecarSdk();
  const app = new SidecarApp();

  let wakeupBridge = null;

  app.page('/', () => readHtmlPage(config, Response, app.config));

  for (const [route, [filename, mime]] of Object.entries(STATIC_ASSETS)) {
    app.api(route, () => readStaticAsset(filename, mime, config, Response), 'GET');
  }

  app.api('/api/swarm', async (data) => {
    const conv = String(
      data?.conversationId || data?.conversation_id || app.config?.conversationId || config?.conversationId || '',
    ).trim();
    if (conv) {
      wakeupBridge?.registerConversation(conv);
    }
    return await handleGetSwarm(data, config, app.config);
  }, 'GET');
  app.api('/api/events', async (data) => {
    const conv = String(
      data?.conversationId || data?.conversation_id || app.config?.conversationId || config?.conversationId || '',
    ).trim();
    if (conv) {
      wakeupBridge?.registerConversation(conv);
    }
    return await handleGetEvents(data, config, app.config, Response);
  }, 'GET');
  app.api('/api/frame', (data) => handleGetFrame(data, config, app.config, Response), 'GET');
  app.api('/api/member/history', (data) => handleGetMemberHistory(data, config, app.config, Response), 'GET');

  const server = await app.run();

  if (typeof app.sendMessage === 'function' && env.HERMES_SWARM_DISABLE_WAKEUP !== '1') {
    const cursorFile = String(env.HERMES_SWARM_CURSOR_FILE || '').trim() || defaultCursorFilePath(env);
    const appDataDirs = Array.isArray(options?.appDataDirs) ? options.appDataDirs : config.appDataDirs;
    wakeupBridge = createWakeupBridge({
      sendMessage: (convId, message, opts) => Promise.resolve(app.sendMessage(convId, message, opts)),
      config: {
        ...config,
        conversationId: app.config?.conversationId || config.conversationId,
        projectId: app.config?.projectId || config.projectId,
      },
      env,
      hostLocalMode: true,
      appDataDirs,
      resolveUpstream: (hints, cfg) => resolveUpstreamUrl(hints, cfg),
      resolveAllUpstreams: (cfg) => resolveAllUpstreamUrls(cfg),
      cursorStore: createCursorStore(cursorFile),
    });
    wakeupBridge.start();
    server.on('close', () => wakeupBridge?.stop());
  }

  let shuttingDown = false;
  const shutdown = (signalName) => {
    if (shuttingDown) return;
    shuttingDown = true;
    try { wakeupBridge?.stop(); } catch {}
    try { server?.close?.(); } catch {}
    if (signalName) process.exit(0);
  };
  process.on('SIGINT', () => shutdown('SIGINT'));
  process.on('SIGTERM', () => shutdown('SIGTERM'));
  process.on('unhandledRejection', (err) => {
    console.error('[swarm_board] unhandledRejection:', err?.stack || err);
  });

  return { app, server, config, wakeupBridge };
}

export async function main() {
  return await startSidecar(process.env);
}

if (isEntrypointModule()) {
  await main();
}
