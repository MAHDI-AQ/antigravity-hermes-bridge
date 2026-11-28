/**
 * @fileoverview Tier-aware wakeup bridge for Hermes Swarm.
 *
 * Supports two modes:
 *   1. Host-local multi-conversation mode (`hostLocalMode: true`, default in `main.mjs`):
 *      Starts unconditionally at sidecar boot even when `ANTIGRAVITY_CONVERSATION_ID`
 *      is empty and before the UI pane is opened. Subscribes to upstream
 *      `GET /internal/wake/events` SSE (Bearer auth, backoff reconnect) and runs a
 *      5s fallback reconcile on `GET /internal/wake/swarms` -> `GET /board?conversationId=...`
 *      for every active swarm whose `primaryConversationId` or `coordinatorConversationId`
 *      is local to this Antigravity host (`<appData>/brain/<convId>` across
 *      `findAppDataDirs()`, `config.conversationId`, or `registerConversation(convId)`).
 *   2. Single-conversation mode (`hostLocalMode: false` with `config.conversationId`):
 *      Preserves 1:1 session-scoped behavior on `GET /board/events?conversationId=...`
 *      and `GET /board?conversationId=...` (10s fallback reconcile).
 *
 * Strict 3-tier isolation:
 *   - Tier-2 workers -> Tier-2 Coordinator (`coordinatorConversationId` ONLY;
 *     never falls back to `primaryConversationId`).
 *   - Tier-2 Coordinator escalations (`@primary` / `kind="escalation"`) ->
 *     Primary (`primaryConversationId` ONLY).
 *   - Tier-3 leaf subagents -> never wake Tier 1 or Tier 2 directly.
 *
 * Cursor durability & multi-window dedup:
 *   `cursor read -> await sendMessage(...) -> cursor write` is guarded by an
 *   atomic lockfile (`<cursorFile>.lock` via `wx` + stale PID/age > 15s eviction)
 *   and cursors advance ONLY AFTER `sendMessage` resolves.
 */

import {
  existsSync,
  mkdirSync,
  readdirSync,
  readFileSync,
  renameSync,
  statSync,
  unlinkSync,
  writeFileSync,
} from 'node:fs';
import { homedir } from 'node:os';
import { basename, dirname, join, resolve } from 'node:path';

const DEFAULT_RECONCILE_MS = 10_000;
const DEFAULT_HOST_LOCAL_RECONCILE_MS = 5_000;
const DEFAULT_CONNECT_TIMEOUT_MS = 5_000;
const DEFAULT_FETCH_TIMEOUT_MS = 6_000;
const TRANSIENT_LOG_THROTTLE_MS = 60_000;
const MAX_SSE_BACKOFF_MS = 5_000;
const MIN_HEALTHY_SSE_MS = 5_000;
const DEFAULT_SSE_IDLE_TIMEOUT_MS = 45_000;
const MAX_SSE_BUFFER_BYTES = 2 * 1024 * 1024;
const DEFAULT_STALE_LOCK_MS = 15_000;
const DEFAULT_LIVE_LOCK_CEILING_MS = 20_000;
const DEFAULT_LOCK_WAIT_TIMEOUT_MS = 25_000;
const DEFAULT_LOCK_RETRY_MS = 40;
const COMPLETION_DEDUP_WINDOW_MS = 2_000;
const MAX_CACHED_SWARMS = 200;
const ABSENT_SWARM_PRUNE_TICKS = 12;

const ESCALATION_WAKE_KINDS = new Set([
  'question',
  'blocker',
  'escalation',
  'approval_request',
]);
const HANDOFF_WAKE_KINDS = new Set([
  'handoff',
  'handoff_ready',
]);

const MENTION_TOKEN_RE = /(?:^|[^\p{L}\p{Nd}_-])@([a-z0-9][a-z0-9_-]*)(?=$|[^\p{L}\p{Nd}_-])/giu;
const tokenFileCache = new Map(); // filePath -> { mtimeMs, token }

function logError(logger, ...args) {
  if (typeof logger?.error === 'function') {
    logger.error(...args);
  } else if (typeof logger?.warn === 'function') {
    logger.warn(...args);
  } else {
    console.error(...args);
  }
}

function logWarn(logger, ...args) {
  if (typeof logger?.warn === 'function') {
    logger.warn(...args);
  } else if (typeof logger?.error === 'function') {
    logger.error(...args);
  } else {
    console.warn(...args);
  }
}

function logInfo(logger, ...args) {
  if (typeof logger?.info === 'function') {
    logger.info(...args);
  } else if (typeof logger?.log === 'function') {
    logger.log(...args);
  }
}

function defaultIsPidAlive(pid) {
  const num = typeof pid === 'number' ? pid : Number(String(pid ?? '').trim());
  if (!Number.isInteger(num) || num <= 0) return false;
  try {
    process.kill(num, 0);
    return true;
  } catch (err) {
    return err?.code === 'EPERM';
  }
}

function resolveLazyToken(config, { forceRefresh = false, skipEnvToken = false } = {}) {
  if (!skipEnvToken) {
    const envToken = String(config?.envHttpToken || '').trim();
    if (envToken) return envToken;
    if (config?.hasEnvHttpToken === true && config?.httpToken) return config.httpToken;
  }

  const candidates = [];
  if (config?.tokenFile) candidates.push(resolve(config.tokenFile));
  if (config?.stateDir) candidates.push(join(config.stateDir, 'internal_token'));
  for (const filePath of candidates) {
    try {
      if (filePath && existsSync(filePath)) {
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
      }
    } catch {
      // Ignore unreadable token file.
    }
  }
  return skipEnvToken ? '' : String(config?.httpToken || '').trim();
}

function buildHeaders(config, extra = {}, options = {}) {
  const headers = { ...extra };
  const token = resolveLazyToken(config, options);
  if (token) {
    headers.Authorization = `Bearer ${token}`;
  }
  return headers;
}

function buildUrl(baseUrl, pathname, search = '') {
  const base = String(baseUrl || 'http://127.0.0.1:9910').replace(/\/+$/, '');
  const cleanPath = pathname.startsWith('/') ? pathname : `/${pathname}`;
  return `${base}${cleanPath}${search || ''}`;
}

function extractInlineMentions(text) {
  const out = new Set();
  const src = String(text || '');
  let match;
  MENTION_TOKEN_RE.lastIndex = 0;
  while ((match = MENTION_TOKEN_RE.exec(src)) !== null) {
    out.add(match[1].toLowerCase());
  }
  return out;
}

function collectSwarmConversationIds(swarm) {
  const convCandidates = new Set();
  if (!swarm || typeof swarm !== 'object') return convCandidates;
  for (const field of [
    swarm.primaryConversationId,
    swarm.coordinatorConversationId,
    swarm.conversationId,
    swarm.meta?.primaryConversationId,
    swarm.meta?.coordinatorConversationId,
    swarm.meta?.conversationId,
  ]) {
    if (typeof field === 'string' && field.trim()) {
      convCandidates.add(field.trim());
    }
  }
  for (const list of [swarm.conversationIds, swarm.meta?.conversationIds]) {
    if (Array.isArray(list)) {
      for (const c of list) {
        if (typeof c === 'string' && c.trim()) convCandidates.add(c.trim());
      }
    }
  }
  return convCandidates;
}

/**
 * Locate local Antigravity appData directories containing `brain/`.
 * Checks `ANTIGRAVITY_EXECUTABLE_DATA_DIR` ancestry first, then falls back to `~/.gemini/<product>/brain`.
 */
export function findAppDataDirs(env = process.env, overrideDirs = null) {
  if (Array.isArray(overrideDirs)) {
    return overrideDirs.map((d) => String(d || '').trim()).filter(Boolean);
  }
  const dataDir = String(env?.ANTIGRAVITY_EXECUTABLE_DATA_DIR || '').trim();
  if (dataDir) {
    let cur = resolve(dataDir);
    while (true) {
      if (basename(cur) === 'sidecar_data') {
        const appData = dirname(cur);
        return existsSync(join(appData, 'brain')) ? [appData] : [];
      }
      const parent = dirname(cur);
      if (parent === cur) break;
      cur = parent;
    }
    return [];
  }
  const root = join(homedir(), '.gemini');
  let entries = [];
  try {
    entries = readdirSync(root).sort();
  } catch {
    return [];
  }
  return entries
    .map((name) => join(root, name))
    .filter((candidate) => existsSync(join(candidate, 'brain')));
}

/**
 * Determine whether a `conversationId` belongs to this local Antigravity host.
 */
export function isConversationLocal(convId, options = {}) {
  const clean = String(convId || '').trim();
  if (!clean || clean === '.' || clean === '..' || /[\\/]/.test(clean)) return false;
  if (options.allowUnscoped === true) return true;

  const scoped = String(options.conversationId || '').trim();
  if (scoped && clean === scoped) return true;

  const registered = options.registeredConversations;
  if (registered instanceof Set && registered.has(clean)) return true;

  const dirs = findAppDataDirs(options.env || process.env, options.appDataDirs);
  for (const appData of dirs) {
    if (existsSync(join(appData, 'brain', clean))) return true;
  }
  return false;
}

/**
 * Determine whether a swarm belongs strictly to a single `conversationId` (1:1 mode).
 */
