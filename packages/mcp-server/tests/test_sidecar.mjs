import assert from 'node:assert/strict';
import { execFile, spawn } from 'node:child_process';
import {
  existsSync,
  mkdirSync,
  mkdtempSync,
  readdirSync,
  readFileSync,
  rmSync,
  writeFileSync,
} from 'node:fs';
import { createServer } from 'node:http';
import { homedir, tmpdir } from 'node:os';
import { dirname, join, resolve } from 'node:path';
import test from 'node:test';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { promisify } from 'node:util';

const execFileAsync = promisify(execFile);
const __dirname = dirname(fileURLToPath(import.meta.url));

const BRIDGE_DIR = resolve(__dirname, '..');
const SIBLING_PLUGIN_DIR = resolve(BRIDGE_DIR, '..', 'antigravity-plugin');
const PLUGIN_DIR = process.env.HERMES_SWARM_PLUGIN_DIR
  || (existsSync(SIBLING_PLUGIN_DIR) ? SIBLING_PLUGIN_DIR : join(homedir(), '.gemini', 'config', 'plugins', 'hermes-swarm'));
const SIDECAR_DIR = process.env.HERMES_SWARM_SIDECAR_DIR || join(PLUGIN_DIR, 'sidecars', 'swarm_board');
const MAIN_MJS_PATH = process.env.HERMES_SWARM_MAIN_MJS || join(SIDECAR_DIR, 'main.mjs');
const WAKEUP_BRIDGE_PATH = join(SIDECAR_DIR, 'wakeup_bridge.mjs');
const SIDECAR_JSON_PATH = join(SIDECAR_DIR, 'sidecar.json');
const PACKAGE_JSON_PATH = join(SIDECAR_DIR, 'package.json');
const BOARD_UI_DIR = join(BRIDGE_DIR, 'board_ui');

const sidecar = await import(pathToFileURL(MAIN_MJS_PATH).href);
const wakeup = await import(pathToFileURL(WAKEUP_BRIDGE_PATH).href);

const PNG_1X1 = Buffer.from(
  'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==',
  'base64',
);

function walkFiles(dir, out = []) {
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    const full = join(dir, entry.name);
    if (entry.isDirectory()) {
      walkFiles(full, out);
    } else {
      out.push(full);
    }
  }
  return out;
}

function stripJsComments(source) {
  return String(source)
    .replace(/\/\*[\s\S]*?\*\//g, '')
    .replace(/(^|[^:\\])\/\/.*$/gm, '$1');
}

async function getDeadPid() {
  const child = spawn(process.execPath, ['-e', 'process.exit(0)']);
  const pid = child.pid;
  await new Promise((resolveExit) => child.once('exit', resolveExit));
  return pid;
}

async function listenServer(server) {
  return new Promise((resolveListen, rejectListen) => {
    server.once('error', rejectListen);
    server.listen(0, '127.0.0.1', () => {
      server.removeListener('error', rejectListen);
      resolveListen(server.address().port);
    });
  });
}

function closeServer(server) {
  return new Promise((resolveClose) => {
    if (!server) return resolveClose();
    server.closeAllConnections?.();
    server.close(() => resolveClose());
  });
}

async function spawnSidecarProcess(extraEnv = {}, options = {}) {
  const env = {
    ...process.env,
    ANTIGRAVITY_LS_ADDRESS: 'http://127.0.0.1:1',
    ANTIGRAVITY_CSRF_TOKEN: 'test-csrf-token',
    ANTIGRAVITY_SIDECAR_WEB_PORT: '0',
    HERMES_A2A_BRIDGE_DIR: BRIDGE_DIR,
    ...extraEnv,
  };
  if (extraEnv.ANTIGRAVITY_CONVERSATION_ID === undefined) {
    delete env.ANTIGRAVITY_CONVERSATION_ID;
  }
  const child = spawn(process.execPath, [MAIN_MJS_PATH], {
    env,
    ...(options.cwd ? { cwd: options.cwd } : {}),
    stdio: ['ignore', 'pipe', 'pipe'],
  });

  let stdout = '';
  let stderr = '';
  const port = await new Promise((resolvePort, rejectPort) => {
    const timer = setTimeout(() => {
      rejectPort(new Error(`Timed out waiting for sidecar port. stdout=${stdout} stderr=${stderr}`));
    }, 10000);

    child.stdout.on('data', (chunk) => {
      stdout += chunk.toString('utf-8');
      const m = /listening on 127\.0\.0\.1:(\d+)/.exec(stdout);
      if (m) {
        clearTimeout(timer);
        resolvePort(Number(m[1]));
      }
    });
    child.stderr.on('data', (chunk) => {
      stderr += chunk.toString('utf-8');
    });
    child.once('exit', (code, signal) => {
      clearTimeout(timer);
      rejectPort(new Error(`Sidecar exited prematurely code=${code} signal=${signal} stderr=${stderr}`));
    });
  });

  return {
    child,
    port,
    baseUrl: `http://127.0.0.1:${port}`,
    getOutput: () => ({ stdout, stderr }),
    stop: async () => {
      if (child.exitCode === null && child.signalCode === null) {
        child.kill();
        await new Promise((resolveExit) => child.once('exit', resolveExit));
      }
    },
  };
}

// ---------------------------------------------------------------------------
// 1. Static & Security Hygiene Checks (1:1 Session-Bound Contract)
// ---------------------------------------------------------------------------

test('sidecar, plugin, and board_ui files pass syntax checks, JSON validity, and 1:1 static hygiene', async () => {
  for (const scriptPath of [
    MAIN_MJS_PATH,
    WAKEUP_BRIDGE_PATH,
    join(BOARD_UI_DIR, 'app.js'),
    join(BOARD_UI_DIR, 'hive.js'),
    join(BOARD_UI_DIR, 'channels.mjs'),
    join(BOARD_UI_DIR, 'narrator_bootstrap.js'),
  ]) {
    const { stdout, stderr } = await execFileAsync(process.execPath, ['--check', scriptPath]);
    assert.equal(stdout, '');
    assert.equal(stderr, '');
  }

  const sidecarJson = JSON.parse(readFileSync(SIDECAR_JSON_PATH, 'utf-8'));
  assert.equal(sidecarJson.has_web_ui, true);
  assert.equal(sidecarJson.agent_permissions?.allows_user_interactions, true);
  assert.equal(sidecarJson.env?.SWARM_OVERVIEW, '${flag:enable-swarm-overview}');
  assert.equal(sidecarJson.env?.SWARM_GRAPH_TAB, '${flag:enable-swarm-graph-tab}');
  assert.equal(sidecarJson.env?.SWARM_RAW_TAB, '${flag:enable-swarm-raw-tab}');

  const urlHostsPath = join(SIDECAR_DIR, 'url_hosts.json');
  assert.equal(existsSync(urlHostsPath), true, 'url_hosts.json must exist in sidecar dir');
  const urlHostsJson = JSON.parse(readFileSync(urlHostsPath, 'utf-8'));
  assert.equal(Array.isArray(urlHostsJson), true);
  assert.equal(urlHostsJson.includes('docs.google.com'), true, 'url_hosts.json must include docs.google.com');

  const pkgJson = JSON.parse(readFileSync(PACKAGE_JSON_PATH, 'utf-8'));
  assert.equal(pkgJson.type, 'module');

  const sdk = await sidecar.loadSidecarSdk();
  assert.equal(typeof sdk.SidecarApp, 'function');
  assert.equal(typeof sdk.Response, 'function');

  const forbiddenTermRe = new RegExp(
    '\\b(?:' +
      [
        'c' + 'to',
        'c' + 'to-gemini',
        'c' + 'tomsg',
        's' + 'enior',
        'j' + 'unior',
        'j' + 'uniors',
        'y' + 'olo',
        'o' + 'rg_role',
        'o' + 'rgRole',
        'openFor' + 'Ct' + 'o',
        'actionable' + 'Ct' + 'o',
        'com' + 'pany',
        'qu' + 'een',
        'recommended' + 'Models',
        'own' + 'er',
        'ti' + 'er1',
        'agy-' + 'peer',
      ].join('|') +
      ')\\b',
    'i',
  );

  const leadingSlashUrlRe = /(?:href|src|fetch|EventSource)\s*(?:=|\()\s*['"`]\/(?!\/)/;
  const switcherAndMultiSwarmChecks = [
    ['#' + 'swarm-switcher', /#swarm-switcher/],
    ['swarm' + 'Switcher', /swarmSwitcher/],
    ['selected' + 'Swarm', /selectedSwarm/],
    ['renderSwarm' + 'Switcher', /renderSwarmSwitcher/],
    ['state.' + 'swarms', /state\.swarms/],
    ['/board/' + 'swarms', /\/board\/swarms/],
    ['LS_TOKEN', /LS_TOKEN/],
    ['PYTHON_FALLBACK_DISPATCHER', /PYTHON_FALLBACK_DISPATCHER/],
    ['listeners(request)', /listeners\s*\(\s*['"]request['"]\s*\)/],
  ];

  // Strict token/auth-in-localStorage check: auth/token/secret keys in localStorage are
  // strictly forbidden, while non-secret UI toggle pref `swarm_board.narrator_plain` is permitted.
  const tokenInLocalStorageRe =
    /localStorage\s*(?:\.\s*(?:getItem|setItem|removeItem)\s*\(|\[)\s*['"`][^'"`]*(?:token|auth|secret|csrf|bearer|password|ls_token)[^'"`]*['"`]/i;
  const localStorageAnyRe = /\blocalStorage\b/;

  // board_ui/ (gif.js excluded): raw text must have ZERO hits for any pattern
  for (const filePath of walkFiles(BOARD_UI_DIR)) {
    if (filePath.endsWith('gif.js')) continue;
    const text = readFileSync(filePath, 'utf-8');
    for (const [label, re] of switcherAndMultiSwarmChecks) {
      assert.doesNotMatch(text, re, `${label} found in ${filePath}`);
    }
    assert.doesNotMatch(text, tokenInLocalStorageRe, `Auth/token localStorage key found in ${filePath}`);
    if (localStorageAnyRe.test(text)) {
      assert.match(
        text,
        /swarm_board\.narrator_plain/,
        `localStorage in ${filePath} may only be used for non-secret UI toggle pref swarm_board.narrator_plain`,
      );
    }
    assert.doesNotMatch(text, /\?swarm=/, `?swarm= found in ${filePath}`);
    assert.doesNotMatch(text, /\bPOST\b/, `POST found in ${filePath}`);
    assert.doesNotMatch(text, forbiddenTermRe, `Forbidden term found in ${filePath}`);
    if (/\.(?:html|js|mjs)$/i.test(filePath)) {
      assert.equal(
        leadingSlashUrlRe.test(text),
        false,
        `Leading-slash asset/API URL found in ${filePath}`,
      );
    }
  }

  // hermes-swarm/ (SVG excluded): zero forbidden terms, switcher/multi-swarm symbols,
  // token/auth localStorage, leading-slash URLs, or executable POST / ?swarm= code
  // (except wakeup_bridge.mjs POST /internal/wake/events fallback for >2048-char since_map).
  for (const filePath of walkFiles(PLUGIN_DIR)) {
    if (filePath.toLowerCase().endsWith('.svg')) continue;
    const text = readFileSync(filePath, 'utf-8');
    for (const [label, re] of switcherAndMultiSwarmChecks) {
      assert.doesNotMatch(text, re, `${label} found in plugin file ${filePath}`);
    }
    assert.doesNotMatch(text, tokenInLocalStorageRe, `Auth/token localStorage key found in plugin file ${filePath}`);
    assert.doesNotMatch(text, localStorageAnyRe, `Unexpected localStorage found in plugin file ${filePath}`);
    assert.doesNotMatch(text, forbiddenTermRe, `Forbidden legacy term found in plugin file: ${filePath}`);
    const codeOnly = /\.(?:js|mjs)$/i.test(filePath) ? stripJsComments(text) : text;
    assert.doesNotMatch(codeOnly, /\?swarm=/, `?swarm= found in code of ${filePath}`);
    if (filePath.endsWith('wakeup_bridge.mjs')) {
      const withoutWakePost = codeOnly
        .replace(/method:\s*usePost\s*\?\s*'POST'\s*:\s*'GET'/g, '')
        .replace(/POST\s+\$\{eventsUrl\}\s+returned\s+405/g, '')
        .replace(/\.\.\.\(reqOpts\.method\s*===\s*'POST'\s*\?\s*\{\s*'Content-Type':\s*'application\/json'\s*\}\s*:\s*\{\}\)/g, '');
      assert.doesNotMatch(withoutWakePost, /\bPOST\b/, `Unexpected POST found in ${filePath}`);
    } else {
      assert.doesNotMatch(codeOnly, /\bPOST\b/, `POST found in code of ${filePath}`);
    }
    if (/\.(?:html|js|mjs)$/i.test(filePath)) {
      assert.equal(
        leadingSlashUrlRe.test(codeOnly),
        false,
        `Leading-slash asset/API URL found in ${filePath}`,
      );
    }
  }
});

// ---------------------------------------------------------------------------
// 2. Sidecar Config, Path Validation, Preload Injection, and Upstream Discovery
// ---------------------------------------------------------------------------

test('createSidecarConfig, validateFramePath, isPathWithinRoot, and injectPreloadScript behave cleanly', (t) => {
  const cfg = sidecar.createSidecarConfig({
    HERMES_A2A_BRIDGE_DIR: BRIDGE_DIR,
    HERMES_A2A_HTTP_TOKEN: 'secret-token',
    ANTIGRAVITY_CONVERSATION_ID: 'conv-host',
    ANTIGRAVITY_PROJECT_ID: 'proj-1',
    ANTIGRAVITY_WORKSPACE_PATH: 'C:/Projects/host-ws',
  });
  assert.equal(cfg.bridgeDir, resolve(BRIDGE_DIR));
  assert.equal(cfg.uiDir, join(resolve(BRIDGE_DIR), 'board_ui'));
  assert.equal(cfg.stateDir, join(resolve(BRIDGE_DIR), 'state'));
  assert.equal(cfg.httpToken, 'secret-token');
  assert.equal(cfg.conversationId, 'conv-host');
  assert.equal(cfg.workspace, 'C:/Projects/host-ws');

  // extractDiscoveryHints ignores client ?swarm= and ?workspace= overrides
  assert.deepEqual(
    sidecar.extractDiscoveryHints(
      { swarm: 'evil-swarm', workspace: 'C:/Projects/evil-ws', conversationId: 'conv-q' },
      cfg,
    ),
    { conversationId: 'conv-q', workspace: 'C:/Projects/host-ws' },
  );

  assert.throws(
    () => sidecar.createSidecarConfig({ HERMES_A2A_BRIDGE_DIR: join(BRIDGE_DIR, 'missing-dir-xyz') }),
    /HERMES_A2A_BRIDGE_DIR does not exist/,
  );

  // Frame path validation
  assert.equal(sidecar.validateFramePath('shots/step1.png'), 'shots/step1.png');
  assert.equal(sidecar.validateFramePath('frames\\ok.webp'), 'frames\\ok.webp');
  assert.equal(sidecar.validateFramePath('../secret.png'), '');
  assert.equal(sidecar.validateFramePath('shots/../../etc/passwd.png'), '');
  assert.equal(sidecar.validateFramePath('shots/%2e%2e/secret.png'), '');
  assert.equal(sidecar.validateFramePath('shots/bad\0.png'), '');
  assert.equal(sidecar.validateFramePath('shots/readme.txt'), '');
  assert.equal(sidecar.validateFramePath('shots/data.json'), '');
  assert.equal(sidecar.validateFramePath(''), '');

  // Root containment check rejects sibling-prefix bypass
  const tempRoot = mkdtempSync(join(tmpdir(), 'hermes-sidecar-root-'));
  t.after(() => rmSync(tempRoot, { recursive: true, force: true }));
  const insideDir = join(tempRoot, 'ui');
  const siblingDir = join(tempRoot, 'ui-evil');
  mkdirSync(insideDir, { recursive: true });
  mkdirSync(siblingDir, { recursive: true });
  writeFileSync(join(insideDir, 'ok.txt'), 'ok');
  writeFileSync(join(siblingDir, 'leak.txt'), 'leak');

  assert.equal(sidecar.isPathWithinRoot(join(insideDir, 'ok.txt'), insideDir), true);
  assert.equal(sidecar.isPathWithinRoot(join(siblingDir, 'leak.txt'), insideDir), false);

  // Preload injection guarantees relative ./preload.js
  assert.equal(
    sidecar.injectPreloadScript('<html><head><script src="/preload.js"></script></head></html>'),
    '<html><head><script src="./preload.js"></script></head></html>',
  );
  assert.match(
    sidecar.injectPreloadScript('<html><head></head><body></body></html>'),
    /<script src="\.\/preload\.js"><\/script>/,
  );
});

test('resolveUpstreamUrl and isSwarmScopedToRequest enforce strict 1:1 conversationId scoping', async (t) => {
  const tempState = mkdtempSync(join(tmpdir(), 'hermes-sidecar-reg-'));
  t.after(() => rmSync(tempState, { recursive: true, force: true }));

  const deadPid = await getDeadPid();
  writeFileSync(
    join(tempState, 'registry.json'),
    JSON.stringify({
      servers: {
        deadOne: {
          pid: deadPid,
          httpUrl: 'http://127.0.0.1:19999',
          swarmId: 'alpha',
          conversationId: 'conv-dead',
          workspacePath: 'C:/Projects/alpha',
          updatedAt: '2099-01-01T00:00:00Z',
        },
        alpha: {
          pid: process.pid,
          httpUrl: 'http://127.0.0.1:19911',
          swarmId: 'alpha',
          primaryConversationId: 'conv-alpha',
          coordinatorConversationId: 'conv-alpha-coord',
          workspacePath: 'C:/Projects/alpha',
          updatedAt: '2026-11-22T10:00:00Z',
        },
        beta: {
          pid: process.pid,
          httpUrl: 'http://127.0.0.1:19912',
          swarmId: 'beta',
          primaryConversationId: 'conv-beta',
          workspacePath: 'C:\\Projects\\beta',
          updatedAt: '2026-11-22T11:00:00Z',
        },
      },
    }),
    'utf-8',
  );

  const cfg = sidecar.createSidecarConfig({
    HERMES_A2A_BRIDGE_DIR: BRIDGE_DIR,
    HERMES_A2A_STATE_DIR: tempState,
    HERMES_A2A_BOARD_URL: 'http://127.0.0.1:19900',
    ANTIGRAVITY_CONVERSATION_ID: 'conv-beta',
  });

  // Client ?swarm= and ?workspace= overrides are ignored; host conversationId conv-beta wins
  assert.equal(sidecar.resolveUpstreamUrl({ swarm: 'alpha' }, cfg), 'http://127.0.0.1:19912');
  assert.equal(sidecar.resolveUpstreamUrl({ workspace: 'C:/Projects/alpha' }, cfg), 'http://127.0.0.1:19912');
  assert.equal(sidecar.resolveUpstreamUrl({}, cfg), 'http://127.0.0.1:19912');

  // Trusted host config.workspace matches when conversationId is absent
  assert.equal(
    sidecar.resolveUpstreamUrl({}, { ...cfg, conversationId: '', workspace: 'file:///C:/Projects/alpha/' }),
    'http://127.0.0.1:19911',
  );
  // Explicit conversationId matches primary or coordinator conversation IDs
  assert.equal(
    sidecar.resolveUpstreamUrl({ conversationId: 'conv-alpha-coord' }, { ...cfg, conversationId: '' }),
    'http://127.0.0.1:19911',
  );
  // Dead PIDs are skipped even with futuristic updatedAt
  assert.equal(
    sidecar.resolveUpstreamUrl({ conversationId: 'conv-dead' }, { ...cfg, conversationId: '' }),
    'http://127.0.0.1:19912',
  );

  // Strict isSwarmScopedToRequest checks: concrete swarms without matching conversationId are rejected
  assert.equal(
    sidecar.isSwarmScopedToRequest({ swarm: 'beta', primaryConversationId: 'conv-beta' }, { conversationId: '' }),
    false,
  );
  assert.equal(
    sidecar.isSwarmScopedToRequest({ swarm: 'concrete-no-conv', members: [] }, { conversationId: 'conv-beta' }),
    false,
  );
  assert.equal(
    sidecar.isSwarmScopedToRequest({ id: 'concrete-id-only' }, { conversationId: 'conv-beta' }),
    false,
  );
  assert.equal(
    sidecar.isSwarmScopedToRequest({ meta: { swarmId: 'concrete-meta-only' } }, { conversationId: 'conv-beta' }),
    false,
  );
  assert.equal(
    sidecar.isSwarmScopedToRequest(
      { swarm: 'alpha', primaryConversationId: 'conv-alpha' },
      { conversationId: 'conv-beta' },
    ),
    false,
  );
  assert.equal(
    sidecar.isSwarmScopedToRequest({ swarm: null }, { conversationId: 'conv-beta' }),
    true,
  );
  assert.equal(
    sidecar.isSwarmScopedToRequest(
      { swarm: 'beta', primaryConversationId: 'conv-beta' },
      { conversationId: 'conv-beta' },
    ),
    true,
  );
  assert.equal(
    sidecar.isSwarmScopedToRequest(
      { swarm: 'alpha', coordinatorConversationId: 'conv-alpha-coord' },
      { conversationId: 'conv-alpha-coord' },
    ),
    true,
  );
});

// ---------------------------------------------------------------------------
// 3. Live SidecarApp Routes & 1:1 Session Isolation Boundaries
// ---------------------------------------------------------------------------

test('SidecarApp routes serve UI, /preload.js, static assets, strict 1:1 /api/swarm & /api/frame, and reject all POSTs', async (t) => {
  const tempState = mkdtempSync(join(tmpdir(), 'hermes-sidecar-http-'));
  t.after(() => rmSync(tempState, { recursive: true, force: true }));

  const recorded = [];
  let etagMode = '200';
  let frameMode = 'png';
  let upstreamMode = 'alpha';

  const upstreamServer = createServer((req, res) => {
    const reqUrl = new URL(req.url, 'http://127.0.0.1');
    recorded.push({
      method: req.method,
      pathname: reqUrl.pathname,
      search: reqUrl.search,
      query: Object.fromEntries(reqUrl.searchParams.entries()),
      authorization: req.headers.authorization || '',
      ifNoneMatch: req.headers['if-none-match'] || '',
    });

    if (reqUrl.pathname === '/board') {
      if (upstreamMode === 'mismatched-concrete') {
        res.writeHead(200, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({
          swarm: 'foreign',
          primaryConversationId: 'conv-other',
          coordinatorConversationId: 'conv-other-coord',
          members: [{ name: 'alien', role: 'worker', status: 'working' }],
          messages: [{ seq: 1, agent: 'alien', text: 'secret' }],
        }));
        return;
      }

      if (etagMode === '304' && reqUrl.searchParams.get('etag') === 'etag-v1') {
        res.writeHead(304, { ETag: 'etag-v1' });
        res.end();
        return;
      }
      if (etagMode === 'json-unchanged' && reqUrl.searchParams.get('etag') === 'etag-v1') {
        res.writeHead(200, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({
          unchanged: true,
          swarm: 'alpha',
          etag: 'etag-v1',
          primaryConversationId: 'conv-primary-a',
        }));
        return;
      }

      res.writeHead(200, { 'Content-Type': 'application/json', ETag: 'etag-v1' });
      res.end(JSON.stringify({
        swarm: 'alpha',
        id: 'alpha',
        etag: 'etag-v1',
        primaryConversationId: 'conv-primary-a',
        coordinatorConversationId: 'conv-coord-a',
        swarms: [
          { swarmId: 'alpha', primaryConversationId: 'conv-primary-a', title: 'Alpha' },
          { swarmId: 'foreign', primaryConversationId: 'conv-other', title: 'Foreign' },
        ],
        members: [
          { name: 'coordinator', role: 'coordinator', status: 'waiting', conversationId: 'conv-coord-a' },
        ],
        messages: [],
      }));
      return;
    }

    if (reqUrl.pathname === '/board/frame') {
      if (frameMode === 'bad-mime') {
        res.writeHead(200, { 'Content-Type': 'text/plain; charset=utf-8' });
        res.end('not an image');
        return;
      }
      res.writeHead(200, { 'Content-Type': 'image/png' });
      res.end(PNG_1X1);
      return;
    }

    res.writeHead(404, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify({ error: 'not found' }));
  });

  const upstreamPort = await listenServer(upstreamServer);
  t.after(() => closeServer(upstreamServer));

  writeFileSync(
    join(tempState, 'registry.json'),
    JSON.stringify({
      servers: {
        alpha: {
          pid: process.pid,
          httpUrl: `http://127.0.0.1:${upstreamPort}`,
          swarmId: 'alpha',
          primaryConversationId: 'conv-primary-a',
          coordinatorConversationId: 'conv-coord-a',
        },
      },
    }),
    'utf-8',
  );

  const token = 'secret-bearer-token';

  // First: sidecar with NO ANTIGRAVITY_CONVERSATION_ID returns { conversationId: null, swarm: null, member: null }
  const unscopedProc = await spawnSidecarProcess({
    HERMES_A2A_STATE_DIR: tempState,
    HERMES_A2A_HTTP_TOKEN: token,
    HERMES_A2A_BOARD_URL: `http://127.0.0.1:${upstreamPort}`,
    HERMES_SWARM_DISABLE_WAKEUP: '1',
  });
  try {
    const emptyRes = await fetch(`${unscopedProc.baseUrl}/api/swarm?swarm=alpha&workspace=C%3A%2Fevil`);
    assert.equal(emptyRes.status, 200);
    assert.deepEqual(await emptyRes.json(), { conversationId: null, swarm: null, member: null });
    assert.equal(
      recorded.filter((r) => r.pathname === '/board').length,
      0,
      'Missing conversationId must return immediately without hitting upstream /board',
    );
    const emptyFrame = await fetch(
      `${unscopedProc.baseUrl}/api/frame?swarm=alpha&path=${encodeURIComponent('shots/step1.png')}`,
    );
    assert.equal(emptyFrame.status, 404);
    assert.equal(
      recorded.filter((r) => r.pathname === '/board/frame').length,
      0,
      'Missing conversationId must return 404 on /api/frame without hitting upstream',
    );
  } finally {
    await unscopedProc.stop();
  }

  // Second: session-scoped sidecar bound to ANTIGRAVITY_CONVERSATION_ID='conv-primary-a'
  const sidecarProc = await spawnSidecarProcess({
    HERMES_A2A_STATE_DIR: tempState,
    HERMES_A2A_HTTP_TOKEN: token,
    ANTIGRAVITY_CONVERSATION_ID: 'conv-primary-a',
    HERMES_SWARM_DISABLE_WAKEUP: '1',
  });
  t.after(() => sidecarProc.stop());

  // 1. GET / serves board_ui/index.html with relative ./preload.js
  const idxRes = await fetch(`${sidecarProc.baseUrl}/`);
  assert.equal(idxRes.status, 200);
  assert.match(idxRes.headers.get('content-type') || '', /text\/html/);
  const idxHtml = await idxRes.text();
  assert.match(idxHtml, /<script src="\.\/preload\.js"><\/script>/);
  assert.doesNotMatch(idxHtml, /src="\/preload\.js"/);

  // 2. GET /preload.js served natively by SidecarApp
  const preloadRes = await fetch(`${sidecarProc.baseUrl}/preload.js`);
  assert.equal(preloadRes.status, 200);
  assert.match(preloadRes.headers.get('content-type') || '', /javascript/);
  const preloadJs = await preloadRes.text();
  assert.match(preloadJs, /window\.sidecar/);

  // 3. Static assets from board_ui/
  for (const [assetPath, expectedMime] of [
    ['/styles.css', /text\/css/],
    ['/hive.css', /text\/css/],
    ['/app.js', /javascript/],
    ['/hive.js', /javascript/],
    ['/gif.js', /javascript/],
    ['/channels.mjs', /javascript/],
    ['/narrator_bootstrap.js', /(?:javascript|text\/plain)/],
  ]) {
    const assetRes = await fetch(`${sidecarProc.baseUrl}${assetPath}`);
    assert.equal(assetRes.status, 200, `Expected 200 for ${assetPath}`);
    assert.match(assetRes.headers.get('content-type') || '', expectedMime);
  }

  // 4. GET /api/swarm: ignores ?swarm= and ?workspace=, strips swarms, sends Bearer + host conversationId
  const swRes = await fetch(
    `${sidecarProc.baseUrl}/api/swarm?swarm=foreign&workspace=C%3A%2Fevil-ws`,
  );
  assert.equal(swRes.status, 200);
  const swJson = await swRes.json();
  assert.equal(swJson.online, true);
  assert.equal(swJson.swarm, 'alpha');
  assert.equal(swJson.conversationId, 'conv-primary-a');
  assert.equal('swarms' in swJson, false, 'swarms list must be stripped from 1:1 /api/swarm payload');

  const lastBoardReq = recorded.filter((r) => r.pathname === '/board').at(-1);
  assert.equal(lastBoardReq.query.conversationId, 'conv-primary-a');
  assert.equal('swarm' in lastBoardReq.query, false, '?swarm= must not be forwarded upstream');
  assert.equal('workspace' in lastBoardReq.query, false, '?workspace= must not be forwarded upstream');
  assert.equal(lastBoardReq.authorization, `Bearer ${token}`);

  // Mismatched concrete swarm conversationId returned from upstream is rejected
  upstreamMode = 'mismatched-concrete';
  const mismatchRes = await fetch(`${sidecarProc.baseUrl}/api/swarm`);
  assert.equal(mismatchRes.status, 200);
  const mismatchJson = await mismatchRes.json();
  assert.equal(mismatchJson.swarm, null);
  assert.equal(mismatchJson.member, null);
  assert.deepEqual(mismatchJson.messages, []);
  assert.equal('swarms' in mismatchJson, false);

  // Also rejects when ?conversationId=conv-other does not match upstream primaryConversationId
  upstreamMode = 'alpha';
  const foreignConvRes = await fetch(`${sidecarProc.baseUrl}/api/swarm?conversationId=conv-other`);
  assert.equal(foreignConvRes.status, 200);
  const foreignConvJson = await foreignConvRes.json();
  assert.equal(foreignConvJson.conversationId, 'conv-other');
  assert.equal(foreignConvJson.swarm, null);
  assert.deepEqual(foreignConvJson.messages, []);

  // ETag HTTP 304 handling
  etagMode = '304';
  const etag304Res = await fetch(`${sidecarProc.baseUrl}/api/swarm?etag=etag-v1`);
  assert.equal(etag304Res.status, 200);
  const etag304Json = await etag304Res.json();
  assert.equal(etag304Json.unchanged, true);
  assert.equal(etag304Json.etag, 'etag-v1');
  assert.equal(etag304Json.swarm, 'alpha');
  assert.equal(recorded.at(-1).ifNoneMatch, 'etag-v1');

  // ETag JSON { unchanged: true } handling
  etagMode = 'json-unchanged';
  const etagJsonRes = await fetch(`${sidecarProc.baseUrl}/api/swarm?etag=etag-v1`);
  assert.equal(etagJsonRes.status, 200);
  const etagJsonBody = await etagJsonRes.json();
  assert.equal(etagJsonBody.unchanged, true);
  assert.equal(etagJsonBody.online, true);

  // 5. GET /api/frame: valid PNG, path traversal/non-image/bad-MIME blocked, workspace/swarm params dropped
  const okFrame = await fetch(
    `${sidecarProc.baseUrl}/api/frame?swarm=alpha&workspace=C%3A%2Fevil-root&path=${encodeURIComponent('shots/step1.png')}`,
  );
  assert.equal(okFrame.status, 200);
  assert.equal(okFrame.headers.get('content-type'), 'image/png');
  assert.deepEqual(Buffer.from(await okFrame.arrayBuffer()), PNG_1X1);

  const frameReq = recorded.filter((r) => r.pathname === '/board/frame').at(-1);
  assert.equal(frameReq.authorization, `Bearer ${token}`);
  assert.equal(frameReq.query.conversationId, 'conv-primary-a');
  assert.equal(frameReq.query.path, 'shots/step1.png');
  assert.equal('workspace' in frameReq.query, false, 'workspace param must be dropped');
  assert.equal('workdir' in frameReq.query, false);
  assert.equal('swarm' in frameReq.query, false, 'swarm param must be dropped');

  const prevFrameCount = recorded.filter((r) => r.pathname === '/board/frame').length;
  for (const badPath of [
    '../secret.png',
    'shots/../../secret.png',
    '%2e%2e/secret.png',
    'notes.txt',
    'board.json',
    'app.js',
  ]) {
    const badRes = await fetch(
      `${sidecarProc.baseUrl}/api/frame?path=${encodeURIComponent(badPath)}`,
    );
    assert.equal(badRes.status, 404, `Expected 404 for blocked path ${badPath}`);
  }
  assert.equal(
    recorded.filter((r) => r.pathname === '/board/frame').length,
    prevFrameCount,
    'Blocked frame paths must never reach upstream',
  );

  // Non-image upstream MIME rejected
  frameMode = 'bad-mime';
  const badMimeRes = await fetch(
    `${sidecarProc.baseUrl}/api/frame?path=${encodeURIComponent('shots/fake.png')}`,
  );
  assert.equal(badMimeRes.status, 404);

  // 6. All POST requests return 404 or 405
  for (const postPath of [
    '/api/swarm',
    '/api/events',
    '/api/frame',
    '/styles.css',
    '/board/post',
    '/board/spawn',
    '/board/channels',
    '/board/plan',
    '/board/swarms',
  ]) {
    const postRes = await fetch(`${sidecarProc.baseUrl}${postPath}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({}),
    });
    assert.ok(
      postRes.status === 404 || postRes.status === 405,
      `Expected 404 or 405 on POST ${postPath}, got ${postRes.status}`,
    );
  }
});

test('GET /api/events returns strictly 1:1 conversationId-scoped buffered SSE responses across snapshot/update/unchanged/empty', async (t) => {
  const tempState = mkdtempSync(join(tmpdir(), 'hermes-sidecar-events-'));
  t.after(() => rmSync(tempState, { recursive: true, force: true }));

  const recorded = [];
  let mode = 'snapshot';

  const upstreamServer = createServer((req, res) => {
    const reqUrl = new URL(req.url, 'http://127.0.0.1');
    recorded.push({
      method: req.method,
      pathname: reqUrl.pathname,
      query: Object.fromEntries(reqUrl.searchParams.entries()),
      authorization: req.headers.authorization || '',
      ifNoneMatch: req.headers['if-none-match'] || '',
    });

    if (reqUrl.pathname === '/board') {
      if (mode === 'mismatched') {
        res.writeHead(200, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({
          swarm: 'foreign',
          head: 99,
          primaryConversationId: 'conv-other',
          coordinatorConversationId: 'conv-other-coord',
          swarms: [{ swarmId: 'foreign' }],
          members: [{ name: 'alien', role: 'worker', status: 'working' }],
          messages: [{ seq: 1, agent: 'alien', text: 'secret' }],
        }));
        return;
      }
      if (mode === '304' && reqUrl.searchParams.get('etag') === 'ev-v1') {
        res.writeHead(304, { ETag: 'ev-v1' });
        res.end();
        return;
      }
      if (mode === 'json-unchanged' && reqUrl.searchParams.get('etag') === 'ev-v1') {
        res.writeHead(200, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({
          unchanged: true,
          swarm: 'alpha',
          etag: 'ev-v1',
          primaryConversationId: 'conv-events-a',
          swarms: [{ swarmId: 'alpha' }, { swarmId: 'foreign' }],
        }));
        return;
      }
      if (mode === 'update') {
        res.writeHead(200, { 'Content-Type': 'application/json', ETag: 'ev-v2' });
        res.end(JSON.stringify({
          swarm: 'alpha',
          id: 'alpha',
          etag: 'ev-v2',
          head: 15,
          primaryConversationId: 'conv-events-a',
          coordinatorConversationId: 'conv-events-coord-a',
          swarms: [{ swarmId: 'alpha' }, { swarmId: 'foreign' }],
          members: [{ name: 'coder', role: 'worker', status: 'working', steps: 3 }],
          messages: [],
        }));
        return;
      }
      res.writeHead(200, { 'Content-Type': 'application/json', ETag: 'ev-v1' });
      res.end(JSON.stringify({
        swarm: 'alpha',
        id: 'alpha',
        etag: 'ev-v1',
        head: 12,
        primaryConversationId: 'conv-events-a',
        coordinatorConversationId: 'conv-events-coord-a',
        swarms: [{ swarmId: 'alpha' }, { swarmId: 'foreign' }],
        members: [{ name: 'coordinator', role: 'coordinator', status: 'waiting' }],
        messages: [],
      }));
      return;
    }

    res.writeHead(404);
    res.end();
  });

  const upstreamPort = await listenServer(upstreamServer);
  t.after(() => closeServer(upstreamServer));

  writeFileSync(
    join(tempState, 'registry.json'),
    JSON.stringify({
      servers: {
        alpha: {
          pid: process.pid,
          httpUrl: `http://127.0.0.1:${upstreamPort}`,
          swarmId: 'alpha',
          primaryConversationId: 'conv-events-a',
          coordinatorConversationId: 'conv-events-coord-a',
        },
      },
    }),
    'utf-8',
  );

  const token = 'events-bearer-token';

  const parseSseResponse = async (res) => {
    assert.equal(res.status, 200);
    assert.equal(res.headers.get('content-type'), 'text/event-stream; charset=utf-8');
    assert.equal(res.headers.get('cache-control'), 'no-cache');
    const rawText = await res.text();
    const retryMatch = /^retry:\s*(\d+)/m.exec(rawText);
    const frames = [];
    const stream = new ReadableStream({
      start(controller) {
        controller.enqueue(new TextEncoder().encode(rawText));
        controller.close();
      },
    });
    await wakeup.consumeSseStream(stream, async (frame) => {
      frames.push(frame);
    });
    assert.equal(frames.length, 1, `Expected 1 SSE frame, got ${frames.length}: ${rawText}`);
    return {
      retry: retryMatch ? Number(retryMatch[1]) : null,
      event: frames[0].event,
      id: frames[0].id,
      data: JSON.parse(frames[0].data),
    };
  };

  // 1. Missing conversationId -> event: empty without hitting upstream
  const unscopedProc = await spawnSidecarProcess({
    HERMES_A2A_STATE_DIR: tempState,
    HERMES_A2A_HTTP_TOKEN: token,
    HERMES_A2A_BOARD_URL: `http://127.0.0.1:${upstreamPort}`,
    HERMES_SWARM_DISABLE_WAKEUP: '1',
  });
  try {
    const emptyRes = await fetch(
      `${unscopedProc.baseUrl}/api/events?swarm=alpha&workspace=C%3A%2Fevil`,
    );
    const parsedEmpty = await parseSseResponse(emptyRes);
    assert.equal(parsedEmpty.event, 'empty');
    assert.equal(parsedEmpty.retry, 3000);
    assert.equal(parsedEmpty.id, '0');
    assert.deepEqual(parsedEmpty.data, { conversationId: null, swarm: null, member: null });
    assert.equal('swarms' in parsedEmpty.data, false);
    assert.equal(
      recorded.filter((r) => r.pathname === '/board').length,
      0,
      'Missing conversationId on /api/events must not hit upstream /board',
    );
  } finally {
    await unscopedProc.stop();
  }

  // 2. Session-scoped sidecar bound to conv-events-a
  const sidecarProc = await spawnSidecarProcess({
    HERMES_A2A_STATE_DIR: tempState,
    HERMES_A2A_HTTP_TOKEN: token,
    ANTIGRAVITY_CONVERSATION_ID: 'conv-events-a',
    HERMES_SWARM_DISABLE_WAKEUP: '1',
  });
  t.after(() => sidecarProc.stop());

  // 2a. Initial load (no etag) -> event: snapshot, swarms stripped, ?swarm=&?workspace= dropped
  mode = 'snapshot';
  const snapParsed = await parseSseResponse(
    await fetch(`${sidecarProc.baseUrl}/api/events?swarm=foreign&workspace=C%3A%2Fevil`),
  );
  assert.equal(snapParsed.event, 'snapshot');
  assert.equal(snapParsed.retry, 1000);
  assert.equal(snapParsed.id, '12');
  assert.equal(snapParsed.data.online, true);
  assert.equal(snapParsed.data.swarm, 'alpha');
  assert.equal(snapParsed.data.conversationId, 'conv-events-a');
  assert.equal('swarms' in snapParsed.data, false, 'swarms must be stripped from SSE snapshot');
  const lastSnapReq = recorded.filter((r) => r.pathname === '/board').at(-1);
  assert.equal(lastSnapReq.query.conversationId, 'conv-events-a');
  assert.equal('swarm' in lastSnapReq.query, false);
  assert.equal('workspace' in lastSnapReq.query, false);
  assert.equal(lastSnapReq.authorization, `Bearer ${token}`);

  // 2b. HTTP 304 -> event: unchanged
  mode = '304';
  const unchanged304 = await parseSseResponse(
    await fetch(`${sidecarProc.baseUrl}/api/events?etag=ev-v1`),
  );
  assert.equal(unchanged304.event, 'unchanged');
  assert.equal(unchanged304.retry, 1000);
  assert.equal(unchanged304.data.unchanged, true);
  assert.equal(unchanged304.data.swarm, 'alpha');
  assert.equal('swarms' in unchanged304.data, false);
  assert.equal(recorded.at(-1).ifNoneMatch, 'ev-v1');

  // 2c. JSON { unchanged: true } -> event: unchanged, swarms stripped
  mode = 'json-unchanged';
  const unchangedJson = await parseSseResponse(
    await fetch(`${sidecarProc.baseUrl}/api/events?etag=ev-v1`),
  );
  assert.equal(unchangedJson.event, 'unchanged');
  assert.equal(unchangedJson.retry, 1000);
  assert.equal(unchangedJson.data.unchanged, true);
  assert.equal('swarms' in unchangedJson.data, false);

  // 2d. Newer etag -> event: update, id: 15, swarms stripped
  mode = 'update';
  const updateParsed = await parseSseResponse(
    await fetch(`${sidecarProc.baseUrl}/api/events?etag=ev-v1`),
  );
  assert.equal(updateParsed.event, 'update');
  assert.equal(updateParsed.retry, 1000);
  assert.equal(updateParsed.id, '15');
  assert.equal(updateParsed.data.etag, 'ev-v2');
  assert.equal(updateParsed.data.head, 15);
  assert.equal('swarms' in updateParsed.data, false, 'swarms must be stripped from SSE update');

  // 2e. Mismatched concrete upstream swarm -> event: empty, swarms stripped
  mode = 'mismatched';
  const mismatchParsed = await parseSseResponse(
    await fetch(`${sidecarProc.baseUrl}/api/events`),
  );
  assert.equal(mismatchParsed.event, 'empty');
  assert.equal(mismatchParsed.retry, 3000);
  assert.equal(mismatchParsed.id, '0');
  assert.equal(mismatchParsed.data.swarm, null);
  assert.equal('swarms' in mismatchParsed.data, false);

  // 2f. Foreign ?conversationId=conv-other -> event: empty
  mode = 'snapshot';
  const foreignConvParsed = await parseSseResponse(
    await fetch(`${sidecarProc.baseUrl}/api/events?conversationId=conv-other`),
  );
  assert.equal(foreignConvParsed.event, 'empty');
  assert.equal(foreignConvParsed.retry, 3000);
  assert.equal(foreignConvParsed.data.conversationId, 'conv-other');
  assert.equal(foreignConvParsed.data.swarm, null);
  assert.equal('swarms' in foreignConvParsed.data, false);
});

// ---------------------------------------------------------------------------
// 4. Wakeup Bridge: 1:1 Scoping, 3-Tier Classification, SSE, Cursor Durability
// ---------------------------------------------------------------------------

test('isSwarmInScope and classifyBoardMessage enforce strict 1:1 conversationId scope and 3-tier isolation', () => {
  const swarm = {
    swarmId: 'sw-1',
    primaryConversationId: 'conv-p1',
    coordinatorConversationId: 'conv-c1',
    workspacePath: 'C:\\Projects\\my-app',
  };

  assert.equal(wakeup.isSwarmInScope(swarm, 'conv-p1'), true);
  assert.equal(wakeup.isSwarmInScope(swarm, { conversationId: 'conv-p1' }), true);
  assert.equal(wakeup.isSwarmInScope(swarm, { conversationId: 'conv-c1' }), true);
  assert.equal(wakeup.isSwarmInScope(swarm, { conversationId: 'conv-other' }), false);
  // Workspace-only or empty scope is NEVER accepted in 1:1 mode
  assert.equal(
    wakeup.isSwarmInScope(swarm, { workspaces: ['file:///C:/Projects/my-app/'] }),
    false,
  );
  assert.equal(wakeup.isSwarmInScope(swarm, { allowUnscoped: true }), false);
  assert.equal(wakeup.isSwarmInScope(swarm, ''), false);

  // Tier-2 workers -> coordinator ONLY
  assert.equal(
    wakeup.classifyBoardMessage({
      sender: 'backend',
      senderRole: 'worker',
      kind: 'question',
      status: 'open',
      mentions: ['coordinator'],
      summary: 'Need API shape',
    }),
    'coordinator',
  );
  assert.equal(
    wakeup.classifyBoardMessage({
      sender: 'backend',
      senderRole: 'worker',
      kind: 'post',
      status: 'open',
      summary: '[done] Finished routes',
    }),
    'coordinator',
  );
  assert.equal(
    wakeup.classifyBoardMessage({
      sender: 'monitor',
      senderRole: 'monitor',
      kind: 'post',
      status: 'open',
      summary: '[stall] No progress for 120s',
    }),
    'coordinator',
  );
  // Closed or pure worker-to-worker chatter -> null
  assert.equal(
    wakeup.classifyBoardMessage({
      sender: 'backend',
      senderRole: 'worker',
      kind: 'question',
      status: 'resolved',
      mentions: ['coordinator'],
      summary: 'Already handled',
    }),
    null,
  );
  assert.equal(
    wakeup.classifyBoardMessage({
      sender: 'backend',
      senderRole: 'worker',
      kind: 'post',
      status: 'open',
      mentions: ['frontend'],
      summary: '@frontend schema is ready',
    }),
    null,
  );

  // Coordinator -> primary ONLY on escalation or @primary
  assert.equal(
    wakeup.classifyBoardMessage({
      sender: 'coordinator',
      senderRole: 'coordinator',
      kind: 'escalation',
      status: 'open',
      summary: 'Approve migration?',
    }),
    'primary',
  );
  assert.equal(
    wakeup.classifyBoardMessage({
      sender: 'coordinator',
      senderRole: 'coordinator',
      kind: 'post',
      status: 'open',
      summary: '@primary [done] All verified',
    }),
    'primary',
  );
  assert.equal(
    wakeup.classifyBoardMessage({
      sender: 'coordinator',
      senderRole: 'coordinator',
      kind: 'post',
      status: 'open',
      summary: '@backend please run tests',
    }),
    null,
  );

  // Tier-3 subagents -> ALWAYS ignored regardless of @coordinator/@primary/kind
  assert.equal(
    wakeup.classifyBoardMessage({
      sender: 'leaf-helper',
      senderRole: 'subagent',
      senderTier: 3,
      kind: 'escalation',
      status: 'open',
      mentions: ['coordinator', 'primary'],
      summary: '@coordinator @primary [done] subagent note',
    }),
    null,
  );
  assert.equal(
    wakeup.classifyBoardMessage({
      sender: 'leaf-helper',
      senderTier: 3,
      kind: 'blocker',
      status: 'open',
      summary: '@coordinator stuck',
    }),
    null,
  );
});

test('wakeup_bridge stays completely idle when scopedConv is missing', async () => {
  const fetchCalls = [];
  const bridge = wakeup.createWakeupBridge({
    sendMessage: async () => {
      throw new Error('Must never dispatch when scopedConv is missing');
    },
    config: {
      fallbackBoardUrl: 'http://127.0.0.1:19999',
      conversationId: '',
    },
    fetchImpl: async (url) => {
      fetchCalls.push(String(url));
      return new Response('{}', { status: 200 });
    },
    logger: { warn: () => {} },
  });

  bridge.start();
  const rec = await bridge.reconcileOnce();
  const sseRes = await bridge.handleSseEvent({
    event: 'coordinator_wake',
    data: JSON.stringify({ swarmId: 's1', message: { seq: 1, summary: 'hi' } }),
  });

  assert.deepEqual(rec, { reconciled: false, wakeups: 0 });
  assert.equal(sseRes, 0);
  assert.deepEqual(fetchCalls, []);
  assert.deepEqual(bridge.getState(), {
    stopped: false,
    conversationId: null,
    activeStreams: [],
    cursors: {},
  });
  bridge.stop();
});

test('wakeup_bridge connects directly to /board/events?conversationId=, reconciles /board?conversationId= (zero /board/swarms), enforces cursor durability and 3-tier routing', async (t) => {
  const tempDir = mkdtempSync(join(tmpdir(), 'hermes-wakeup-sse-'));
  t.after(() => rmSync(tempDir, { recursive: true, force: true }));
  const cursorFile = join(tempDir, 'cursors.json');

  const sseClients = new Set();
  const requestedPaths = [];
  const boardRequests = [];
  const sseRequests = [];

  let boardState = {
    swarmId: 'scoped-swarm',
    primaryConversationId: 'conv-primary-1',
    coordinatorConversationId: 'conv-coord-1',
    plan: { goal: { title: 'Clean Observer Rebuild' } },
    activeTasks: [{ taskId: 't1', workerName: 'backend', state: 'TASK_STATE_WORKING' }],
    recentCompletedTasks: [],
    messages: [],
  };

  const upstreamServer = createServer((req, res) => {
    const reqUrl = new URL(req.url, 'http://127.0.0.1');
    requestedPaths.push(reqUrl.pathname);

    if (reqUrl.pathname === '/board') {
      const conv = reqUrl.searchParams.get('conversationId');
      boardRequests.push({
        conversationId: conv,
        swarm: reqUrl.searchParams.get('swarm'),
      });
      if (conv !== 'conv-primary-1') {
        res.writeHead(404, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({ error: 'out of scope' }));
        return;
      }
      res.writeHead(200, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify(boardState));
      return;
    }

    if (reqUrl.pathname === '/board/events') {
      sseRequests.push({
        conversationId: reqUrl.searchParams.get('conversationId'),
        since: reqUrl.searchParams.get('since'),
      });
      res.writeHead(200, {
        'Content-Type': 'text/event-stream; charset=utf-8',
        'Cache-Control': 'no-cache',
      });
      res.write(': connected\n\n');
      sseClients.add(res);
      res.on('close', () => sseClients.delete(res));
      return;
    }

    res.writeHead(404);
    res.end();
  });

  const port = await listenServer(upstreamServer);
  t.after(async () => {
    for (const c of sseClients) c.destroy();
    await closeServer(upstreamServer);
  });

  const sent = [];
  let failNextSend = true;
  const sendMessage = async (convId, message, opts) => {
    if (failNextSend) {
      failNextSend = false;
      throw new Error('transient delivery failure');
    }
    sent.push({ convId, message, opts });
  };

  const cursorStore = wakeup.createCursorStore(cursorFile);
  const bridge = wakeup.createWakeupBridge({
    sendMessage,
    config: {
      fallbackBoardUrl: `http://127.0.0.1:${port}`,
      conversationId: 'conv-primary-1',
      projectId: 'proj-test',
    },
    cursorStore,
    reconcileMs: 60_000,
    logger: { warn: () => {} },
  });

  bridge.start();
  t.after(() => bridge.stop());

  // Wait for SSE client connection
  for (let i = 0; i < 40 && sseClients.size === 0; i++) {
    await new Promise((r) => setTimeout(r, 25));
  }
  assert.equal(sseClients.size, 1);
  assert.equal(sseRequests[0].conversationId, 'conv-primary-1');
  assert.equal(boardRequests.length >= 1, true);
  assert.equal(boardRequests[0].conversationId, 'conv-primary-1');
  assert.equal(boardRequests[0].swarm, null);
  assert.equal(
    requestedPaths.includes('/board/swarms'),
    false,
    'wakeup_bridge must NEVER poll /board/swarms',
  );

  const sseRes = [...sseClients][0];

  // 1. Transient sendMessage failure: cursor MUST NOT advance on coordinator_wake seq=10
  sseRes.write(
    `event: coordinator_wake\ndata: ${JSON.stringify({
      swarmId: 'scoped-swarm',
      coordinatorConversationId: 'conv-coord-1',
      primaryConversationId: 'conv-primary-1',
      message: {
        seq: 10,
        id: 'm-10',
        sender: 'backend',
        senderRole: 'worker',
        kind: 'question',
        status: 'open',
        mentions: ['coordinator'],
        summary: 'Confirm route contract',
      },
    })}\n\n`,
  );
  await new Promise((r) => setTimeout(r, 80));
  assert.equal(sent.length, 0);
  assert.equal(cursorStore.load('scoped-swarm').coordinatorSeq, 0);
  assert.equal(cursorStore.load('scoped-swarm').lastDispatchedSeq, 0);

  // 2. Retry same seq=10 -> now succeeds and advances cursor
  await bridge.handleSseEvent('scoped-swarm', {
    event: 'coordinator_wake',
    data: JSON.stringify({
      swarmId: 'scoped-swarm',
      coordinatorConversationId: 'conv-coord-1',
      primaryConversationId: 'conv-primary-1',
      message: {
        seq: 10,
        id: 'm-10',
        sender: 'backend',
        senderRole: 'worker',
        kind: 'question',
        status: 'open',
        mentions: ['coordinator'],
        summary: 'Confirm route contract',
      },
    }),
  });
  assert.equal(sent.length, 1);
  assert.equal(sent[0].convId, 'conv-coord-1');
  assert.match(sent[0].message, /Confirm route contract/);
  assert.equal(cursorStore.load('scoped-swarm').coordinatorSeq, 10);
  assert.equal(cursorStore.load('scoped-swarm').lastDispatchedSeq, 10);

  // 3. Tier-3 subagent coordinator_wake SSE event is ignored
  sseRes.write(
    `event: coordinator_wake\ndata: ${JSON.stringify({
      swarmId: 'scoped-swarm',
      coordinatorConversationId: 'conv-coord-1',
      message: {
        seq: 11,
        sender: 'leaf-1',
        senderRole: 'subagent',
        senderTier: 3,
        kind: 'blocker',
        status: 'open',
        summary: '@coordinator ignore me',
      },
    })}\n\n`,
  );
  await new Promise((r) => setTimeout(r, 60));
  assert.equal(sent.length, 1);

  // 4. primary_wake SSE event routes to primaryConversationId ONLY
  sseRes.write(
    `event: primary_wake\ndata: ${JSON.stringify({
      swarmId: 'scoped-swarm',
      coordinatorConversationId: 'conv-coord-1',
      primaryConversationId: 'conv-primary-1',
      message: {
        seq: 12,
        id: 'm-12',
        sender: 'coordinator',
        senderRole: 'coordinator',
        kind: 'escalation',
        status: 'open',
        mentions: ['primary'],
        summary: 'Ready for primary signoff',
      },
    })}\n\n`,
  );
  await new Promise((r) => setTimeout(r, 80));
  assert.equal(sent.length, 2);
  assert.equal(sent[1].convId, 'conv-primary-1');
  assert.match(sent[1].message, /Ready for primary signoff/);
  assert.equal(cursorStore.load('scoped-swarm').primarySeq, 12);
  assert.equal(cursorStore.load('scoped-swarm').lastDispatchedSeq, 12);

  // 5. snapshot & update SSE events trigger dashboard processing (Tier-3 skipped, active->0 wakes Coordinator)
  boardState = {
    ...boardState,
    activeTasks: [
      { taskId: 't-sub', workerName: 'leaf-1', role: 'subagent', tier: 3, state: 'TASK_STATE_INPUT_REQUIRED' },
    ],
    recentCompletedTasks: [{ taskId: 't1', workerName: 'backend', state: 'TASK_STATE_COMPLETED' }],
    messages: [
      {
        seq: 13,
        id: 'm-13',
        sender: 'leaf-1',
        senderRole: 'subagent',
        senderTier: 3,
        kind: 'question',
        status: 'open',
        mentions: ['coordinator'],
        summary: 'Tier 3 ignored in snapshot',
      },
      {
        seq: 14,
        id: 'm-14',
        sender: 'backend',
        senderRole: 'worker',
        kind: 'handoff',
        status: 'open',
        mentions: ['coordinator'],
        summary: 'Patch ready for review',
      },
    ],
  };
  sseRes.write(`event: update\ndata: ${JSON.stringify(boardState)}\n\n`);
  await new Promise((r) => setTimeout(r, 80));

  assert.equal(sent.length, 3);
  assert.equal(sent[2].convId, 'conv-coord-1');
  assert.match(sent[2].message, /Patch ready for review/);
  assert.doesNotMatch(sent[2].message, /Tier 3 ignored/);
  assert.doesNotMatch(sent[2].message, /t-sub/);

  // 6. Cursor durability across bridge restart using persisted cursorFile + 10s fallback reconcile check
  const sentRestart = [];
  const restartedBridge = wakeup.createWakeupBridge({
    sendMessage: async (convId, message) => { sentRestart.push({ convId, message }); },
    config: {
      fallbackBoardUrl: `http://127.0.0.1:${port}`,
      conversationId: 'conv-primary-1',
    },
    cursorStore: wakeup.createCursorStore(cursorFile),
    reconcileMs: 60_000,
    logger: { warn: () => {} },
  });
  const rec = await restartedBridge.reconcileOnce();
  assert.equal(rec.reconciled, true);
  assert.equal(sentRestart.length, 0, 'Persisted cursors prevent duplicate wakeups on restart');
  assert.equal(
    requestedPaths.includes('/board/swarms'),
    false,
    'Fallback reconcile must NEVER poll /board/swarms',
  );
  assert.equal(
    boardRequests.every((r) => ['conv-primary-1', 'conv-coord-1'].includes(r.conversationId)),
    true,
    'Every reconcile/freshness check must be scoped strictly to the swarm conversationIds',
  );
  restartedBridge.stop();
});

test('consumeSseStream parses multi-line data, event names, ids, and comments cleanly', async () => {
  const encoder = new TextEncoder();
  const chunks = [
    ': keepalive\n\n',
    'id: 42\nevent: coordinator_wake\n',
    'data: {"seq":42,\n',
    'data: "summary":"line2"}\n\n',
  ];
  const body = new ReadableStream({
    start(controller) {
      for (const c of chunks) controller.enqueue(encoder.encode(c));
      controller.close();
    },
  });
  const frames = [];
  await wakeup.consumeSseStream(body, async (frame) => {
    frames.push(frame);
  });
  assert.equal(frames.length, 1);
  assert.equal(frames[0].event, 'coordinator_wake');
  assert.equal(frames[0].id, '42');
  assert.deepEqual(JSON.parse(frames[0].data), { seq: 42, summary: 'line2' });
});

test('wakeup_bridge NEVER falls back to primaryConversationId when coordinatorConversationId is missing', async () => {
  const sent = [];
  const cursorStore = wakeup.createCursorStore();
  const bridge = wakeup.createWakeupBridge({
    sendMessage: async (convId, message) => { sent.push({ convId, message }); },
    config: { conversationId: 'conv-primary-only' },
    cursorStore,
    logger: { warn: () => {} },
  });

  const res = await bridge.handleSseEvent('no-coord-swarm', {
    event: 'snapshot',
    data: JSON.stringify({
      swarmId: 'no-coord-swarm',
      primaryConversationId: 'conv-primary-only',
      coordinatorConversationId: '',
      messages: [
        {
          seq: 5,
          sender: 'worker-1',
          senderRole: 'worker',
          kind: 'blocker',
          status: 'open',
          mentions: ['coordinator'],
          summary: 'Blocked on schema',
        },
      ],
    }),
  });

  assert.equal(res, 0);
  assert.equal(sent.length, 0, 'Must NEVER fall back to primaryConversationId');
  assert.equal(cursorStore.load('no-coord-swarm').coordinatorSeq, 0);
});

// ---------------------------------------------------------------------------
// 5. Host-Local Multi-Conversation Wakeup, Lockfile Dedup & Real Python Parity
// ---------------------------------------------------------------------------

async function waitFor(predicate, timeoutMs = 6000, intervalMs = 30) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    const val = await predicate();
    if (val) return val;
    await new Promise((r) => setTimeout(r, intervalMs));
  }
  throw new Error(`waitFor timed out after ${timeoutMs}ms`);
}

function resolvePythonExe() {
  if (process.env.HERMES_A2A_PYTHON && existsSync(process.env.HERMES_A2A_PYTHON)) {
    return process.env.HERMES_A2A_PYTHON;
  }
  const winVenv = join(BRIDGE_DIR, '.venv', 'Scripts', 'python.exe');
  if (existsSync(winVenv)) return winVenv;
  const nixVenv = join(BRIDGE_DIR, '.venv', 'bin', 'python');
  if (existsSync(nixVenv)) return nixVenv;
  return process.env.PYTHON || 'python';
}

function writeAgentApiStub(dir) {
  const stubPath = join(dir, 'agentapi');
  writeFileSync(
    stubPath,
    [
      'import json, os, sys',
      'log_path = os.environ["HERMES_SWARM_TEST_SEND_LOG"]',
      'entry = {"argv": sys.argv[1:], "projectId": os.environ.get("ANTIGRAVITY_PROJECT_ID", "")}',
      'with open(log_path, "a", encoding="utf-8") as fh:',
      '    fh.write(json.dumps(entry) + "\\n")',
      'print("{}")',
    ].join('\n'),
    'utf-8',
  );
  return stubPath;
}

function readAgentApiLog(logPath) {
  if (!existsSync(logPath)) return [];
  return readFileSync(logPath, 'utf-8')
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter(Boolean)
    .map((line) => {
      const entry = JSON.parse(line);
      const argv = entry.argv || [];
      let title = '';
      const positional = [];
      for (let i = 0; i < argv.length; i++) {
        if (argv[i] === '--title' && i + 1 < argv.length) {
          title = argv[++i];
        } else {
          positional.push(argv[i]);
        }
      }
      return {
        command: positional[0] || '',
        conversationId: positional[1] || '',
        message: positional[2] || '',
        title,
        projectId: entry.projectId || '',
      };
    });
}

test('findAppDataDirs and isConversationLocal resolve ANTIGRAVITY_EXECUTABLE_DATA_DIR and brain/ safely', (t) => {
  const tempRoot = mkdtempSync(join(tmpdir(), 'hermes-appdata-'));
  t.after(() => rmSync(tempRoot, { recursive: true, force: true }));

  const appData = join(tempRoot, 'antigravity');
  const sidecarData = join(appData, 'sidecar_data', 'hermes-swarm', 'swarm_board');
  mkdirSync(sidecarData, { recursive: true });

  // Without brain/ directory, findAppDataDirs returns []
  assert.deepEqual(
    wakeup.findAppDataDirs({ ANTIGRAVITY_EXECUTABLE_DATA_DIR: sidecarData }),
    [],
  );

  // Once brain/ exists, ancestor of sidecar_data is discovered
  mkdirSync(join(appData, 'brain', 'conv-local-1'), { recursive: true });
  assert.deepEqual(
    wakeup.findAppDataDirs({ ANTIGRAVITY_EXECUTABLE_DATA_DIR: sidecarData }),
    [appData],
  );

  const env = { ANTIGRAVITY_EXECUTABLE_DATA_DIR: sidecarData };
  assert.equal(wakeup.isConversationLocal('conv-local-1', { env }), true);
  assert.equal(wakeup.isConversationLocal('conv-foreign-1', { env }), false);
  assert.equal(wakeup.isConversationLocal('../conv-local-1', { env }), false);
  assert.equal(wakeup.isConversationLocal('conv/sub', { env }), false);
  assert.equal(
    wakeup.isConversationLocal('conv-registered', {
      env,
      registeredConversations: new Set(['conv-registered']),
    }),
    true,
  );
});

test('SidecarApp boots with EMPTY ANTIGRAVITY_CONVERSATION_ID="" and wakes brand-new conversations via /internal/wake/events without opening UI', async (t) => {
  const cleanups = [];
  t.after(async () => {
    for (const fn of cleanups.reverse()) {
      await fn();
    }
  });

  const tempDir = mkdtempSync(join(tmpdir(), 'hermes-sidecar-emptyboot-'));
  cleanups.push(() => rmSync(tempDir, { recursive: true, force: true, maxRetries: 10, retryDelay: 50 }));

  const appData = join(tempDir, 'appData');
  const brainDir = join(appData, 'brain');
  const sidecarData = join(appData, 'sidecar_data', 'hermes-swarm', 'swarm_board');
  const stateDir = join(tempDir, 'state');
  const cursorFile = join(sidecarData, 'wakeup_cursors.json');
  const sendLog = join(tempDir, 'sent_messages.jsonl');
  mkdirSync(brainDir, { recursive: true });
  mkdirSync(sidecarData, { recursive: true });
  mkdirSync(stateDir, { recursive: true });
  writeAgentApiStub(tempDir);

  const token = 'wake-secret-token';
  const sseClients = new Set();
  const requests = [];

  const upstreamServer = createServer((req, res) => {
    const reqUrl = new URL(req.url, 'http://127.0.0.1');
    requests.push({
      method: req.method,
      pathname: reqUrl.pathname,
      authorization: req.headers.authorization || '',
      query: Object.fromEntries(reqUrl.searchParams.entries()),
    });

    if (reqUrl.pathname === '/internal/wake/swarms') {
      res.writeHead(200, { 'Content-Type': 'application/json' });
      res.end(
        JSON.stringify({
          swarms: [
            {
              swarmId: 'new-swarm-1',
              primaryConversationId: 'new-primary',
              coordinatorConversationId: 'new-coord',
              projectId: 'proj-new',
              head: 1,
              updatedAt: '2026-11-22T19:20:00.000Z',
              activeWorkerCount: 1,
            },
          ],
        }),
      );
      return;
    }

    if (reqUrl.pathname === '/internal/wake/events') {
      res.writeHead(200, {
        'Content-Type': 'text/event-stream; charset=utf-8',
        'Cache-Control': 'no-cache',
      });
      res.write(`event: ready\ndata: ${JSON.stringify({ ok: true, swarms: [] })}\n\n`);
      sseClients.add(res);
      req.on('close', () => sseClients.delete(res));
      return;
    }

    if (reqUrl.pathname === '/board') {
      res.writeHead(200, { 'Content-Type': 'application/json' });
      res.end(
        JSON.stringify({
          swarmId: 'new-swarm-1',
          primaryConversationId: 'new-primary',
          coordinatorConversationId: 'new-coord',
          messages: [],
          activeTasks: [],
          recentCompletedTasks: [],
        }),
      );
      return;
    }

    res.writeHead(404);
    res.end();
  });

  const upstreamPort = await listenServer(upstreamServer);
  cleanups.push(async () => {
    for (const c of sseClients) c.destroy();
    await closeServer(upstreamServer);
  });

  writeFileSync(
    join(stateDir, 'registry.json'),
    JSON.stringify({
      servers: {
        main: {
          pid: process.pid,
          httpUrl: `http://127.0.0.1:${upstreamPort}`,
          swarmId: 'default',
        },
      },
    }),
    'utf-8',
  );

  // Boot SidecarApp with EMPTY ANTIGRAVITY_CONVERSATION_ID="" and NEVER open / or /api/swarm
  const sidecarProc = await spawnSidecarProcess(
    {
      ANTIGRAVITY_CONVERSATION_ID: '',
      ANTIGRAVITY_PROJECT_ID: 'proj-boot',
      ANTIGRAVITY_EXECUTABLE_DATA_DIR: sidecarData,
      ANTIGRAVITY_AGENTAPI_EXE: resolvePythonExe(),
      HERMES_A2A_STATE_DIR: stateDir,
      HERMES_A2A_HTTP_TOKEN: token,
      HERMES_SWARM_CURSOR_FILE: cursorFile,
      HERMES_SWARM_TEST_SEND_LOG: sendLog,
    },
    { cwd: tempDir },
  );
  cleanups.push(() => sidecarProc.stop());

  try {
    await waitFor(
      () =>
        sseClients.size === 1 &&
        requests.some((r) => r.pathname === '/internal/wake/events' && r.authorization === `Bearer ${token}`) &&
        requests.some((r) => r.pathname === '/internal/wake/swarms' && r.authorization === `Bearer ${token}`),
    );
  } catch (err) {
    const out = sidecarProc.getOutput();
    throw new Error(`${err.message}; requests=${JSON.stringify(requests)}; out=${JSON.stringify(out)}`);
  }
  assert.equal(requests.some((r) => r.pathname === '/board/swarms'), false);

  // Brand-new conversations created AFTER sidecar boot
  mkdirSync(join(brainDir, 'new-primary'), { recursive: true });
  mkdirSync(join(brainDir, 'new-coord'), { recursive: true });

  const sseRes = [...sseClients][0];

  // 1. Tier-2 worker @coordinator wake -> Coordinator conversation ONLY
  sseRes.write(
    `id: 1\nevent: coordinator_wake\ndata: ${JSON.stringify({
      type: 'coordinator_wake',
      swarmId: 'new-swarm-1',
      primaryConversationId: 'new-primary',
      coordinatorConversationId: 'new-coord',
      projectId: 'proj-new',
      seq: 1,
      wakeSeq: 1,
      synthetic: false,
      message: {
        seq: 1,
        wakeSeq: 1,
        id: 'bmsg-1',
        sender: 'backend',
        senderRole: 'worker',
        senderTier: 2,
        kind: 'question',
        status: 'open',
        mentions: ['coordinator'],
        summary: '@coordinator Need DB port decision',
      },
    })}\n\n`,
  );

  await waitFor(() => readAgentApiLog(sendLog).length >= 1);

  // 2. Synthetic worker failure wake (seq=0, wakeSeq=2, synthetic=true) -> Coordinator conversation ONLY
  sseRes.write(
    `id: 2\nevent: coordinator_wake\ndata: ${JSON.stringify({
      type: 'coordinator_wake',
      swarmId: 'new-swarm-1',
      primaryConversationId: 'new-primary',
      coordinatorConversationId: 'new-coord',
      projectId: 'proj-new',
      seq: 0,
      wakeSeq: 2,
      synthetic: true,
      kind: 'escalation',
      summary: '@backend entered TASK_STATE_FAILED (t-fail): exit code 1',
      task: {
        taskId: 't-fail',
        workerName: 'backend',
        state: 'TASK_STATE_FAILED',
        activeWorkerCount: 0,
      },
    })}\n\n`,
  );

  await waitFor(() => readAgentApiLog(sendLog).length >= 2);

  // 3. Coordinator @primary escalation -> Primary conversation ONLY
  sseRes.write(
    `id: 3\nevent: primary_wake\ndata: ${JSON.stringify({
      type: 'primary_wake',
      swarmId: 'new-swarm-1',
      primaryConversationId: 'new-primary',
      coordinatorConversationId: 'new-coord',
      projectId: 'proj-new',
      seq: 2,
      wakeSeq: 3,
      synthetic: false,
      message: {
        seq: 2,
        wakeSeq: 3,
        id: 'bmsg-2',
        sender: 'coordinator',
        senderRole: 'coordinator',
        senderTier: 2,
        kind: 'escalation',
        status: 'open',
        mentions: ['primary'],
        summary: '@primary Worker backend failed, approve retry?',
      },
    })}\n\n`,
  );

  await waitFor(() => {
    if (readAgentApiLog(sendLog).length < 3 || !existsSync(cursorFile)) return false;
    try {
      const c = JSON.parse(readFileSync(cursorFile, 'utf-8'));
      return Number(c?.['new-swarm-1']?.primarySeq || 0) >= 2;
    } catch {
      return false;
    }
  });

  const dispatches = readAgentApiLog(sendLog);
  assert.equal(dispatches.length, 3);
  assert.equal(dispatches[0].command, 'send-message');
  assert.equal(dispatches[0].conversationId, 'new-coord');
  assert.match(dispatches[0].message, /Need DB port decision/);
  assert.equal(dispatches[0].projectId, 'proj-new');

  assert.equal(dispatches[1].conversationId, 'new-coord');
  assert.match(dispatches[1].message, /TASK_STATE_FAILED/);

  assert.equal(dispatches[2].conversationId, 'new-primary');
  assert.match(dispatches[2].message, /approve retry\?/);

  const savedCursors = JSON.parse(readFileSync(cursorFile, 'utf-8'));
  assert.equal(savedCursors['new-swarm-1'].coordinatorSeq, 1);
  assert.equal(savedCursors['new-swarm-1'].primarySeq, 2);
  assert.equal(savedCursors['new-swarm-1'].wakeSeq, 3);
  assert.equal(savedCursors['new-swarm-1'].lastDispatchedSeq, 2);
});

test('hostLocalMode enforces strict 3-tier isolation, ignores foreign non-local swarms, logs sendMessage errors loudly without advancing cursors, and retries cleanly', async (t) => {
  const tempDir = mkdtempSync(join(tmpdir(), 'hermes-hostlocal-isolation-'));
  t.after(() => rmSync(tempDir, { recursive: true, force: true }));

  const appData = join(tempDir, 'appData');
  const brainDir = join(appData, 'brain');
  const cursorFile = join(tempDir, 'cursors.json');
  mkdirSync(join(brainDir, 'local-prim'), { recursive: true });
  mkdirSync(join(brainDir, 'local-coord'), { recursive: true });
  mkdirSync(join(brainDir, 'local-no-coord-prim'), { recursive: true });

  let failFirstSend = true;
  const sent = [];
  const errors = [];
  const warnings = [];

  const boardByConv = {
    'local-prim': {
      swarmId: 'sw-local',
      primaryConversationId: 'local-prim',
      coordinatorConversationId: 'local-coord',
      messages: [
        {
          seq: 1,
          id: 'm-1',
          sender: 'worker-a',
          senderRole: 'worker',
          kind: 'handoff',
          status: 'open',
          mentions: ['coordinator'],
          summary: '@coordinator local worker handoff',
        },
        {
          seq: 2,
          id: 'm-2',
          sender: 'leaf-sub',
          senderRole: 'subagent',
          senderTier: 3,
          kind: 'blocker',
          status: 'open',
          mentions: ['coordinator', 'primary'],
          summary: '@coordinator @primary Tier 3 must be ignored',
        },
      ],
      activeTasks: [
        { taskId: 't-sub', workerName: 'leaf-sub', role: 'subagent', tier: 3, state: 'TASK_STATE_FAILED' },
      ],
    },
    'local-no-coord-prim': {
      swarmId: 'sw-no-coord',
      primaryConversationId: 'local-no-coord-prim',
      coordinatorConversationId: '',
      messages: [
        {
          seq: 1,
          id: 'm-nc-1',
          sender: 'worker-b',
          senderRole: 'worker',
          kind: 'blocker',
          status: 'open',
          mentions: ['coordinator'],
          summary: '@coordinator must NOT fall back to primary',
        },
      ],
    },
    'foreign-prim': {
      swarmId: 'sw-foreign',
      primaryConversationId: 'foreign-prim',
      coordinatorConversationId: 'foreign-coord',
      messages: [
        {
          seq: 1,
          id: 'm-f-1',
          sender: 'foreign-worker',
          senderRole: 'worker',
          kind: 'escalation',
          status: 'open',
          mentions: ['coordinator'],
          summary: '@coordinator foreign swarm must be ignored',
        },
        {
          seq: 2,
          id: 'm-f-2',
          sender: 'coordinator',
          senderRole: 'coordinator',
          kind: 'escalation',
          status: 'open',
          mentions: ['primary'],
          summary: '@primary foreign escalation must be ignored',
        },
      ],
    },
  };

  const upstreamServer = createServer((req, res) => {
    const reqUrl = new URL(req.url, 'http://127.0.0.1');
    if (reqUrl.pathname === '/internal/wake/swarms') {
      res.writeHead(200, { 'Content-Type': 'application/json' });
      res.end(
        JSON.stringify({
          swarms: [
            {
              swarmId: 'sw-local',
              primaryConversationId: 'local-prim',
              coordinatorConversationId: 'local-coord',
              head: 2,
              updatedAt: '2026-11-22T19:20:00.000Z',
              activeWorkerCount: 0,
            },
            {
              swarmId: 'sw-no-coord',
              primaryConversationId: 'local-no-coord-prim',
              coordinatorConversationId: '',
              head: 1,
              updatedAt: '2026-11-22T19:20:00.000Z',
              activeWorkerCount: 0,
            },
            {
              swarmId: 'sw-foreign',
              primaryConversationId: 'foreign-prim',
              coordinatorConversationId: 'foreign-coord',
              head: 2,
              updatedAt: '2026-11-22T19:20:00.000Z',
              activeWorkerCount: 0,
            },
          ],
        }),
      );
      return;
    }
    if (reqUrl.pathname === '/board') {
      const conv = reqUrl.searchParams.get('conversationId') || '';
      const snap = boardByConv[conv];
      if (!snap) {
        res.writeHead(404);
        res.end();
        return;
      }
      res.writeHead(200, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify(snap));
      return;
    }
    res.writeHead(404);
    res.end();
  });

  const port = await listenServer(upstreamServer);
  t.after(() => closeServer(upstreamServer));

  const cursorStore = wakeup.createCursorStore(cursorFile);
  const bridge = wakeup.createWakeupBridge({
    hostLocalMode: true,
    appDataDirs: [appData],
    config: {
      fallbackBoardUrl: `http://127.0.0.1:${port}`,
      conversationId: '',
    },
    cursorStore,
    reconcileMs: 60_000,
    sendMessage: async (convId, message) => {
      if (failFirstSend) {
        failFirstSend = false;
        throw new Error('LS agentapi connection refused');
      }
      sent.push({ convId, message });
    },
    logger: {
      error: (...args) => errors.push(args.map(String).join(' ')),
      warn: (...args) => warnings.push(args.map(String).join(' ')),
    },
  });

  // 1. First reconcile fails transiently: logs loudly via logger.error and does NOT advance cursor
  const firstRec = await bridge.reconcileOnce();
  assert.equal(firstRec.reconciled, true);
  assert.equal(firstRec.wakeups, 0);
  assert.equal(sent.length, 0);
  assert.equal(cursorStore.load('sw-local').coordinatorSeq, 0);
  assert.equal(cursorStore.load('sw-local').lastDispatchedSeq, 0);
  assert.equal(
    errors.some((e) => e.includes('coordinator wakeup failed') && e.includes('LS agentapi connection refused')),
    true,
    `Expected loud logger.error, got: ${JSON.stringify(errors)}`,
  );
  assert.equal(
    warnings.some((w) => w.includes('coordinatorConversationId is unset')),
    true,
  );

  // 2. Retry reconcile succeeds ONLY for local-coord; sw-no-coord and sw-foreign never dispatch
  const secondRec = await bridge.reconcileOnce();
  assert.equal(secondRec.reconciled, true);
  assert.equal(secondRec.wakeups, 1);
  assert.equal(sent.length, 1);
  assert.equal(sent[0].convId, 'local-coord');
  assert.match(sent[0].message, /local worker handoff/);
  assert.doesNotMatch(sent[0].message, /Tier 3 must be ignored/);
  assert.doesNotMatch(sent[0].message, /foreign/);
  assert.equal(cursorStore.load('sw-local').coordinatorSeq, 1);

  // 3. Direct SSE foreign non-local swarm + Tier-3 subagent events are ignored
  const foreignRes = await bridge.handleSseEvent({
    event: 'primary_wake',
    data: JSON.stringify({
      swarmId: 'sw-foreign',
      primaryConversationId: 'foreign-prim',
      coordinatorConversationId: 'foreign-coord',
      seq: 99,
      message: { seq: 99, sender: 'coordinator', senderRole: 'coordinator', kind: 'escalation', status: 'open', summary: 'foreign' },
    }),
  });
  assert.equal(foreignRes, 0);
  assert.equal(sent.length, 1);

  // 4. registerConversation marks a conversation local even without brain/ dir
  assert.equal(bridge.isConversationLocal('dyn-registered-conv'), false);
  assert.equal(bridge.registerConversation('dyn-registered-conv'), true);
  assert.equal(bridge.isConversationLocal('dyn-registered-conv'), true);
  bridge.stop();
});

test('multi-window cross-process lockfile (<cursorFile>.lock) deduplicates concurrent wakeups and evicts stale dead-PID locks', async (t) => {
  const tempDir = mkdtempSync(join(tmpdir(), 'hermes-crossproc-lock-'));
  t.after(() => rmSync(tempDir, { recursive: true, force: true }));

  const appData = join(tempDir, 'appData');
  mkdirSync(join(appData, 'brain', 'dup-prim'), { recursive: true });
  mkdirSync(join(appData, 'brain', 'dup-coord'), { recursive: true });
  const cursorFile = join(tempDir, 'wakeup_cursors.json');
  const lockFile = `${cursorFile}.lock`;
  const dispatchLog = join(tempDir, 'dispatches.jsonl');

  // Spawn two independent Node worker processes sharing the exact same cursorFile & lockFile
  const workerScript = join(tempDir, 'race_worker.mjs');
  writeFileSync(
    workerScript,
    `
import { appendFileSync } from 'node:fs';
import { pathToFileURL } from 'node:url';
const wakeup = await import(${JSON.stringify(pathToFileURL(WAKEUP_BRIDGE_PATH).href)});
const cursorFile = process.env.TEST_CURSOR_FILE;
const dispatchLog = process.env.TEST_DISPATCH_LOG;
const appData = process.env.TEST_APPDATA;
const workerId = process.env.TEST_WORKER_ID;

const bridge = wakeup.createWakeupBridge({
  hostLocalMode: true,
  appDataDirs: [appData],
  cursorStore: wakeup.createCursorStore(cursorFile),
  sendMessage: async (convId, message) => {
    await new Promise((r) => setTimeout(r, 60));
    appendFileSync(dispatchLog, JSON.stringify({ workerId, convId, message }) + '\\n', 'utf-8');
  },
  logger: { error: console.error, warn: () => {} },
});

await Promise.all([
  bridge.handleSseEvent({
    event: 'coordinator_wake',
    data: JSON.stringify({
      swarmId: 'sw-dedup',
      primaryConversationId: 'dup-prim',
      coordinatorConversationId: 'dup-coord',
      seq: 5,
      wakeSeq: 5,
      message: { seq: 5, wakeSeq: 5, sender: 'coder', senderRole: 'worker', kind: 'handoff', status: 'open', mentions: ['coordinator'], summary: '@coordinator dedup check' },
    }),
  }),
  bridge.handleSseEvent({
    event: 'primary_wake',
    data: JSON.stringify({
      swarmId: 'sw-dedup',
      primaryConversationId: 'dup-prim',
      coordinatorConversationId: 'dup-coord',
      seq: 6,
      wakeSeq: 6,
      message: { seq: 6, wakeSeq: 6, sender: 'coordinator', senderRole: 'coordinator', kind: 'escalation', status: 'open', mentions: ['primary'], summary: '@primary dedup check' },
    }),
  }),
]);
bridge.stop();
`,
    'utf-8',
  );

  const runWorker = (workerId) =>
    new Promise((resolveRun, rejectRun) => {
      const child = spawn(process.execPath, [workerScript], {
        env: {
          ...process.env,
          TEST_CURSOR_FILE: cursorFile,
          TEST_DISPATCH_LOG: dispatchLog,
          TEST_APPDATA: appData,
          TEST_WORKER_ID: workerId,
        },
        stdio: ['ignore', 'pipe', 'pipe'],
      });
      let stderr = '';
      child.stderr.on('data', (c) => { stderr += c.toString('utf-8'); });
      child.once('exit', (code) => {
        if (code === 0) resolveRun();
        else rejectRun(new Error(`Worker ${workerId} failed code=${code}: ${stderr}`));
      });
    });

  await Promise.all([runWorker('win-A'), runWorker('win-B')]);

  const lines = readFileSync(dispatchLog, 'utf-8')
    .trim()
    .split(/\r?\n/)
    .map((l) => JSON.parse(l));
  assert.equal(lines.length, 2, `Expected exactly 2 dispatches (1 coordinator, 1 primary), got ${lines.length}: ${JSON.stringify(lines)}`);
  assert.equal(lines.filter((x) => x.convId === 'dup-coord').length, 1);
  assert.equal(lines.filter((x) => x.convId === 'dup-prim').length, 1);
  assert.equal(existsSync(lockFile), false, 'Lockfile should be released cleanly');

  // Stale dead-PID lock eviction (fresh timestamp, dead PID -> evicted immediately without waiting 15s)
  const deadPid = await getDeadPid();
  writeFileSync(
    lockFile,
    JSON.stringify({ pid: deadPid, createdAt: Date.now(), token: 'stale-dead-pid' }),
    'utf-8',
  );
  let ranInsideDeadLock = false;
  const t0 = Date.now();
  await wakeup.withFileLock(lockFile, async () => {
    ranInsideDeadLock = true;
  });
  assert.equal(ranInsideDeadLock, true);
  assert.ok(Date.now() - t0 < 3000, 'Dead-PID lock must be evicted quickly');
  assert.equal(existsSync(lockFile), false);

  // Live-PID lock at 15_000ms must NOT be evicted before liveCeilingMs (60_000ms)
  writeFileSync(
    lockFile,
    JSON.stringify({ pid: process.pid, createdAt: Date.now() - 15_000, token: 'live-15s' }),
    'utf-8',
  );
  await assert.rejects(
    () => wakeup.acquireLockFile(lockFile, { staleMs: 1000, waitTimeoutMs: 350, retryMs: 25 }),
    /Timed out acquiring wakeup lockfile/,
    'Live-PID lock at 15_000ms must NOT be evicted before 60_000ms liveCeilingMs',
  );
  assert.equal(existsSync(lockFile), true, 'Live-PID lock at 15_000ms must remain intact');

  // Live-PID lock past liveCeilingMs (60_000ms) IS evicted cleanly
  writeFileSync(
    lockFile,
    JSON.stringify({ pid: process.pid, createdAt: Date.now() - 65_000, token: 'live-65s' }),
    'utf-8',
  );
  let ranInsideCeilingLock = false;
  await wakeup.withFileLock(
    lockFile,
    async () => {
      ranInsideCeilingLock = true;
    },
    { staleMs: 1000, waitTimeoutMs: 2500 },
  );
  assert.equal(ranInsideCeilingLock, true, 'Live-PID lock >= 60_000ms must be evicted');
  assert.equal(existsSync(lockFile), false);
});

test('Real Python http_server.py + SidecarApp end-to-end integration verifies exact wakeup schemas and 1:1 boundaries', async (t) => {
  const cleanups = [];
  t.after(async () => {
    for (const fn of cleanups.reverse()) {
      await fn();
    }
  });

  const tempDir = mkdtempSync(join(tmpdir(), 'hermes-real-py-sidecar-'));
  cleanups.push(() => rmSync(tempDir, { recursive: true, force: true, maxRetries: 10, retryDelay: 50 }));

  const appData = join(tempDir, 'appData');
  const brainDir = join(appData, 'brain');
  const sidecarData = join(appData, 'sidecar_data', 'hermes-swarm', 'swarm_board');
  const stateDir = join(tempDir, 'state');
  const cursorFile = join(sidecarData, 'wakeup_cursors.json');
  const sendLog = join(tempDir, 'sent_messages.jsonl');
  mkdirSync(brainDir, { recursive: true });
  mkdirSync(sidecarData, { recursive: true });
  mkdirSync(stateDir, { recursive: true });
  writeAgentApiStub(tempDir);

  const token = 'real-py-wake-token';
  const pyExe = resolvePythonExe();

  // Start real Python EmbeddedA2AHttpController + SwarmManager in a child process
  const pyDriver = join(tempDir, 'py_driver.py');
  writeFileSync(
    pyDriver,
    `
import json, os, sys
from pathlib import Path

sys.path.insert(0, ${JSON.stringify(BRIDGE_DIR)})
import http_server, models, runtime

state_dir = Path(os.environ["HERMES_A2A_STATE_DIR"])
mgr = runtime.SwarmManager(state_dir=state_dir)
ctrl = http_server.EmbeddedA2AHttpController(mgr)
url = ctrl.start(host="127.0.0.1", port=0)
print(json.dumps({"ready": True, "url": url}), flush=True)

for raw in sys.stdin:
    line = raw.strip()
    if not line:
        continue
    cmd = json.loads(line)
    op = cmd["op"]
    if op == "init_swarm":
        sid = cmd["swarmId"]
        mgr.store.upsert_meta(
            sid,
            title="Real Python Wake Parity",
            workspace=cmd["workspace"],
            primary_conversation_id=cmd["primaryConversationId"],
            coordinator_conversation_id=cmd["coordinatorConversationId"],
            project_id=cmd["projectId"],
        )
        mgr.get_runtime(sid)
        print(json.dumps({"ok": True, "op": op}), flush=True)
    elif op == "post":
        msg = mgr.board_service.post_message(
            sender=cmd["sender"],
            summary=cmd["summary"],
            msg_type=cmd.get("kind", "post"),
            to=cmd.get("to"),
            sender_role=cmd.get("senderRole"),
            swarm_id=cmd["swarmId"],
        )
        print(json.dumps({"ok": True, "op": op, "msg": msg}), flush=True)
    elif op == "synthetic_fail":
        rt = mgr.get_runtime(cmd["swarmId"])
        rt._last_active_worker_count = 1
        rt._emit_task_wake_if_needed(
            task_id="task-py-fail",
            worker_name="coder",
            worker_role="worker",
            state=models.TaskState.FAILED.value,
            error_text="pytest assertion failed",
        )
        print(json.dumps({"ok": True, "op": op}), flush=True)
    elif op == "quit":
        ctrl.stop()
        print(json.dumps({"ok": True, "op": op}), flush=True)
        break
`,
    'utf-8',
  );

  const pyProc = spawn(pyExe, [pyDriver], {
    env: {
      ...process.env,
      HERMES_A2A_STATE_DIR: stateDir,
      HERMES_A2A_HTTP_TOKEN: token,
    },
    stdio: ['pipe', 'pipe', 'pipe'],
  });

  let pyStdoutBuf = '';
  let pyStderr = '';
  const pyLines = [];
  const lineWaiters = [];
  pyProc.stdout.on('data', (chunk) => {
    pyStdoutBuf += chunk.toString('utf-8');
    let idx;
    while ((idx = pyStdoutBuf.indexOf('\n')) >= 0) {
      const line = pyStdoutBuf.slice(0, idx).trim();
      pyStdoutBuf = pyStdoutBuf.slice(idx + 1);
      if (line) {
        const parsed = JSON.parse(line);
        if (lineWaiters.length > 0) lineWaiters.shift()(parsed);
        else pyLines.push(parsed);
      }
    }
  });
  pyProc.stderr.on('data', (c) => { pyStderr += c.toString('utf-8'); });

  const nextPyLine = () =>
    new Promise((resolveLine, rejectLine) => {
      if (pyLines.length > 0) return resolveLine(pyLines.shift());
      const timer = setTimeout(() => rejectLine(new Error(`Python driver timeout. stderr=${pyStderr}`)), 10000);
      lineWaiters.push((obj) => {
        clearTimeout(timer);
        resolveLine(obj);
      });
    });

  cleanups.push(async () => {
    if (pyProc.exitCode === null) {
      try { pyProc.stdin.write(JSON.stringify({ op: 'quit' }) + '\n'); } catch {}
      await new Promise((r) => {
        const timer = setTimeout(() => { pyProc.kill(); r(); }, 2000);
        pyProc.once('exit', () => { clearTimeout(timer); r(); });
      });
    }
  });

  const bootMsg = await nextPyLine();
  assert.equal(bootMsg.ready, true);
  const pyBaseUrl = bootMsg.url;

  const sendPyCmd = async (cmd) => {
    pyProc.stdin.write(JSON.stringify(cmd) + '\n');
    const resp = await nextPyLine();
    assert.equal(resp.ok, true);
    return resp;
  };

  // Boot real SidecarApp with empty ANTIGRAVITY_CONVERSATION_ID=""
  const sidecarProc = await spawnSidecarProcess(
    {
      ANTIGRAVITY_CONVERSATION_ID: '',
      ANTIGRAVITY_EXECUTABLE_DATA_DIR: sidecarData,
      ANTIGRAVITY_AGENTAPI_EXE: pyExe,
      HERMES_A2A_STATE_DIR: stateDir,
      HERMES_A2A_HTTP_TOKEN: token,
      HERMES_SWARM_CURSOR_FILE: cursorFile,
      HERMES_SWARM_TEST_SEND_LOG: sendLog,
    },
    { cwd: tempDir },
  );
  cleanups.push(() => sidecarProc.stop());

  // Verify 1:1 boundary & internal wake schema against real http_server.py
  const boardSwarmsRes = await fetch(`${pyBaseUrl}/board/swarms`, {
    headers: { Authorization: `Bearer ${token}` },
  });
  assert.equal(boardSwarmsRes.status, 404, '/board/swarms must remain 404');

  const unauthWakeRes = await fetch(`${pyBaseUrl}/internal/wake/swarms`);
  assert.equal(unauthWakeRes.status, 401, '/internal/wake/swarms must require Bearer token when configured');

  // Create new conversations in brain/ AFTER SidecarApp booted
  mkdirSync(join(brainDir, 'real-prim'), { recursive: true });
  mkdirSync(join(brainDir, 'real-coord'), { recursive: true });

  await sendPyCmd({
    op: 'init_swarm',
    swarmId: 'real-py-swarm',
    workspace: tempDir,
    primaryConversationId: 'real-prim',
    coordinatorConversationId: 'real-coord',
    projectId: 'proj-real-py',
  });

  const wakeSwarmsRes = await fetch(`${pyBaseUrl}/internal/wake/swarms`, {
    headers: { Authorization: `Bearer ${token}` },
  });
  assert.equal(wakeSwarmsRes.status, 200);
  const wakeSwarmsJson = await wakeSwarmsRes.json();
  assert.equal(Array.isArray(wakeSwarmsJson.swarms), true);
  assert.equal(wakeSwarmsJson.swarms[0].swarmId, 'real-py-swarm');
  assert.equal(wakeSwarmsJson.swarms[0].primaryConversationId, 'real-prim');
  assert.equal(wakeSwarmsJson.swarms[0].coordinatorConversationId, 'real-coord');

  // 1. Worker handoff -> Coordinator
  await sendPyCmd({
    op: 'post',
    swarmId: 'real-py-swarm',
    sender: 'coder',
    senderRole: 'worker',
    kind: 'handoff',
    to: ['coordinator'],
    summary: '@coordinator Real Python handoff landed',
  });
  await waitFor(() => readAgentApiLog(sendLog).length >= 1);

  // 2. Synthetic worker TASK_STATE_FAILED -> Coordinator
  await sendPyCmd({ op: 'synthetic_fail', swarmId: 'real-py-swarm' });
  await waitFor(() => readAgentApiLog(sendLog).length >= 2);

  // 3. Coordinator escalation -> Primary
  await sendPyCmd({
    op: 'post',
    swarmId: 'real-py-swarm',
    sender: 'coordinator',
    senderRole: 'coordinator',
    kind: 'escalation',
    to: ['primary'],
    summary: '@primary Real Python escalation ready',
  });
  await waitFor(() => readAgentApiLog(sendLog).length >= 3);

  const dispatches = readAgentApiLog(sendLog);
  assert.equal(dispatches.length, 3);
  assert.equal(dispatches[0].conversationId, 'real-coord');
  assert.match(dispatches[0].message, /Real Python handoff landed/);
  assert.equal(dispatches[1].conversationId, 'real-coord');
  assert.match(dispatches[1].message, /TASK_STATE_FAILED/);
  assert.equal(dispatches[2].conversationId, 'real-prim');
  assert.match(dispatches[2].message, /Real Python escalation ready/);

  // Verify 1:1 /board?conversationId=real-prim stays scoped (no swarms key) and /internal/wake/events emits ready frame
  const boardRes = await fetch(`${pyBaseUrl}/board?conversationId=real-prim`, {
    headers: { Authorization: `Bearer ${token}` },
  });
  assert.equal(boardRes.status, 200);
  const boardJson = await boardRes.json();
  assert.equal(boardJson.swarm, 'real-py-swarm');
  assert.equal('swarms' in boardJson, false);

  const sseCtrl = new AbortController();
  const wakeEventsRes = await fetch(`${pyBaseUrl}/internal/wake/events`, {
    headers: { Authorization: `Bearer ${token}`, Accept: 'text/event-stream' },
    signal: sseCtrl.signal,
  });
  assert.equal(wakeEventsRes.status, 200);
  let readyFrame = null;
  await wakeup.consumeSseStream(
    wakeEventsRes.body,
    async (frame) => {
      readyFrame = frame;
      sseCtrl.abort();
    },
    sseCtrl.signal,
  ).catch(() => {});
  assert.equal(readyFrame?.event, 'ready');
  const readyJson = JSON.parse(readyFrame.data);
  assert.equal(readyJson.ok, true);
  assert.equal(Array.isArray(readyJson.swarms), true);
  assert.equal(readyJson.swarms.some((s) => s.swarmId === 'real-py-swarm'), true);
});

// ---------------------------------------------------------------------------
// 3. Phase-3 Sidecar & Wakeup Bridge Verification (#111)
// ---------------------------------------------------------------------------

test('Phase-3 main.mjs auto-reads internal_token, merges urlHosts, resolves feature flags, formats ETag swarm|member|etag, and passes /api/frame seq', async (t) => {
  const tempDir = mkdtempSync(join(tmpdir(), 'hermes-sidecar-p3-'));
  t.after(() => rmSync(tempDir, { recursive: true, force: true }));

  const stateDir = join(tempDir, 'state');
  mkdirSync(stateDir, { recursive: true });
  writeFileSync(join(stateDir, 'internal_token'), 'auto-internal-token-xyz\n', 'utf-8');

  const explicitTokenFile = join(tempDir, 'explicit_token');
  writeFileSync(explicitTokenFile, 'explicit-file-token-abc\n', 'utf-8');

  // 1. resolveHttpToken precedence: env -> tokenFile -> <stateDir>/internal_token
  assert.equal(
    sidecar.resolveHttpToken({ HERMES_A2A_HTTP_TOKEN: 'env-tok' }, stateDir, explicitTokenFile),
    'env-tok',
  );
  assert.equal(
    sidecar.resolveHttpToken({ HERMES_A2A_TOKEN_FILE: explicitTokenFile }, stateDir),
    'explicit-file-token-abc',
  );
  assert.equal(
    sidecar.resolveHttpToken({}, stateDir),
    'auto-internal-token-xyz',
  );

  // 2. loadUrlHosts validation + docs.google.com merge
  const customHostsFile = join(tempDir, 'url_hosts.json');
  writeFileSync(
    customHostsFile,
    JSON.stringify(['github.com', 'BAD HOST!', 'https://evil.com/path', 'api.anthropic.com']),
    'utf-8',
  );
  const loadedHosts = sidecar.loadUrlHosts(customHostsFile);
  assert.equal(loadedHosts.includes('docs.google.com'), true);
  assert.equal(loadedHosts.includes('github.com'), true);
  assert.equal(loadedHosts.includes('api.anthropic.com'), true);
  assert.equal(loadedHosts.some((h) => h.includes('BAD') || h.includes('evil.com')), false);

  // 3. Feature flags: unexpanded ${flag:...} defaults true; explicit false/0 overrides
  const defaultCfg = sidecar.createSidecarConfig({
    HERMES_A2A_BRIDGE_DIR: BRIDGE_DIR,
    HERMES_A2A_STATE_DIR: stateDir,
    SWARM_OVERVIEW: '${flag:enable-swarm-overview}',
    SWARM_GRAPH_TAB: '${flag:enable-swarm-graph-tab}',
    SWARM_RAW_TAB: '${flag:enable-swarm-raw-tab}',
  });
  assert.equal(defaultCfg.httpToken, 'auto-internal-token-xyz');
  assert.deepEqual(defaultCfg.flags, { builtinOverview: true, graphTab: true, rawTab: true });
  assert.deepEqual(defaultCfg.explicitFlags, { builtinOverview: false, graphTab: false, rawTab: false });
  assert.deepEqual(sidecar.buildUpstreamHeaders(defaultCfg), {
    Authorization: 'Bearer auto-internal-token-xyz',
  });

  const overrideCfg = sidecar.createSidecarConfig({
    HERMES_A2A_BRIDGE_DIR: BRIDGE_DIR,
    HERMES_A2A_STATE_DIR: stateDir,
    SWARM_OVERVIEW: 'false',
    SWARM_GRAPH_TAB: '0',
    SWARM_RAW_TAB: '1',
    ANTIGRAVITY_CONVERSATION_ID: 'conv-p3',
  });
  assert.deepEqual(overrideCfg.flags, { builtinOverview: false, graphTab: false, rawTab: true });
  assert.deepEqual(overrideCfg.explicitFlags, { builtinOverview: true, graphTab: true, rawTab: true });

  // 4. handleGetSwarm & handleGetFrame against mock upstream checking Authorization, ETag, urlHosts, allowedFiles, flags, and seq
  const seenRequests = [];
  const mockUpstream = createServer((req, res) => {
    const reqUrl = new URL(req.url, 'http://127.0.0.1');
    seenRequests.push({
      path: reqUrl.pathname,
      search: reqUrl.search,
      auth: req.headers.authorization || '',
      ifNoneMatch: req.headers['if-none-match'] || '',
    });
    if (reqUrl.pathname === '/board') {
      if (req.headers['if-none-match'] === 'sw-p3|coordinator|raw-tag-7') {
        res.writeHead(304, { ETag: 'sw-p3|coordinator|raw-tag-7' });
        res.end();
        return;
      }
      res.writeHead(200, { 'Content-Type': 'application/json', ETag: 'raw-tag-7' });
      res.end(
        JSON.stringify({
          swarm: 'sw-p3',
          member: 'coordinator',
          primaryConversationId: 'conv-p3',
          builtinOverview: true,
          urlHosts: ['linear.app'],
          allowedFiles: { 'view.json': 'file:///tmp/view.json' },
          members: [{ name: 'coordinator', role: 'coordinator', status: 'waiting' }],
          messages: [],
        }),
      );
      return;
    }
    if (reqUrl.pathname === '/board/frame') {
      res.writeHead(200, { 'Content-Type': 'image/png' });
      res.end(PNG_1X1);
      return;
    }
    res.writeHead(404);
    res.end();
  });
  const upstreamPort = await listenServer(mockUpstream);
  t.after(() => closeServer(mockUpstream));

  const liveCfg = sidecar.createSidecarConfig({
    HERMES_A2A_BRIDGE_DIR: BRIDGE_DIR,
    HERMES_A2A_STATE_DIR: stateDir,
    HERMES_A2A_BOARD_URL: `http://127.0.0.1:${upstreamPort}`,
    ANTIGRAVITY_CONVERSATION_ID: 'conv-p3',
    SWARM_OVERVIEW: 'false',
    SWARM_GRAPH_TAB: '${flag:enable-swarm-graph-tab}',
    SWARM_RAW_TAB: '${flag:enable-swarm-raw-tab}',
  });

  const swarmRes = await sidecar.handleGetSwarm({ conversationId: 'conv-p3' }, liveCfg);
  assert.equal(swarmRes.online, true);
  assert.equal(swarmRes.etag, 'sw-p3|coordinator|raw-tag-7');
  assert.equal(swarmRes.builtinOverview, false, 'Explicit SWARM_OVERVIEW=false must override upstream');
  assert.equal(swarmRes.graphTab, true);
  assert.equal(swarmRes.rawTab, true);
  assert.equal(swarmRes.urlHosts.includes('docs.google.com'), true);
  assert.equal(swarmRes.urlHosts.includes('linear.app'), true);
  assert.deepEqual(swarmRes.allowedFiles, { 'view.json': 'file:///tmp/view.json' });
  assert.equal(seenRequests[0].auth, 'Bearer auto-internal-token-xyz');

  const notModified = await sidecar.handleGetSwarm(
    { conversationId: 'conv-p3', etag: 'sw-p3|coordinator|raw-tag-7' },
    liveCfg,
  );
  assert.equal(notModified.unchanged, true);
  assert.equal(notModified.swarm, 'sw-p3');
  assert.equal(seenRequests[1].ifNoneMatch, 'sw-p3|coordinator|raw-tag-7');

  const sdk = await sidecar.loadSidecarSdk();
  await sidecar.handleGetFrame(
    { conversationId: 'conv-p3', path: 'shots/step1.png', seq: '42' },
    liveCfg,
    {},
    sdk.Response,
  );
  await sidecar.handleGetFrame(
    { conversationId: 'conv-p3', path: 'shots/step1.png', seq: 'not-a-number' },
    liveCfg,
    {},
    sdk.Response,
  );
  assert.equal(seenRequests[2].path, '/board/frame');
  assert.match(seenRequests[2].search, /seq=42/);
  assert.equal(seenRequests[2].auth, 'Bearer auto-internal-token-xyz');
  assert.equal(seenRequests[3].search.includes('seq='), false, 'Non-numeric seq must be stripped');
});

test('Phase-3 wakeup_bridge.mjs: unified syntheticKey dedup, swarm_wait double-wake suppression, and POST /internal/wake/events >2048 fallback', async (t) => {
  const tempDir = mkdtempSync(join(tmpdir(), 'hermes-wakeup-p3-'));
  t.after(() => rmSync(tempDir, { recursive: true, force: true }));

  const cursorFile = join(tempDir, 'cursors.json');
  const appData = join(tempDir, 'appData');
  mkdirSync(join(appData, 'brain', 'conv-prim'), { recursive: true });
  mkdirSync(join(appData, 'brain', 'conv-coord'), { recursive: true });

  const sent = [];
  const mockApp = {
    sendMessage: async (convId, message) => {
      sent.push({ convId, message });
    },
  };

  let currentDash = {
    swarmId: 'sw-dedup',
    primaryConversationId: 'conv-prim',
    coordinatorConversationId: 'conv-coord',
    messages: [],
    activeTasks: [
      { taskId: 'task-1', workerName: 'coder', role: 'worker', state: 'TASK_STATE_WORKING' },
      { taskId: 'task-2', workerName: 'ui', role: 'worker', state: 'TASK_STATE_WORKING' },
    ],
  };

  const dashServer = createServer((req, res) => {
    const reqUrl = new URL(req.url, 'http://127.0.0.1');
    if (reqUrl.pathname === '/board') {
      res.writeHead(200, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify(currentDash));
      return;
    }
    res.writeHead(404);
    res.end();
  });
  const dashPort = await listenServer(dashServer);
  t.after(() => closeServer(dashServer));

  const bridge = wakeup.createWakeupBridge({
    sendMessage: async (convId, message) => {
      sent.push({ convId, message });
    },
    config: {
      conversationId: 'conv-prim',
      stateDir: tempDir,
      fallbackBoardUrl: `http://127.0.0.1:${dashPort}`,
    },
    appDataDirs: [appData],
    cursorStore: wakeup.createCursorStore(cursorFile),
    env: { ANTIGRAVITY_EXECUTABLE_DATA_DIR: appData },
    logger: { log: () => {}, warn: () => {}, error: () => {} },
  });
  t.after(() => bridge.stop());

  // Prime task states as WORKING, then transition task-1 -> TASK_STATE_FAILED
  await bridge.reconcileOnce();
  assert.equal(sent.length, 0);

  currentDash = {
    swarmId: 'sw-dedup',
    primaryConversationId: 'conv-prim',
    coordinatorConversationId: 'conv-coord',
    messages: [],
    activeTasks: [
      { taskId: 'task-1', workerName: 'coder', role: 'worker', state: 'TASK_STATE_FAILED', error: 'boom' },
      { taskId: 'task-2', workerName: 'ui', role: 'worker', state: 'TASK_STATE_WORKING' },
    ],
  };

  // 1. Unified syntheticKey dedup across processDashboard (via reconcileOnce) & handleSseEvent
  await bridge.reconcileOnce();
  assert.equal(sent.length, 1);
  assert.match(sent[0].message, /TASK_STATE_FAILED/);

  // Same task failure arriving over SSE with same syntheticKey must be deduplicated
  await bridge.handleSseEvent({
    event: 'coordinator_wake',
    data: JSON.stringify({
      swarmId: 'sw-dedup',
      primaryConversationId: 'conv-prim',
      coordinatorConversationId: 'conv-coord',
      synthetic: true,
      wakeSeq: 1,
      task: { taskId: 'task-1', workerName: 'coder', state: 'TASK_STATE_FAILED' },
      summary: '@coder entered TASK_STATE_FAILED (task-1)',
    }),
  });
  assert.equal(sent.length, 1, 'Unified syntheticKey must prevent SSE duplicate after processDashboard');

  // Reverse direction: SSE arrives first for task-2, then processDashboard sees task-2 -> 0 duplicates
  await bridge.handleSseEvent({
    event: 'coordinator_wake',
    data: JSON.stringify({
      swarmId: 'sw-dedup',
      primaryConversationId: 'conv-prim',
      coordinatorConversationId: 'conv-coord',
      synthetic: true,
      wakeSeq: 2,
      task: { taskId: 'task-2', workerName: 'ui', state: 'TASK_STATE_FAILED' },
      summary: '@ui entered TASK_STATE_FAILED (task-2)',
    }),
  });
  assert.equal(sent.length, 2);
  currentDash = {
    swarmId: 'sw-dedup',
    primaryConversationId: 'conv-prim',
    coordinatorConversationId: 'conv-coord',
    messages: [],
    activeTasks: [
      { taskId: 'task-1', workerName: 'coder', role: 'worker', state: 'TASK_STATE_FAILED', error: 'boom' },
      { taskId: 'task-2', workerName: 'ui', role: 'worker', state: 'TASK_STATE_FAILED', error: 'css err' },
    ],
  };
  await bridge.reconcileOnce();
  assert.equal(sent.length, 2, 'Unified syntheticKey must prevent processDashboard duplicate after SSE');

  // 2. swarm_wait double-wake suppression: coordinatorWaiting=true suppresses sendMessage but advances cursor
  currentDash = {
    swarmId: 'sw-dedup',
    primaryConversationId: 'conv-prim',
    coordinatorConversationId: 'conv-coord',
    coordinatorWaiting: true,
    messages: [
      {
        seq: 10,
        id: 'm-10',
        sender: 'coder',
        senderRole: 'worker',
        kind: 'handoff',
        mentions: ['coordinator'],
        summary: '@coordinator waiting-covered handoff',
      },
    ],
  };
  await bridge.reconcileOnce();
  assert.equal(sent.length, 2, 'coordinatorWaiting=true must suppress duplicate app.sendMessage');
  let cursorsAfterWait = JSON.parse(readFileSync(cursorFile, 'utf-8'));
  assert.equal(cursorsAfterWait['sw-dedup'].lastDispatchedSeq, 10, 'Cursor must still advance when suppressed');

  // waitSince > 0 covering seq 11 (> waitSince) also suppresses sendMessage while advancing cursor
  currentDash = {
    swarmId: 'sw-dedup',
    primaryConversationId: 'conv-prim',
    coordinatorConversationId: 'conv-coord',
    members: [{ name: 'coordinator', role: 'coordinator', waitSince: 10 }],
    messages: [
      {
        seq: 11,
        id: 'm-11',
        sender: 'coder',
        senderRole: 'worker',
        kind: 'handoff',
        mentions: ['coordinator'],
        summary: '@coordinator waitSince-covered handoff',
      },
    ],
  };
  await bridge.reconcileOnce();
  assert.equal(sent.length, 2, 'waitSince covering post seq must suppress duplicate app.sendMessage');
  cursorsAfterWait = JSON.parse(readFileSync(cursorFile, 'utf-8'));
  assert.equal(cursorsAfterWait['sw-dedup'].lastDispatchedSeq, 11);

  // 3. POST /internal/wake/events fallback when serialized since_map > 2048 chars (+ 405 -> bounded GET fallback)
  const bigCursorFile = join(tempDir, 'big_cursors.json');
  const bigCursors = {};
  for (let i = 0; i < 90; i++) {
    bigCursors[`long-swarm-identifier-for-since-map-overflow-${String(i).padStart(3, '0')}`] = {
      lastDispatchedSeq: 100 + i,
      wakeSeq: 50 + i,
    };
  }
  writeFileSync(bigCursorFile, JSON.stringify(bigCursors), 'utf-8');

  const wakeRequests = [];
  let postAttemptCount = 0;
  const wakeServer = createServer(async (req, res) => {
    const reqUrl = new URL(req.url, 'http://127.0.0.1');
    let bodyText = '';
    for await (const chunk of req) bodyText += chunk.toString('utf-8');
    wakeRequests.push({ method: req.method, pathname: reqUrl.pathname, search: reqUrl.search, bodyText });

    if (reqUrl.pathname === '/internal/wake/swarms') {
      res.writeHead(200, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify({ swarms: [] }));
      return;
    }
    if (reqUrl.pathname === '/internal/wake/events') {
      if (req.method === 'POST') {
        postAttemptCount += 1;
        if (postAttemptCount === 1) {
          res.writeHead(405, { 'Content-Type': 'text/plain' });
          res.end('Method Not Allowed');
          return;
        }
      }
      res.writeHead(200, { 'Content-Type': 'text/event-stream; charset=utf-8' });
      res.write(`event: ready\ndata: ${JSON.stringify({ ok: true, swarms: [] })}\n\n`);
      setTimeout(() => {
        try { res.end(); } catch {}
      }, 60);
      return;
    }
    res.writeHead(404);
    res.end();
  });
  const wakePort = await listenServer(wakeServer);
  t.after(() => closeServer(wakeServer));

  const postBridge = wakeup.createWakeupBridge({
    sendMessage: mockApp.sendMessage,
    hostLocalMode: true,
    allowUnscoped: true,
    config: {
      stateDir: tempDir,
      fallbackBoardUrl: `http://127.0.0.1:${wakePort}`,
    },
    appDataDirs: [appData],
    resolveAllUpstreams: () => [`http://127.0.0.1:${wakePort}`],
    cursorStore: wakeup.createCursorStore(bigCursorFile),
    env: { ANTIGRAVITY_EXECUTABLE_DATA_DIR: appData },
    logger: { log: () => {}, warn: () => {}, error: () => {} },
  });
  t.after(() => bridge.stop());
  postBridge.start();
  t.after(() => postBridge.stop());

  const waitUntil = async (fn, ms = 4000) => {
    const deadline = Date.now() + ms;
    while (Date.now() < deadline) {
      if (fn()) return;
      await new Promise((r) => setTimeout(r, 25));
    }
    assert.fail('Timed out waiting for wakeRequests');
  };
  await waitUntil(() => wakeRequests.some((r) => r.method === 'GET' && r.pathname === '/internal/wake/events'));

  const postReq = wakeRequests.find((r) => r.method === 'POST' && r.pathname === '/internal/wake/events');
  assert.ok(postReq, 'Expected POST /internal/wake/events when since_map > 2048 chars');
  const postBody = JSON.parse(postReq.bodyText);
  assert.equal(postBody.active_only, true);
  assert.ok(JSON.stringify(postBody.since_map).length > 2048);

  const fallbackGet = wakeRequests.find((r) => r.method === 'GET' && r.pathname === '/internal/wake/events');
  assert.ok(fallbackGet, 'Expected bounded GET fallback after 405');
  const fallbackSinceMap = new URLSearchParams(fallbackGet.search).get('since_map') || '';
  assert.ok(fallbackSinceMap.length > 0 && fallbackSinceMap.length <= 2000, `Bounded since_map length=${fallbackSinceMap.length}`);
});

// ---------------------------------------------------------------------------
// #140 Regressions: peer handoff routing, fresh swarm_wait suppression,
// 2s completion dedup, non-OK HTTP logging + dynamic tokenFile rotation
// ---------------------------------------------------------------------------

test('#140 regressions: @verifier routing, fresh swarm_wait suppression across SSE+dash, 2s completion dedup, and HTTP status/tokenFile rotation', async (t) => {
  // 1. classifyBoardMessage routing
  assert.equal(
    wakeup.classifyBoardMessage({
      seq: 1,
      sender: 'coder',
      senderRole: 'worker',
      kind: 'handoff',
      status: 'open',
      mentions: ['verifier'],
      summary: '@verifier please verify slice 1',
    }),
    null,
    'Addressed peer handoff (@verifier) must NOT wake coordinator',
  );
  assert.equal(
    wakeup.classifyBoardMessage({
      seq: 2,
      sender: 'coder',
      senderRole: 'worker',
      kind: 'handoff_ready',
      status: 'open',
      summary: '@verifier [done] ready for check',
    }),
    null,
    'Inline @verifier [done] handoff_ready must NOT wake coordinator',
  );
  assert.equal(
    wakeup.classifyBoardMessage({
      seq: 3,
      sender: 'coder',
      senderRole: 'worker',
      kind: 'handoff',
      status: 'open',
      summary: '@coordinator [done] finished',
    }),
    'coordinator',
    '@coordinator mention must wake coordinator',
  );
  assert.equal(
    wakeup.classifyBoardMessage({
      seq: 4,
      sender: 'coder',
      senderRole: 'worker',
      kind: 'handoff',
      status: 'open',
      summary: '@all schema updated',
    }),
    'coordinator',
    '@all mention must wake coordinator',
  );
  assert.equal(
    wakeup.classifyBoardMessage({
      seq: 5,
      sender: 'coder',
      senderRole: 'worker',
      kind: 'handoff',
      status: 'open',
      mentions: [],
      summary: 'Unaddressed handoff ready',
    }),
    'coordinator',
    'Unaddressed handoff must wake coordinator',
  );
  for (const kind of ['question', 'blocker', 'escalation', 'approval_request']) {
    assert.equal(
      wakeup.classifyBoardMessage({
        seq: 6,
        sender: 'coder',
        senderRole: 'worker',
        kind,
        status: 'open',
        mentions: ['verifier'],
        summary: `@verifier ${kind}`,
      }),
      'coordinator',
      `Escalation kind=${kind} must wake coordinator even with peer mention`,
    );
  }
  assert.equal(
    wakeup.classifyBoardMessage({
      seq: 7,
      sender: 'monitor',
      senderRole: 'worker',
      kind: 'status',
      status: 'open',
      mentions: ['coder'],
      summary: '[stall] @coder appears stalled',
    }),
    'coordinator',
    '[stall] prefix must wake coordinator',
  );

  // 2. handleSseEvent @verifier suppression, fresh /board swarm_wait suppression, 2s completion dedup
  const tempDir = mkdtempSync(join(tmpdir(), 'hermes-140-'));
  t.after(() => rmSync(tempDir, { recursive: true, force: true }));

  const appData = join(tempDir, 'appData');
  mkdirSync(join(appData, 'brain', 'conv-prim-140'), { recursive: true });
  mkdirSync(join(appData, 'brain', 'conv-coord-140'), { recursive: true });

  const tokenFile = join(tempDir, 'internal_token');
  writeFileSync(tokenFile, 'tok-v1', 'utf-8');

  let activeToken = 'tok-v1';
  let boardResponseStatus = 200;
  let boardPayload = {
    swarmId: 'sw-140',
    primaryConversationId: 'conv-prim-140',
    coordinatorConversationId: 'conv-coord-140',
    coordinatorWaiting: false,
    messages: [],
    activeTasks: [{ taskId: 't-1', workerName: 'coder', state: 'TASK_STATE_WORKING' }],
  };

  const authHeadersSeen = [];
  const server = createServer((req, res) => {
    const reqUrl = new URL(req.url, 'http://127.0.0.1');
    authHeadersSeen.push(req.headers.authorization || '');
    if (req.headers.authorization !== `Bearer ${activeToken}`) {
      res.writeHead(401, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify({ error: 'unauthorized' }));
      return;
    }
    if (reqUrl.pathname === '/board') {
      if (boardResponseStatus !== 200) {
        res.writeHead(boardResponseStatus, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({ error: `HTTP ${boardResponseStatus}` }));
        return;
      }
      res.writeHead(200, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify(boardPayload));
      return;
    }
    if (reqUrl.pathname === '/board/frame') {
      if (boardResponseStatus !== 200) {
        res.writeHead(boardResponseStatus, { 'Content-Type': 'text/plain' });
        res.end('err');
        return;
      }
      res.writeHead(200, { 'Content-Type': 'image/png' });
      res.end(PNG_1X1);
      return;
    }
    res.writeHead(404);
    res.end();
  });
  const port = await listenServer(server);
  t.after(() => closeServer(server));

  const sent = [];
  const warnLogs = [];
  const cursorFile = join(tempDir, 'cursors.json');
  const bridge = wakeup.createWakeupBridge({
    sendMessage: async (convId, message) => {
      sent.push({ convId, message });
    },
    hostLocalMode: true,
    allowUnscoped: true,
    config: {
      stateDir: tempDir,
      tokenFile,
      fallbackBoardUrl: `http://127.0.0.1:${port}`,
    },
    appDataDirs: [appData],
    resolveAllUpstreams: () => [`http://127.0.0.1:${port}`],
    cursorStore: wakeup.createCursorStore(cursorFile),
    env: { ANTIGRAVITY_EXECUTABLE_DATA_DIR: appData },
    logger: {
      log: () => {},
      warn: (msg) => warnLogs.push(String(msg)),
      error: (msg) => warnLogs.push(String(msg)),
    },
  });
  t.after(() => bridge.stop());

  // 2a. SSE coordinator_wake for @verifier handoff must NOT wake coordinator
  const verifierRes = await bridge.handleSseEvent({
    event: 'coordinator_wake',
    data: JSON.stringify({
      swarmId: 'sw-140',
      primaryConversationId: 'conv-prim-140',
      coordinatorConversationId: 'conv-coord-140',
      seq: 20,
      message: {
        seq: 20,
        sender: 'coder',
        senderRole: 'worker',
        kind: 'handoff',
        status: 'open',
        mentions: ['verifier'],
        summary: '@verifier check this',
      },
    }),
  });
  assert.equal(verifierRes, 0);
  assert.equal(sent.length, 0);

  // 2b. Fresh /board coordinatorWaiting=true suppresses SSE wake even when SSE payload omits coordinatorWaiting
  boardPayload = {
    swarmId: 'sw-140',
    primaryConversationId: 'conv-prim-140',
    coordinatorConversationId: 'conv-coord-140',
    coordinatorWaiting: true,
    messages: [],
    activeTasks: [{ taskId: 't-1', workerName: 'coder', state: 'TASK_STATE_WORKING' }],
  };
  await bridge.handleSseEvent({
    event: 'coordinator_wake',
    data: JSON.stringify({
      swarmId: 'sw-140',
      primaryConversationId: 'conv-prim-140',
      coordinatorConversationId: 'conv-coord-140',
      seq: 21,
      message: {
        seq: 21,
        sender: 'coder',
        senderRole: 'worker',
        kind: 'handoff',
        status: 'open',
        mentions: ['coordinator'],
        summary: '@coordinator waiting on swarm_wait',
      },
    }),
  });
  assert.equal(sent.length, 0, 'Fresh /board coordinatorWaiting=true must suppress SSE sendMessage');
  assert.equal(wakeup.createCursorStore(cursorFile).load('sw-140').coordinatorSeq, 21);

  // 2c. COMPLETION_DEDUP_WINDOW_MS (2000ms): terminal task wake#N deduplicates All active workers finished
  boardPayload = {
    swarmId: 'sw-140',
    primaryConversationId: 'conv-prim-140',
    coordinatorConversationId: 'conv-coord-140',
    coordinatorWaiting: false,
    messages: [],
    activeTasks: [{ taskId: 't-1', workerName: 'coder', state: 'TASK_STATE_WORKING' }],
  };
  // Seed prevActiveCounts = 1 via dashboard snapshot
  await bridge.handleSseEvent({
    event: 'snapshot',
    data: JSON.stringify(boardPayload),
  });
  // Now terminal task SSE arrives (wakeSeq 1)
  await bridge.handleSseEvent({
    event: 'coordinator_wake',
    data: JSON.stringify({
      swarmId: 'sw-140',
      primaryConversationId: 'conv-prim-140',
      coordinatorConversationId: 'conv-coord-140',
      synthetic: true,
      wakeSeq: 1,
      task: { taskId: 't-1', workerName: 'coder', state: 'TASK_STATE_COMPLETED' },
      summary: '@coder entered TASK_STATE_COMPLETED (t-1)',
    }),
  });
  assert.equal(sent.length, 1, 'Terminal task completion should dispatch once');

  // Immediately followed by dashboard showing 0 activeTasks -> All active workers finished must be deduped!
  await bridge.handleSseEvent({
    event: 'snapshot',
    data: JSON.stringify({
      ...boardPayload,
      activeTasks: [],
      tasks: [{ taskId: 't-1', workerName: 'coder', state: 'TASK_STATE_COMPLETED' }],
    }),
  });
  assert.equal(sent.length, 1, 'All active workers finished within 2000ms must be deduplicated');

  // 3. Token rotation (401 -> re-read tokenFile & retry) + non-OK status logging in wakeup_bridge & main.mjs
  activeToken = 'tok-v2';
  writeFileSync(tokenFile, 'tok-v2', 'utf-8');
  boardPayload = {
    swarmId: 'sw-140',
    primaryConversationId: 'conv-prim-140',
    coordinatorConversationId: 'conv-coord-140',
    coordinatorWaiting: false,
    messages: [],
    activeTasks: [],
  };
  await bridge.handleSseEvent({
    event: 'coordinator_wake',
    data: JSON.stringify({
      swarmId: 'sw-140',
      primaryConversationId: 'conv-prim-140',
      coordinatorConversationId: 'conv-coord-140',
      seq: 22,
      message: {
        seq: 22,
        sender: 'coder',
        senderRole: 'worker',
        kind: 'handoff',
        status: 'open',
        mentions: ['coordinator'],
        summary: '@coordinator after token rotation',
      },
    }),
  });
  assert.equal(sent.length, 2, 'wakeup_bridge must retry 401 after re-reading rotated tokenFile');
  assert.ok(authHeadersSeen.includes('Bearer tok-v2'));

  // Non-OK status logging in wakeup_bridge (403 & 500)
  boardResponseStatus = 403;
  await bridge.handleSseEvent({
    event: 'coordinator_wake',
    data: JSON.stringify({
      swarmId: 'sw-140',
      primaryConversationId: 'conv-prim-140',
      coordinatorConversationId: 'conv-coord-140',
      seq: 23,
      message: {
        seq: 23,
        sender: 'coder',
        senderRole: 'worker',
        kind: 'handoff',
        status: 'open',
        mentions: ['coordinator'],
        summary: '@coordinator 403 check',
      },
    }),
  });
  assert.ok(warnLogs.some((l) => l.includes('HTTP 403')), `Expected HTTP 403 log, got: ${JSON.stringify(warnLogs)}`);

  // main.mjs dynamic tokenFile rotation on 401 + mtime change + 403/500 logging
  const mainCfg = sidecar.createSidecarConfig({
    HERMES_A2A_STATE_DIR: tempDir,
    HERMES_A2A_TOKEN_FILE: tokenFile,
    HERMES_A2A_BOARD_URL: `http://127.0.0.1:${port}`,
    ANTIGRAVITY_CONVERSATION_ID: 'conv-prim-140',
  });
  activeToken = 'tok-v3';
  writeFileSync(tokenFile, 'tok-v3', 'utf-8');
  boardResponseStatus = 200;

  const origWarn = console.warn;
  const mainWarns = [];
  console.warn = (...args) => mainWarns.push(args.map(String).join(' '));
  try {
    const okSwarm = await sidecar.handleGetSwarm({ conversationId: 'conv-prim-140' }, mainCfg, { conversationId: 'conv-prim-140' });
    assert.equal(okSwarm.swarmId, 'sw-140');
    assert.ok(authHeadersSeen.includes('Bearer tok-v3'));

    for (const errCode of [401, 403, 500]) {
      boardResponseStatus = errCode;
      activeToken = errCode === 401 ? 'never-matches' : 'tok-v3';
      const swErr = await sidecar.handleGetSwarm({ conversationId: 'conv-prim-140' }, mainCfg, { conversationId: 'conv-prim-140' });
      assert.equal(swErr.status, errCode);
      assert.match(swErr.error, new RegExp(`HTTP ${errCode}`));
      assert.ok(mainWarns.some((w) => w.includes(`GET /board returned HTTP ${errCode}`)), `Missing GET /board ${errCode} warn`);

      class FakeResp {
        constructor(body, init) {
          this.body = body;
          this.status = init.status;
          this.contentType = init.contentType;
        }
      }
      const frErr = await sidecar.handleGetFrame(
        { conversationId: 'conv-prim-140', path: 'frames/test.png' },
        mainCfg,
        { conversationId: 'conv-prim-140' },
        FakeResp,
      );
      assert.equal(frErr.status, errCode === 500 ? 502 : errCode);
      assert.ok(mainWarns.some((w) => w.includes(`GET /board/frame returned HTTP ${errCode}`)), `Missing GET /board/frame ${errCode} warn`);
    }
  } finally {
    console.warn = origWarn;
  }
});

test('#160 regressions: handleGetSwarm anti-flicker lastGoodSwarmByConv, cold-start/401/403 boundaries, and wakeup_bridge 6000ms + throttled transient logging', async (t) => {
  // 1. Verify 6000ms timeout constants in main.mjs and wakeup_bridge.mjs
  const mainSrc = readFileSync(MAIN_MJS_PATH, 'utf-8');
  const wakeupSrc = readFileSync(WAKEUP_BRIDGE_PATH, 'utf-8');
  assert.match(mainSrc, /BOARD_FETCH_TIMEOUT_MS\s*=\s*6000\b/);
  assert.match(wakeupSrc, /DEFAULT_FETCH_TIMEOUT_MS\s*=\s*6_?000\b/);

  const cfg = sidecar.createSidecarConfig({
    HERMES_A2A_BOARD_URL: 'http://127.0.0.1:9910',
    HERMES_A2A_HTTP_TOKEN: 'tok-160',
  });

  // 2. Genuine cold start returns { online: true, transientError: false, swarm: null }
  const coldPayload = {
    swarm: null,
    primaryConversationId: 'conv-160-cold',
    members: [],
    messages: [],
  };
  const coldRes = await sidecar.handleGetSwarm(
    { conversationId: 'conv-160-cold' },
    cfg,
    { conversationId: 'conv-160-cold' },
    async () => new Response(JSON.stringify(coldPayload), {
      status: 200,
      headers: { 'Content-Type': 'application/json' },
    }),
  );
  assert.equal(coldRes.online, true);
  assert.equal(coldRes.transientError, false);
  assert.equal(coldRes.swarm, null);

  // Cold start + 503 returns { online: false, transientError: true, swarm: null }
  const origWarn = console.warn;
  const warnLines = [];
  console.warn = (...args) => warnLines.push(args.map(String).join(' '));
  try {
    const cold503 = await sidecar.handleGetSwarm(
      { conversationId: 'conv-160-cold' },
      cfg,
      { conversationId: 'conv-160-cold' },
      async () => new Response('Service Unavailable', { status: 503 }),
    );
    assert.equal(cold503.online, false);
    assert.equal(cold503.transientError, true);
    assert.equal(cold503.status, 503);
    assert.equal(cold503.swarm, null);

    // 3. Active session populates lastGoodSwarmByConv; 503 & TimeoutError preserve lastGood (never swarm: null)
    const activeConv = 'conv-160-active';
    const activePayload = {
      swarm: 'sw-160',
      id: 'sw-160',
      etag: 'etag-160',
      primaryConversationId: activeConv,
      coordinatorConversationId: 'conv-160-coord',
      members: [
        { name: 'coordinator', role: 'coordinator', status: 'waiting' },
        { name: 'coder', role: 'worker', status: 'working' },
      ],
      messages: [{ seq: 1, agent: 'coordinator', text: 'Working' }],
    };
    const activeOk = await sidecar.handleGetSwarm(
      { conversationId: activeConv },
      cfg,
      { conversationId: activeConv },
      async (_url, init) => {
        assert.ok(init?.signal, 'handleGetSwarm must pass AbortSignal.timeout');
        return new Response(JSON.stringify(activePayload), {
          status: 200,
          headers: { 'Content-Type': 'application/json', ETag: 'etag-160' },
        });
      },
    );
    assert.equal(activeOk.online, true);
    assert.equal(activeOk.transientError, false);
    assert.equal(activeOk.swarm, 'sw-160');

    const active503 = await sidecar.handleGetSwarm(
      { conversationId: activeConv },
      cfg,
      { conversationId: activeConv },
      async () => new Response('Upstream busy', { status: 503 }),
    );
    assert.equal(active503.online, false);
    assert.equal(active503.transientError, true);
    assert.equal(active503.unchanged, true);
    assert.equal(active503.status, 503);
    assert.equal(active503.swarm, 'sw-160', '503 on active session must preserve lastGood swarm');
    assert.equal(active503.members.length, 2);

    const activeTimeout = await sidecar.handleGetSwarm(
      { conversationId: activeConv },
      cfg,
      { conversationId: activeConv },
      async () => {
        const err = new Error('The operation was aborted due to timeout');
        err.name = 'TimeoutError';
        throw err;
      },
    );
    assert.equal(activeTimeout.online, false);
    assert.equal(activeTimeout.transientError, true);
    assert.equal(activeTimeout.unchanged, true);
    assert.equal(activeTimeout.status, 504);
    assert.equal(activeTimeout.swarm, 'sw-160', 'TimeoutError on active session must preserve lastGood swarm');

    // 4. 401 and 403 NEVER serve stale lastGood
    for (const authErr of [401, 403]) {
      const resAuth = await sidecar.handleGetSwarm(
        { conversationId: activeConv },
        cfg,
        { conversationId: activeConv },
        async () => new Response('Auth error', { status: authErr }),
      );
      assert.equal(resAuth.online, false);
      assert.equal(resAuth.transientError, false);
      assert.equal(resAuth.status, authErr);
      assert.equal(resAuth.swarm, null, `HTTP ${authErr} must never serve stale lastGood`);
    }

    // 5. Genuine { swarm: null } clears lastGoodSwarmByConv so a subsequent 503 returns swarm: null
    const cleared = await sidecar.handleGetSwarm(
      { conversationId: activeConv },
      cfg,
      { conversationId: activeConv },
      async () => new Response(JSON.stringify({
        swarm: null,
        primaryConversationId: activeConv,
        members: [],
        messages: [],
      }), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      }),
    );
    assert.equal(cleared.online, true);
    assert.equal(cleared.transientError, false);
    assert.equal(cleared.swarm, null);

    const afterClear503 = await sidecar.handleGetSwarm(
      { conversationId: activeConv },
      cfg,
      { conversationId: activeConv },
      async () => new Response('Unavailable', { status: 503 }),
    );
    assert.equal(afterClear503.online, false);
    assert.equal(afterClear503.transientError, true);
    assert.equal(afterClear503.swarm, null);
  } finally {
    console.warn = origWarn;
  }

  // 6. wakeup_bridge.mjs: 6000ms timeout + throttled transient logging without crashing SSE loops
  const tempState = mkdtempSync(join(tmpdir(), 'hermes-wakeup-160-'));
  t.after(() => rmSync(tempState, { recursive: true, force: true }));
  const loggerCalls = [];
  let mode = 'timeout';
  let sseCalls = 0;

  const bridge160 = wakeup.createWakeupBridge({
    sendMessage: async () => {},
    config: {
      conversationId: 'conv-160-wake',
      boardUrl: 'http://127.0.0.1:9910',
      httpToken: 'tok-160',
    },
    hostLocalMode: false,
    reconcileIntervalMs: 30,
    cursorStore: wakeup.createCursorStore(join(tempState, 'cursors.json')),
    logger: {
      warn: (...args) => loggerCalls.push({ level: 'warn', text: args.map(String).join(' ') }),
      error: (...args) => loggerCalls.push({ level: 'error', text: args.map(String).join(' ') }),
    },
    fetchImpl: async (url) => {
      const u = String(url);
      if (u.includes('/board/events')) {
        sseCalls += 1;
        if (mode === 'timeout') {
          const err = new Error('The operation was aborted due to timeout');
          err.name = 'TimeoutError';
          throw err;
        }
        return new Response('event: snapshot\ndata: {"swarmId":"sw-160","primaryConversationId":"conv-160-wake","messages":[]}\n\n', {
          status: 200,
          headers: { 'Content-Type': 'text/event-stream' },
        });
      }
      if (mode === 'timeout') {
        const err = new Error('The operation was aborted due to timeout');
        err.name = 'TimeoutError';
        throw err;
      }
      if (mode === '503') {
        return new Response('Service Unavailable', { status: 503 });
      }
      return new Response(JSON.stringify({
        swarmId: 'sw-160',
        primaryConversationId: 'conv-160-wake',
        messages: [],
      }), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      });
    },
  });

  bridge160.start();
  t.after(() => bridge160.stop());
  await new Promise((r) => setTimeout(r, 40));
  await bridge160.reconcileOnce();

  const timeoutLogs = loggerCalls.filter((l) => l.text.includes('reconcile /board'));
  assert.equal(timeoutLogs.length, 1, `Expected TimeoutError reconcile log throttled to 1, got ${timeoutLogs.length}`);

  mode = '503';
  await bridge160.reconcileOnce();
  await bridge160.reconcileOnce();
  const http503Logs = loggerCalls.filter((l) => l.text.includes('HTTP 503'));
  assert.equal(http503Logs.length, 1, `Expected HTTP 503 log to be throttled to 1 within 60s, got ${http503Logs.length}`);

  mode = 'ok';
  await bridge160.reconcileOnce();
  await new Promise((r) => setTimeout(r, 80));
  assert.ok(sseCalls >= 1, 'SSE loop must remain alive and recover without crashing');
  bridge160.stop();
});

// ---------------------------------------------------------------------------
// 11. Phase-2 Hardening Regressions (#186): wakeup_bridge + main.mjs
// ---------------------------------------------------------------------------

test('#186 regressions: wakeup_bridge unread worker wake behind coordinator post, MAX_SSE_BUFFER_BYTES, and fetch outside eventLock', async (t) => {
  const tempState = mkdtempSync(join(tmpdir(), 'hermes-186-wake-'));
  t.after(() => rmSync(tempState, { recursive: true, force: true }));

  // 1) Unread worker wake (#5 @coordinator [done]) is NOT dropped when coordinator already posted #6
  const sentWakes = [];
  const bridge1 = wakeup.createWakeupBridge({
    sendMessage: async (convId, text) => {
      sentWakes.push({ convId, text });
    },
    config: {
      conversationId: 'conv-186-coord',
      boardUrl: 'http://127.0.0.1:9911',
      httpToken: 'tok-186',
    },
    hostLocalMode: false,
    cursorStore: wakeup.createCursorStore(join(tempState, 'cursors-1.json')),
    fetchImpl: async (url) => {
      if (String(url).includes('/board/events')) {
        return new Response('event: heartbeat\ndata: {}\n\n', {
          status: 200,
          headers: { 'Content-Type': 'text/event-stream' },
        });
      }
      return new Response(JSON.stringify({
        swarmId: 'sw-186',
        primaryConversationId: 'conv-186-coord',
        coordinatorConversationId: 'conv-186-coord',
        messages: [
          { seq: 4, agent: 'coordinator', role: 'coordinator', text: '@worker start task' },
          { seq: 5, agent: 'worker', role: 'worker', text: '@coordinator [done] finished slice' },
          { seq: 6, agent: 'coordinator', role: 'coordinator', text: '@other-worker next task' },
        ],
      }), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      });
    },
  });
  t.after(() => bridge1.stop());

  const rec1 = await bridge1.reconcileOnce();
  assert.equal(rec1.reconciled, true);
  assert.equal(rec1.wakeups, 1, 'Worker wake #5 must NOT be dropped even though coordinator posted #6');
  assert.equal(sentWakes.length, 1);
  assert.match(sentWakes[0].text, /finished slice/);
  bridge1.stop();

  // 2) consumeSseStream enforces maxBufferBytes (MAX_SSE_BUFFER_BYTES)
  const oversizedChunk = new TextEncoder().encode('data: ' + 'A'.repeat(2048) + '\n\n');
  const oversizedBody = new ReadableStream({
    start(controller) {
      controller.enqueue(oversizedChunk);
      controller.close();
    },
  });
  await assert.rejects(
    () => wakeup.consumeSseStream(oversizedBody, async () => {}, undefined, { maxBufferBytes: 512, idleTimeoutMs: 0 }),
    /SSE buffer exceeded 512 bytes/,
  );

  // 3) reconcileOnce runs HTTP fetches outside eventLock (concurrent fetch does not deadlock or block on slow sendMessage)
  let releaseSend = null;
  const sendStarted = new Promise((res) => {
    releaseSend = res;
  });
  let unblockSend = null;
  const sendGate = new Promise((res) => { unblockSend = res; });
  let fetchCount = 0;

  const bridge2 = wakeup.createWakeupBridge({
    sendMessage: async () => {
      releaseSend?.();
      await sendGate;
    },
    config: {
      conversationId: 'conv-186-lock',
      boardUrl: 'http://127.0.0.1:9912',
      httpToken: 'tok-186-lock',
    },
    hostLocalMode: false,
    cursorStore: wakeup.createCursorStore(join(tempState, 'cursors-2.json')),
    fetchImpl: async (url) => {
      if (String(url).includes('/board/events')) {
        return new Response('', { status: 200, headers: { 'Content-Type': 'text/event-stream' } });
      }
      fetchCount += 1;
      return new Response(JSON.stringify({
        swarmId: 'sw-186-lock',
        primaryConversationId: 'conv-186-lock',
        coordinatorConversationId: 'conv-186-lock',
        messages: [{ seq: 1, agent: 'w1', role: 'worker', text: '@coordinator [done] hold lock' }],
      }), { status: 200, headers: { 'Content-Type': 'application/json' } });
    },
  });
  t.after(() => bridge2.stop());

  const pReconcile = bridge2.reconcileOnce();
  await sendStarted;
  // While sendMessage is blocked inside eventLock, reconcileOnce HTTP fetch has already completed outside eventLock
  assert.equal(fetchCount, 1);
  unblockSend();
  await pReconcile;
  bridge2.stop();
});

test('#186 regressions: main.mjs checks scope before unchanged:true, caps oversized responses (413/502), and prunes conv caches', async (t) => {
  const tempState = mkdtempSync(join(tmpdir(), 'hermes-186-main-'));
  t.after(() => rmSync(tempState, { recursive: true, force: true }));

  let mode = 'unscoped-unchanged';
  const upstreamServer = createServer((req, res) => {
    const reqUrl = new URL(req.url, 'http://127.0.0.1');
    if (reqUrl.pathname === '/board') {
      if (mode === 'unscoped-unchanged') {
        res.writeHead(200, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({ unchanged: true, swarm: 'alpha', etag: 'e1' }));
        return;
      }
      if (mode === 'oversized-json') {
        res.writeHead(200, {
          'Content-Type': 'application/json',
          'Content-Length': String(17 * 1024 * 1024),
        });
        res.end('{}');
        return;
      }
      const conv = reqUrl.searchParams.get('conversationId') || 'conv-default';
      res.writeHead(200, { 'Content-Type': 'application/json', ETag: `etag-${conv}` });
      res.end(JSON.stringify({
        swarm: `sw-${conv}`,
        id: `sw-${conv}`,
        etag: `etag-${conv}`,
        primaryConversationId: conv,
        members: [],
        messages: [],
      }));
      return;
    }
    if (reqUrl.pathname === '/board/frame') {
      res.writeHead(200, {
        'Content-Type': 'image/png',
        'Content-Length': String(9 * 1024 * 1024),
      });
      res.end(PNG_1X1);
      return;
    }
    res.writeHead(404);
    res.end();
  });

  const upstreamPort = await listenServer(upstreamServer);
  t.after(() => closeServer(upstreamServer));

  const config = sidecar.createSidecarConfig({
    HERMES_A2A_STATE_DIR: tempState,
    HERMES_A2A_BOARD_URL: `http://127.0.0.1:${upstreamPort}`,
    ANTIGRAVITY_CONVERSATION_ID: 'conv-186-a',
  });

  // 1) Unscoped { unchanged: true } MUST be rejected (not returned as unchanged: true)
  const unscopedRes = await sidecar.handleGetSwarm(
    { conversationId: 'conv-186-a', etag: 'e1' },
    config,
  );
  assert.notEqual(unscopedRes.unchanged, true, 'Unscoped unchanged:true must be rejected');
  assert.equal(unscopedRes.swarm, null);

  // 2) Oversized /board JSON (>16 MiB) -> 502 error response
  mode = 'oversized-json';
  const oversizedSwarmRes = await sidecar.handleGetSwarm(
    { conversationId: 'conv-186-oversize' },
    config,
    {},
  );
  assert.equal(oversizedSwarmRes.status, 502);

  // 3) Oversized /board/frame (>8 MiB) -> 413 Payload too large
  const oversizedFrameRes = await sidecar.handleGetFrame(
    { conversationId: 'conv-186-a', path: 'shots/huge.png' },
    config,
    {},
    Response,
  );
  assert.equal(oversizedFrameRes.status, 413);

  // 4) Conv caches prune cleanly beyond MAX_CONV_CACHE_ENTRIES (100) without error
  mode = 'scoped-ok';
  for (let i = 0; i < 105; i += 1) {
    const r = await sidecar.handleGetSwarm({ conversationId: `conv-prune-${i}` }, config);
    assert.equal(r.swarm, `sw-conv-prune-${i}`);
  }
});

test('handleGetSwarm accepts unchanged:true without conv IDs ONLY when cached swarm matches', async (t) => {
  const tempState = mkdtempSync(join(tmpdir(), 'hermes-sidecar-unchanged-'));
  t.after(() => rmSync(tempState, { recursive: true, force: true }));

  let step = 'prime';
  const upstreamServer = createServer((req, res) => {
    const reqUrl = new URL(req.url, 'http://127.0.0.1');
    if (reqUrl.pathname === '/board') {
      const conv = reqUrl.searchParams.get('conversationId') || '';
      res.writeHead(200, { 'Content-Type': 'application/json' });
      if (step === 'prime') {
        res.end(JSON.stringify({
          swarm: 'sw-cached-ok',
          primaryConversationId: conv,
          etag: 'e-1',
          members: [],
          messages: [],
        }));
        return;
      }
      if (step === 'unchanged-match') {
        res.end(JSON.stringify({
          unchanged: true,
          swarm: 'sw-cached-ok',
          etag: 'e-1',
        }));
        return;
      }
      if (step === 'unchanged-mismatch') {
        res.end(JSON.stringify({
          unchanged: true,
          swarm: 'sw-other',
          etag: 'e-1',
        }));
        return;
      }
      res.end(JSON.stringify({
        unchanged: true,
        swarm: 'sw-uncached',
        etag: 'e-1',
      }));
      return;
    }
    res.writeHead(404);
    res.end();
  });

  const upstreamPort = await listenServer(upstreamServer);
  t.after(() => closeServer(upstreamServer));

  const config = sidecar.createSidecarConfig({
    HERMES_A2A_STATE_DIR: tempState,
    HERMES_A2A_BOARD_URL: `http://127.0.0.1:${upstreamPort}`,
  });

  // 1) Uncached unchanged:true without conv IDs -> fail-closed
  step = 'unchanged-uncached';
  const uncachedRes = await sidecar.handleGetSwarm({ conversationId: 'conv-unch-new', etag: 'e-1' }, config);
  assert.notEqual(uncachedRes.unchanged, true);
  assert.equal(uncachedRes.swarm, null);

  // 2) Prime cache, then unchanged:true with matching swarm -> accepted
  step = 'prime';
  const primeRes = await sidecar.handleGetSwarm({ conversationId: 'conv-unch-cached' }, config);
  assert.equal(primeRes.swarm, 'sw-cached-ok');

  step = 'unchanged-match';
  const matchRes = await sidecar.handleGetSwarm({ conversationId: 'conv-unch-cached', etag: 'e-1' }, config);
  assert.equal(matchRes.unchanged, true);
  assert.equal(matchRes.swarm, 'sw-cached-ok');

  // 3) Cached conv receives unchanged:true for a DIFFERENT swarm -> rejected
  step = 'unchanged-mismatch';
  const mismatchRes = await sidecar.handleGetSwarm({ conversationId: 'conv-unch-cached', etag: 'e-1' }, config);
  assert.notEqual(mismatchRes.unchanged, true);
  assert.equal(mismatchRes.swarm, null);
});

test('GET /api/member/history validates params, forwards query params, and enforces 1:1 conversation scope', async (t) => {
  const tempState = mkdtempSync(join(tmpdir(), 'hermes-sidecar-history-'));
  t.after(() => rmSync(tempState, { recursive: true, force: true }));

  const seenRequests = [];
  let responseMode = 'ok';
  const upstreamServer = createServer((req, res) => {
    const reqUrl = new URL(req.url, 'http://127.0.0.1');
    seenRequests.push({
      pathname: reqUrl.pathname,
      searchParams: Object.fromEntries(reqUrl.searchParams.entries()),
      auth: req.headers.authorization || '',
    });
    if (reqUrl.pathname === '/board/member/history') {
      if (responseMode === 'mismatch') {
        res.writeHead(200, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({
          ok: true,
          swarm: 'sw-other',
          primaryConversationId: 'conv-other',
          member: reqUrl.searchParams.get('member'),
          messages: [{ id: 1, role: 'assistant', content: 'leaked' }],
        }));
        return;
      }
      res.writeHead(200, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify({
        ok: true,
        swarm: 'sw-hist',
        primaryConversationId: reqUrl.searchParams.get('conversationId'),
        member: reqUrl.searchParams.get('member'),
        state: 'waiting',
        messages: [
          {
            id: 11,
            role: 'assistant',
            content: 'History payload ok',
            reasoning: 'Thinking step',
            reasoningTokens: 15,
          },
        ],
        hasMore: false,
        lastId: 11,
      }));
      return;
    }
    res.writeHead(404);
    res.end();
  });

  const upstreamPort = await listenServer(upstreamServer);
  t.after(() => closeServer(upstreamServer));

  const sidecarProc = await spawnSidecarProcess({
    HERMES_A2A_STATE_DIR: tempState,
    HERMES_A2A_BOARD_URL: `http://127.0.0.1:${upstreamPort}`,
    HERMES_A2A_HTTP_TOKEN: 'hist-secret-token',
    HERMES_SWARM_DISABLE_WAKEUP: '1',
    ANTIGRAVITY_CONVERSATION_ID: '',
  });
  t.after(() => sidecarProc.stop());

  // 1) 400 on missing conversationId
  const missingConvRes = await fetch(`${sidecarProc.baseUrl}/api/member/history?member=backend`);
  assert.equal(missingConvRes.status, 400);
  const missingConvJson = await missingConvRes.json();
  assert.equal(missingConvJson.ok, false);
  assert.match(missingConvJson.error, /conversationId and member are required/i);

  // 2) 400 on missing member
  const missingMemberRes = await fetch(`${sidecarProc.baseUrl}/api/member/history?conversationId=conv-hist-1`);
  assert.equal(missingMemberRes.status, 400);
  const missingMemberJson = await missingMemberRes.json();
  assert.equal(missingMemberJson.ok, false);
  assert.match(missingMemberJson.error, /conversationId and member are required/i);
  assert.equal(seenRequests.length, 0, '400 validation failures must not hit upstream');

  // 3) 200 forwarding to mock upstream /board/member/history with query params
  const okRes = await fetch(
    `${sidecarProc.baseUrl}/api/member/history?conversationId=conv-hist-1&member=backend&after_id=5&limit=50&subagent_session_id=sub-9`,
  );
  assert.equal(okRes.status, 200);
  const okJson = await okRes.json();
  assert.equal(okJson.ok, true);
  assert.equal(okJson.member, 'backend');
  assert.equal(okJson.messages.length, 1);
  assert.equal(okJson.messages[0].content, 'History payload ok');

  assert.equal(seenRequests.length, 1);
  assert.equal(seenRequests[0].pathname, '/board/member/history');
  assert.deepEqual(seenRequests[0].searchParams, {
    conversationId: 'conv-hist-1',
    member: 'backend',
    after_id: '5',
    limit: '50',
    subagent_session_id: 'sub-9',
  });
  assert.equal(seenRequests[0].auth, 'Bearer hist-secret-token');

  // 4) 403 on swarm scope mismatch
  responseMode = 'mismatch';
  const mismatchRes = await fetch(
    `${sidecarProc.baseUrl}/api/member/history?conversationId=conv-hist-1&member=backend`,
  );
  assert.equal(mismatchRes.status, 403);
  const mismatchJson = await mismatchRes.json();
  assert.equal(mismatchJson.ok, false);
  assert.match(mismatchJson.error, /scope validation/i);
});