export function isSwarmInScope(swarm, scopeOrConv) {
  if (!swarm || typeof swarm !== 'object') return false;
  const scopedConv = typeof scopeOrConv === 'string'
    ? scopeOrConv.trim()
    : String(scopeOrConv?.conversationId || '').trim();
  if (!scopedConv) return false;
  return collectSwarmConversationIds(swarm).has(scopedConv);
}

/**
 * Classify a board message into its wakeup target tier:
 *   - 'coordinator': Tier-2 worker or Primary `@coordinator` event needing Coordinator attention
 *   - 'primary': Tier-2 Coordinator escalation needing Primary attention
 *   - null: no wakeup (including all Tier-3 subagent activity)
 */
export function classifyBoardMessage(msg) {
  if (!msg || typeof msg !== 'object') return null;

  const status = String(msg.status || 'open').trim().toLowerCase();
  if (status !== 'open') return null;

  const role = String(msg.senderRole || msg.role || '').trim().toLowerCase();
  const tier = Number(msg.senderTier || 0);
  if (role === 'subagent' || tier === 3) return null;

  const sender = String(msg.sender || msg.agent || msg.from || '').trim().toLowerCase();
  const kind = String(msg.kind || msg.type || '').trim().toLowerCase();
  const summary = String(msg.summary || msg.text || '').trim();
  const summaryLower = summary.toLowerCase();

  const mentions = new Set();
  const rawMentions = Array.isArray(msg.mentions)
    ? msg.mentions
    : (Array.isArray(msg.to) ? msg.to : []);
  for (const m of rawMentions) {
    const clean = String(m || '').replace(/^@/, '').trim().toLowerCase();
    if (clean) mentions.add(clean);
  }
  for (const inline of extractInlineMentions(summary)) {
    mentions.add(inline);
  }

  if (role === 'coordinator' || sender === 'coordinator') {
    if (kind === 'escalation' || mentions.has('primary')) {
      return 'primary';
    }
    return null;
  }

  if (role === 'primary' || sender === 'primary') {
    return mentions.has('coordinator') || mentions.has('all') ? 'coordinator' : null;
  }

  if (mentions.has('coordinator') || mentions.has('all')) return 'coordinator';
  if (ESCALATION_WAKE_KINDS.has(kind)) return 'coordinator';
  if (summaryLower.startsWith('[stall]')) return 'coordinator';
  if (HANDOFF_WAKE_KINDS.has(kind) || summaryLower.startsWith('[done]')) {
    return mentions.size === 0 ? 'coordinator' : null;
  }
  return null;
}

/**
 * Parse a `text/event-stream` ReadableStream / Web ReadableStream, invoking
 * `onEvent({ event, data, id })` for every complete SSE frame.
 */
export async function consumeSseStream(
  body,
  onEvent,
  signal,
  {
    idleTimeoutMs = DEFAULT_SSE_IDLE_TIMEOUT_MS,
    maxBufferBytes = MAX_SSE_BUFFER_BYTES,
  } = {},
) {
  if (!body || typeof body.getReader !== 'function') return;
  const reader = body.getReader();
  const decoder = new TextDecoder('utf-8');
  let buffer = '';
  let eventName = 'message';
  let eventId = '';
  let dataLines = [];
  const effectiveIdleMs = Number(idleTimeoutMs) > 0 ? Number(idleTimeoutMs) : 0;
  const effectiveMaxBytes = Number(maxBufferBytes) > 0 ? Number(maxBufferBytes) : MAX_SSE_BUFFER_BYTES;

  const flush = async () => {
    if (dataLines.length === 0) {
      eventName = 'message';
      eventId = '';
      return;
    }
    const rawData = dataLines.join('\n');
    const frame = { event: eventName || 'message', data: rawData, id: eventId };
    eventName = 'message';
    eventId = '';
    dataLines = [];
    await onEvent(frame);
  };

  const readNextChunk = async () => {
    if (!effectiveIdleMs) return await reader.read();
    let timer = null;
    try {
      return await Promise.race([
        reader.read(),
        new Promise((_, reject) => {
          timer = setTimeout(() => {
            reject(new Error(`SSE stream idle timeout after ${effectiveIdleMs}ms`));
          }, effectiveIdleMs);
          if (typeof timer.unref === 'function') timer.unref();
        }),
      ]);
    } finally {
      if (timer) clearTimeout(timer);
    }
  };

  try {
    while (!signal?.aborted) {
      const { value, done } = await readNextChunk();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      if (Buffer.byteLength(buffer, 'utf8') > effectiveMaxBytes) {
        throw new Error(`SSE buffer exceeded ${effectiveMaxBytes} bytes`);
      }
      let nlIdx;
      while ((nlIdx = buffer.indexOf('\n')) >= 0) {
        let line = buffer.slice(0, nlIdx);
        buffer = buffer.slice(nlIdx + 1);
        if (line.endsWith('\r')) line = line.slice(0, -1);
        if (line === '') {
          await flush();
          continue;
        }
        if (line.startsWith(':')) continue;
        const colon = line.indexOf(':');
        const field = colon >= 0 ? line.slice(0, colon) : line;
        let val = colon >= 0 ? line.slice(colon + 1) : '';
        if (val.startsWith(' ')) val = val.slice(1);
        if (field === 'event') eventName = val;
        else if (field === 'data') dataLines.push(val);
        else if (field === 'id') eventId = val;
      }
    }
  } finally {
    try {
      await reader.cancel();
    } catch {
      // Ignore reader cleanup errors on abort.
    }
  }
}

const activeHeldLocks = new Map(); // lockPath -> token
let lockCleanupRegistered = false;

function releaseHeldLockToken(lockPath, token) {
  if (!lockPath || !token) return;
  try {
    const raw = readFileSync(lockPath, 'utf-8').trim();
    const parsed = raw ? JSON.parse(raw) : null;
    if (parsed?.token === token) {
      unlinkSync(lockPath);
    }
  } catch {
    // Lock file already removed or replaced.
  } finally {
    if (activeHeldLocks.get(lockPath) === token) {
      activeHeldLocks.delete(lockPath);
    }
  }
}

function cleanupAllHeldLocks() {
  for (const [lockPath, token] of [...activeHeldLocks.entries()]) {
    releaseHeldLockToken(lockPath, token);
  }
}

function ensureLockCleanupHandlers() {
  if (lockCleanupRegistered) return;
  lockCleanupRegistered = true;
  process.on('exit', cleanupAllHeldLocks);
  for (const sig of ['SIGINT', 'SIGTERM']) {
    const onSig = () => {
      cleanupAllHeldLocks();
      if (process.listenerCount(sig) === 1) {
        process.removeListener(sig, onSig);
        try {
          process.kill(process.pid, sig);
        } catch {
          process.exit(1);
        }
      }
    };
    process.on(sig, onSig);
  }
}

/**
 * Acquire an atomic cross-process lockfile via exclusive create (`wx`), evicting
 * stale locks whose holder PID is dead or whose age exceeds `staleMs` (default 15s).
 */
export async function acquireLockFile(
  lockPath,
  {
    staleMs = DEFAULT_STALE_LOCK_MS,
    liveCeilingMs = DEFAULT_LIVE_LOCK_CEILING_MS,
    waitTimeoutMs = DEFAULT_LOCK_WAIT_TIMEOUT_MS,
    retryMs = DEFAULT_LOCK_RETRY_MS,
    isPidAlive = defaultIsPidAlive,
  } = {},
) {
  if (!lockPath) return () => {};
  ensureLockCleanupHandlers();
  const maxStaleMs = Math.max(1000, Number(staleMs) || DEFAULT_STALE_LOCK_MS);
  const hardCeilingMs = Math.max(maxStaleMs, Number(liveCeilingMs) || DEFAULT_LIVE_LOCK_CEILING_MS);
  const timeoutMs = Math.max(200, Number(waitTimeoutMs) || DEFAULT_LOCK_WAIT_TIMEOUT_MS);
  const stepMs = Math.max(10, Number(retryMs) || DEFAULT_LOCK_RETRY_MS);
  const deadline = Date.now() + timeoutMs;

  mkdirSync(dirname(lockPath), { recursive: true });

  while (true) {
    const token = `${process.pid}-${Date.now()}-${Math.random().toString(36).slice(2, 10)}`;
    try {
      writeFileSync(
        lockPath,
        JSON.stringify({ pid: process.pid, createdAt: Date.now(), token }),
        { flag: 'wx', encoding: 'utf-8' },
      );
      activeHeldLocks.set(lockPath, token);
      return () => releaseHeldLockToken(lockPath, token);
    } catch (err) {
      if (err?.code === 'ENOENT') {
        mkdirSync(dirname(lockPath), { recursive: true });
        continue;
      }
      if (err?.code !== 'EEXIST') throw err;

      let evicted = false;
      try {
        const st = statSync(lockPath);
        let holderPid = 0;
        let holderToken = '';
        let createdAt = st.mtimeMs;
        let parsedOk = false;
        try {
          const raw = readFileSync(lockPath, 'utf-8').trim();
          if (raw) {
            const parsed = JSON.parse(raw);
            holderPid = Number(parsed?.pid) || 0;
            holderToken = String(parsed?.token || '');
            if (Number(parsed?.createdAt) > 0) {
              createdAt = Number(parsed.createdAt);
            }
            parsedOk = true;
          }
        } catch {
          // Holder may still be flushing or left corrupt bytes.
        }
        const ageMs = Date.now() - Math.min(createdAt, st.mtimeMs);
        const liveHolder = parsedOk && holderPid > 0 && isPidAlive(holderPid);
        const deadHolder = parsedOk && holderPid > 0 && !isPidAlive(holderPid);
        const shouldEvict = deadHolder || (liveHolder ? ageMs >= hardCeilingMs : ageMs > maxStaleMs);
        if (shouldEvict) {
          const evictedPath = `${lockPath}.evicted.${process.pid}.${Date.now()}`;
          try {
            renameSync(lockPath, evictedPath);
            try {
              unlinkSync(evictedPath);
            } catch {
              // Ignore cleanup failure on evicted file.
            }
            evicted = true;
          } catch {
            // Fallback: verify token hasn't changed before unlinking.
            try {
              if (holderToken) {
                const recheckRaw = readFileSync(lockPath, 'utf-8').trim();
                const recheck = recheckRaw ? JSON.parse(recheckRaw) : null;
                if (recheck?.token === holderToken) {
                  unlinkSync(lockPath);
                  evicted = true;
                }
              }
            } catch {
              // Another waiter evicted first.
            }
          }
        }
      } catch (statErr) {
        if (statErr?.code === 'ENOENT') {
          evicted = true;
        }
      }

      if (evicted) continue;
      if (Date.now() >= deadline) {
        throw new Error(`Timed out acquiring wakeup lockfile: ${lockPath}`);
      }
      await new Promise((r) => setTimeout(r, stepMs));
    }
  }
}

/**
 * Run `fn` exclusively under an atomic lockfile (no-op wrapper when `lockPath` is empty).
 */
export async function withFileLock(lockPath, fn, options = {}) {
  if (!lockPath) return await fn();
  const release = await acquireLockFile(lockPath, options);
  try {
    return await fn();
  } finally {
    release();
  }
}

/**
 * Persistent (when `cursorFilePath` is provided) or in-memory per-swarm wakeup
 * cursor store tracking `{ lastDispatchedSeq, coordinatorSeq, primarySeq }`.
 */
export function createCursorStore(cursorFilePath = '') {
  const memory = new Map();
  let tmpSeq = 0;

  function readAllFromDisk() {
    if (!cursorFilePath) return {};
    try {
      if (!existsSync(cursorFilePath)) return {};
      const text = readFileSync(cursorFilePath, 'utf-8').trim();
      const parsed = text ? JSON.parse(text) : {};
      return parsed && typeof parsed === 'object' && !Array.isArray(parsed) ? parsed : {};
    } catch {
      return {};
    }
  }

  function normalizeCursor(raw) {
    const coord = Number(raw?.coordinatorSeq) || 0;
    const prim = Number(raw?.primarySeq) || 0;
    const last = Math.max(Number(raw?.lastDispatchedSeq) || 0, coord, prim);
    const wakeSeq = Number(raw?.wakeSeq) || 0;
    return wakeSeq > 0
      ? { lastDispatchedSeq: last, coordinatorSeq: coord, primarySeq: prim, wakeSeq }
      : { lastDispatchedSeq: last, coordinatorSeq: coord, primarySeq: prim };
  }

  function mergeCursor(a, b) {
    const na = normalizeCursor(a);
    const nb = normalizeCursor(b);
    const wakeSeq = Math.max(Number(na.wakeSeq) || 0, Number(nb.wakeSeq) || 0);
    return wakeSeq > 0
      ? {
          lastDispatchedSeq: Math.max(na.lastDispatchedSeq, nb.lastDispatchedSeq),
          coordinatorSeq: Math.max(na.coordinatorSeq, nb.coordinatorSeq),
          primarySeq: Math.max(na.primarySeq, nb.primarySeq),
          wakeSeq,
        }
      : {
          lastDispatchedSeq: Math.max(na.lastDispatchedSeq, nb.lastDispatchedSeq),
          coordinatorSeq: Math.max(na.coordinatorSeq, nb.coordinatorSeq),
          primarySeq: Math.max(na.primarySeq, nb.primarySeq),
        };
  }

  return {
    filePath: cursorFilePath || '',
    load(swarmId) {
      const sid = String(swarmId || '').trim();
      if (!sid) return { lastDispatchedSeq: 0, coordinatorSeq: 0, primarySeq: 0 };
      if (cursorFilePath) {
        const disk = readAllFromDisk();
        if (disk[sid]) {
          const cur = normalizeCursor(disk[sid]);
          memory.set(sid, cur);
          return { ...cur };
        }
      }
      const existing = memory.get(sid);
      return existing ? { ...existing } : { lastDispatchedSeq: 0, coordinatorSeq: 0, primarySeq: 0 };
    },
    save(swarmId, cursor) {
      const sid = String(swarmId || '').trim();
      if (!sid) return;
      const norm = normalizeCursor(cursor);
      memory.set(sid, norm);
      if (!cursorFilePath) return;
      try {
        mkdirSync(dirname(cursorFilePath), { recursive: true });
        const all = readAllFromDisk();
        const prev = all[sid] ? normalizeCursor(all[sid]) : { lastDispatchedSeq: 0, coordinatorSeq: 0, primarySeq: 0 };
        const merged = mergeCursor(prev, norm);
        all[sid] = merged;
        memory.set(sid, merged);
        tmpSeq += 1;
        const tmp = `${cursorFilePath}.tmp.${process.pid}.${Date.now()}.${tmpSeq}`;
        try {
          writeFileSync(tmp, JSON.stringify(all, null, 2), 'utf-8');
          const sleepBuf = new Int32Array(new SharedArrayBuffer(4));
          for (let attempt = 0; attempt < 5; attempt += 1) {
            try {
              renameSync(tmp, cursorFilePath);
              break;
            } catch (renErr) {
              const code = renErr?.code;
              if ((code === 'EPERM' || code === 'EBUSY' || code === 'EACCES') && attempt < 4) {
                Atomics.wait(sleepBuf, 0, 0, 15 * (attempt + 1));
                continue;
              }
              throw renErr;
            }
          }
        } finally {
          if (existsSync(tmp)) {
            try {
              unlinkSync(tmp);
            } catch {
              // Ignore temp cleanup failure.
            }
          }
        }
      } catch (err) {
        // Keep in-memory cursor if disk persistence fails, but log loudly.
        console.error(`[hermes-swarm] failed to persist cursor file ${cursorFilePath}:`, err?.message || err);
      }
    },
    all() {
      const out = {};
      if (cursorFilePath) {
        const disk = readAllFromDisk();
        for (const [k, v] of Object.entries(disk)) {
          out[k] = normalizeCursor(v);
        }
      }
      for (const [k, v] of memory.entries()) {
        out[k] = out[k] ? mergeCursor(out[k], v) : { ...v };
      }
      return out;
    },
  };
}

export function defaultCursorFilePath(env = process.env) {
  const dataDir = String(env?.ANTIGRAVITY_EXECUTABLE_DATA_DIR || '').trim();
  return dataDir ? join(dataDir, 'wakeup_cursors.json') : '';
}

function resolveSwarmTargets(dash, cachedMeta = {}) {
  const meta = dash?.meta && typeof dash.meta === 'object' ? dash.meta : {};
  const coordinatorConversationId = String(
    dash?.coordinatorConversationId || meta.coordinatorConversationId || cachedMeta?.coordinatorConversationId || '',
  ).trim();
  const primaryConversationId = String(
    dash?.primaryConversationId ||
      meta.primaryConversationId ||
      cachedMeta?.primaryConversationId ||
      dash?.conversationId ||
      meta.conversationId ||
      cachedMeta?.conversationId ||
      '',
  ).trim();
  const projectId = String(
    dash?.projectId || meta.projectId || cachedMeta?.projectId || '',
  ).trim();
  const title = String(
    dash?.plan?.goal?.title || dash?.title || meta.title || cachedMeta?.title || '',
  ).trim();
  return { coordinatorConversationId, primaryConversationId, projectId, title };
}

function collectBoardMessages(dash) {
  const raw = [];
  const seen = new Set();
  const sources = [
    dash?.messages,
    dash?.recentBoardMessages,
    dash?.openForCoordinator,
    dash?.openForPrimary,
  ];
  for (const list of sources) {
    if (!Array.isArray(list)) continue;
    for (const m of list) {
      if (!m || typeof m !== 'object') continue;
      const key = m.seq ? `seq:${m.seq}` : (m.id ? `id:${m.id}` : '');
      if (key && seen.has(key)) continue;
      if (key) seen.add(key);
      raw.push(m);
    }
  }
  raw.sort((a, b) => (Number(a.seq) || 0) - (Number(b.seq) || 0));
  return raw;
}

function collectTasks(dash) {
  const out = [];
  const seen = new Set();
  for (const list of [dash?.tasks, dash?.activeTasks, dash?.recentCompletedTasks]) {
    if (!Array.isArray(list)) continue;
    for (const t of list) {
      if (!t || typeof t !== 'object') continue;
      const tid = String(t.taskId || t.id || '').trim();
      if (!tid || seen.has(tid)) continue;
      seen.add(tid);
      out.push({ ...t, taskId: tid });
    }
  }
  return out;
}

/**
 * Create a tier-aware SSE + fallback-reconcile wakeup bridge.
 *
 * When `hostLocalMode: true`, runs unconditionally across all local Antigravity
 * conversations (`GET /internal/wake/events` + 5s `GET /internal/wake/swarms`).
 * When `hostLocalMode: false` (default for 1:1 tests), stays scoped to `config.conversationId`.
 */
export function createWakeupBridge({
  sendMessage,
  config = {},
  env = process.env,
  hostLocalMode = false,
  allowUnscoped = false,
  appDataDirs = null,
  resolveUpstream,
  resolveAllUpstreams,
  fetchImpl = fetch,
  reconcileMs,
  connectTimeoutMs = DEFAULT_CONNECT_TIMEOUT_MS,
  cursorStore = createCursorStore(defaultCursorFilePath(env)),
  lockFilePath,
  staleLockMs = DEFAULT_STALE_LOCK_MS,
  lockWaitTimeoutMs = DEFAULT_LOCK_WAIT_TIMEOUT_MS,
  logger = console,
} = {}) {
  const isHostLocalMode = hostLocalMode === true;
  const scopedConv = String(config?.conversationId || '').trim();
  const effectiveReconcileMs = reconcileMs ?? (isHostLocalMode ? DEFAULT_HOST_LOCAL_RECONCILE_MS : DEFAULT_RECONCILE_MS);
  const effectiveLockPath = String(
    lockFilePath !== undefined ? lockFilePath : (cursorStore?.filePath ? `${cursorStore.filePath}.lock` : ''),
  ).trim();
  const effectiveAppDataDirs = Array.isArray(appDataDirs)
    ? appDataDirs
    : (Array.isArray(config?.appDataDirs) ? config.appDataDirs : null);

  const registeredConvs = new Set();
  if (scopedConv) registeredConvs.add(scopedConv);

  const seenTaskStates = new Map();
  const prevActiveCounts = new Map();
  const allDoneGenBySwarm = new Map();
  const cachedMetaBySwarm = new Map();
  const seenSyntheticKeys = new Set();
  const recentCompletionWakeAtBySwarm = new Map();
  const absentTicksBySwarm = new Map();

  function setCachedMeta(sid, meta) {
    if (!sid) return;
    if (cachedMetaBySwarm.has(sid)) {
      cachedMetaBySwarm.delete(sid);
    }
    cachedMetaBySwarm.set(sid, meta);
    while (cachedMetaBySwarm.size > MAX_CACHED_SWARMS) {
      const oldestSid = cachedMetaBySwarm.keys().next().value;
      if (oldestSid === undefined) break;
      pruneSwarmState(oldestSid);
    }
  }

  function pruneSwarmState(sid) {
    if (!sid) return;
    cachedMetaBySwarm.delete(sid);
    seenTaskStates.delete(sid);
    prevActiveCounts.delete(sid);
    allDoneGenBySwarm.delete(sid);
    recentCompletionWakeAtBySwarm.delete(sid);
    absentTicksBySwarm.delete(sid);
  }

  function pruneStaleSwarms(seenIds, allBasesOk) {
    if (!allBasesOk) return;
    for (const sid of [...cachedMetaBySwarm.keys()]) {
      if (seenIds.has(sid)) {
        absentTicksBySwarm.delete(sid);
      } else {
        const ticks = (absentTicksBySwarm.get(sid) || 0) + 1;
        if (ticks >= ABSENT_SWARM_PRUNE_TICKS) {
          pruneSwarmState(sid);
        } else {
          absentTicksBySwarm.set(sid, ticks);
        }
      }
    }
  }

  function recordSyntheticKey(key) {
    if (!key) return;
    seenSyntheticKeys.add(key);
    if (seenSyntheticKeys.size > 2000) {
      const oldest = seenSyntheticKeys.values().next().value;
      if (oldest !== undefined) seenSyntheticKeys.delete(oldest);
    }
  }

  function isTerminalTaskState(state) {
    const s = String(state || '').trim().toUpperCase();
    return s === 'TASK_STATE_COMPLETED' || s === 'TASK_STATE_FAILED' || s === 'TASK_STATE_CANCELED';
  }

  function isWithinCompletionDedupWindow(sid, now = Date.now()) {
    const prev = recentCompletionWakeAtBySwarm.get(sid) || 0;
    return prev > 0 && (now - prev) <= COMPLETION_DEDUP_WINDOW_MS;
  }

  function markCompletionWake(sid, now = Date.now()) {
    if (!sid) return;
    recentCompletionWakeAtBySwarm.set(sid, now);
  }

  function isCoordinatorWaitingInSwarmWait(sourceObj, events = []) {
    if (!sourceObj || typeof sourceObj !== 'object') return false;
    if (
      sourceObj.coordinatorWaiting === true ||
      sourceObj.coordinator_waiting === true ||
      sourceObj.meta?.coordinatorWaiting === true ||
      sourceObj.meta?.coordinator_waiting === true ||
      sourceObj.message?.coordinatorWaiting === true
    ) {
      return true;
    }
    const members = Array.isArray(sourceObj.members)
      ? sourceObj.members
      : (sourceObj.members && typeof sourceObj.members === 'object' ? Object.values(sourceObj.members) : []);
    const coord = members.find((m) => m && (m.name === 'coordinator' || m.role === 'coordinator'));
    if (coord && (coord.coordinatorWaiting === true || coord.waitingInSwarmWait === true || coord.waiting === true)) {
      return true;
    }
    const waitSince = Number(
      coord?.waitSince ??
      coord?.wait_since ??
      sourceObj.waitSince ??
      sourceObj.wait_since ??
      sourceObj.meta?.waitSince ??
      sourceObj.meta?.wait_since ??
      0,
    );
    if (Number.isFinite(waitSince) && waitSince > 0 && Array.isArray(events) && events.length > 0) {
      return events.every((ev) => Number(ev.seq || 0) > waitSince);
    }
    return false;
  }

  let singleSseEntry = null; // { controller, stopped, backoffMs }
  const hostSseEntries = new Map(); // baseUrl -> { controller, stopped, backoffMs }
  let eventLock = Promise.resolve();
  let knownSwarmId = '';
  let reconcileTimer = null;
  let reconcileInFlight = false;
  let stopped = false;
  const transientLogTimestamps = new Map();

  function logTransient(level, key, ...args) {
    const now = Date.now();
    const last = transientLogTimestamps.get(key) || 0;
    if (now - last < TRANSIENT_LOG_THROTTLE_MS) return;
    transientLogTimestamps.set(key, now);
    if (transientLogTimestamps.size > 500) {
      const cutoff = now - TRANSIENT_LOG_THROTTLE_MS * 10;
      for (const [k, ts] of transientLogTimestamps.entries()) {
        if (ts < cutoff || transientLogTimestamps.size > 500) {
          transientLogTimestamps.delete(k);
        } else {
          break;
        }
      }
    }
    if (level === 'warn') {
      logWarn(logger, ...args);
    } else {
      logError(logger, ...args);
    }
  }

  function clearTransientLog(key) {
    transientLogTimestamps.delete(key);
  }

  function isLocalConv(convId) {
    return isConversationLocal(convId, {
      env,
      appDataDirs: effectiveAppDataDirs,
      conversationId: scopedConv,
      registeredConversations: registeredConvs,
      allowUnscoped,
    });
  }

  function isSwarmTargetsLocal(targets) {
    if (!targets || typeof targets !== 'object') return false;
    return isLocalConv(targets.primaryConversationId) || isLocalConv(targets.coordinatorConversationId);
  }

  function getUpstreamBase(convHint = '') {
    if (typeof resolveUpstream === 'function') {
      return resolveUpstream({ conversationId: convHint || scopedConv }, config);
    }
    return config?.fallbackBoardUrl || 'http://127.0.0.1:9910';
  }

  function getUpstreamBases() {
    if (typeof resolveAllUpstreams === 'function') {
      const list = resolveAllUpstreams(config);
      if (Array.isArray(list) && list.length > 0) {
        return [...new Set(list.map((u) => String(u || '').trim().replace(/\/+$/, '')).filter(Boolean))];
      }
    }
    return [String(getUpstreamBase('') || 'http://127.0.0.1:9910').trim().replace(/\/+$/, '')];
  }

  function withLock(fn) {
    const next = eventLock.then(fn, fn);
    eventLock = next.catch(() => {});
    return next;
  }

  async function fetchJson(url) {
    const firstHeaders = buildHeaders(config, { Accept: 'application/json' });
    let res = await fetchImpl(url, {
      headers: firstHeaders,
      signal: AbortSignal.timeout(DEFAULT_FETCH_TIMEOUT_MS),
    });
    if (res.status === 401) {
      const hasEnvToken = Boolean(config?.hasEnvHttpToken || String(config?.envHttpToken || '').trim());
      const retryHeaders = buildHeaders(
        config,
        { Accept: 'application/json' },
        { forceRefresh: true, skipEnvToken: hasEnvToken },
      );
      if (retryHeaders.Authorization && retryHeaders.Authorization !== firstHeaders.Authorization) {
        if (hasEnvToken) {
          logTransient(
            'warn',
            `token-fallback:${url}`,
            `[hermes-swarm] HTTP 401 with HERMES_A2A_HTTP_TOKEN on ${url}; falling back to internal_token`,
          );
        }
        try { await res.body?.cancel?.(); } catch {}
        res = await fetchImpl(url, {
          headers: retryHeaders,
          signal: AbortSignal.timeout(DEFAULT_FETCH_TIMEOUT_MS),
        });
      }
    }
    if (!res.ok) {
      try { await res.body?.cancel?.(); } catch {}
      return { ok: false, status: res.status, data: null };
    }
    return { ok: true, status: res.status, data: await res.json() };
  }

  async function fetchBoardForConversation(convId, baseUrlOverride = '') {
    const clean = String(convId || '').trim();
    if (!clean) return null;
    const base = baseUrlOverride || getUpstreamBase(clean);
    const qs = new URLSearchParams({ conversationId: clean });
    const res = await fetchJson(buildUrl(base, '/board', `?${qs.toString()}`));
    if (!res.ok) {
      logTransient(
        'warn',
        `board-http:${base}:${clean}:${res.status}`,
        `[hermes-swarm] GET ${base}/board?conversationId=${clean} returned HTTP ${res.status}`,
      );
      return null;
    }
    clearTransientLog(`board-http:${base}:${clean}:${res.status}`);
    return res.data;
  }

  async function resolveFreshCoordinatorSuppression(sid, sourceObj, events, targets, { isFreshDash = false } = {}) {
    if (isCoordinatorWaitingInSwarmWait(sourceObj, events)) return true;
    const cached = cachedMetaBySwarm.get(sid);
    if (cached && cached !== sourceObj && isCoordinatorWaitingInSwarmWait(cached, events)) {
      return true;
    }
    if (!isFreshDash) {
      const queryConv = targets?.coordinatorConversationId || targets?.primaryConversationId || scopedConv;
      if (queryConv) {
        try {
          const freshDash = await fetchBoardForConversation(queryConv);
          if (freshDash && typeof freshDash === 'object') {
            const prevMeta = cachedMetaBySwarm.get(sid) || {};
            cachedMetaBySwarm.set(sid, { ...prevMeta, ...(freshDash.meta || {}), ...freshDash });
            if (isCoordinatorWaitingInSwarmWait(freshDash, events)) {
              return true;
            }
          }
        } catch (err) {
          logTransient(
            'warn',
            `fresh-suppression:${sid}`,
            `[hermes-swarm] fresh suppression board check failed for swarm=${sid}:`,
            err?.message || err,
          );
        }
      }
    }
    return false;
  }

  async function dispatchTierWake({ sid, tier, events, targets, suppressSend = false }) {
    if (!Array.isArray(events) || events.length === 0) return false;
    const isCoord = tier === 'coordinator';
    const targetConv = isCoord ? targets.coordinatorConversationId : targets.primaryConversationId;
    if (!targetConv) {
      // Strict 3-tier isolation: NEVER fall back to primaryConversationId for coordinator wakes.
      logTransient(
        'warn',
        `missing-target:${sid}:${tier}`,
        `[hermes-swarm] skipping Tier-${isCoord ? 2 : 1} wakeup for swarm=${sid}: ${
          isCoord ? 'coordinatorConversationId' : 'primaryConversationId'
        } is unset`,
      );
      return false;
    }

    const cursorField = isCoord ? 'coordinatorSeq' : 'primarySeq';
    const projectId = targets.projectId || config?.projectId || undefined;
    const title = isCoord
      ? (targets.title ? `Swarm wakeup: ${targets.title}` : `Swarm wakeup (${sid})`)
      : (targets.title ? `Swarm escalation: ${targets.title}` : `Swarm escalation (${sid})`);

    try {
      return await withFileLock(
        effectiveLockPath,
        async () => {
          const cur = cursorStore.load(sid);
          const pending = events.filter((ev) => {
            if (ev.seq > 0) return ev.seq > cur[cursorField];
            if (ev.wakeSeq > 0) return ev.wakeSeq > (Number(cur.wakeSeq) || 0);
            if (ev.syntheticKey && seenSyntheticKeys.has(ev.syntheticKey)) return false;
            return true;
          });
          if (pending.length === 0) return false;

          const maxSeq = pending.reduce((acc, ev) => (ev.seq > acc ? ev.seq : acc), 0);
          const maxWakeSeq = pending.reduce((acc, ev) => (ev.wakeSeq > acc ? ev.wakeSeq : acc), 0);

          if (!suppressSend) {
            const notice = isCoord
              ? [
                  `[Hermes Swarm Wakeup — swarm=${sid}]`,
                  ...pending.map((ev) => `- ${ev.line}`),
                  `Inspect via swarm_manage(action="status") or swarm_board_read() and continue as Coordinator (escalate to @primary only for user decisions or final [done]).`,
                ].join('\n')
              : [
                  `[Hermes Swarm Escalation — swarm=${sid}]`,
                  ...pending.map((ev) => `- ${ev.line}`),
                  `Inspect via swarm_manage(action="status") and respond to your Coordinator or the user.`,
                ].join('\n');

            try {
              await sendMessage(targetConv, notice, { title, projectId });
              logInfo(
                logger,
                `[hermes-swarm] dispatched ${isCoord ? 'coordinator wakeup' : 'primary escalation'} to ${targetConv} (swarm=${sid}, seq=${maxSeq}, wakeSeq=${maxWakeSeq}, events=${pending.length})`,
              );
            } catch (err) {
              logError(
                logger,
                `[hermes-swarm] ${isCoord ? 'coordinator wakeup' : 'primary escalation'} failed for ${targetConv} (swarm=${sid}):`,
                err?.message || err,
              );
              return false;
            }
          } else {
            logInfo(
              logger,
              `[hermes-swarm] suppressed ${isCoord ? 'coordinator wakeup' : 'primary escalation'} for ${targetConv} (swarm=${sid}, seq=${maxSeq}, wakeSeq=${maxWakeSeq}; coordinator waiting in swarm_wait)`,
            );
          }

          for (const ev of pending) {
            if (ev.syntheticKey) recordSyntheticKey(ev.syntheticKey);
          }
          if (maxSeq > 0 || maxWakeSeq > 0) {
            const latest = cursorStore.load(sid);
            if (maxSeq > 0) {
              latest[cursorField] = Math.max(latest[cursorField], maxSeq);
              latest.lastDispatchedSeq = Math.max(latest.lastDispatchedSeq, maxSeq);
            }
            if (maxWakeSeq > 0) {
              latest.wakeSeq = Math.max(Number(latest.wakeSeq) || 0, maxWakeSeq);
            }
            cursorStore.save(sid, latest);
          }
          return !suppressSend;
        },
        {
          staleMs: staleLockMs,
          waitTimeoutMs: lockWaitTimeoutMs,
          isPidAlive: config?.isPidAlive || defaultIsPidAlive,
        },
      );
    } catch (lockErr) {
      logError(
        logger,
        `[hermes-swarm] wakeup lock failed for swarm=${sid} tier=${tier}:`,
        lockErr?.message || lockErr,
      );
      return false;
    }
  }

  async function processDashboard(sidHint, dash, { isFreshDash = false } = {}) {
    if (stopped || typeof sendMessage !== 'function') return 0;
    if (!isHostLocalMode && !scopedConv) return 0;
    if (!dash || typeof dash !== 'object') return 0;

    const concreteSwarm = dash.swarmId || dash.swarm || dash.id || dash.meta?.swarmId || dash.meta?.id;
    if (!concreteSwarm && !Array.isArray(dash.messages) && !Array.isArray(dash.tasks)) {
      return 0;
    }

    const sid = String(concreteSwarm || sidHint || scopedConv || '').trim();
    if (!sid) return 0;

    const prevMeta = cachedMetaBySwarm.get(sid) || {};
    const mergedMeta = { ...prevMeta, ...(dash.meta || {}), ...dash };

    if (isHostLocalMode) {
      if (!isSwarmTargetsLocal(resolveSwarmTargets(dash, mergedMeta))) return 0;
    } else if (!isSwarmInScope(mergedMeta, scopedConv)) {
      return 0;
    }

    knownSwarmId = sid;
    if (dash.archived) {
      pruneSwarmState(sid);
      return 0;
    }
    setCachedMeta(sid, mergedMeta);

    const cursor = cursorStore.load(sid);
    const targets = resolveSwarmTargets(dash, mergedMeta);
    const msgs = collectBoardMessages(dash);

    const coordEvents = [];
    const primaryEvents = [];

    for (const m of msgs) {
      const seq = Number(m.seq) || 0;
      if (seq <= 0) continue;
      const tierTarget = classifyBoardMessage(m);
      if (!tierTarget) continue;

      const sender = String(m.sender || m.agent || 'worker').trim();
      const kind = String(m.kind || m.type || 'post').trim();
      const summary = String(m.summary || m.text || '').replace(/\s+/g, ' ').trim().slice(0, 180);
      const idSuffix = m.id ? ` (id=${m.id})` : '';
      const line = `@${sender} [${kind} #${seq}]: ${summary}${idSuffix}`;

      if (tierTarget === 'coordinator' && seq > cursor.coordinatorSeq) {
        coordEvents.push({ seq, line, isDoneMsg: summary.toLowerCase().startsWith('[done]') });
      } else if (tierTarget === 'primary' && seq > cursor.primarySeq) {
        primaryEvents.push({ seq, line });
      }
    }

    const allTasks = collectTasks(dash);
    let taskMap = seenTaskStates.get(sid);
    if (!taskMap) {
      taskMap = new Map();
      seenTaskStates.set(sid, taskMap);
    }
    for (const t of allTasks) {
      if (Number(t.tier || 0) === 3 || String(t.role || '').toLowerCase() === 'subagent') continue;
      const prev = taskMap.get(t.taskId);
      const curState = String(t.state || '');
      taskMap.set(t.taskId, curState);
      if (curState === 'TASK_STATE_INPUT_REQUIRED' && prev !== 'TASK_STATE_INPUT_REQUIRED') {
        coordEvents.push({
          seq: 0,
          syntheticKey: `${sid}:task:${t.taskId}:${curState}`,
          line: `@${t.workerName || t.name || t.taskId} paused in TASK_STATE_INPUT_REQUIRED (${t.taskId})`,
        });
      } else if (curState === 'TASK_STATE_FAILED' && prev && prev !== 'TASK_STATE_FAILED') {
        coordEvents.push({
          seq: 0,
          syntheticKey: `${sid}:task:${t.taskId}:${curState}`,
          line: `@${t.workerName || t.name || t.taskId} entered TASK_STATE_FAILED (${t.taskId})`,
        });
      }
    }

    const activeCount = Array.isArray(dash.activeTasks)
      ? dash.activeTasks.length
      : allTasks.filter((t) => ['TASK_STATE_WORKING', 'TASK_STATE_SUBMITTED'].includes(String(t.state || ''))).length;
    const prevActive = prevActiveCounts.get(sid) ?? 0;
    prevActiveCounts.set(sid, activeCount);
    if (prevActive === 0 && activeCount > 0) {
      allDoneGenBySwarm.set(sid, (allDoneGenBySwarm.get(sid) || 0) + 1);
    }
    if (prevActive > 0 && activeCount === 0) {
      const label = targets.title || sid;
      const gen = allDoneGenBySwarm.get(sid) || 0;
      const allDoneKey = `${sid}:all_done:${gen}`;
      const hasCompletionInBatch = coordEvents.some((ev) => ev.isDoneMsg || ev.isTerminalTask);
      if (isWithinCompletionDedupWindow(sid) || hasCompletionInBatch) {
        recordSyntheticKey(allDoneKey);
      } else {
        coordEvents.push({
          seq: 0,
          syntheticKey: allDoneKey,
          isAllDone: true,
          line: `All active workers in "${label}" (${sid}) finished. Inspect status/diffs and continue.`,
        });
      }
    }

    let wakeups = 0;
    if (coordEvents.length > 0) {
      const suppressCoordSend = await resolveFreshCoordinatorSuppression(sid, dash, coordEvents, targets, { isFreshDash });
      if (await dispatchTierWake({ sid, tier: 'coordinator', events: coordEvents, targets, suppressSend: suppressCoordSend })) {
        if (coordEvents.some((ev) => ev.isAllDone || ev.isTerminalTask || ev.isDoneMsg)) {
          markCompletionWake(sid);
        }
        wakeups += 1;
      } else if (suppressCoordSend && coordEvents.some((ev) => ev.isAllDone || ev.isTerminalTask)) {
        markCompletionWake(sid);
      }
    }
    if (primaryEvents.length > 0) {
      if (await dispatchTierWake({ sid, tier: 'primary', events: primaryEvents, targets })) {
        wakeups += 1;
      }
    }
    return wakeups;
  }

  async function reconcileSingleBoard() {
    let dash;
    try {
      dash = await fetchBoardForConversation(scopedConv);
      clearTransientLog(`single-board:${scopedConv}`);
    } catch (err) {
      logTransient(
        'warn',
        `single-board:${scopedConv}`,
        `[hermes-swarm] reconcile /board?conversationId=${scopedConv} failed:`,
        err?.message || err,
      );
      return 0;
    }
    if (!dash || dash.online === false) return 0;
    const sid = String(dash.swarmId || dash.swarm || dash.id || knownSwarmId || scopedConv).trim();
    if (dash.archived) {
      pruneSwarmState(sid);
      return 0;
    }
    if (!sid || !isSwarmInScope(dash, scopedConv)) return 0;
    return await withLock(() => processDashboard(sid, dash, { isFreshDash: true }));
  }

  async function reconcileHostLocal() {
    let wakeups = 0;
    const bases = getUpstreamBases();
    const seenSwarmIds = new Set();
    let allBasesOk = bases.length > 0;
    for (const baseUrl of bases) {
      let swarmsRes;
      try {
        swarmsRes = await fetchJson(buildUrl(baseUrl, '/internal/wake/swarms'));
        clearTransientLog(`wake-swarms-err:${baseUrl}`);
      } catch (err) {
        allBasesOk = false;
        logTransient(
          'error',
          `wake-swarms-err:${baseUrl}`,
          `[hermes-swarm] failed to poll ${baseUrl}/internal/wake/swarms:`,
          err?.message || err,
        );
        continue;
      }
      if (!swarmsRes.ok) {
        allBasesOk = false;
        logTransient(
          'error',
          `wake-swarms-http:${baseUrl}:${swarmsRes.status}`,
          `[hermes-swarm] GET ${baseUrl}/internal/wake/swarms returned HTTP ${swarmsRes.status}`,
        );
        continue;
      }
      clearTransientLog(`wake-swarms-http:${baseUrl}:${swarmsRes.status}`);
      const swarms = Array.isArray(swarmsRes.data?.swarms) ? swarmsRes.data.swarms : [];
      for (const sw of swarms) {
        if (stopped) break;
        if (!sw || typeof sw !== 'object') continue;
        const sid = String(sw.swarmId || sw.swarm || sw.id || '').trim();
        if (!sid) continue;
        if (sw.archived) {
          pruneSwarmState(sid);
          continue;
        }
        seenSwarmIds.add(sid);

        const prevMeta = cachedMetaBySwarm.get(sid) || {};
        const mergedHint = { ...prevMeta, ...sw };
        const targets = resolveSwarmTargets(mergedHint, prevMeta);
        // NEVER rely on swarmId alone without checking primaryConversationId / coordinatorConversationId.
        if (!isSwarmTargetsLocal(targets)) continue;

        setCachedMeta(sid, mergedHint);
        const queryConv = targets.primaryConversationId || targets.coordinatorConversationId;
        if (!queryConv) continue;

        try {
          const dash = await fetchBoardForConversation(queryConv, baseUrl);
          clearTransientLog(`reconcile-board:${baseUrl}:${queryConv}`);
          if (!dash || dash.online === false) continue;
          if (dash.archived) {
            pruneSwarmState(sid);
            continue;
          }
          wakeups += await withLock(() => processDashboard(sid, dash, { isFreshDash: true }));
        } catch (err) {
          logTransient(
            'error',
            `reconcile-board:${baseUrl}:${queryConv}`,
            `[hermes-swarm] reconcile /board?conversationId=${queryConv} failed (swarm=${sid}):`,
            err?.message || err,
          );
        }
      }
    }
    pruneStaleSwarms(seenSwarmIds, allBasesOk);
    return wakeups;
  }

  async function handleSseEvent(sidOrFrame, maybeFrame) {
    const frame = maybeFrame && typeof maybeFrame === 'object' ? maybeFrame : sidOrFrame;
    const sidHint = typeof sidOrFrame === 'string' ? sidOrFrame : '';
    if (stopped || !frame || typeof frame.data !== 'string') return 0;
    if (!isHostLocalMode && !scopedConv) return 0;

    let payload;
    try {
      payload = JSON.parse(frame.data);
    } catch {
      return 0;
    }
    if (!payload || typeof payload !== 'object') return 0;

    return await withLock(async () => {
      const eventName = String(frame.event || payload.event || '').trim();
      if (eventName === 'keepalive') return 0;
      if (eventName === 'ready') {
        if (isHostLocalMode && Array.isArray(payload.swarms)) {
          for (const sw of payload.swarms) {
            const swId = String(sw?.swarmId || sw?.swarm || sw?.id || '').trim();
            if (!swId) continue;
            const prevMeta = cachedMetaBySwarm.get(swId) || {};
            const targets = resolveSwarmTargets(sw, prevMeta);
            if (isSwarmTargetsLocal(targets)) {
              cachedMetaBySwarm.set(swId, { ...prevMeta, ...sw });
            }
          }
        }
        return 0;
      }

      const sid = String(
        isHostLocalMode
          ? (payload.swarmId || payload.swarm || payload.id || sidHint || '')
          : (payload.swarmId || payload.swarm || payload.id || sidHint || knownSwarmId || scopedConv || ''),
      ).trim();

      if (eventName === 'coordinator_wake' || eventName === 'primary_wake') {
        if (!sid) return 0;
        const prevMeta = cachedMetaBySwarm.get(sid) || {};

        if (!isHostLocalMode) {
          if (collectSwarmConversationIds(payload).size > 0 && !isSwarmInScope(payload, scopedConv)) {
            return 0;
          }
        }

        let targets = resolveSwarmTargets(payload, prevMeta);
        if (isHostLocalMode) {
          if (!isSwarmTargetsLocal(targets)) return 0;
        } else {
          const needCoord = eventName === 'coordinator_wake' && !targets.coordinatorConversationId;
          const needPrim = eventName === 'primary_wake' && !targets.primaryConversationId;
          if (needCoord || needPrim) {
            const dash = await fetchBoardForConversation(scopedConv);
            if (dash) {
              if (!isSwarmInScope(dash, scopedConv)) return 0;
              const merged = { ...prevMeta, ...(dash.meta || {}), ...dash };
              cachedMetaBySwarm.set(sid, merged);
              targets = resolveSwarmTargets(dash, merged);
            }
          }
        }

        if (targets.primaryConversationId || targets.coordinatorConversationId) {
          cachedMetaBySwarm.set(sid, { ...prevMeta, ...payload, ...targets });
        }

        const msg = payload.message && typeof payload.message === 'object' ? payload.message : payload;
        if (String(msg.senderRole || '').toLowerCase() === 'subagent' || Number(msg.senderTier || 0) === 3) {
          return 0;
        }

        const seq = Number(msg.seq || payload.seq || 0);
        const wakeSeq = Number(msg.wakeSeq || payload.wakeSeq || 0);
        const isSynthetic = seq <= 0 && (
          wakeSeq > 0 ||
          msg.synthetic === true ||
          payload.synthetic === true ||
          Boolean(payload.task)
        );
        if (seq <= 0 && !isSynthetic) return 0;

        const sender = String(msg.sender || msg.agent || (eventName === 'primary_wake' ? 'coordinator' : 'worker')).trim();
        const kind = String(msg.kind || msg.type || (eventName === 'primary_wake' ? 'escalation' : 'post')).trim();
        const summary = String(msg.summary || msg.text || payload.summary || '').replace(/\s+/g, ' ').trim().slice(0, 180);
        const seqTag = seq > 0 ? `#${seq}` : (wakeSeq > 0 ? `wake#${wakeSeq}` : 'event');
        const line = `@${sender} [${kind} ${seqTag}]: ${summary}${msg.id ? ` (id=${msg.id})` : ''}`;

        let syntheticKey = '';
        let isCompletionTransition = false;
        let isAllDoneEvent = false;
        if (seq <= 0) {
          const taskId = String(payload.task?.taskId || payload.task?.id || msg.taskId || '').trim();
          const taskState = String(payload.task?.state || msg.state || payload.state || '').trim();
          if (taskId && taskState) {
            syntheticKey = `${sid}:task:${taskId}:${taskState}`;
            isCompletionTransition = isTerminalTaskState(taskState);
            let tMap = seenTaskStates.get(sid);
            if (!tMap) {
              tMap = new Map();
              seenTaskStates.set(sid, tMap);
            }
            tMap.set(taskId, taskState);
          } else if (kind === 'all_done' || payload.kind === 'all_done' || /^All active workers in /i.test(summary)) {
            const gen = Number(payload.generation ?? payload.gen ?? allDoneGenBySwarm.get(sid) ?? 0);
            syntheticKey = `${sid}:all_done:${gen}`;
            isAllDoneEvent = true;
            isCompletionTransition = true;
            prevActiveCounts.set(sid, 0);
          } else {
            syntheticKey = `${sid}:${eventName}:${msg.id || wakeSeq || `${taskId}:${summary}`}`;
          }
        }

        const cursor = cursorStore.load(sid);
        if (eventName === 'coordinator_wake') {
          if (seq > 0) {
            const hasMsgContext = Boolean(msg.summary || msg.text || msg.mentions || msg.to || msg.kind || msg.type);
            if (hasMsgContext && classifyBoardMessage(msg) !== 'coordinator') {
              return 0;
            }
          }
          if (seq > 0 && seq <= cursor.coordinatorSeq) return 0;
          if (seq <= 0 && wakeSeq > 0 && wakeSeq <= (Number(cursor.wakeSeq) || 0)) return 0;
          if (syntheticKey && seenSyntheticKeys.has(syntheticKey)) return 0;

          if (seq <= 0 && isCompletionTransition && isWithinCompletionDedupWindow(sid)) {
            if (syntheticKey) recordSyntheticKey(syntheticKey);
            if (wakeSeq > 0) {
              await withFileLock(
                effectiveLockPath,
                async () => {
                  const latest = cursorStore.load(sid);
                  latest.wakeSeq = Math.max(Number(latest.wakeSeq) || 0, wakeSeq);
                  cursorStore.save(sid, latest);
                },
                {
                  staleMs: staleLockMs,
                  waitTimeoutMs: lockWaitTimeoutMs,
                  isPidAlive: config?.isPidAlive || defaultIsPidAlive,
                },
              );
            }
            return 0;
          }

          const evObj = {
            seq,
            wakeSeq,
            syntheticKey,
            isAllDone: isAllDoneEvent,
            isTerminalTask: isCompletionTransition && !isAllDoneEvent,
            line,
          };
          const suppressCoordSend = await resolveFreshCoordinatorSuppression(
            sid,
            payload,
            [evObj],
            targets,
            { isFreshDash: false },
          );
          const dispatched = await dispatchTierWake({
            sid,
            tier: 'coordinator',
            events: [evObj],
            targets,
            suppressSend: suppressCoordSend,
          });
          if (isCompletionTransition || (seq > 0 && summary.toLowerCase().startsWith('[done]'))) {
            markCompletionWake(sid);
          }
          return dispatched ? 1 : 0;
        }

        if (seq <= 0) return 0;
        if (seq <= cursor.primarySeq) return 0;
        return (await dispatchTierWake({
          sid,
          tier: 'primary',
          events: [{ seq, wakeSeq, line }],
          targets,
        }))
          ? 1
          : 0;
      }

      if (eventName === 'snapshot' || eventName === 'update' || eventName === 'message') {
        if (Array.isArray(payload.messages) || Array.isArray(payload.tasks) || Array.isArray(payload.activeTasks)) {
          return await processDashboard(sid, payload);
        }
        return isHostLocalMode ? await reconcileHostLocal() : await reconcileSingleBoard();
      }

      return 0;
    });
  }

  function startSingleSseSubscription() {
    if (stopped || !scopedConv || singleSseEntry) return;
    const baseUrl = String(getUpstreamBase(scopedConv) || '').trim().replace(/\/+$/, '');
    const entry = { controller: null, stopped: false, backoffMs: 1000, baseUrl };
    singleSseEntry = entry;

    const loop = async () => {
      while (!stopped && !entry.stopped && singleSseEntry === entry) {
        const ctrl = new AbortController();
        entry.controller = ctrl;
        const connectTimer = setTimeout(() => {
          try { ctrl.abort(); } catch {}
        }, Math.max(500, Number(connectTimeoutMs) || DEFAULT_CONNECT_TIMEOUT_MS));

        const qs = new URLSearchParams({ conversationId: scopedConv });
        const cursorKey = knownSwarmId || scopedConv;
        const cursor = cursorStore.load(cursorKey);
        if (cursor.lastDispatchedSeq > 0) {
          qs.set('since', String(cursor.lastDispatchedSeq));
        }
        const currentBase = String(getUpstreamBase(scopedConv) || '').trim().replace(/\/+$/, '');
        entry.baseUrl = currentBase;
        const eventsUrl = buildUrl(currentBase, '/board/events', `?${qs.toString()}`);

        try {
          const firstHeaders = buildHeaders(config, { Accept: 'text/event-stream' });
          let res = await fetchImpl(eventsUrl, {
            headers: firstHeaders,
            signal: ctrl.signal,
          });
          if (res.status === 401) {
            const hasEnvToken = Boolean(config?.hasEnvHttpToken || String(config?.envHttpToken || '').trim());
            const retryHeaders = buildHeaders(
              config,
              { Accept: 'text/event-stream' },
              { forceRefresh: true, skipEnvToken: hasEnvToken },
            );
            if (retryHeaders.Authorization && retryHeaders.Authorization !== firstHeaders.Authorization) {
              if (hasEnvToken) {
                logTransient(
                  'warn',
                  `token-fallback:${eventsUrl}`,
                  `[hermes-swarm] HTTP 401 with HERMES_A2A_HTTP_TOKEN on ${eventsUrl}; falling back to internal_token`,
                );
              }
              try { await res.body?.cancel?.(); } catch {}
              res = await fetchImpl(eventsUrl, {
                headers: retryHeaders,
                signal: ctrl.signal,
              });
            }
          }
          clearTimeout(connectTimer);
          if (res.ok && res.body) {
            const streamStartMs = Date.now();
            await consumeSseStream(res.body, (frame) => handleSseEvent(frame), ctrl.signal);
            if (Date.now() - streamStartMs >= MIN_HEALTHY_SSE_MS) {
              entry.backoffMs = 1000;
            }
          } else {
            try { await res.body?.cancel?.(); } catch {}
            if (!stopped && !entry.stopped) {
              logError(logger, `[hermes-swarm] SSE stream ${eventsUrl} returned HTTP ${res.status}`);
            }
          }
        } catch (err) {
          clearTimeout(connectTimer);
          if (!stopped && !entry.stopped && err?.name !== 'AbortError' && err?.name !== 'TimeoutError') {
            logTransient('error', `sse-single:${eventsUrl}`, `[hermes-swarm] SSE connection failed (${eventsUrl}):`, err?.message || err);
          }
        }

        if (stopped || entry.stopped || singleSseEntry !== entry) break;
        const delayMs = Math.max(100, Math.floor(Math.random() * entry.backoffMs));
        await new Promise((r) => setTimeout(r, delayMs));
        entry.backoffMs = Math.min(MAX_SSE_BACKOFF_MS, entry.backoffMs * 2);
      }
    };

    loop().catch((err) => {
      if (!stopped) {
        logError(logger, '[hermes-swarm] single SSE loop crashed:', err?.message || err);
      }
    });
  }

  function startHostLocalSseForBase(baseUrl) {
    if (stopped || hostSseEntries.has(baseUrl)) return;
    const entry = { controller: null, stopped: false, backoffMs: 1000 };
    hostSseEntries.set(baseUrl, entry);

    const loop = async () => {
      while (!stopped && !entry.stopped && hostSseEntries.get(baseUrl) === entry) {
        const ctrl = new AbortController();
        entry.controller = ctrl;
        const connectTimer = setTimeout(() => {
          try { ctrl.abort(); } catch {}
        }, Math.max(500, Number(connectTimeoutMs) || DEFAULT_CONNECT_TIMEOUT_MS));

        const activeIds = new Set(cachedMetaBySwarm.keys());
        if (knownSwarmId) activeIds.add(knownSwarmId);

        const allCursors = cursorStore.all();
        const sinceMap = {};
        for (const [sid, cur] of Object.entries(allCursors)) {
          if (activeIds.size > 0 && !activeIds.has(sid)) continue;
          const seqVal = Number(cur?.lastDispatchedSeq) || 0;
          const wakeVal = Number(cur?.wakeSeq) || 0;
          if (seqVal > 0 || wakeVal > 0) {
            sinceMap[sid] = wakeVal > 0 ? { seq: seqVal, wakeSeq: wakeVal } : seqVal;
          }
        }

        const encodedMap = Object.keys(sinceMap).length > 0 ? JSON.stringify(sinceMap) : '';
        const usePost = Boolean(encodedMap && encodedMap.length > 2048);
        const qs = new URLSearchParams();
        if (encodedMap && !usePost) {
          qs.set('since_map', encodedMap);
        }
        const search = qs.toString() ? `?${qs.toString()}` : '';
        const eventsUrl = buildUrl(baseUrl, '/internal/wake/events', search);

        try {
          let reqUrl = eventsUrl;
          let reqOpts = {
            method: usePost ? 'POST' : 'GET',
            headers: buildHeaders(config, {
              Accept: 'text/event-stream',
              ...(usePost ? { 'Content-Type': 'application/json' } : {}),
            }),
            ...(usePost ? { body: JSON.stringify({ since_map: sinceMap, active_only: true }) } : {}),
            signal: ctrl.signal,
          };
          let res = await fetchImpl(reqUrl, reqOpts);
          if (usePost && res.status === 405) {
            try { await res.body?.cancel?.(); } catch {}
            logWarn(logger, `[hermes-swarm] POST ${eventsUrl} returned 405; falling back to bounded GET since_map`);
            const trimmed = {};
            for (const [sid, val] of Object.entries(sinceMap)) {
              trimmed[sid] = val;
              if (JSON.stringify(trimmed).length > 2000) {
                delete trimmed[sid];
                break;
              }
            }
            const fallbackQs = new URLSearchParams();
            if (Object.keys(trimmed).length > 0) {
              fallbackQs.set('since_map', JSON.stringify(trimmed));
            }
            reqUrl = buildUrl(baseUrl, '/internal/wake/events', fallbackQs.toString() ? `?${fallbackQs.toString()}` : '');
            reqOpts = {
              headers: buildHeaders(config, { Accept: 'text/event-stream' }),
              signal: ctrl.signal,
            };
            res = await fetchImpl(reqUrl, reqOpts);
          }
          if (res.status === 401) {
            const hasEnvToken = Boolean(config?.hasEnvHttpToken || String(config?.envHttpToken || '').trim());
            const retryHeaders = buildHeaders(
              config,
              {
                Accept: 'text/event-stream',
                ...(reqOpts.method === 'POST' ? { 'Content-Type': 'application/json' } : {}),
              },
              { forceRefresh: true, skipEnvToken: hasEnvToken },
            );
            if (retryHeaders.Authorization && retryHeaders.Authorization !== reqOpts.headers?.Authorization) {
              if (hasEnvToken) {
                logTransient(
                  'warn',
                  `token-fallback:${reqUrl}`,
                  `[hermes-swarm] HTTP 401 with HERMES_A2A_HTTP_TOKEN on ${reqUrl}; falling back to internal_token`,
                );
              }
              try { await res.body?.cancel?.(); } catch {}
              res = await fetchImpl(reqUrl, { ...reqOpts, headers: retryHeaders });
            }
          }
          clearTimeout(connectTimer);
          if (res.ok && res.body) {
            const streamStartMs = Date.now();
            await consumeSseStream(res.body, (frame) => handleSseEvent(frame), ctrl.signal);
            if (Date.now() - streamStartMs >= MIN_HEALTHY_SSE_MS) {
              entry.backoffMs = 1000;
            }
          } else {
            try { await res.body?.cancel?.(); } catch {}
            if (!stopped && !entry.stopped) {
              logError(logger, `[hermes-swarm] wake SSE ${eventsUrl} returned HTTP ${res.status}`);
            }
          }
        } catch (err) {
          clearTimeout(connectTimer);
          if (!stopped && !entry.stopped && err?.name !== 'AbortError' && err?.name !== 'TimeoutError') {
            logTransient('error', `sse-host:${baseUrl}`, `[hermes-swarm] wake SSE failed (${eventsUrl}):`, err?.message || err);
          }
        }

        if (stopped || entry.stopped || hostSseEntries.get(baseUrl) !== entry) break;
        const delayMs = Math.max(100, Math.floor(Math.random() * entry.backoffMs));
        await new Promise((r) => setTimeout(r, delayMs));
        entry.backoffMs = Math.min(MAX_SSE_BACKOFF_MS, entry.backoffMs * 2);
      }
    };

    loop().catch((err) => {
      if (!stopped) {
        logError(logger, `[hermes-swarm] host-local SSE loop crashed (${baseUrl}):`, err?.message || err);
      }
    });
  }

  function ensureSseSubscription() {
    if (stopped) return;
    if (!isHostLocalMode) {
      const desiredBase = String(getUpstreamBase(scopedConv) || '').trim().replace(/\/+$/, '');
      if (singleSseEntry && singleSseEntry.baseUrl !== desiredBase) {
        singleSseEntry.stopped = true;
        try { singleSseEntry.controller?.abort(); } catch {}
        singleSseEntry = null;
      }
      startSingleSseSubscription();
      return;
    }
    const desired = new Set(getUpstreamBases());
    for (const [baseUrl, entry] of hostSseEntries.entries()) {
      if (!desired.has(baseUrl)) {
        entry.stopped = true;
        try { entry.controller?.abort(); } catch {}
        hostSseEntries.delete(baseUrl);
      }
    }
    for (const baseUrl of desired) {
      startHostLocalSseForBase(baseUrl);
    }
  }

  function stopSseSubscription() {
    if (singleSseEntry) {
      singleSseEntry.stopped = true;
      try { singleSseEntry.controller?.abort(); } catch {}
      singleSseEntry = null;
    }
    for (const entry of hostSseEntries.values()) {
      entry.stopped = true;
      try { entry.controller?.abort(); } catch {}
    }
    hostSseEntries.clear();
  }

  async function reconcileOnce() {
    if (stopped || reconcileInFlight || typeof sendMessage !== 'function') {
      return { reconciled: false, wakeups: 0 };
    }
    if (!isHostLocalMode && !scopedConv) {
      return { reconciled: false, wakeups: 0 };
    }
    reconcileInFlight = true;
    try {
      ensureSseSubscription();
      const wakeups = await (isHostLocalMode ? reconcileHostLocal() : reconcileSingleBoard());
      return { reconciled: true, wakeups };
    } catch (err) {
      logError(logger, '[hermes-swarm] wakeup reconcile failed:', err?.message || err);
      return { reconciled: false, wakeups: 0 };
    } finally {
      reconcileInFlight = false;
    }
  }

  function registerConversation(convId) {
    const clean = String(convId || '').trim();
    if (!clean) return false;
    const isNew = !registeredConvs.has(clean);
    if (!isNew) {
      registeredConvs.delete(clean);
    }
    registeredConvs.add(clean);
    while (registeredConvs.size > 500) {
      const oldest = registeredConvs.values().next().value;
      if (oldest === undefined) break;
      if (oldest === scopedConv) {
        registeredConvs.delete(oldest);
        registeredConvs.add(oldest);
      } else {
        registeredConvs.delete(oldest);
      }
    }
    if (isNew && isHostLocalMode && !stopped && reconcileTimer) {
      reconcileOnce().catch(() => {});
    }
    return true;
  }

  function start() {
    if (stopped || reconcileTimer) return;
    if (typeof sendMessage !== 'function') return;
    if (!isHostLocalMode && !scopedConv) return;

    ensureSseSubscription();
    reconcileOnce().catch(() => {});
    reconcileTimer = setInterval(() => {
      reconcileOnce().catch(() => {});
    }, Math.max(1000, Number(effectiveReconcileMs) || DEFAULT_HOST_LOCAL_RECONCILE_MS));
    if (typeof reconcileTimer.unref === 'function') reconcileTimer.unref();
  }

  function stop() {
    stopped = true;
    if (reconcileTimer) {
      clearInterval(reconcileTimer);
      reconcileTimer = null;
    }
    stopSseSubscription();
  }

  function getState() {
    if (!isHostLocalMode) {
      return {
        stopped,
        conversationId: scopedConv || null,
        activeStreams: singleSseEntry && !singleSseEntry.stopped ? [scopedConv] : [],
        cursors: cursorStore.all(),
      };
    }
    const activeStreams = [];
    for (const [baseUrl, entry] of hostSseEntries.entries()) {
      if (!entry.stopped) activeStreams.push(baseUrl);
    }
    return {
      stopped,
      hostLocalMode: true,
      conversationId: scopedConv || null,
      registeredConversations: [...registeredConvs],
      activeStreams,
      cursors: cursorStore.all(),
    };
  }

  return {
    start,
    stop,
    reconcileOnce,
    handleSseEvent,
    registerConversation,
    isConversationLocal: isLocalConv,
    getState,
  };
}
