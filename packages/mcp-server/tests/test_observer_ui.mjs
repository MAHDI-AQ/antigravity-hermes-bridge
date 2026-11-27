import assert from 'node:assert/strict';
import { existsSync, mkdirSync, mkdtempSync, readdirSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { createServer, request as httpRequest } from 'node:http';
import { spawn } from 'node:child_process';
import { homedir, tmpdir } from 'node:os';
import { dirname, join, resolve } from 'node:path';
import test from 'node:test';
import { fileURLToPath, pathToFileURL } from 'node:url';

const __dirname = dirname(fileURLToPath(import.meta.url));
const BRIDGE_DIR = resolve(__dirname, '..');
const BOARD_UI_DIR = join(BRIDGE_DIR, 'board_ui');
const SIBLING_SIDECAR_DIR = resolve(BRIDGE_DIR, '..', 'antigravity-plugin', 'sidecars', 'swarm_board');
const SIDECAR_DIR = process.env.HERMES_SWARM_SIDECAR_DIR
  || (existsSync(SIBLING_SIDECAR_DIR)
    ? SIBLING_SIDECAR_DIR
    : join(homedir(), '.gemini', 'config', 'plugins', 'hermes-swarm', 'sidecars', 'swarm_board'));
const MAIN_MJS_PATH = process.env.HERMES_SWARM_MAIN_MJS || join(SIDECAR_DIR, 'main.mjs');

const PNG_1X1 = Buffer.from(
  'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==',
  'base64',
);

async function loadPlaywright() {
  for (const pkg of ['playwright', 'playwright-core']) {
    try {
      const mod = await import(pkg);
      return mod.default || mod;
    } catch {
      // continue
    }
  }

  const candidates = [];
  const npxCache = join(homedir(), 'AppData', 'Local', 'npm-cache', '_npx');
  if (existsSync(npxCache)) {
    for (const sub of readdirSync(npxCache)) {
      candidates.push(join(npxCache, sub, 'node_modules', 'playwright-core', 'index.js'));
      candidates.push(join(npxCache, sub, 'node_modules', 'playwright', 'index.js'));
    }
  }
  const linksDir = join(homedir(), 'AppData', 'Local', 'ms-playwright', '.links');
  if (existsSync(linksDir)) {
    for (const entry of readdirSync(linksDir)) {
      try {
        const target = readFileSync(join(linksDir, entry), 'utf-8').trim();
        candidates.push(join(target, 'index.js'));
      } catch {
        // ignore
      }
    }
  }

  for (const candidate of candidates) {
    if (existsSync(candidate)) {
      const mod = await import(pathToFileURL(candidate).href);
      return mod.default || mod;
    }
  }
  throw new Error('Playwright package could not be resolved');
}

function findBrowserExecutable() {
  const candidates = [
    'C:/Program Files/Google/Chrome/Application/chrome.exe',
    'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe',
    '/usr/bin/google-chrome',
    '/usr/bin/chromium',
    '/usr/bin/chromium-browser',
  ];
  for (const p of candidates) {
    if (existsSync(p)) return p;
  }
  return undefined;
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

async function spawnSidecarProcess(extraEnv = {}) {
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
    stop: async () => {
      if (child.exitCode === null && child.signalCode === null) {
        child.kill();
        await new Promise((resolveExit) => child.once('exit', resolveExit));
      }
    },
  };
}

function createSubpathProxy(targetPort, subpathPrefix) {
  const proxyLeaks = [];
  const server = createServer((req, res) => {
    const reqUrl = new URL(req.url, 'http://127.0.0.1');
    if (!reqUrl.pathname.startsWith(subpathPrefix)) {
      proxyLeaks.push(reqUrl.pathname);
      res.writeHead(404, { 'Content-Type': 'text/plain' });
      res.end(`Subpath escape blocked: ${reqUrl.pathname}`);
      return;
    }
    const stripped = reqUrl.pathname.slice(subpathPrefix.length) || '/';
    const forwardPath = `${stripped}${reqUrl.search}`;
    const fwd = httpRequest(
      {
        hostname: '127.0.0.1',
        port: targetPort,
        path: forwardPath,
        method: req.method,
        headers: req.headers,
      },
      (upRes) => {
        res.writeHead(upRes.statusCode || 502, upRes.headers);
        upRes.pipe(res);
      },
    );
    fwd.on('error', (err) => {
      res.writeHead(502);
      res.end(String(err));
    });
    req.pipe(fwd);
  });
  return { server, proxyLeaks };
}

function buildRealisticV2Snapshot() {
  // Deliberately omits view.exists to verify #raw-btn-view shows via raw.view alone.
  return {
    swarm: 'ui-verify-swarm',
    id: 'ui-verify-swarm',
    etag: 'v2-etag-1',
    head: 4,
    title: 'Observer Verification Swarm',
    dir: 'C:/Projects/demo',
    primaryConversationId: 'conv-primary-ui',
    coordinatorConversationId: 'conv-coord-ui',
    meta: {
      swarmId: 'ui-verify-swarm',
      title: 'Observer Verification Swarm',
      workspace: 'C:/Projects/demo',
      primaryConversationId: 'conv-primary-ui',
      coordinatorConversationId: 'conv-coord-ui',
    },
    plan: {
      goal: {
        title: 'Observer Verification',
        line: 'Verify all 5 tabs and 3-tier read-only UI',
      },
      threads: [
        {
          id: 'th-1',
          title: 'Core Bridge',
          goal: 'Verify sidecar and UI parity',
          workstreams: ['backend', 'ui'],
        },
      ],
      tasks: {
        backend: 'Verify /board routes & SSE',
        ui: 'Render clean-sheet observer',
      },
    },
    channels: [
      { name: 'main', topic: 'Swarm main channel' },
      { name: 'backend', topic: 'Backend APIs', created_by: 'coordinator' },
    ],
    members: [
      {
        name: 'coordinator',
        role: 'coordinator',
        status: 'working',
        conversationId: 'conv-coord-ui',
        steps: 4,
        posts: 2,
        now: 'Reviewing verification run',
        subagents: [],
      },
      {
        name: 'backend',
        role: 'worker',
        status: 'working',
        model: 'claude-sonnet',
        steps: 7,
        posts: 2,
        now: 'Checking /board and /board/frame',
        channels: ['backend'],
        subagents: [
          {
            id: 'sub-1',
            name: 'schema-checker',
            task: 'Inspect JSON contract',
            status: 'working',
            model: 'flash',
            steps: 3,
            summary: 'Checking fields',
          },
          {
            id: 'sub-2',
            name: 'frame-auditor',
            task: 'Verify path containment',
            status: 'completed',
            model: 'flash',
            steps: 2,
            summary: 'Done',
          },
        ],
      },
      {
        name: 'ui',
        role: 'worker',
        status: 'waiting',
        model: 'gemini-pro',
        steps: 5,
        posts: 1,
        now: 'Delivered read-only observer',
        subagents: [],
      },
      {
        name: 'narrator',
        role: 'narrator',
        status: 'waiting',
        steps: 2,
        posts: 1,
        lastReadSeq: 4,
        subagents: [],
      },
    ],
    messages: [
      {
        seq: 1,
        id: 'bmsg-1001',
        agent: 'coordinator',
        sender: 'coordinator',
        type: 'post',
        channel: 'main',
        text: '@backend @ui Start verification wave',
        time: '2026-11-22T18:00:00Z',
        ts: 1795370400000,
        mentions: ['backend', 'ui'],
      },
      {
        seq: 2,
        id: 'bmsg-2002',
        agent: 'backend',
        sender: 'backend',
        type: 'decision',
        channel: 'backend',
        text: '@ui Route contract ready on #1',
        time: '2026-11-22T18:01:00Z',
        ts: 1795370460000,
        mentions: ['ui'],
        replyTo: 1,
      },
      {
        seq: 3,
        id: 'bmsg-3003',
        agent: 'ui',
        sender: 'ui',
        type: 'handoff',
        channel: 'main',
        text: '@coordinator Observer tabs ready for check #2',
        time: '2026-11-22T18:02:00Z',
        ts: 1795370520000,
        mentions: ['coordinator'],
        replyTo: 'bmsg-2002',
      },
      {
        seq: 4,
        id: 'bmsg-4004',
        agent: 'narrator',
        sender: 'narrator',
        type: 'post',
        channel: 'main',
        text: 'Progress summary updated',
        time: '2026-11-22T18:03:00Z',
        ts: 1795370580000,
        replyTo: 'bmsg-missing',
      },
    ],
    frames: [
      { path: 'shots/step1.png', seq: 2, caption: 'Initial layout' },
      { path: 'shots/step2.png', seq: 3, caption: 'Verified layout' },
    ],
    systemAttention: [
      {
        id: 'att-1',
        kind: 'info',
        member: 'coordinator',
        conversationId: 'conv-coord-ui',
        text: 'Coordinator awaiting verification result',
      },
    ],
    view: {
      dir: 'C:/Projects/demo',
      json: {
        as_of_seq: 4,
        headline: 'All systems nominal',
        phase: { label: 'Verification', index: 1, total: 1 },
        goal: { title: 'Observer Verification', line: 'All 5 tabs green' },
        threads: [
          {
            id: 'th-1',
            title: 'Core Bridge',
            goal: 'Verify sidecar and UI parity',
            workstreams: ['backend', 'ui'],
            state: 'running',
          },
        ],
        workstreams: [
          { id: 'backend', title: 'Backend', state: 'running', assignees: ['backend'] },
          { id: 'ui', title: 'Observer UI', state: 'done', assignees: ['ui'] },
        ],
      },
    },
    raw: {
      board: JSON.stringify({ swarm: 'ui-verify-swarm', head: 4 }, null, 2),
      meta: JSON.stringify({ title: 'Observer Verification Swarm' }, null, 2),
      members: JSON.stringify([{ name: 'coordinator', role: 'coordinator' }], null, 2),
      view: JSON.stringify({ headline: 'All systems nominal' }, null, 2),
    },
  };
}

function attachPageObservers(page, originPrefix) {
  const consoleErrors = [];
  const pageErrors = [];
  const failedRequests = [];
  const originResponses = [];

  page.on('console', (msg) => {
    if (msg.type() === 'error') {
      consoleErrors.push({ text: msg.text(), url: msg.location()?.url || '' });
    }
  });
  page.on('pageerror', (err) => {
    pageErrors.push(err?.message || String(err));
  });
  page.on('requestfailed', (req) => {
    failedRequests.push({ url: req.url(), reason: req.failure()?.errorText || 'failed' });
  });
  page.on('response', (res) => {
    const url = res.url();
    if (url.startsWith(originPrefix)) {
      originResponses.push({ url, status: res.status() });
    }
    if (res.status() >= 400) {
      failedRequests.push({ url, reason: `HTTP ${res.status()}` });
    }
  });

  return { consoleErrors, pageErrors, failedRequests, originResponses };
}

async function verifyEmptyObserverPage(browser, pageUrl, originPrefix) {
  const context = await browser.newContext({ viewport: { width: 1120, height: 820 } });
  const page = await context.newPage();
  const obs = attachPageObservers(page, originPrefix);

  await page.goto(pageUrl, { waitUntil: 'networkidle' });
  await page.waitForSelector('#empty:not(.hidden)', { timeout: 10000 });

  const domState = await page.evaluate(() => ({
    boardHidden: document.getElementById('board')?.classList.contains('hidden') ?? false,
    loadingHidden: document.getElementById('loading')?.classList.contains('hidden') ?? false,
    emptyTitle: document.getElementById('empty-title')?.textContent?.trim() || '',
    emptyBody: document.getElementById('empty-body')?.textContent?.trim() || '',
    hasSwitcher:
      document.querySelector('#swarm-switcher') !== null ||
      document.querySelector('#swarm-switcher-wrap') !== null ||
      document.querySelector('select#swarm-switcher') !== null ||
      document.querySelector('.swarm-switch') !== null,
  }));

  assert.equal(domState.boardHidden, true, '#board must stay hidden when swarm is null');
  assert.equal(domState.loadingHidden, true, '#loading must hide when #empty renders');
  assert.equal(domState.emptyTitle, 'No swarm in this conversation');
  assert.ok(domState.emptyBody.length > 0, '#empty-body must explain empty state');
  assert.equal(domState.hasSwitcher, false, 'Zero swarm switcher controls allowed in DOM');

  for (const r of obs.originResponses) {
    assert.equal(r.url.includes('swarm='), false, `Unexpected ?swarm= query param in ${r.url}`);
  }

  const loadedPaths = new Set(
    obs.originResponses
      .filter((r) => r.status === 200)
      .map((r) => new URL(r.url).pathname),
  );

  await context.close();
  assert.deepEqual(obs.failedRequests, [], `Failed network requests: ${JSON.stringify(obs.failedRequests)}`);
  const realConsoleErrors = obs.consoleErrors.filter(
    (c) => !c.url.includes('/_sidecar/get-conversation-metadata') && !/Failed to resolve project ID/i.test(c.text),
  );
  assert.deepEqual(realConsoleErrors, [], `Console errors: ${JSON.stringify(realConsoleErrors)}`);
  assert.deepEqual(obs.pageErrors, [], `Page errors: ${JSON.stringify(obs.pageErrors)}`);
  return { loadedPaths };
}

async function verifyObserverPage(browser, pageUrl, originPrefix) {
  const context = await browser.newContext({ viewport: { width: 1120, height: 820 } });
  const page = await context.newPage();
  const obs = attachPageObservers(page, originPrefix);

  await page.goto(pageUrl, { waitUntil: 'networkidle' });

  // Wait for board to become visible
  await page.waitForSelector('#board:not(.hidden)', { timeout: 10000 });
  const titleText = await page.textContent('#swarm-title');
  assert.equal(titleText.trim(), 'Observer Verification Swarm');

  // Verify window.sidecar loaded via ./preload.js
  const hasSidecar = await page.evaluate(() => typeof window.sidecar === 'object' && typeof window.sidecar.fetch === 'function');
  assert.equal(hasSidecar, true, 'window.sidecar must be initialized from ./preload.js');

  // Verify zero swarm-switcher and zero mutation controls anywhere in the DOM
  const forbiddenControls = await page.evaluate(() => {
    const bodyText = document.body.innerText || '';
    const badTextHits = [];
    for (const needle of ['+ Spawn', 'Needs ' + 'CT' + 'O', 'Nudge Coordinator', 'Clean Archive']) {
      if (bodyText.includes(needle)) badTextHits.push(needle);
    }
    const badSelectors = [
      '#swarm-switcher',
      '#swarm-switcher-wrap',
      'select#swarm-switcher',
      '.swarm-switch',
      '#btn-spawn',
      '#board-composer',
      '#spawn-modal',
      '#channel-modal',
      '#swarm-modal',
      '#drawer-backdrop',
      '[data-nudge-' + 'ct' + 'o]',
      'textarea',
    ].filter((sel) => document.querySelector(sel) !== null);
    const allButtons = [...document.querySelectorAll('button')].map((b) => b.textContent.trim());
    const badButtons = allButtons.filter((t) => /^(?:Edit|Digest|Reply|Ack|\+ Spawn|Spawn)$/i.test(t));
    return { badTextHits, badSelectors, badButtons };
  });
  assert.deepEqual(forbiddenControls.badTextHits, []);
  assert.deepEqual(forbiddenControls.badSelectors, []);
  assert.deepEqual(forbiddenControls.badButtons, []);

  // Verify reactive host theme CSS variable sync (light -> dark) via preload.js message
  await page.evaluate(() => {
    window.postMessage(
      {
        type: 'theme-change',
        payload: {
          cssVariables: {
            '--background': '#ffffff',
            '--foreground': '#111111',
            '--card': '#f5f6f8',
            '--primary': '#1a73e8',
          },
        },
      },
      '*',
    );
  });
  await page.waitForFunction(() => document.documentElement.dataset.theme === 'light', null, { timeout: 3000 });
  const lightBg = await page.evaluate(() => getComputedStyle(document.body).backgroundColor);
  assert.equal(lightBg, 'rgb(255, 255, 255)');

  await page.evaluate(() => {
    window.postMessage(
      {
        type: 'theme-change',
        payload: {
          cssVariables: {
            '--background': '#0e0e10',
            '--foreground': '#eceef2',
            '--card': '#16171b',
            '--primary': '#3898ec',
          },
        },
      },
      '*',
    );
  });
  await page.waitForFunction(() => document.documentElement.dataset.theme === 'dark', null, { timeout: 3000 });
  const darkBg = await page.evaluate(() => getComputedStyle(document.body).backgroundColor);
  assert.equal(darkBg, 'rgb(14, 14, 16)');

  // Tab 1: Overview ("Coordinator · Antigravity" — ops-only, zero result images in Overview)
  await page.click('#tabbtn-overview');
  await page.waitForSelector('#tab-overview.active');
  const overviewText = await page.textContent('#tab-overview');
  assert.match(overviewText, /Coordinator · Antigravity/);
  assert.match(overviewText, /Observer Verification/);
  assert.equal(await page.locator('#tab-overview .o2-reel').count(), 0, 'Overview must NOT render .o2-reel result images');
  assert.equal(await page.locator('#tab-overview img').count(), 0, 'Overview must NOT render result images directly');

  // Tab 2: Board (channels, messages, filters, replyTo resolution)
  await page.click('#tabbtn-feed');
  await page.waitForSelector('#tab-feed.active');
  const feedCount = await page.locator('#feed [data-testid="swarm-message"]').count();
  assert.equal(feedCount, 4);
  const feedText = await page.textContent('#feed');
  assert.equal(feedText.includes('#NaN'), false, `Unexpected #NaN in #feed: ${feedText}`);
  assert.equal(feedText.includes('NaN'), false, `Unexpected NaN in #feed: ${feedText}`);

  const replyPillChecks = await page.evaluate(() => {
    const readHeadReply = (seq) => {
      const head = document.querySelector(`#feed article.msg[data-seq="${seq}"] .msg-head`);
      if (!head) return null;
      const btn = head.querySelector('button.seq-ref');
      const note = head.querySelector('.msg-note');
      return {
        btnText: btn ? btn.textContent.trim() : null,
        dataSeqRef: btn ? btn.getAttribute('data-seq-ref') : null,
        noteText: note ? note.textContent.trim() : null,
      };
    };
    return {
      m2: readHeadReply(2),
      m3: readHeadReply(3),
      m4: readHeadReply(4),
    };
  });
  assert.deepEqual(replyPillChecks.m2, { btnText: '↩ #1', dataSeqRef: '1', noteText: null });
  assert.deepEqual(replyPillChecks.m3, { btnText: '↩ #2', dataSeqRef: '2', noteText: null });
  assert.deepEqual(replyPillChecks.m4, { btnText: null, dataSeqRef: null, noteText: '↩ reply' });

  const chanTabsText = await page.textContent('#channel-tabs');
  assert.match(chanTabsText, /#main/);
  assert.match(chanTabsText, /#backend/);

  // Tab 3: Members (shows "Coordinator · Antigravity" + nested read-only Tier-3 Subagents)
  await page.click('#tabbtn-members');
  await page.waitForSelector('#tab-members.active');
  const membersText = await page.textContent('#members');
  assert.match(membersText, /Coordinator · Antigravity/);
  assert.match(membersText, /Subagents · 2/);
  assert.match(membersText, /schema-checker/);
  assert.match(membersText, /frame-auditor/);
  const subRowCount = await page.locator('#members [data-testid="swarm-subagent-row"]').count();
  assert.equal(subRowCount, 2);

  // Tab 4: Graph (Hive view + Mentions SVG view)
  await page.click('#tabbtn-graph');
  await page.waitForSelector('#tab-graph.active');
  const hiveContent = await page.locator('#hive .o2').count();
  assert.ok(hiveContent > 0, 'Hive view should render in Graph tab');
  await page.click('#gview-mentions');
  await page.waitForFunction(() => document.querySelectorAll('#graph .node').length >= 3, null, { timeout: 3000 });

  // Tab 5: Raw (board.json, meta.json, members.json, view.json via #raw-btn-view even without view.exists)
  await page.click('#tabbtn-raw');
  await page.waitForSelector('#tab-raw.active');
  const rawText = await page.textContent('#raw');
  assert.match(rawText, /"ui-verify-swarm"/);
  const viewBtnVisible = await page.evaluate(() => {
    const btn = document.getElementById('raw-btn-view');
    return Boolean(btn && !btn.classList.contains('hidden'));
  });
  assert.equal(viewBtnVisible, true, '#raw-btn-view must be visible when raw.view is present without view.exists');
  await page.click('#raw-btn-view');
  const rawViewText = await page.textContent('#raw');
  assert.match(rawViewText, /"All systems nominal"/);

  for (const r of obs.originResponses) {
    assert.equal(r.url.includes('swarm='), false, `Unexpected ?swarm= query param in ${r.url}`);
  }

  const loadedPaths = new Set(
    obs.originResponses
      .filter((r) => r.status === 200)
      .map((r) => new URL(r.url).pathname),
  );

  await context.close();

  assert.deepEqual(obs.failedRequests, [], `Failed network requests observed: ${JSON.stringify(obs.failedRequests)}`);
  assert.deepEqual(obs.consoleErrors, [], `Console errors observed: ${JSON.stringify(obs.consoleErrors)}`);
  assert.deepEqual(obs.pageErrors, [], `Page errors observed: ${JSON.stringify(obs.pageErrors)}`);

  return { loadedPaths };
}

test('board_ui HTML, CSS, and JS contain zero swarm-switcher or multi-swarm artifacts', () => {
  for (const fileName of ['index.html', 'styles.css', 'app.js', 'hive.js']) {
    const text = readFileSync(join(BOARD_UI_DIR, fileName), 'utf-8');
    assert.doesNotMatch(text, /#swarm-switcher/i, `#swarm-switcher found in ${fileName}`);
    assert.doesNotMatch(text, /swarm-switcher/i, `swarm-switcher found in ${fileName}`);
    assert.doesNotMatch(text, /swarmSwitcher/, `swarmSwitcher found in ${fileName}`);
    assert.doesNotMatch(text, /selectedSwarm/, `selectedSwarm found in ${fileName}`);
    assert.doesNotMatch(text, /renderSwarmSwitcher/, `renderSwarmSwitcher found in ${fileName}`);
    assert.doesNotMatch(text, /state\.swarms/, `state.swarms found in ${fileName}`);
    assert.doesNotMatch(text, /\?swarm=/, `?swarm= found in ${fileName}`);
    assert.doesNotMatch(text, /\.swarm-switch\b/, `.swarm-switch found in ${fileName}`);
  }
});

test('Observer UI renders clean empty state when swarm is null at / and subpath proxy', async (t) => {
  const pw = await loadPlaywright();
  const executablePath = findBrowserExecutable();
  const browser = await pw.chromium.launch({
    headless: true,
    args: ['--no-proxy-server', '--proxy-bypass-list=<-loopback>'],
    ...(executablePath ? { executablePath } : {}),
  });
  t.after(() => browser.close());

  const tempState = mkdtempSync(join(tmpdir(), 'hermes-observer-empty-'));
  t.after(() => rmSync(tempState, { recursive: true, force: true }));

  const upstreamServer = createServer((req, res) => {
    const reqUrl = new URL(req.url, 'http://127.0.0.1');
    if (reqUrl.pathname === '/board') {
      res.writeHead(200, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify({ conversationId: 'conv-empty-ui', swarm: null, member: null }));
      return;
    }
    res.writeHead(404);
    res.end('Not found');
  });

  const upstreamPort = await listenServer(upstreamServer);
  t.after(() => closeServer(upstreamServer));

  const sidecarProc = await spawnSidecarProcess({
    HERMES_A2A_STATE_DIR: tempState,
    HERMES_A2A_BOARD_URL: `http://127.0.0.1:${upstreamPort}`,
    ANTIGRAVITY_CONVERSATION_ID: 'conv-empty-ui',
    HERMES_SWARM_DISABLE_WAKEUP: '1',
  });
  t.after(() => sidecarProc.stop());

  const SUBPATH_PREFIX = '/remote-proxy/plugin/hermes-swarm/swarm_board';
  const proxy = createSubpathProxy(sidecarProc.port, SUBPATH_PREFIX);
  const proxyPort = await listenServer(proxy.server);
  t.after(() => closeServer(proxy.server));

  const rootEmpty = await verifyEmptyObserverPage(
    browser,
    `${sidecarProc.baseUrl}/`,
    sidecarProc.baseUrl,
  );
  assert.equal(rootEmpty.loadedPaths.has('/api/swarm'), true);

  const subEmpty = await verifyEmptyObserverPage(
    browser,
    `http://127.0.0.1:${proxyPort}${SUBPATH_PREFIX}/`,
    `http://127.0.0.1:${proxyPort}`,
  );
  assert.deepEqual(proxy.proxyLeaks, [], `Subpath proxy caught root-escaping requests: ${JSON.stringify(proxy.proxyLeaks)}`);
  assert.equal(subEmpty.loadedPaths.has(`${SUBPATH_PREFIX}/api/swarm`), true);
});

test('Observer UI passes headless Chromium verification at root / and behind Remote Control subpath proxy', async (t) => {
  const pw = await loadPlaywright();
  const executablePath = findBrowserExecutable();
  const browser = await pw.chromium.launch({
    headless: true,
    args: ['--no-proxy-server', '--proxy-bypass-list=<-loopback>'],
    ...(executablePath ? { executablePath } : {}),
  });
  t.after(() => browser.close());

  const tempState = mkdtempSync(join(tmpdir(), 'hermes-observer-ui-'));
  t.after(() => rmSync(tempState, { recursive: true, force: true }));

  const snapshot = buildRealisticV2Snapshot();
  assert.equal('exists' in snapshot.view, false, 'Snapshot deliberately omits view.exists');

  const upstreamServer = createServer((req, res) => {
    const reqUrl = new URL(req.url, 'http://127.0.0.1');
    if (reqUrl.pathname === '/board') {
      const etag = reqUrl.searchParams.get('etag');
      if (etag && etag === snapshot.etag) {
        res.writeHead(200, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({
          unchanged: true,
          swarm: snapshot.swarm,
          etag: snapshot.etag,
          primaryConversationId: snapshot.primaryConversationId,
        }));
        return;
      }
      res.writeHead(200, { 'Content-Type': 'application/json', ETag: snapshot.etag });
      res.end(JSON.stringify(snapshot));
      return;
    }
    if (reqUrl.pathname === '/board/frame') {
      res.writeHead(200, { 'Content-Type': 'image/png' });
      res.end(PNG_1X1);
      return;
    }
    res.writeHead(404);
    res.end('Not found');
  });

  const upstreamPort = await listenServer(upstreamServer);
  t.after(() => closeServer(upstreamServer));

  writeFileSync(
    join(tempState, 'registry.json'),
    JSON.stringify({
      servers: {
        'ui-verify-swarm': {
          pid: process.pid,
          httpUrl: `http://127.0.0.1:${upstreamPort}`,
          swarmId: 'ui-verify-swarm',
          primaryConversationId: 'conv-primary-ui',
          coordinatorConversationId: 'conv-coord-ui',
        },
      },
    }),
    'utf-8',
  );

  const sidecarProc = await spawnSidecarProcess({
    HERMES_A2A_STATE_DIR: tempState,
    ANTIGRAVITY_CONVERSATION_ID: 'conv-primary-ui',
    HERMES_SWARM_DISABLE_WAKEUP: '1',
  });
  t.after(() => sidecarProc.stop());

  const SUBPATH_PREFIX = '/remote-proxy/plugin/hermes-swarm/swarm_board';
  const proxy = createSubpathProxy(sidecarProc.port, SUBPATH_PREFIX);
  const proxyPort = await listenServer(proxy.server);
  t.after(() => closeServer(proxy.server));

  // 1. Verify at root /
  const rootResult = await verifyObserverPage(
    browser,
    `${sidecarProc.baseUrl}/`,
    sidecarProc.baseUrl,
  );
  for (const p of [
    '/',
    '/preload.js',
    '/styles.css',
    '/hive.css',
    '/gif.js',
    '/hive.js',
    '/app.js',
    '/api/swarm',
  ]) {
    assert.equal(rootResult.loadedPaths.has(p), true, `Missing root asset/API load: ${p}`);
  }
  assert.equal(rootResult.loadedPaths.has('/api/frame'), false, 'Overview must NOT auto-load /api/frame result images');

  // 2. Verify behind Remote Control subpath proxy
  const subpathBase = `http://127.0.0.1:${proxyPort}${SUBPATH_PREFIX}`;
  const subpathResult = await verifyObserverPage(
    browser,
    `${subpathBase}/`,
    `http://127.0.0.1:${proxyPort}`,
  );
  assert.deepEqual(proxy.proxyLeaks, [], `Subpath proxy caught root-escaping requests: ${JSON.stringify(proxy.proxyLeaks)}`);
  for (const p of [
    `${SUBPATH_PREFIX}/`,
    `${SUBPATH_PREFIX}/preload.js`,
    `${SUBPATH_PREFIX}/styles.css`,
    `${SUBPATH_PREFIX}/hive.css`,
    `${SUBPATH_PREFIX}/gif.js`,
    `${SUBPATH_PREFIX}/hive.js`,
    `${SUBPATH_PREFIX}/app.js`,
    `${SUBPATH_PREFIX}/api/swarm`,
  ]) {
    assert.equal(subpathResult.loadedPaths.has(p), true, `Missing subpath asset/API load: ${p}`);
  }
  assert.equal(subpathResult.loadedPaths.has(`${SUBPATH_PREFIX}/api/frame`), false, 'Overview must NOT auto-load /api/frame behind subpath proxy');
});

function build48c27eefSnapshot({ withViewHtml = false } = {}) {
  const baseTs = 1795370400000;
  const msgs = [];
  for (let i = 1; i <= 18; i++) {
    const channel = i % 2 === 0 ? 'impl' : 'main';
    const sender = i === 1 ? 'coordinator' : ['coder', 'researcher', 'verifier', 'coordinator', 'narrator', 'monitor'][i % 6];
    const mentions = sender === 'coordinator' ? ['coder', 'researcher'] : ['coordinator'];
    msgs.push({
      seq: i,
      id: `msg-${i}`,
      agent: sender,
      sender,
      senderRole: sender === 'coordinator' ? 'coordinator' : (sender === 'narrator' || sender === 'monitor' ? sender : 'worker'),
      type: i === 18 ? 'handoff' : (i % 4 === 0 ? 'decision' : 'post'),
      kind: i === 18 ? 'handoff' : (i % 4 === 0 ? 'decision' : 'post'),
      channel,
      text: i === 1
        ? '@coder @researcher @verifier Implement multiply(a, b), inspect README.md/notes.txt, and verify unittest suite.'
        : i === 18
          ? '@coordinator [done] Verified multiply(a, b) and all 10 unit tests pass from repo root.'
          : `@${mentions[0]} Progress update #${i}: checking edge cases (zero, negatives, NaN/float precision, CRLF/LF sha256 parity) in sandbox-repo.`,
      summary: `Progress update #${i}`,
      mentions,
      all: false,
      time: new Date(baseTs + i * 30000).toISOString(),
      ts: baseTs + i * 30000,
    });
  }

  return {
    swarm: '48c27eef-de1c-4f8e-9f2b-bbba7681316e',
    id: '48c27eef-de1c-4f8e-9f2b-bbba7681316e',
    etag: withViewHtml ? 'etag-48c27eef-html' : 'etag-48c27eef-plain',
    head: 18,
    title: 'Sandbox Swarm E2E Verification Round',
    dir: 'C:/Projects/sandbox-repo',
    primaryConversationId: 'conv-primary-48c27eef',
    coordinatorConversationId: 'conv-coord-48c27eef',
    meta: {
      swarmId: '48c27eef-de1c-4f8e-9f2b-bbba7681316e',
      title: 'Sandbox Swarm E2E Verification Round',
      goal: 'Implement multiply, verify tests, validate live swarm board.',
      workspace: 'C:/Projects/sandbox-repo',
      primaryConversationId: 'conv-primary-48c27eef',
      coordinatorConversationId: 'conv-coord-48c27eef',
    },
    plan: {
      goal: {
        title: 'Sandbox Swarm E2E Verification Round',
        line: 'Implement multiply, verify tests, validate live swarm board.',
      },
      summary: 'Implement multiply, verify tests, validate live swarm board.',
      threads: [
        {
          id: 't-impl',
          title: 'Multiply implementation and verification',
          goal: 'Implement multiply, inspect docs, pass unittest suite.',
          now: 'Coding and testing multiply(a, b)',
          state: 'running',
          tasks: ['coder', 'researcher', 'verifier'],
        },
        {
          id: 't-obs',
          title: 'Swarm board overview and monitoring',
          goal: 'Maintain Overview view.json and stall checks.',
          now: 'Narrator and monitor active',
          state: 'running',
          tasks: ['narrator', 'monitor'],
        },
      ],
      tasks: {
        coder: {
          title: 'Implement multiply(a, b) in app.py',
          status: 'active',
          note: 'Writing multiply() and edge-case tests',
          assignee: 'coder',
        },
        researcher: {
          title: 'Inspect README.md and notes.txt constraints',
          status: 'active',
          note: 'Checking repo baseline files and LF sha256',
          assignee: 'researcher',
        },
        verifier: {
          title: 'Run unittest suite and verify parity',
          status: 'done',
          note: 'All 10 tests OK from repo root',
          assignee: 'verifier',
        },
        narrator: {
          title: 'Maintain Overview view.json summary',
          status: 'active',
          note: 'Updating view.json for head #18',
          assignee: 'narrator',
        },
        monitor: {
          title: 'Watch board for stalled workers',
          status: 'active',
          note: 'Monitoring active workers',
          assignee: 'monitor',
        },
      },
    },
    channels: [
      { name: 'main', topic: 'Swarm main channel' },
      { name: 'impl', topic: 'Implementation channel', created_by: 'coordinator' },
    ],
    members: [
      {
        name: 'coordinator',
        role: 'coordinator',
        status: 'working',
        conversationId: 'conv-coord-48c27eef',
        steps: 6,
        posts: 3,
        now: 'Coordinating implementation and verification threads',
        subagents: [],
      },
      {
        name: 'coder',
        role: 'worker',
        status: 'working',
        model: 'gpt-5.4-codex',
        steps: 12,
        posts: 4,
        assignment: 'Implement multiply(a, b) in app.py',
        now: 'Adding multiply(a, b) and validating edge cases',
        channels: ['main', 'impl'],
        subagents: [
          {
            id: 'call_4d6838c96eca4462bc37e156-0',
            name: 'edge-case-reviewer-extremely-long-identifier-check',
            task: 'Review edge cases for a `multiply(a, b)` Python function to be added to app.py (zero, negatives, NaN, inf, bool coercion, very large ints, complex numbers, and CRLF/LF parity across worktrees)',
            status: 'done',
            model: 'deepseek-v4.1-flash',
            steps: 4,
            summary: 'Recommended plain a * b implementation',
          },
        ],
      },
      {
        name: 'researcher',
        role: 'researcher',
        status: 'working',
        model: 'claude-sonnet-4.6',
        steps: 8,
        posts: 3,
        assignment: 'Inspect README.md and notes.txt constraints',
        now: 'Verifying repo baseline files and SHA-256 fingerprints',
        channels: ['main', 'impl'],
        subagents: [
          {
            id: 'call_08ce62cfba924cd1ab5df176-0',
            name: 'Read the two files README.md and notes.txt at /tmp/sandbox-repo',
            task: 'Read the two files README.md and notes.txt at /tmp/sandbox-repo and extract every constraint verbatim',
            status: 'done',
            model: 'deepseek-v4.1-flash',
            steps: 3,
            summary: 'No extra constraints found',
          },
        ],
      },
      {
        name: 'verifier',
        role: 'verifier',
        status: 'waiting',
        model: 'claude-sonnet-4.6',
        steps: 9,
        posts: 4,
        assignment: 'Run unittest suite and verify parity',
        now: '[done] 10 tests pass',
        channels: ['main', 'impl'],
        subagents: [],
      },
      {
        name: 'narrator',
        role: 'narrator',
        roleWorker: true,
        narrator: true,
        status: 'working',
        model: 'gemini-2.5-flash',
        steps: 5,
        posts: 2,
        assignment: 'Maintain Overview view.json summary',
        now: 'Updating view.json with latest thread progress',
        channels: ['*'],
        subagents: [],
      },
      {
        name: 'monitor',
        role: 'monitor',
        roleWorker: true,
        narrator: false,
        status: 'working',
        model: 'gemini-2.5-flash',
        steps: 4,
        posts: 2,
        assignment: 'Watch board for stalled workers',
        now: 'Watching all active workers',
        channels: ['*'],
        subagents: [],
      },
    ],
    messages: msgs,
    frames: [
      { path: 'shots/step1.png', seq: 2, caption: 'Initial implementation' },
      { path: 'shots/step2.png', seq: 18, caption: 'Final verified suite' },
    ],
    narrator: {
      name: 'narrator',
      role: 'narrator',
      status: 'working',
      lastReadSeq: 18,
    },
    narratorState: 'working',
    view: {
      exists: true,
      dir: 'C:/Projects/sandbox-repo',
      json: {
        as_of_seq: 18,
        headline: 'Sandbox Swarm E2E Verification Round',
        phase: { label: 'Verification', index: 1, total: 1 },
        goal: {
          title: 'Sandbox Swarm E2E Verification Round',
          line: 'Implement multiply, verify tests, validate live swarm board.',
        },
        threads: [
          {
            id: 'board-validation',
            title: 'Live proof the 1:1 session-bound swarm works',
            outcome: 'done',
            refs: ['#1', '#18'],
            why: 'Whole round traced',
          },
        ],
      },
      ...(withViewHtml
        ? {
            html: '<!DOCTYPE html><html><body><div id="custom-narrator-page"><h1>Narrator Custom Overview</h1><p>Custom view.html rendered.</p></div></body></html>',
            htmlKey: '48c27eef-html-v1',
          }
        : {}),
    },
    raw: {
      board: JSON.stringify({ swarm: '48c27eef-de1c-4f8e-9f2b-bbba7681316e', head: 18 }, null, 2),
      meta: JSON.stringify({ title: 'Sandbox Swarm E2E Verification Round' }, null, 2),
      members: JSON.stringify([{ name: 'coordinator', role: 'coordinator' }], null, 2),
      view: JSON.stringify({ headline: 'Sandbox Swarm E2E Verification Round' }, null, 2),
    },
  };
}

function parseRgb(colorStr) {
  const m = /rgba?\(\s*(\d+),\s*(\d+),\s*(\d+)/i.exec(String(colorStr || ''));
  if (!m) return null;
  return [Number(m[1]), Number(m[2]), Number(m[3])];
}

function relLuminance(rgb) {
  if (!rgb) return NaN;
  const [r, g, b] = rgb.map((v) => v / 255);
  return 0.2126 * r + 0.7152 * g + 0.0722 * b;
}

test('Observer UI verifies 48c27eef regression fixes: dark/light themes, Board scroll non-overlap, Members 6 + role workers, Tier-3 overflow, Overview threads/tasks & screenshots', async (t) => {
  const pw = await loadPlaywright();
  const executablePath = findBrowserExecutable();
  const browser = await pw.chromium.launch({
    headless: true,
    args: ['--no-proxy-server', '--proxy-bypass-list=<-loopback>'],
    ...(executablePath ? { executablePath } : {}),
  });
  t.after(() => browser.close());

  const tempState = mkdtempSync(join(tmpdir(), 'hermes-observer-48c27eef-'));
  t.after(() => rmSync(tempState, { recursive: true, force: true }));

  let currentSnapshot = build48c27eefSnapshot({ withViewHtml: false });

  const upstreamServer = createServer((req, res) => {
    const reqUrl = new URL(req.url, 'http://127.0.0.1');
    if (reqUrl.pathname === '/board') {
      const etag = reqUrl.searchParams.get('etag');
      if (etag && etag === currentSnapshot.etag) {
        res.writeHead(200, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({
          unchanged: true,
          swarm: currentSnapshot.swarm,
          etag: currentSnapshot.etag,
          primaryConversationId: currentSnapshot.primaryConversationId,
        }));
        return;
      }
      res.writeHead(200, { 'Content-Type': 'application/json', ETag: currentSnapshot.etag });
      res.end(JSON.stringify(currentSnapshot));
      return;
    }
    if (reqUrl.pathname === '/board/frame') {
      res.writeHead(200, { 'Content-Type': 'image/png' });
      res.end(PNG_1X1);
      return;
    }
    res.writeHead(404);
    res.end('Not found');
  });

  const upstreamPort = await listenServer(upstreamServer);
  t.after(() => closeServer(upstreamServer));

  writeFileSync(
    join(tempState, 'registry.json'),
    JSON.stringify({
      servers: {
        '48c27eef-de1c-4f8e-9f2b-bbba7681316e': {
          pid: process.pid,
          httpUrl: `http://127.0.0.1:${upstreamPort}`,
          swarmId: '48c27eef-de1c-4f8e-9f2b-bbba7681316e',
          primaryConversationId: 'conv-primary-48c27eef',
          coordinatorConversationId: 'conv-coord-48c27eef',
        },
      },
    }),
    'utf-8',
  );

  const sidecarProc = await spawnSidecarProcess({
    HERMES_A2A_STATE_DIR: tempState,
    ANTIGRAVITY_CONVERSATION_ID: 'conv-primary-48c27eef',
    HERMES_SWARM_DISABLE_WAKEUP: '1',
    SWARM_OVERVIEW: 'false',
  });
  t.after(() => sidecarProc.stop());

  const SUBPATH_PREFIX = '/remote-proxy/plugin/hermes-swarm/swarm_board';
  const proxy = createSubpathProxy(sidecarProc.port, SUBPATH_PREFIX);
  const proxyPort = await listenServer(proxy.server);
  t.after(() => closeServer(proxy.server));

  const screenshotDir = process.env.VERIFY_SCREENSHOT_DIR
    || join(homedir(), '.gemini', 'antigravity', 'brain', '2cb6b9ce-2fd7-45f3-ba7a-7955066ece6e', 'scratch', 'verify_ui_screenshots');
  mkdirSync(screenshotDir, { recursive: true });

  async function verify48c27eefAtUrl(pageUrl, originPrefix, saveShots) {
    const context = await browser.newContext({ viewport: { width: 1280, height: 860 }, colorScheme: 'dark' });
    const page = await context.newPage();
    const obs = attachPageObservers(page, originPrefix);

    await page.goto(pageUrl, { waitUntil: 'networkidle' });
    await page.waitForSelector('#board:not(.hidden)', { timeout: 10000 });
    await page.click('#tabbtn-overview');
    await page.waitForSelector('#tab-overview.active', { timeout: 5000 });

    // 1. Default standalone DARK theme polarity & contrast on Overview
    const darkOverviewStyles = await page.evaluate(() => {
      const cs = (sel) => {
        const el = document.querySelector(sel);
        if (!el) return null;
        const s = getComputedStyle(el);
        return { bg: s.backgroundColor, fg: s.color };
      };
      return {
        theme: document.documentElement.dataset.theme,
        html: cs('html'),
        body: cs('body'),
        header: cs('.header'),
        title: cs('#swarm-title'),
        lead: cs('.o2-lead'),
      };
    });
    assert.equal(darkOverviewStyles.theme, 'dark');
    assert.equal(darkOverviewStyles.body.bg, 'rgb(14, 14, 16)');
    assert.ok(relLuminance(parseRgb(darkOverviewStyles.html.bg)) < 0.1, 'dark html bg');
    assert.ok(relLuminance(parseRgb(darkOverviewStyles.header.bg)) < 0.1, 'dark header bg');
    assert.ok(relLuminance(parseRgb(darkOverviewStyles.lead.bg)) < 0.15, 'dark .o2-lead bg');
    assert.ok(relLuminance(parseRgb(darkOverviewStyles.title.fg)) > 0.7, 'dark #swarm-title fg must be high contrast');

    // 2. Real Overview threads & tasks (t-impl, t-obs, X of Y tasks done)
    const overviewText = await page.textContent('#tab-overview');
    assert.match(overviewText, /Multiply implementation and verification/);
    assert.match(overviewText, /Swarm board overview and monitoring/);
    assert.match(overviewText, /1 of 5\s*tasks done/);

    // Expand both thread cards and verify real task titles render
    await page.click('[data-o2t="t-impl"]');
    await page.click('[data-o2t="t-obs"]');
    const expandedOverviewText = await page.textContent('#tab-overview');
    assert.match(expandedOverviewText, /Implement multiply in app\.py/);
    assert.match(expandedOverviewText, /Inspect README\.md and notes\.txt constraints/);
    assert.match(expandedOverviewText, /Run unittest suite and verify parity/);
    assert.match(expandedOverviewText, /Maintain Overview view\.json summary/);
    assert.match(expandedOverviewText, /Watch board for stalled workers/);

    if (saveShots) {
      await page.screenshot({ path: join(screenshotDir, 'dark_overview.png'), fullPage: true });
    }

    // 3. Accurate Members count (4 non-role members) & Role Workers (narrator, monitor) + Tier-3 zero overflow at 1280px
    assert.equal((await page.textContent('#tab-count-members')).trim(), '4');
    await page.click('#tabbtn-members');
    await page.waitForSelector('#tab-members.active');
    const membersText = await page.textContent('#members');
    assert.match(membersText, /Role workers/);
    assert.match(membersText, /@narrator/);
    assert.match(membersText, /@monitor/);
    assert.match(membersText, /@coder/);
    assert.match(membersText, /@researcher/);
    assert.match(membersText, /@verifier/);

    const membersDarkCheck = await page.evaluate(() => {
      const memEl = document.getElementById('members');
      const roleBadges = [...document.querySelectorAll('[data-testid="swarm-member-role"]')].map((el) => ({
        text: el.textContent.trim(),
        visible: el.offsetWidth > 0 && el.offsetHeight > 0,
      }));
      const subRows = [...document.querySelectorAll('[data-testid="swarm-subagent-row"]')];
      const maxSubRight = Math.max(0, ...subRows.map((r) => r.getBoundingClientRect().right));
      const minSubLeft = Math.min(Infinity, ...subRows.map((r) => r.getBoundingClientRect().left));
      return {
        bodyBg: getComputedStyle(document.body).backgroundColor,
        memFg: getComputedStyle(memEl).color,
        roleBadges,
        scrollWidth: document.documentElement.scrollWidth,
        innerWidth: window.innerWidth,
        subCount: subRows.length,
        maxSubRight,
        minSubLeft,
      };
    });
    assert.equal(membersDarkCheck.bodyBg, 'rgb(14, 14, 16)');
    assert.ok(relLuminance(parseRgb(membersDarkCheck.memFg)) > 0.7);
    assert.ok(membersDarkCheck.roleBadges.some((b) => b.text === 'narrator' && b.visible));
    assert.ok(membersDarkCheck.roleBadges.some((b) => b.text === 'monitor' && b.visible));
    assert.equal(membersDarkCheck.subCount, 2);
    assert.ok(membersDarkCheck.scrollWidth <= membersDarkCheck.innerWidth, `1280px horizontal overflow: ${membersDarkCheck.scrollWidth} > ${membersDarkCheck.innerWidth}`);
    assert.ok(membersDarkCheck.maxSubRight <= membersDarkCheck.innerWidth, `1280px sub-row overflow: ${membersDarkCheck.maxSubRight} > ${membersDarkCheck.innerWidth}`);

    // Narrow sidecar width (360px) Tier-3 subagent zero-overflow check + screenshot
    await page.setViewportSize({ width: 360, height: 780 });
    await page.waitForTimeout(100);
    const narrowCheck = await page.evaluate(() => {
      const subRows = [...document.querySelectorAll('[data-testid="swarm-subagent-row"]')];
      return {
        scrollWidth: document.documentElement.scrollWidth,
        innerWidth: window.innerWidth,
        maxSubRight: Math.max(0, ...subRows.map((r) => r.getBoundingClientRect().right)),
        subContainersOk: [...document.querySelectorAll('.o2-subagents')].every((el) => el.scrollWidth <= el.clientWidth + 1),
      };
    });
    assert.ok(narrowCheck.scrollWidth <= narrowCheck.innerWidth, `360px horizontal overflow: ${narrowCheck.scrollWidth} > ${narrowCheck.innerWidth}`);
    assert.ok(narrowCheck.maxSubRight <= narrowCheck.innerWidth, `360px sub-row overflow: ${narrowCheck.maxSubRight} > ${narrowCheck.innerWidth}`);
    assert.equal(narrowCheck.subContainersOk, true, '360px .o2-subagents container overflowed');
    if (saveShots) {
      await page.screenshot({ path: join(screenshotDir, 'dark_members_narrow.png'), fullPage: true });
    }

    // Restore 1280px & verify Board scroll header non-overlap
    await page.setViewportSize({ width: 1280, height: 860 });
    await page.click('#tabbtn-feed');
    await page.waitForSelector('#tab-feed.active');
    const feedCount = await page.locator('#feed [data-testid="swarm-message"]').count();
    assert.equal(feedCount, 18);
    const feed48Text = await page.textContent('#feed');
    assert.equal(feed48Text.includes('#NaN'), false, 'Expected zero #NaN in 48c27eef #feed');

    await page.evaluate(() => {
      const f = document.getElementById('feed');
      f.scrollTop = 400;
      f.dispatchEvent(new Event('scroll'));
    });
    await page.waitForTimeout(100);
    const scrollCheck = await page.evaluate(() => {
      const header = document.querySelector('.header');
      const toolbar = document.querySelector('#tab-feed .toolbar');
      const feed = document.getElementById('feed');
      const msgs = [...document.querySelectorAll('#feed [data-testid="swarm-message"]')];
      const hRect = header.getBoundingClientRect();
      const tRect = toolbar.getBoundingClientRect();
      const fRect = feed.getBoundingClientRect();
      const firstVisible = msgs.find((m) => m.getBoundingClientRect().bottom > fRect.top);
      return {
        windowScrollY: window.scrollY,
        feedScrollTop: feed.scrollTop,
        headerTop: hRect.top,
        headerBottom: hRect.bottom,
        toolbarTop: tRect.top,
        toolbarBottom: tRect.bottom,
        toolbarStuck: toolbar.classList.contains('stuck'),
        feedTop: fRect.top,
        firstVisibleTop: firstVisible ? firstVisible.getBoundingClientRect().top : null,
        feedBg: getComputedStyle(feed).backgroundColor,
        bodyBg: getComputedStyle(document.body).backgroundColor,
      };
    });
    assert.ok(scrollCheck.feedScrollTop > 0, `Expected #feed to scroll (feedScrollTop=${scrollCheck.feedScrollTop})`);
    assert.equal(scrollCheck.headerTop, 0, `Expected .header pinned at top (headerTop=${scrollCheck.headerTop})`);
    assert.ok(scrollCheck.headerBottom <= scrollCheck.toolbarTop + 0.5, `Header overlaps .toolbar: ${scrollCheck.headerBottom} > ${scrollCheck.toolbarTop}`);
    assert.ok(scrollCheck.headerBottom <= scrollCheck.feedTop, `Header overlaps #feed: ${scrollCheck.headerBottom} > ${scrollCheck.feedTop}`);
    assert.ok(scrollCheck.toolbarBottom <= scrollCheck.feedTop + 0.5, `Toolbar overlaps #feed: ${scrollCheck.toolbarBottom} > ${scrollCheck.feedTop}`);
    assert.equal(scrollCheck.toolbarStuck, true, 'Expected .toolbar.stuck when #feed.scrollTop > 0');
    if (scrollCheck.firstVisibleTop !== null) {
      assert.ok(scrollCheck.firstVisibleTop >= scrollCheck.headerBottom, `Header overlaps .msg: ${scrollCheck.firstVisibleTop} < ${scrollCheck.headerBottom}`);
    }
    if (saveShots) {
      await page.screenshot({ path: join(screenshotDir, 'dark_board_scrolled.png'), fullPage: true });
    }

    // Verify Graph + Raw tabs in dark mode
    await page.click('#tabbtn-graph');
    await page.waitForSelector('#tab-graph.active');
    assert.ok((await page.locator('#hive .o2').count()) > 0);
    await page.click('#tabbtn-raw');
    await page.waitForSelector('#tab-raw.active');
    assert.match(await page.textContent('#raw'), /48c27eef-de1c-4f8e-9f2b-bbba7681316e/);

    // 4. Switch to LIGHT theme via theme-change and verify cohesive light polarity on Overview & Members
    await page.evaluate(() => {
      window.postMessage(
        {
          type: 'theme-change',
          payload: {
            cssVariables: {
              '--background': '#ffffff',
              '--foreground': '#111111',
              '--card': '#f5f6f8',
              '--primary': '#1a73e8',
            },
          },
        },
        '*',
      );
    });
    await page.waitForFunction(() => document.documentElement.dataset.theme === 'light', null, { timeout: 3000 });

    await page.click('#tabbtn-overview');
    await page.waitForSelector('#tab-overview.active');
    await page.waitForFunction(() => getComputedStyle(document.getElementById('tab-overview')).opacity === '1', null, { timeout: 2000 });
    const lightOverviewStyles = await page.evaluate(() => {
      const cs = (sel) => {
        const el = document.querySelector(sel);
        if (!el) return null;
        const s = getComputedStyle(el);
        return { bg: s.backgroundColor, fg: s.color };
      };
      return {
        html: cs('html'),
        body: cs('body'),
        header: cs('.header'),
        title: cs('#swarm-title'),
        lead: cs('.o2-lead'),
      };
    });
    assert.equal(lightOverviewStyles.body.bg, 'rgb(255, 255, 255)');
    assert.ok(relLuminance(parseRgb(lightOverviewStyles.html.bg)) > 0.85, 'light html bg');
    assert.ok(relLuminance(parseRgb(lightOverviewStyles.header.bg)) > 0.85, 'light header bg');
    assert.ok(relLuminance(parseRgb(lightOverviewStyles.lead.bg)) > 0.85, 'light .o2-lead bg must NOT stay dark');
    assert.ok(relLuminance(parseRgb(lightOverviewStyles.title.fg)) < 0.25, 'light #swarm-title fg must be dark');
    if (saveShots) {
      await page.screenshot({ path: join(screenshotDir, 'light_overview.png'), fullPage: true });
    }

    await page.click('#tabbtn-members');
    await page.waitForSelector('#tab-members.active');
    await page.waitForFunction(() => getComputedStyle(document.getElementById('tab-members')).opacity === '1', null, { timeout: 2000 });
    const lightMembersStyles = await page.evaluate(() => ({
      bodyBg: getComputedStyle(document.body).backgroundColor,
      memFg: getComputedStyle(document.getElementById('members')).color,
      rowAuthFg: getComputedStyle(document.querySelector('.o2-mauth')).color,
    }));
    assert.equal(lightMembersStyles.bodyBg, 'rgb(255, 255, 255)');
    assert.ok(relLuminance(parseRgb(lightMembersStyles.memFg)) < 0.25, 'light #members text must be dark');
    assert.ok(relLuminance(parseRgb(lightMembersStyles.rowAuthFg)) < 0.25, 'light member author text must be dark');
    if (saveShots) {
      await page.screenshot({ path: join(screenshotDir, 'light_members.png'), fullPage: true });
    }

    assert.equal(await page.locator('#swarm-switcher').count(), 0);
    await context.close();
    const realFailedRequests = obs.failedRequests.filter(
      (r) => !(r.reason === 'net::ERR_ABORTED' && /fonts\.g(?:static|oogleapis)\.com/.test(r.url)),
    );
    assert.deepEqual(realFailedRequests, [], `Failed network requests: ${JSON.stringify(realFailedRequests)}`);
    const realConsoleErrors = obs.consoleErrors.filter(
    (c) => !c.url.includes('/_sidecar/get-conversation-metadata') && !/Failed to resolve project ID/i.test(c.text),
  );
  assert.deepEqual(realConsoleErrors, [], `Console errors: ${JSON.stringify(realConsoleErrors)}`);
    assert.deepEqual(obs.pageErrors, [], `Page errors: ${JSON.stringify(obs.pageErrors)}`);
  }

  await verify48c27eefAtUrl(`${sidecarProc.baseUrl}/`, sidecarProc.baseUrl, true);
  await verify48c27eefAtUrl(
    `http://127.0.0.1:${proxyPort}${SUBPATH_PREFIX}/`,
    `http://127.0.0.1:${proxyPort}`,
    false,
  );
  assert.deepEqual(proxy.proxyLeaks, [], `Subpath proxy leaks: ${JSON.stringify(proxy.proxyLeaks)}`);

  // 5. Verify Narrator's page view toggle (#ov-mode) when view.html + view.json are present
  currentSnapshot = build48c27eefSnapshot({ withViewHtml: true });
  const ovCtx = await browser.newContext({ viewport: { width: 1280, height: 860 } });
  const ovPage = await ovCtx.newPage();
  const ovObs = attachPageObservers(ovPage, sidecarProc.baseUrl);
  await ovPage.goto(`${sidecarProc.baseUrl}/`, { waitUntil: 'networkidle' });
  await ovPage.waitForSelector('#tab-overview.active', { timeout: 10000 });
  await ovPage.waitForSelector('#ov-mode:not(.hidden)', { timeout: 5000 });
  assert.equal((await ovPage.textContent('#ov-mode')).trim(), "Narrator's page");
  await ovPage.click('#ov-mode');
  await ovPage.waitForSelector('#ov-frame:not(.hidden) iframe[data-testid="swarm-overview-iframe"]', { timeout: 5000 });
  const narratorText = await ovPage.frameLocator('iframe[data-testid="swarm-overview-iframe"]').locator('#custom-narrator-page').textContent({ timeout: 5000 });
  assert.match(narratorText, /Narrator Custom Overview/);
  assert.equal((await ovPage.textContent('#ov-mode')).trim(), 'Overview');
  assert.equal(
    ovObs.originResponses.some((r) => new URL(r.url).pathname === '/narrator_bootstrap.js' && r.status === 200),
    true,
    'Expected ./narrator_bootstrap.js to return HTTP 200',
  );
  await ovPage.click('#ov-mode');
  await ovPage.waitForSelector('#ov-plain:not(.hidden)', { timeout: 5000 });
  await ovCtx.close();
  assert.deepEqual(ovObs.failedRequests, [], `Narrator's page failed requests: ${JSON.stringify(ovObs.failedRequests)}`);
  assert.deepEqual(ovObs.consoleErrors, [], `Narrator's page console errors: ${JSON.stringify(ovObs.consoleErrors)}`);
  assert.deepEqual(ovObs.pageErrors, [], `Narrator's page errors: ${JSON.stringify(ovObs.pageErrors)}`);
});

function buildAliveTelemetrySnapshot({ active = true, etag = 'alive-etag-1', coderSteps = 18 } = {}) {
  const baseTs = 1795370400000;
  return {
    swarm: 'alive-telemetry-swarm',
    id: 'alive-telemetry-swarm',
    etag,
    head: 6,
    title: 'Alive Telemetry Verification Swarm',
    dir: 'C:/Projects/sandbox-repo',
    primaryConversationId: 'conv-primary-alive',
    coordinatorConversationId: 'conv-coord-alive',
    meta: {
      swarmId: 'alive-telemetry-swarm',
      title: 'Alive Telemetry Verification Swarm',
      goal: 'Verify live telemetry strip, expandable tools, adaptive polling, and Hive role workers.',
      workspace: 'C:/Projects/sandbox-repo',
      primaryConversationId: 'conv-primary-alive',
      coordinatorConversationId: 'conv-coord-alive',
    },
    telemetry: active
      ? {
          activeWorkers: 3,
          waitingWorkers: 2,
          killedWorkers: 0,
          subagentCount: 2,
          totalSteps: 26 + coderSteps,
          totalToolCalls: 67,
          totalTokens: 128450,
          avgTps: 64.2,
          liveTps: 81.5,
          totalCostUsd: 0.42,
        }
      : {
          activeWorkers: 0,
          waitingWorkers: 5,
          killedWorkers: 0,
          subagentCount: 2,
          totalSteps: 26 + coderSteps,
          totalToolCalls: 67,
          totalTokens: 128450,
          avgTps: 64.2,
          liveTps: 0,
          totalCostUsd: 0.42,
        },
    plan: {
      goal: {
        title: 'Alive Telemetry Verification Swarm',
        line: 'Verify live telemetry strip, expandable tools, adaptive polling, and Hive role workers.',
      },
      summary: 'Verify live telemetry strip, expandable tools, adaptive polling, and Hive role workers.',
      threads: [
        {
          id: 't-impl',
          title: 'Core Telemetry & SSE Bridge',
          goal: 'Stream live metrics and verify parity',
          now: active ? 'Running telemetry checks' : 'Telemetry checks settled',
          state: active ? 'running' : 'done',
          tasks: ['coder', 'verifier'],
        },
        {
          id: 't-obs',
          title: 'Swarm Observability & Monitoring',
          goal: 'Maintain Overview view.json and stall checks',
          now: active ? 'Narrator and monitor active' : 'Watching board',
          state: active ? 'running' : 'done',
          tasks: ['narrator', 'monitor'],
        },
      ],
      tasks: {
        coder: {
          title: 'Verify live tool telemetry and recent activity',
          status: active ? 'active' : 'done',
          note: active ? 'Inspecting board_ui/hive.js' : 'Verified live telemetry',
          assignee: 'coder',
        },
        verifier: {
          title: 'Verify dark/light contrast and 360px layout',
          status: 'done',
          note: 'Contrast and layout checks pass',
          assignee: 'verifier',
        },
      },
    },
    channels: [
      { name: 'main', topic: 'Swarm main channel' },
      { name: 'impl', topic: 'Implementation channel', created_by: 'coordinator' },
    ],
    members: [
      {
        name: 'coordinator',
        role: 'coordinator',
        status: active ? 'working' : 'waiting',
        conversationId: 'conv-coord-alive',
        steps: 8,
        toolCalls: 12,
        currentTool: active
          ? { name: 'swarm_status', preview: 'alive-telemetry-swarm', startedAt: '2026-11-22T20:00:00Z' }
          : null,
        recentTools: [
          { name: 'swarm_status', preview: 'alive-telemetry-swarm', status: active ? 'running' : 'ok', durationMs: 120, at: '2026-11-22T20:00:00Z' },
        ],
        tokens: { input: 12000, output: 3200, total: 15200, contextUsed: 15200, contextLimit: 200000, contextPct: 8 },
        tps: 55.0,
        elapsedMs: 14500,
        costUsd: 0.08,
        posts: 2,
        subagents: [],
      },
      {
        name: 'coder',
        role: 'worker',
        status: active ? 'working' : 'waiting',
        model: 'gpt-5.4-codex',
        steps: coderSteps,
        toolCalls: 29,
        currentTool: active
          ? { name: 'read_file', preview: 'board_ui/hive.js:480-530', startedAt: '2026-11-22T20:01:00Z' }
          : null,
        recentTools: [
          { name: 'read_file', preview: 'board_ui/hive.js:480-530', status: active ? 'running' : 'ok', durationMs: 42, at: '2026-11-22T20:01:00Z' },
          { name: 'run_command', preview: 'node tests/test_sidecar.mjs', status: 'ok', durationMs: 3180, at: '2026-11-22T20:00:50Z' },
          { name: 'edit_file', preview: 'board_ui/app.js', status: 'ok', durationMs: 85, at: '2026-11-22T20:00:35Z' },
          { name: 'search_grep', preview: 'telemetry-strip', status: 'error', durationMs: 19, at: '2026-11-22T20:00:10Z' },
        ],
        tokens: { input: 54000, output: 11400, total: 65400, contextUsed: 65400, contextLimit: 200000, contextPct: 33 },
        tps: 81.5,
        elapsedMs: 28200,
        costUsd: 0.21,
        posts: 2,
        channels: ['main', 'impl'],
        subagents: [
          {
            id: 'sub-alive-1',
            name: 'ast-scanner',
            task: 'Verify telemetry DOM testids and CSS overflow guards across mobile and desktop widths',
            status: active ? 'working' : 'done',
            model: 'deepseek-v4.1-flash',
            steps: 5,
            toolCalls: 7,
            tokens: { input: 8200, output: 2100, total: 10300 },
            tps: 94.2,
            elapsedMs: 6400,
            summary: 'Scanned board_ui components',
          },
          {
            id: 'sub-alive-2',
            name: 'theme-contrast-check',
            task: 'Verify #0e0e10 dark and #ffffff light luminance ratios',
            status: 'done',
            model: 'gemini-2.5-flash',
            steps: 3,
            toolCalls: 4,
            tokens: { input: 4100, output: 1200, total: 5300 },
            tps: 72.0,
            elapsedMs: 3150,
            summary: 'All tokens pass',
          },
        ],
      },
      {
        name: 'verifier',
        role: 'verifier',
        status: 'waiting',
        model: 'claude-sonnet-4.6',
        steps: 9,
        toolCalls: 14,
        currentTool: null,
        recentTools: [
          { name: 'run_command', preview: 'node --check board_ui/hive.js', status: 'ok', durationMs: 240, at: '2026-11-22T20:00:20Z' },
        ],
        tokens: { input: 22000, output: 4300, total: 26300, contextUsed: 26300, contextLimit: 200000, contextPct: 13 },
        tps: 48.3,
        elapsedMs: 11200,
        costUsd: 0.09,
        posts: 1,
        channels: ['main', 'impl'],
        subagents: [],
      },
      {
        name: 'narrator',
        role: 'narrator',
        roleWorker: true,
        narrator: true,
        status: active ? 'working' : 'waiting',
        model: 'gemini-2.5-flash',
        steps: 5,
        toolCalls: 7,
        currentTool: active
          ? { name: 'write_file', preview: 'view.json', startedAt: '2026-11-22T20:01:02Z' }
          : null,
        recentTools: [
          { name: 'write_file', preview: 'view.json', status: active ? 'running' : 'ok', durationMs: 65, at: '2026-11-22T20:01:02Z' },
        ],
        tokens: { input: 8500, output: 1850, total: 10350, contextUsed: 10350, contextLimit: 128000, contextPct: 8 },
        tps: 68.0,
        elapsedMs: 4900,
        costUsd: 0.02,
        posts: 1,
        channels: ['*'],
        subagents: [],
      },
      {
        name: 'monitor',
        role: 'monitor',
        roleWorker: true,
        narrator: false,
        status: 'waiting',
        model: 'gemini-2.5-flash',
        steps: 4,
        toolCalls: 5,
        currentTool: null,
        recentTools: [
          { name: 'swarm_read', preview: 'since=5', status: 'ok', durationMs: 31, at: '2026-11-22T20:00:45Z' },
        ],
        tokens: { input: 6200, output: 1200, total: 7400, contextUsed: 7400, contextLimit: 128000, contextPct: 6 },
        tps: 61.0,
        elapsedMs: 2800,
        costUsd: 0.02,
        posts: 0,
        channels: ['*'],
        subagents: [],
      },
    ],
    messages: [
      {
        seq: 1,
        id: 'm-1',
        agent: 'coordinator',
        sender: 'coordinator',
        type: 'post',
        channel: 'main',
        text: '@coder @verifier Verify alive telemetry UI and Hive placement.',
        time: new Date(baseTs + 10000).toISOString(),
        ts: baseTs + 10000,
        mentions: ['coder', 'verifier'],
      },
      {
        seq: 2,
        id: 'm-2',
        agent: 'coder',
        sender: 'coder',
        type: 'post',
        channel: 'impl',
        text: '@verifier Telemetry strip and tool pills wired.',
        time: new Date(baseTs + 20000).toISOString(),
        ts: baseTs + 20000,
        mentions: ['verifier'],
      },
      {
        seq: 3,
        id: 'm-3',
        agent: 'verifier',
        sender: 'verifier',
        type: 'decision',
        channel: 'impl',
        text: '@coordinator Dark/light contrast and 360px overflow checks pass.',
        time: new Date(baseTs + 30000).toISOString(),
        ts: baseTs + 30000,
        mentions: ['coordinator'],
      },
    ],
    frames: [
      { path: 'shots/step1.png', seq: 1, caption: 'Initial telemetry wire' },
      { path: 'shots/step2.png', seq: 3, caption: 'Expanded activity verified' },
    ],
    narrator: {
      name: 'narrator',
      role: 'narrator',
      status: active ? 'working' : 'waiting',
      lastReadSeq: 3,
    },
    narratorState: active ? 'working' : 'waiting',
    view: {
      exists: true,
      dir: 'C:/Projects/sandbox-repo',
      json: {
        as_of_seq: 3,
        headline: 'Alive Telemetry Verification Swarm',
        phase: { label: 'Live Telemetry', index: 1, total: 1 },
        goal: {
          title: 'Alive Telemetry Verification Swarm',
          line: 'Verify live telemetry strip, expandable tools, adaptive polling, and Hive role workers.',
        },
        threads: [
          {
            id: 'obs-summary',
            title: 'Telemetry & Hive parity check',
            outcome: active ? 'active' : 'done',
            refs: ['#1', '#3'],
          },
        ],
      },
    },
    raw: {
      board: JSON.stringify({ swarm: 'alive-telemetry-swarm', head: 6 }, null, 2),
      meta: JSON.stringify({ title: 'Alive Telemetry Verification Swarm' }, null, 2),
      members: JSON.stringify([{ name: 'coordinator', role: 'coordinator' }], null, 2),
      view: JSON.stringify({ headline: 'Alive Telemetry Verification Swarm' }, null, 2),
    },
  };
}

test('Observer UI verifies alive telemetry strip, expandable recent tools, Tier-3 metrics, Hive role-worker hex placement, adaptive polling, and subpath parity', async (t) => {
  const pw = await loadPlaywright();
  const executablePath = findBrowserExecutable();
  const browser = await pw.chromium.launch({
    headless: true,
    args: ['--no-proxy-server', '--proxy-bypass-list=<-loopback>'],
    ...(executablePath ? { executablePath } : {}),
  });
  t.after(() => browser.close());

  const tempState = mkdtempSync(join(tmpdir(), 'hermes-observer-alive-'));
  t.after(() => rmSync(tempState, { recursive: true, force: true }));

  let currentSnapshot = buildAliveTelemetrySnapshot({ active: true, etag: 'alive-etag-1', coderSteps: 18 });
  const boardPolls = [];

  const upstreamServer = createServer((req, res) => {
    const reqUrl = new URL(req.url, 'http://127.0.0.1');
    if (reqUrl.pathname === '/board') {
      const etag = reqUrl.searchParams.get('etag') || '';
      boardPolls.push({
        ts: Date.now(),
        etag,
        active: (currentSnapshot.telemetry?.activeWorkers || 0) > 0,
        swarmParam: reqUrl.searchParams.get('swarm'),
      });
      if (etag && etag === currentSnapshot.etag) {
        res.writeHead(200, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({
          unchanged: true,
          swarm: currentSnapshot.swarm,
          etag: currentSnapshot.etag,
          primaryConversationId: currentSnapshot.primaryConversationId,
        }));
        return;
      }
      res.writeHead(200, { 'Content-Type': 'application/json', ETag: currentSnapshot.etag });
      res.end(JSON.stringify(currentSnapshot));
      return;
    }
    if (reqUrl.pathname === '/board/frame') {
      res.writeHead(200, { 'Content-Type': 'image/png' });
      res.end(PNG_1X1);
      return;
    }
    res.writeHead(404);
    res.end('Not found');
  });

  const upstreamPort = await listenServer(upstreamServer);
  t.after(() => closeServer(upstreamServer));

  writeFileSync(
    join(tempState, 'registry.json'),
    JSON.stringify({
      servers: {
        'alive-telemetry-swarm': {
          pid: process.pid,
          httpUrl: `http://127.0.0.1:${upstreamPort}`,
          swarmId: 'alive-telemetry-swarm',
          primaryConversationId: 'conv-primary-alive',
          coordinatorConversationId: 'conv-coord-alive',
        },
      },
    }),
    'utf-8',
  );

  const sidecarProc = await spawnSidecarProcess({
    HERMES_A2A_STATE_DIR: tempState,
    ANTIGRAVITY_CONVERSATION_ID: 'conv-primary-alive',
    HERMES_SWARM_DISABLE_WAKEUP: '1',
  });
  t.after(() => sidecarProc.stop());

  const SUBPATH_PREFIX = '/remote-proxy/plugin/hermes-swarm/swarm_board';
  const proxy = createSubpathProxy(sidecarProc.port, SUBPATH_PREFIX);
  const proxyPort = await listenServer(proxy.server);
  t.after(() => closeServer(proxy.server));

  const screenshotDir = process.env.VERIFY_UPGRADE_SCREENSHOT_DIR
    || join(homedir(), '.gemini', 'antigravity', 'brain', '2cb6b9ce-2fd7-45f3-ba7a-7955066ece6e', 'scratch', 'g2h_verify_screenshots');
  mkdirSync(screenshotDir, { recursive: true });

  async function verifyAliveAtUrl(pageUrl, originPrefix, expectedSwarmPath, saveShots) {
    currentSnapshot = buildAliveTelemetrySnapshot({ active: true, etag: 'alive-etag-1', coderSteps: 18 });
    const context = await browser.newContext({ viewport: { width: 1280, height: 860 }, colorScheme: 'dark' });
    const page = await context.newPage();
    const obs = attachPageObservers(page, originPrefix);

    await page.goto(pageUrl, { waitUntil: 'networkidle' });
    await page.waitForSelector('#board:not(.hidden)', { timeout: 10000 });
    await page.click('#tabbtn-overview');
    await page.waitForSelector('#tab-overview.active', { timeout: 5000 });

    // 1. Telemetry strip live counts + dark polarity (#0e0e10)
    const stripText = await page.textContent('#telemetry-strip');
    const stripVisible = await page.evaluate(() => !document.getElementById('telemetry-strip').classList.contains('hidden'));
    assert.equal(stripVisible, true, '#telemetry-strip must be visible when telemetry is active');
    assert.match(stripText, /3 active/);
    assert.match(stripText, /2 sub/);
    assert.match(stripText, /44 steps/);
    assert.match(stripText, /67 tools/);
    assert.match(stripText, /128k proc/);
    assert.match(stripText, /82 t\/s/);
    assert.match(stripText, /\$0\.42/);

    const darkBg = await page.evaluate(() => ({
      theme: document.documentElement.dataset.theme,
      bodyBg: getComputedStyle(document.body).backgroundColor,
      titleFg: getComputedStyle(document.getElementById('swarm-title')).color,
      hasSwitcher: document.querySelector('#swarm-switcher') !== null,
    }));
    assert.equal(darkBg.theme, 'dark');
    assert.equal(darkBg.bodyBg, 'rgb(14, 14, 16)');
    assert.ok(relLuminance(parseRgb(darkBg.titleFg)) > 0.7);
    assert.equal(darkBg.hasSwitcher, false);

    // 2. Overview running row shows currentTool + compact tokens/TPS
    const runningCheck = await page.evaluate(() => {
      const act = [...document.querySelectorAll('[data-testid="swarm-running-activity"]')].map((e) => e.textContent.trim());
      const tel = [...document.querySelectorAll('[data-testid="swarm-running-telemetry"]')].map((e) => e.textContent.trim());
      return { act, tel };
    });
    assert.ok(runningCheck.act.some((t) => t.includes('Running read_file(board_ui/hive.js:480-530)')), `Missing Overview currentTool: ${JSON.stringify(runningCheck)}`);
    assert.ok(runningCheck.tel.some((t) => t.includes('65.4k tok') && t.includes('82 t/s')), `Missing Overview running telemetry: ${JSON.stringify(runningCheck)}`);

    if (saveShots) {
      await page.screenshot({ path: join(screenshotDir, 'overview_dark.png'), fullPage: true });
    }

    // 3. Members tab: pulsing indicator, .o2-toolpill, .o2-mmetrics, closed-by-default <details.o2-tools>, Tier-3 metrics, 360px zero overflow
    await page.click('#tabbtn-members');
    await page.waitForSelector('#tab-members.active');

    const initialMembers = await page.evaluate(() => {
      const coderRow = [...document.querySelectorAll('.o2-mrow')].find((r) => r.dataset.openMember === 'coder');
      const details = coderRow?.querySelector('details.o2-tools');
      return {
        workingDotCount: document.querySelectorAll('.o2-mavd.working').length,
        toolPill: coderRow?.querySelector('.o2-toolpill')?.textContent?.trim() || '',
        metrics: coderRow?.querySelector('.o2-mmetrics')?.textContent?.trim() || '',
        metricsSpans: [...(coderRow?.querySelectorAll('.o2-mmetrics > span') || [])].map((s) => s.textContent.trim()),
        detailsExists: Boolean(details),
        detailsOpen: details ? details.open : null,
        summaryText: details?.querySelector('summary')?.textContent?.trim() || '',
        subRows: [...(coderRow?.querySelectorAll('[data-testid="swarm-subagent-row"]') || [])].map((row) => ({
          hasDot: Boolean(row.querySelector('.o2-sub-dot')),
          name: row.querySelector('.o2-sub-name')?.textContent?.trim() || '',
          meta: row.querySelector('.o2-sub-meta')?.textContent?.trim() || '',
        })),
      };
    });
    assert.ok(initialMembers.workingDotCount >= 3, `Expected working pulsing dots, got ${initialMembers.workingDotCount}`);
    assert.equal(initialMembers.toolPill, 'Running read_file(board_ui/hive.js:480-530)');
    assert.deepEqual(initialMembers.metricsSpans, ['18 steps', '29 tools', '65.4k proc', '54.0k pure', '82 t/s', '65.4k/200k 33%', '$0.21']);
    assert.equal(initialMembers.detailsExists, true);
    assert.equal(initialMembers.detailsOpen, false, '<details class="o2-tools"> must start CLOSED by default');
    assert.equal(initialMembers.summaryText, 'Recent activity (4)');
    assert.equal(initialMembers.subRows.length, 2);
    assert.equal(initialMembers.subRows[0].hasDot, true);
    assert.equal(initialMembers.subRows[0].name, 'ast-scanner');
    assert.equal(initialMembers.subRows[0].meta, 'deepseek-v4.1-flash · 5 steps · 10.3k proc · 94 t/s · 6.4s');
    assert.equal(initialMembers.subRows[1].meta, 'gemini-2.5-flash · 3 steps · 5.3k proc · 72 t/s · 3.1s');

    // 360px dark zero-overflow check + screenshot
    await page.setViewportSize({ width: 360, height: 780 });
    await page.waitForTimeout(100);
    const overflow360 = await page.evaluate(() => ({
      scrollWidth: document.documentElement.scrollWidth,
      innerWidth: window.innerWidth,
      subOk: [...document.querySelectorAll('.o2-subagents')].every((el) => el.scrollWidth <= el.clientWidth + 1),
      toolsOk: [...document.querySelectorAll('.o2-tools')].every((el) => el.scrollWidth <= el.clientWidth + 1),
      subMetaMaxRight: Math.max(0, ...[...document.querySelectorAll('.o2-sub-meta')].map((el) => el.getBoundingClientRect().right)),
    }));
    assert.ok(overflow360.scrollWidth <= overflow360.innerWidth, `360px overflow: ${JSON.stringify(overflow360)}`);
    assert.equal(overflow360.subOk, true);
    assert.equal(overflow360.toolsOk, true);
    assert.ok(overflow360.subMetaMaxRight <= overflow360.innerWidth, `360px sub-meta overflow: ${overflow360.subMetaMaxRight} > ${overflow360.innerWidth}`);
    if (saveShots) {
      await page.screenshot({ path: join(screenshotDir, 'members_dark_360.png'), fullPage: true });
    }

    // Restore 1280px, switch to Light (#ffffff), expand <details.o2-tools> without triggering card navigation, verify persistence across 1s poll refresh!
    await page.setViewportSize({ width: 1280, height: 860 });
    await page.evaluate(() => {
      window.__toggleConvCalls = [];
      window.sidecar = window.sidecar || {};
      window.sidecar.ui = window.sidecar.ui || {};
      window.sidecar.ui.toggleConversation = (cid) => window.__toggleConvCalls.push(cid);
      window.postMessage(
        {
          type: 'theme-change',
          payload: {
            cssVariables: {
              '--background': '#ffffff',
              '--foreground': '#111111',
              '--card': '#f5f6f8',
              '--primary': '#1a73e8',
            },
          },
        },
        '*',
      );
    });
    await page.waitForFunction(() => document.documentElement.dataset.theme === 'light', null, { timeout: 3000 });
    await page.waitForFunction(() => getComputedStyle(document.body).backgroundColor === 'rgb(255, 255, 255)', null, { timeout: 3000 });

    const coderSummary = page.locator('.o2-mrow[data-open-member="coder"] details.o2-tools > summary');
    await coderSummary.click();
    const afterExpand = await page.evaluate(() => {
      const coderRow = document.querySelector('.o2-mrow[data-open-member="coder"]');
      const details = coderRow?.querySelector('details.o2-tools');
      const rows = [...(details?.querySelectorAll('.o2-tool-row') || [])].map((r) => ({
        call: r.querySelector('.o2-tool-call')?.textContent?.trim() || '',
        meta: r.querySelector('.o2-tool-meta')?.textContent?.trim() || '',
      }));
      return {
        open: details?.open ?? false,
        rows,
        toggleConvCalls: window.__toggleConvCalls,
        activeTab: document.querySelector('.tab.active')?.dataset?.tab,
        bodyBg: getComputedStyle(document.body).backgroundColor,
        memFg: getComputedStyle(document.getElementById('members')).color,
        scrollWidth: document.documentElement.scrollWidth,
        innerWidth: window.innerWidth,
      };
    });
    assert.equal(afterExpand.open, true, 'Clicking summary must expand <details class="o2-tools">');
    assert.equal(afterExpand.rows.length, 4);
    assert.equal(afterExpand.rows[0].call, 'read_file(board_ui/hive.js:480-530)');
    assert.deepEqual(afterExpand.toggleConvCalls, [], 'Expanding recent tools must NOT trigger card navigation');
    assert.equal(afterExpand.activeTab, 'members', 'Expanding recent tools must NOT change tabs');
    assert.equal(afterExpand.bodyBg, 'rgb(255, 255, 255)');
    assert.ok(relLuminance(parseRgb(afterExpand.memFg)) < 0.25);
    assert.ok(afterExpand.scrollWidth <= afterExpand.innerWidth);

    // Trigger live poll update (new etag + coderSteps: 19) and assert <details.o2-tools> stays OPEN across poll refresh!
    currentSnapshot = buildAliveTelemetrySnapshot({ active: true, etag: 'alive-etag-2', coderSteps: 19 });
    await page.waitForFunction(
      () => (document.querySelector('.o2-mrow[data-open-member="coder"] .o2-mmetrics')?.textContent || '').includes('19 steps'),
      null,
      { timeout: 4000 },
    );
    const persistedOpen = await page.evaluate(
      () => document.querySelector('.o2-mrow[data-open-member="coder"] details.o2-tools')?.open ?? false,
    );
    assert.equal(persistedOpen, true, '<details class="o2-tools"> must stay OPEN across live poll refreshes');

    if (saveShots) {
      await page.screenshot({ path: join(screenshotDir, 'members_light_expanded.png'), fullPage: true });
    }

    // 4. Graph Hive view: monitor and narrator plotted INSIDE their assigned plan.threads hex cell!
    await page.evaluate(() => {
      window.postMessage(
        {
          type: 'theme-change',
          payload: {
            cssVariables: {
              '--background': '#0e0e10',
              '--foreground': '#eceef2',
              '--card': '#16171b',
              '--primary': '#3898ec',
            },
          },
        },
        '*',
      );
    });
    await page.waitForFunction(() => document.documentElement.dataset.theme === 'dark', null, { timeout: 3000 });

    await page.click('#tabbtn-graph');
    await page.waitForSelector('#tab-graph.active');
    await page.waitForSelector('#hive .hv-bee[data-n="monitor"]', { timeout: 5000 });
    await page.waitForSelector('#hive .hv-bee[data-n="narrator"]', { timeout: 5000 });

    const hiveGeometry = await page.evaluate(() => {
      const cells = [...document.querySelectorAll('#hive .hv-cell')].map((el) => ({
        idx: el.getAttribute('data-c'),
        title: el.querySelector('title')?.textContent || '',
        rect: el.getBoundingClientRect(),
      }));
      const obsCell = cells.find((c) => c.title.includes('Swarm Observability'));
      const otherCell = cells.find((c) => c.title.startsWith('Other'));
      const beeRect = (name) => {
        const b = document.querySelector(`#hive .hv-bee[data-n="${name}"]`);
        if (!b) return null;
        const r = b.getBoundingClientRect();
        return { cx: r.left + r.width / 2, cy: r.top + r.height / 2, visible: r.width > 0 && r.height > 0 };
      };
      const inside = (pt, box) => Boolean(pt && box && pt.cx >= box.left && pt.cx <= box.right && pt.cy >= box.top && pt.cy <= box.bottom);
      const mon = beeRect('monitor');
      const nar = beeRect('narrator');
      return {
        obsIdx: obsCell?.idx ?? null,
        obsTitle: obsCell?.title || '',
        hasOtherCell: Boolean(otherCell),
        monitorInsideObsHex: inside(mon, obsCell?.rect),
        narratorInsideObsHex: inside(nar, obsCell?.rect),
      };
    });
    assert.ok(hiveGeometry.obsIdx !== null, 'Swarm Observability hex cell must exist');
    assert.equal(hiveGeometry.hasOtherCell, false, 'Role workers must NOT spill into an Other hex cell');
    assert.equal(hiveGeometry.monitorInsideObsHex, true, `monitor bee must be plotted inside Swarm Observability hex (${JSON.stringify(hiveGeometry)})`);
    assert.equal(hiveGeometry.narratorInsideObsHex, true, `narrator bee must be plotted inside Swarm Observability hex (${JSON.stringify(hiveGeometry)})`);

    if (saveShots) {
      await page.screenshot({ path: join(screenshotDir, 'graph_hive_dark.png'), fullPage: true });
    }

    // Click the Swarm Observability hex cell and verify detail card lists @narrator and @monitor (not empty!)
    await page.click(`#hive .hv-cell[data-c="${hiveGeometry.obsIdx}"]`);
    await page.waitForSelector('#hive .hv-card:not([hidden])', { timeout: 3000 });
    const obsCardText = await page.textContent('#hive .hv-card');
    assert.doesNotMatch(obsCardText, /No one is on this thread yet/);
    assert.match(obsCardText, /@narrator/);
    assert.match(obsCardText, /@monitor/);

    // 5. Adaptive polling proof (1000ms active vs 3000ms idle) via relative ./api/swarm?etag=
    const activeStartIdx = boardPolls.length;
    await page.waitForTimeout(2600);
    const activeSlice = boardPolls.slice(activeStartIdx);
    assert.ok(activeSlice.length >= 2, `Expected >=2 active 1000ms polls within 2600ms, got ${activeSlice.length}`);
    for (let i = 1; i < activeSlice.length; i++) {
      const dt = activeSlice[i].ts - activeSlice[i - 1].ts;
      assert.ok(dt >= 750 && dt < 2000, `Expected ~1000ms active poll interval, got ${dt}ms`);
    }

    // Transition to idle snapshot -> next poll must wait ~3000ms
    currentSnapshot = buildAliveTelemetrySnapshot({ active: false, etag: 'alive-idle-1', coderSteps: 19 });
    await page.waitForFunction(
      () => (document.getElementById('telemetry-strip')?.textContent || '').includes('5 waiting'),
      null,
      { timeout: 3000 },
    );
    const idleAnchorTs = boardPolls.at(-1).ts;
    const idleIdxAfter = boardPolls.length;
    await page.waitForTimeout(2000);
    assert.equal(
      boardPolls.length,
      idleIdxAfter,
      `Expected zero polls during first 2000ms of 3000ms idle cadence, got ${boardPolls.length - idleIdxAfter}`,
    );
    waitForIdlePoll: {
      const deadline = Date.now() + 3000;
      while (Date.now() < deadline) {
        if (boardPolls.length > idleIdxAfter) break waitForIdlePoll;
        await page.waitForTimeout(50);
      }
      assert.fail('Expected 3000ms idle poll to fire');
    }
    const idleInterval = boardPolls[idleIdxAfter].ts - idleAnchorTs;
    assert.ok(idleInterval >= 2500 && idleInterval <= 4500, `Expected ~3000ms idle poll interval, got ${idleInterval}ms`);

    // Verify zero ?swarm= leaks and expected ./api/swarm path
    for (const r of obs.originResponses) {
      assert.equal(r.url.includes('swarm='), false, `Unexpected ?swarm= query param in ${r.url}`);
    }
    assert.equal(
      obs.originResponses.some((r) => new URL(r.url).pathname === expectedSwarmPath && r.status === 200),
      true,
      `Expected ${expectedSwarmPath} 200 response`,
    );

    await context.close();
    const realFailedRequests = obs.failedRequests.filter(
      (r) => !(r.reason === 'net::ERR_ABORTED' && /fonts\.g(?:static|oogleapis)\.com/.test(r.url)),
    );
    assert.deepEqual(realFailedRequests, [], `Failed network requests: ${JSON.stringify(realFailedRequests)}`);
    const realConsoleErrors = obs.consoleErrors.filter(
    (c) => !c.url.includes('/_sidecar/get-conversation-metadata') && !/Failed to resolve project ID/i.test(c.text),
  );
  assert.deepEqual(realConsoleErrors, [], `Console errors: ${JSON.stringify(realConsoleErrors)}`);
    assert.deepEqual(obs.pageErrors, [], `Page errors: ${JSON.stringify(obs.pageErrors)}`);
  }

  await verifyAliveAtUrl(`${sidecarProc.baseUrl}/`, sidecarProc.baseUrl, '/api/swarm', true);
  await verifyAliveAtUrl(
    `http://127.0.0.1:${proxyPort}${SUBPATH_PREFIX}/`,
    `http://127.0.0.1:${proxyPort}`,
    `${SUBPATH_PREFIX}/api/swarm`,
    false,
  );
  assert.deepEqual(proxy.proxyLeaks, [], `Subpath proxy leaks: ${JSON.stringify(proxy.proxyLeaks)}`);
});

test('Phase-3 Observer UI: HEXD SVG, e.source guard, ov-system, ov-open-coord, Narrator prefs/NAVIGATE, channels normChannel + New divider, replied tag at / and subpath', async (t) => {
  // 1. Direct channels.mjs unit checks for normChannel & visibleUnread
  const channelsMod = await import(pathToFileURL(join(BOARD_UI_DIR, 'channels.mjs')).href);
  assert.equal(channelsMod.normChannel('#general'), 'main');
  assert.equal(channelsMod.normChannel('general'), 'main');
  assert.equal(channelsMod.normChannel('main'), 'main');
  assert.equal(channelsMod.normChannel('#Backend'), 'backend');
  assert.equal(channelsMod.normChannel('*'), '*');
  assert.equal(channelsMod.normChannel(null), 'main');
  assert.equal(channelsMod.channelOf({ channel: '#general' }), 'main');
  assert.equal(channelsMod.channelOf({ channel: '#backend' }), 'backend');
  assert.deepEqual(channelsMod.controlOf({ control: { op: 'create', channel: '#backend' } }), { op: 'create', channel: 'backend' });

  const pw = await loadPlaywright();
  const executablePath = findBrowserExecutable();
  const browser = await pw.chromium.launch({
    headless: true,
    args: ['--no-proxy-server', '--proxy-bypass-list=<-loopback>'],
    ...(executablePath ? { executablePath } : {}),
  });
  t.after(() => browser.close());

  const tempState = mkdtempSync(join(tmpdir(), 'hermes-observer-p3-'));
  t.after(() => rmSync(tempState, { recursive: true, force: true }));

  const screenshotDir = process.env.VERIFY_UPGRADE_SCREENSHOT_DIR
    || join(homedir(), '.gemini', 'antigravity', 'brain', '2cb6b9ce-2fd7-45f3-ba7a-7955066ece6e', 'scratch', 'g2h_verify_screenshots');
  mkdirSync(screenshotDir, { recursive: true });

  const makePhase3Snapshot = ({ withNarrator = true, withViewHtml = true } = {}) => ({
    swarm: 'phase3-ui-swarm',
    id: 'phase3-ui-swarm',
    etag: `p3-${withNarrator ? 'nar' : 'nonar'}-${withViewHtml ? 'html' : 'plain'}`,
    head: 5,
    title: 'Phase-3 Parity Swarm',
    dir: 'C:/Projects/demo',
    primaryConversationId: 'conv-prim-p3',
    coordinatorConversationId: 'conv-coord-p3',
    member: 'coordinator',
    builtinOverview: true,
    ...(withNarrator ? {} : { narratorState: 'killed', narrator: { name: 'narrator', status: 'killed', killed: true } }),
    urlHosts: ['docs.google.com'],
    allowedFiles: { 'docs/spec.md': 'file:///C:/Projects/demo/docs/spec.md' },
    unseenBy: { coordinator: [] },
    channels: [
      { name: '#general', topic: 'Main channel' },
      { name: '#backend', topic: 'Backend APIs', created_by: 'coordinator' },
    ],
    plan: {
      goal: { title: 'Phase-3 Parity', line: 'Verify all newly closed items' },
      threads: [{ id: 'th-1', title: 'Core', goal: 'All green', workstreams: ['backend'] }],
      tasks: { backend: 'Verify channels & unread' },
    },
    members: [
      {
        name: 'coordinator',
        role: 'coordinator',
        status: 'waiting',
        conversationId: 'conv-coord-p3',
        lastReadSeq: 3,
        replied: true,
        replyAsk: 'Confirm Phase-3 parity',
        steps: 6,
        posts: 2,
      },
      {
        name: 'backend',
        role: 'worker',
        status: 'working',
        model: 'claude-sonnet',
        channels: ['#backend'],
        steps: 9,
        posts: 2,
        replied: true,
      },
      ...(withNarrator
        ? [
            {
              name: 'narrator',
              role: 'narrator',
              status: 'waiting',
              steps: 2,
              posts: 1,
            },
          ]
        : []),
    ],
    messages: [
      {
        seq: 1,
        id: 'm-1',
        agent: 'coordinator',
        sender: 'coordinator',
        type: 'post',
        channel: '#general',
        text: '@backend Kick off Phase-3 check',
        ts: 1795370400000,
        mentions: ['backend'],
      },
      {
        seq: 2,
        id: 'm-2',
        agent: 'backend',
        sender: 'backend',
        type: 'decision',
        channel: '#backend',
        text: 'Channel normalization active on #1',
        ts: 1795370460000,
        replyTo: 1,
      },
      {
        seq: 3,
        id: 'm-3',
        agent: 'backend',
        sender: 'backend',
        type: 'post',
        channel: 'main',
        text: 'Reply by id #m-2',
        ts: 1795370520000,
        replyTo: 'm-2',
      },
      {
        seq: 4,
        id: 'm-4',
        agent: 'backend',
        sender: 'backend',
        type: 'handoff',
        channel: '#backend',
        text: '@coordinator Unread backend handoff',
        ts: 1795370580000,
        mentions: ['coordinator'],
        replyTo: 'missing-id',
      },
      {
        seq: 5,
        id: 'm-5',
        agent: 'coordinator',
        sender: 'coordinator',
        type: 'post',
        channel: 'main',
        text: 'Second unread message',
        ts: 1795370640000,
      },
    ],
    systemAttention: [
      {
        id: 'sys-1',
        kind: 'warning',
        member: 'coordinator',
        conversationId: 'conv-coord-p3',
        text: 'Action needed on Phase-3 check',
      },
    ],
    view: {
      dir: 'C:/Projects/demo',
      exists: true,
      ...(withViewHtml
        ? {
            html: [
              '<!doctype html><html><body><h1>Narrator Custom Page</h1>',
              '<button id="nav-ok-url" onclick="parent.postMessage({type:\'NAVIGATE\',action:{type:\'url\',target:\'https://docs.google.com/document/d/123\'}},\'*\')">Open Doc</button>',
              '<button id="nav-bad-url" onclick="parent.postMessage({type:\'NAVIGATE\',action:{type:\'url\',target:\'https://evil.example.com/phish\'}},\'*\')">Bad Url</button>',
              '<button id="nav-ok-file" onclick="parent.postMessage({type:\'NAVIGATE\',action:{type:\'file\',target:\'docs/spec.md\'}},\'*\')">Open Spec</button>',
              '<button id="nav-bad-file" onclick="parent.postMessage({type:\'NAVIGATE\',action:{type:\'file\',target:\'../secrets.txt\'}},\'*\')">Bad File</button>',
              '<script>parent.postMessage({type:"READY"},"*");parent.postMessage({type:"RENDERED",height:240},"*");</script>',
              '</body></html>',
            ].join(''),
          }
        : {}),
      json: {
        as_of_seq: 5,
        headline: 'Phase-3 Parity Check',
        goal: { title: 'Phase-3 Parity', line: 'All items verified' },
        threads: [{ id: 'th-1', title: 'Core', goal: 'All green', workstreams: ['backend'], state: 'running' }],
        workstreams: [{ id: 'backend', title: 'Backend', state: 'running', assignees: ['backend'] }],
      },
    },
    raw: {
      board: JSON.stringify({ swarm: 'phase3-ui-swarm', head: 5 }, null, 2),
      meta: JSON.stringify({ title: 'Phase-3 Parity Swarm' }, null, 2),
      members: JSON.stringify([{ name: 'coordinator', role: 'coordinator' }], null, 2),
      view: JSON.stringify({ headline: 'Phase-3 Parity Check' }, null, 2),
    },
  });

  let currentSnapshot = makePhase3Snapshot({ withNarrator: true, withViewHtml: true });
  const upstreamServer = createServer((req, res) => {
    const reqUrl = new URL(req.url, 'http://127.0.0.1');
    if (reqUrl.pathname === '/board') {
      res.writeHead(200, { 'Content-Type': 'application/json', ETag: currentSnapshot.etag });
      res.end(JSON.stringify(currentSnapshot));
      return;
    }
    res.writeHead(404);
    res.end('Not found');
  });
  const upstreamPort = await listenServer(upstreamServer);
  t.after(() => closeServer(upstreamServer));

  writeFileSync(
    join(tempState, 'registry.json'),
    JSON.stringify({
      servers: {
        'phase3-ui-swarm': {
          pid: process.pid,
          httpUrl: `http://127.0.0.1:${upstreamPort}`,
          swarmId: 'phase3-ui-swarm',
          primaryConversationId: 'conv-prim-p3',
          coordinatorConversationId: 'conv-coord-p3',
        },
      },
    }),
    'utf-8',
  );

  const sidecarProc = await spawnSidecarProcess({
    HERMES_A2A_STATE_DIR: tempState,
    ANTIGRAVITY_CONVERSATION_ID: 'conv-prim-p3',
    HERMES_SWARM_DISABLE_WAKEUP: '1',
    SWARM_OVERVIEW: 'false',
  });
  t.after(() => sidecarProc.stop());

  const SUBPATH_PREFIX = '/remote-proxy/plugin/hermes-swarm/swarm_board';
  const proxy = createSubpathProxy(sidecarProc.port, SUBPATH_PREFIX);
  const proxyPort = await listenServer(proxy.server);
  t.after(() => closeServer(proxy.server));

  async function verifyPhase3AtUrl(pageUrl, originPrefix, saveShots) {
    currentSnapshot = makePhase3Snapshot({ withNarrator: true, withViewHtml: true });
    const context = await browser.newContext({ viewport: { width: 1280, height: 860 }, colorScheme: 'dark' });
    const page = await context.newPage();
    const obs = attachPageObservers(page, originPrefix);

    await page.addInitScript(() => {
      if (window === window.top) {
        window.__openedUrls = [];
        window.__auxPaneCalls = [];
        window.__coordToggleCalls = [];
        window.open = (url) => {
          window.__openedUrls.push(String(url));
          return null;
        };
        window.sidecar = window.sidecar || {};
        window.sidecar.ui = {
          toggleConversation: (cid) => window.__coordToggleCalls.push(cid),
          toggleAuxPane: (arg) => window.__auxPaneCalls.push(arg),
        };
      }
    });

    await page.goto(pageUrl, { waitUntil: 'networkidle' });
    await page.waitForSelector('#board:not(.hidden)', { timeout: 10000 });
    await page.click('#tabbtn-overview');
    await page.waitForSelector('#tab-overview.active', { timeout: 5000 });

    // 1. e.source guard on theme-change present in board_ui/app.js & same-window theme-change works
    const appJsText = readFileSync(join(BOARD_UI_DIR, 'app.js'), 'utf-8');
    assert.match(appJsText, /event\.source\s*!==\s*window\s*&&\s*event\.source\s*!==\s*window\.parent/);
    assert.equal(await page.evaluate(() => document.documentElement.dataset.theme), 'dark');

    // 2. Narrator's page #ov-mode toggle + swarm_board.narrator_plain localStorage persistence across reload
    await page.waitForSelector('#ov-mode:not(.hidden)', { timeout: 3000 });
    await page.click('#ov-mode');
    await page.waitForSelector('#ov-frame:not(.hidden) iframe[data-testid="swarm-overview-iframe"]', { timeout: 5000 });
    assert.equal(await page.evaluate(() => localStorage.getItem('swarm_board.narrator_plain')), '0');
    await page.reload({ waitUntil: 'networkidle' });
    await page.evaluate(() => {
      window.__openedUrls = [];
      window.__auxPaneCalls = [];
      window.__coordToggleCalls = [];
      window.sidecar = window.sidecar || {};
      window.sidecar.ui = {
        toggleConversation: (cid) => window.__coordToggleCalls.push(cid),
        toggleAuxPane: (arg) => window.__auxPaneCalls.push(arg),
      };
    });
    await page.click('#tabbtn-overview');
    await page.waitForSelector('#ov-frame:not(.hidden) iframe[data-testid="swarm-overview-iframe"]', { timeout: 10000 });
    assert.equal(await page.evaluate(() => localStorage.getItem('swarm_board.narrator_plain')), '0');

    // 3. ov-system & ov-open-coord (visible when plain=false / Narrator's page active)
    await page.waitForSelector('#ov-system:not(.hidden)', { timeout: 3000 });
    assert.match(await page.textContent('#ov-system'), /Action needed on Phase-3 check/);
    await page.waitForSelector('#ov-open-coord:not(.hidden)', { timeout: 3000 });
    await page.click('#ov-open-coord');
    assert.deepEqual(await page.evaluate(() => window.__coordToggleCalls), ['conv-coord-p3']);

    // 4. Narrator NAVIGATE urlHosts & allowedFiles enforcement (fresh page at ?tab=overview so parentInputAt === -Infinity)
    async function triggerNavAction(btnSel) {
      const navPage = await context.newPage();
      await navPage.addInitScript(() => {
        if (window === window.top) {
          localStorage.setItem('swarm_board.narrator_plain', '0');
          window.__openedUrls = [];
          window.__auxPaneCalls = [];
          window.open = (url) => {
            window.__openedUrls.push(String(url));
            return null;
          };
        }
      });
      const sep = pageUrl.includes('?') ? '&' : '?';
      await navPage.goto(`${pageUrl}${sep}tab=overview`, { waitUntil: 'networkidle' });
      await navPage.evaluate(() => {
        window.sidecar = window.sidecar || {};
        window.sidecar.ui = {
          toggleAuxPane: (arg) => window.__auxPaneCalls.push(arg),
        };
      });
      await navPage.waitForSelector('#ov-frame:not(.hidden) iframe[data-testid="swarm-overview-iframe"]', { timeout: 5000 });
      await navPage.frameLocator('iframe[data-testid="swarm-overview-iframe"]').locator(btnSel).click();
      await navPage.waitForTimeout(200);
      const res = await navPage.evaluate(() => ({
        openedUrls: window.__openedUrls,
        auxPaneCalls: window.__auxPaneCalls,
        toastText: document.getElementById('toast')?.textContent || '',
      }));
      await navPage.close();
      return res;
    }

    const okUrlRes = await triggerNavAction('#nav-ok-url');
    assert.deepEqual(okUrlRes.openedUrls, ['https://docs.google.com/document/d/123']);
    const badUrlRes = await triggerNavAction('#nav-bad-url');
    assert.deepEqual(badUrlRes.openedUrls, []);
    assert.match(badUrlRes.toastText, /isn't allowed/);
    const okFileRes = await triggerNavAction('#nav-ok-file');
    assert.deepEqual(okFileRes.auxPaneCalls, [{ tabType: 'fileView', uri: 'file:///C:/Projects/demo/docs/spec.md' }]);
    const badFileRes = await triggerNavAction('#nav-bad-file');
    assert.deepEqual(badFileRes.auxPaneCalls, []);
    assert.match(badFileRes.toastText, /isn't allowed/);

    // Toggle back to plain overview and verify swarm_board.narrator_plain is cleared (null = default plain)
    await page.click('#ov-mode');
    await page.waitForSelector('#ov-plain:not(.hidden)', { timeout: 5000 });
    assert.equal(await page.evaluate(() => localStorage.getItem('swarm_board.narrator_plain')), null);
    await page.evaluate(() => localStorage.removeItem('swarm_board.narrator_plain'));

    // 6. nextNarratorName() when narrator is killed (rendered as narrator-1 in #ov-notice)
    currentSnapshot = makePhase3Snapshot({ withNarrator: false, withViewHtml: false });
    await page.waitForFunction(
      () => (document.getElementById('ov-notice')?.textContent || '').includes('narrator-1'),
      null,
      { timeout: 4000 },
    );

    // 7. Members tab: replied tag & HEXD inline SVG background (28x24.25, no 4px clip)
    await page.click('#tabbtn-members');
    await page.waitForSelector('#tab-members.active');
    const hexBg = await page.evaluate(() => {
      const el = document.querySelector('.o2-mrow[data-open-member="coordinator"] .o2-mav');
      return el ? getComputedStyle(el).backgroundImage : '';
    });
    assert.match(hexBg, /data:image\/svg\+xml/);
    assert.match(hexBg, /24\.25/);
    const repliedTags = await page.locator('[data-testid="swarm-member-replied"]').count();
    assert.ok(repliedTags >= 2, `Expected >=2 replied tags, got ${repliedTags}`);

    // 8. Board tab: normalized channels (#general -> main, #backend -> backend), "New" unread divider, #NaN-free reply
    await page.click('#tabbtn-feed');
    await page.waitForSelector('#tab-feed.active');
    const feedText = await page.textContent('#feed');
    assert.equal(feedText.includes('#NaN'), false, 'Expected zero #NaN in #feed');
    const hasNewDivider = await page.evaluate(() => {
      const divs = [...document.querySelectorAll('#feed .divider, #feed .new-line, #feed .unread-divider, #feed [data-testid="swarm-unread-divider"], #feed [data-testid="swarm-new-divider"]')];
      return divs.some((d) => /New/i.test(d.textContent || '')) || /New/.test(document.getElementById('feed')?.innerText || '');
    });
    assert.equal(hasNewDivider, true, 'Expected "New" unread divider in #feed');

    if (saveShots) {
      await page.waitForTimeout(250);
      await page.screenshot({ path: join(screenshotDir, 'board_channels_new.png'), fullPage: true });
    }

    await context.close();
    const realFailedRequests = obs.failedRequests.filter(
      (r) => !(r.reason === 'net::ERR_ABORTED' && /fonts\.g(?:static|oogleapis)\.com/.test(r.url)),
    );
    assert.deepEqual(realFailedRequests, [], `Failed network requests: ${JSON.stringify(realFailedRequests)}`);
    const realConsoleErrors = obs.consoleErrors.filter(
    (c) => !c.url.includes('/_sidecar/get-conversation-metadata') && !/Failed to resolve project ID/i.test(c.text),
  );
  assert.deepEqual(realConsoleErrors, [], `Console errors: ${JSON.stringify(realConsoleErrors)}`);
    assert.deepEqual(obs.pageErrors, [], `Page errors: ${JSON.stringify(obs.pageErrors)}`);
  }

  await verifyPhase3AtUrl(`${sidecarProc.baseUrl}/`, sidecarProc.baseUrl, true);
  await verifyPhase3AtUrl(
    `http://127.0.0.1:${proxyPort}${SUBPATH_PREFIX}/`,
    `http://127.0.0.1:${proxyPort}`,
    false,
  );
  assert.deepEqual(proxy.proxyLeaks, [], `Subpath proxy leaks: ${JSON.stringify(proxy.proxyLeaks)}`);
});

// ---------------------------------------------------------------------------
// #139 Regressions: SWARM_OVERVIEW=true #ov-mode, 1-frame reel, Hive Role workers,
// prefers-color-scheme: light, cleanAssignment() boilerplate, visible .msg-more
// ---------------------------------------------------------------------------

test('#139 regressions: #ov-mode with SWARM_OVERVIEW=true, 1-frame reel, Hive Role workers, light prefers-color-scheme, cleanAssignment, and visible .msg-more at / and subpath', async (t) => {
  const pw = await loadPlaywright();
  const executablePath = findBrowserExecutable();
  const browser = await pw.chromium.launch({
    headless: true,
    ...(executablePath ? { executablePath } : {}),
  });
  t.after(() => browser.close());

  const tempState = mkdtempSync(join(tmpdir(), 'hermes-139-ui-'));
  t.after(() => rmSync(tempState, { recursive: true, force: true }));

  const nowMs = Date.now();
  const longMessageText = Array.from({ length: 12 }, (_, i) => `Line ${i + 1}: Verification details for clamped message expansion and visible Show more pill affordance.`).join('\n');

  const snapshot139 = {
    swarm: 'swarm-139-ui',
    title: 'Phase-2 #139 Regression Swarm',
    etag: 'etag-139-v1',
    head: 3,
    primaryConversationId: 'conv-prim-139',
    coordinatorConversationId: 'conv-coord-139',
    member: 'coordinator',
    plan: {
      goal: { title: 'Verify #139 UI, Hive, and Narrator fixes', line: 'All #139 fixes verified' },
      threads: [
        { id: 't-backend', title: 'Backend API', owner: 'backend-dev', workstreams: ['ws-health'] },
      ],
      tasks: { 'ws-health': 'Implement /api/health' },
    },
    members: [
      {
        name: 'coordinator',
        role: 'coordinator',
        status: 'waiting',
        steps: 10,
      },
      {
        name: 'backend-dev',
        role: 'worker',
        status: 'working',
        steps: 7,
        assignment: "You are 'backend-dev' in swarm '12345678-1234-1234-1234-1234567890ab' (Tier 2): Implement the /api/health endpoint cleanly.",
      },
      {
        name: 'narrator',
        role: 'narrator',
        roleWorker: true,
        status: 'idle',
        assignment: 'Maintain Narrator overview',
      },
      {
        name: 'monitor',
        role: 'monitor',
        roleWorker: true,
        status: 'waiting',
        assignment: 'Monitor worker stalls',
      },
    ],
    frames: [
      { seq: 2, path: 'frames/step1.png', caption: 'Single progress frame' },
    ],
    view: {
      exists: true,
      htmlKey: 'v139-html',
      html: '<!doctype html><html><body><h1 id="narrator-h1">Narrator Custom Page</h1><script>parent.postMessage({type:"READY"},"*");parent.postMessage({type:"RENDERED",height:180},"*");</script></body></html>',
      json: {
        as_of_seq: 3,
        headline: 'Phase-2 #139 Check',
        goal: { title: 'Verify #139 UI, Hive, and Narrator fixes', line: 'All #139 fixes verified' },
        threads: [{ id: 't-backend', title: 'Backend API', workstreams: ['ws-health'], state: 'running' }],
        workstreams: [{ id: 'ws-health', title: 'Implement /api/health', state: 'running', assignees: ['backend-dev'] }],
      },
    },
    messages: [
      { seq: 1, id: 'm-1', ts: nowMs - 2000, agent: 'coordinator', sender: 'coordinator', type: 'post', text: '@backend-dev Implement /api/health', mentions: ['backend-dev'] },
      { seq: 2, id: 'm-2', ts: nowMs - 1000, agent: 'backend-dev', sender: 'backend-dev', type: 'status', text: 'Posted single progress frame' },
      { seq: 3, id: 'm-3', ts: nowMs, agent: 'backend-dev', sender: 'backend-dev', type: 'post', text: longMessageText },
    ],
  };

  const upstreamServer = createServer((req, res) => {
    const reqUrl = new URL(req.url, 'http://127.0.0.1');
    if (reqUrl.pathname === '/board') {
      res.writeHead(200, { 'Content-Type': 'application/json', ETag: snapshot139.etag });
      res.end(JSON.stringify(snapshot139));
      return;
    }
    if (reqUrl.pathname === '/board/events') {
      res.writeHead(200, { 'Content-Type': 'text/event-stream; charset=utf-8' });
      res.write(`event: snapshot\ndata: ${JSON.stringify(snapshot139)}\n\n`);
      return;
    }
    if (reqUrl.pathname === '/board/frame') {
      res.writeHead(200, { 'Content-Type': 'image/png' });
      res.end(PNG_1X1);
      return;
    }
    res.writeHead(404);
    res.end('Not found');
  });
  const upstreamPort = await listenServer(upstreamServer);
  t.after(() => closeServer(upstreamServer));

  writeFileSync(
    join(tempState, 'registry.json'),
    JSON.stringify({
      servers: {
        'swarm-139-ui': {
          pid: process.pid,
          httpUrl: `http://127.0.0.1:${upstreamPort}`,
          swarmId: 'swarm-139-ui',
          primaryConversationId: 'conv-prim-139',
          coordinatorConversationId: 'conv-coord-139',
        },
      },
    }),
    'utf-8',
  );

  // SWARM_OVERVIEW=true (builtinOverview: true) + canFrame: true
  const sidecarProc = await spawnSidecarProcess({
    HERMES_A2A_STATE_DIR: tempState,
    ANTIGRAVITY_CONVERSATION_ID: 'conv-prim-139',
    HERMES_SWARM_DISABLE_WAKEUP: '1',
    SWARM_OVERVIEW: 'true',
  });
  t.after(() => sidecarProc.stop());

  const SUBPATH_PREFIX = '/remote-proxy/plugin/hermes-swarm/swarm_board';
  const proxy = createSubpathProxy(sidecarProc.port, SUBPATH_PREFIX);
  const proxyPort = await listenServer(proxy.server);
  t.after(() => closeServer(proxy.server));

  async function verify139AtUrl(pageUrl, originPrefix) {
    const context = await browser.newContext({ viewport: { width: 1280, height: 860 }, colorScheme: 'dark' });
    const page = await context.newPage();
    const obs = attachPageObservers(page, originPrefix);

    await page.goto(pageUrl, { waitUntil: 'networkidle' });
    await page.waitForSelector('#board:not(.hidden)', { timeout: 10000 });
    await page.click('#tabbtn-overview');
    await page.waitForSelector('#tab-overview.active', { timeout: 5000 });

    // 1. Overview stays strictly ops-only: zero .o2-reel / <img> even when frames exist in snapshot
    assert.equal(await page.locator('#tab-overview .o2-reel').count(), 0, 'Overview must omit .o2-reel');
    assert.equal(await page.locator('#tab-overview img').count(), 0, 'Overview must omit result images');

    // 2. #ov-mode ("Narrator's page") is visible/clickable even when SWARM_OVERVIEW=true + canFrame=true
    await page.waitForSelector('#ov-mode:not(.hidden)', { timeout: 3000 });
    assert.match(await page.textContent('#ov-mode'), /Narrator/i);
    await page.click('#ov-mode');
    await page.waitForSelector('#ov-frame:not(.hidden) iframe[data-testid="swarm-overview-iframe"]', { timeout: 5000 });
    // Toggle back to Overview so plain view remains clean
    await page.click('#ov-mode');
    await page.waitForSelector('#ov-plain:not(.hidden)', { timeout: 5000 });
    await page.evaluate(() => localStorage.removeItem('swarm_board.narrator_plain'));

    // 3. Unhomed roleWorkers (narrator/monitor) render in a Hive 'Role workers' hex
    await page.click('#tabbtn-graph');
    await page.waitForSelector('#tab-graph.active', { timeout: 5000 });
    await page.waitForFunction(
      () => /Role workers/i.test(document.getElementById('tab-graph')?.textContent || ''),
      null,
      { timeout: 5000 },
    );

    // 4. emulateMedia({ colorScheme: 'light' }) switches standalone UI to light palette cleanly
    await page.emulateMedia({ colorScheme: 'light' });
    await page.waitForFunction(() => document.documentElement.dataset.theme === 'light', null, { timeout: 3000 });
    const lightBodyBg = await page.evaluate(() => getComputedStyle(document.body).backgroundColor);
    assert.ok(relLuminance(parseRgb(lightBodyBg)) > 0.7, `Expected light body background, got ${lightBodyBg}`);
    await page.emulateMedia({ colorScheme: 'dark' });
    await page.waitForFunction(() => document.documentElement.dataset.theme === 'dark', null, { timeout: 3000 });

    // 5. cleanAssignment() strips "You are '<name>' in swarm '<id>'..." boilerplate in Members cards
    await page.click('#tabbtn-members');
    await page.waitForSelector('#tab-members.active', { timeout: 5000 });
    const membersText = await page.textContent('#tab-members');
    assert.match(membersText, /Implement the \/api\/health endpoint cleanly/);
    assert.equal(membersText.includes("You are 'backend-dev' in swarm"), false, 'Boilerplate must be stripped');

    // 6. Clamped Board messages show a visible .msg-more affordance (not sr-only)
    await page.click('#tabbtn-feed');
    await page.waitForSelector('#tab-feed.active', { timeout: 5000 });
    await page.waitForSelector('#feed .msg-more:not(.hidden)', { timeout: 5000 });
    const moreBtn = page.locator('#feed .msg-more:not(.hidden)').first();
    const moreBox = await moreBtn.boundingBox();
    assert.ok(moreBox && moreBox.width > 12 && moreBox.height > 12, `Expected visible .msg-more pill, got ${JSON.stringify(moreBox)}`);
    const moreStyle = await moreBtn.evaluate((el) => {
      const s = getComputedStyle(el);
      return { display: s.display, visibility: s.visibility, clip: s.clip, clipPath: s.clipPath };
    });
    assert.notEqual(moreStyle.display, 'none');
    assert.notEqual(moreStyle.visibility, 'hidden');
    assert.equal(moreStyle.clipPath, 'none');
    await moreBtn.click();
    assert.equal(await moreBtn.getAttribute('aria-expanded'), 'true');

    await context.close();
    const realFailedRequests = obs.failedRequests.filter(
      (r) => !(r.reason === 'net::ERR_ABORTED' && /fonts\.g(?:static|oogleapis)\.com/.test(r.url)),
    );
    assert.deepEqual(realFailedRequests, [], `Failed network requests: ${JSON.stringify(realFailedRequests)}`);
    const realConsoleErrors = obs.consoleErrors.filter(
    (c) => !c.url.includes('/_sidecar/get-conversation-metadata') && !/Failed to resolve project ID/i.test(c.text),
  );
  assert.deepEqual(realConsoleErrors, [], `Console errors: ${JSON.stringify(realConsoleErrors)}`);
    assert.deepEqual(obs.pageErrors, [], `Page errors: ${JSON.stringify(obs.pageErrors)}`);
  }

  await verify139AtUrl(`${sidecarProc.baseUrl}/`, sidecarProc.baseUrl);
  await verify139AtUrl(
    `http://127.0.0.1:${proxyPort}${SUBPATH_PREFIX}/`,
    `http://127.0.0.1:${proxyPort}`,
  );
  assert.deepEqual(proxy.proxyLeaks, [], `Subpath proxy leaks: ${JSON.stringify(proxy.proxyLeaks)}`);
});

test('#161 regressions: anti-flicker Reconnecting recovery, honest telemetry, coordinator read/post, active-subagent stuck suppression, sanitizeView, and 4 screenshots at / and subpath', async (t) => {
  const pw = await loadPlaywright();
  const executablePath = findBrowserExecutable();
  const browser = await pw.chromium.launch({
    headless: true,
    ...(executablePath ? { executablePath } : {}),
  });
  t.after(() => browser.close());

  const tempState = mkdtempSync(join(tmpdir(), 'hermes-ui-161-'));
  t.after(() => rmSync(tempState, { recursive: true, force: true }));

  const screenshotDir = join(tempState, 'screenshots');
  mkdirSync(screenshotDir, { recursive: true });

  const nowMs = Date.now();
  let boardMode = 'ok';
  let etagCounter = 1;

  function makeSnapshot161(etagVal) {
    return {
      swarm: 'swarm-161-ui',
      id: 'swarm-161-ui',
      title: 'Phase-2 #161 Verification',
      etag: etagVal,
      primaryConversationId: 'conv-prim-161',
      coordinatorConversationId: 'conv-coord-161',
      builtinOverview: true,
      graphTab: true,
      rawTab: true,
      urlHosts: ['127.0.0.1'],
      telemetry: {
        tokens: { new: 1190000, cacheRead: 4500000, total: 5690000 },
        contextUsed: 0,
        contextWindow: 131072,
        costUsd: null,
      },
      systemAttention: [
        {
          id: 'sys-backend-dev-stuck',
          member: 'backend-dev',
          kind: 'stuck',
          detail: 'Tool running > 10m',
        },
      ],
      members: [
        {
          name: 'coordinator',
          role: 'coordinator',
          status: 'waiting',
          conversationId: 'conv-coord-161',
          posts: 0,
          lastPostSeq: 2,
          lastReadSeq: 7,
        },
        {
          name: 'backend-dev',
          role: 'worker',
          status: 'working',
          model: 'claude-sonnet-4.6',
          assignment: 'Implement anti-flicker & telemetry',
          tokens: { new: 1190000, cacheRead: 4500000, total: 5690000 },
          contextUsed: 0,
          contextWindow: 131072,
          costUsd: null,
          currentTool: { name: 'delegate_task', startedAt: new Date(nowMs - 900_000).toISOString() },
          recentTools: [{ name: 'read_file', ok: true, durationMs: 120 }],
          subagents: [
            {
              id: 'sub-1',
              name: 'subagent-1',
              status: 'working',
              task: 'Running bounded verification',
              updatedAt: new Date(nowMs - 5_000).toISOString(),
            },
          ],
          posts: 2,
          lastPostSeq: 3,
          lastReadSeq: 7,
        },
        {
          name: 'narrator',
          role: 'narrator',
          roleWorker: true,
          status: 'waiting',
          assignment: 'Summarize progress',
        },
      ],
      frames: [
        { seq: 2, path: 'frames/step1.png', caption: 'Step 1 frame' },
        { seq: 3, path: 'frames/step2.png', caption: 'Step 2 shot' },
      ],
      view: {
        exists: true,
        htmlKey: 'v161-html',
        html: `<!doctype html><html><body>
          <h1 id="narrator-h1">Narrator #161</h1>
          <div id="narrator-summary"></div>
          <script>
            window.__narratorData = null;
            if (window.narrator && typeof window.narrator.onData === 'function') {
              window.narrator.onData(function(v) {
                window.__narratorData = v;
                document.getElementById('narrator-summary').textContent = v && v.running_summary ? v.running_summary : '';
                if (window.narrator.rendered) window.narrator.rendered({ as_of_seq: v && v.as_of_seq });
              });
            }
          </script>
        </body></html>`,
        json: {
          as_of_seq: 4,
          headline: 'Phase-2 #161 Check',
          running_summary: 'Phase 2 verification running smoothly.',
          goal: { title: 'Verify #161 anti-flicker & telemetry', line: 'All #161 fixes verified' },
          threads: [{ id: 't-1', title: 'Sidecar & UI', workstreams: ['ws-1'], state: 'running' }],
          workstreams: [{ id: 'ws-1', title: 'Anti-flicker & Telemetry', state: 'running', assignees: ['backend-dev'] }],
          reel: {
            frames: [{ path: 'frames/step1.png', seq: 2, caption: 'Step 1 frame' }],
          },
          screenshots: [{ path: 'frames/step2.png', seq: 3, caption: 'Step 2 shot' }],
        },
      },
      channels: ['general', 'reviews'],
      unreadByChannel: { general: 1 },
      messages: [
        { seq: 1, id: 'm-1', ts: nowMs - 4000, agent: 'coordinator', sender: 'coordinator', type: 'control', channel: 'general', control: { action: 'init' }, text: 'Initialized swarm' },
        { seq: 2, id: 'm-2', ts: nowMs - 3000, agent: 'coordinator', sender: 'coordinator', type: 'control', channel: 'general', control: { action: 'add', target: 'backend-dev' }, text: 'Added backend-dev' },
        { seq: 3, id: 'm-3', ts: nowMs - 2000, agent: 'backend-dev', sender: 'backend-dev', type: 'post', channel: 'general', text: 'Posted Step 1 frame' },
        { seq: 4, id: 'm-4', ts: nowMs - 1000, agent: 'backend-dev', sender: 'backend-dev', type: 'post', channel: 'reviews', text: 'Ready for review' },
      ],
    };
  }

  const upstreamServer = createServer((req, res) => {
    const reqUrl = new URL(req.url, 'http://127.0.0.1');
    if (reqUrl.pathname === '/board') {
      if (boardMode === '503') {
        res.writeHead(503, { 'Content-Type': 'text/plain' });
        res.end('Upstream unavailable');
        return;
      }
      const snap = makeSnapshot161(`etag-161-${etagCounter}`);
      res.writeHead(200, { 'Content-Type': 'application/json', ETag: snap.etag });
      res.end(JSON.stringify(snap));
      return;
    }
    if (reqUrl.pathname === '/board/events') {
      res.writeHead(200, { 'Content-Type': 'text/event-stream; charset=utf-8' });
      const snap = makeSnapshot161(`etag-161-${etagCounter}`);
      res.write(`event: snapshot\ndata: ${JSON.stringify(snap)}\n\n`);
      return;
    }
    if (reqUrl.pathname === '/board/frame') {
      res.writeHead(200, { 'Content-Type': 'image/png' });
      res.end(PNG_1X1);
      return;
    }
    res.writeHead(404);
    res.end('Not found');
  });
  const upstreamPort = await listenServer(upstreamServer);
  t.after(() => closeServer(upstreamServer));

  writeFileSync(
    join(tempState, 'registry.json'),
    JSON.stringify({
      servers: {
        'swarm-161-ui': {
          pid: process.pid,
          httpUrl: `http://127.0.0.1:${upstreamPort}`,
          swarmId: 'swarm-161-ui',
          primaryConversationId: 'conv-prim-161',
          coordinatorConversationId: 'conv-coord-161',
        },
      },
    }),
    'utf-8',
  );

  const sidecarProc = await spawnSidecarProcess({
    HERMES_A2A_STATE_DIR: tempState,
    ANTIGRAVITY_CONVERSATION_ID: 'conv-prim-161',
    HERMES_SWARM_DISABLE_WAKEUP: '1',
    SWARM_OVERVIEW: 'true',
  });
  t.after(() => sidecarProc.stop());

  const SUBPATH_PREFIX = '/remote-proxy/plugin/hermes-swarm/swarm_board';
  const proxy = createSubpathProxy(sidecarProc.port, SUBPATH_PREFIX);
  const proxyPort = await listenServer(proxy.server);
  t.after(() => closeServer(proxy.server));

  async function verify161AtUrl(pageUrl, originPrefix, saveScreenshots) {
    boardMode = 'ok';
    etagCounter += 1;
    const context = await browser.newContext({ viewport: { width: 1280, height: 860 }, colorScheme: 'dark' });
    const page = await context.newPage();
    const obs = attachPageObservers(page, originPrefix);

    await page.goto(pageUrl, { waitUntil: 'networkidle' });
    await page.waitForSelector('#board:not(.hidden)', { timeout: 10000 });

    // 1. Honest telemetry: 1.19M new + cacheRead tooltip, ctx % hidden when contextUsed == 0, costUsd == null omitted (not $0.00)
    await page.click('#tabbtn-members');
    await page.waitForSelector('#tab-members.active', { timeout: 5000 });
    const telemetryCheck = await page.evaluate(() => {
      const strip = document.getElementById('telemetry-strip')?.textContent || '';
      const members = document.getElementById('tab-members')?.textContent || '';
      const tipEls = [...document.querySelectorAll('[title]')].map((el) => el.getAttribute('title') || '');
      const hasProcTok = /5\.69M proc/.test(members);
      const hasCacheTip = tipEls.some((t) => /Processed: 5\.69M/.test(t) && /Cached: 4\.50M/.test(t));
      const hasZeroCost = /\$0\.00/.test(strip) || /\$0\.00/.test(members);
      const hasZeroCtx = /\b0%\s*ctx\b/i.test(strip) || /\b0%\s*ctx\b/i.test(members);
      return { hasProcTok, hasCacheTip, hasZeroCost, hasZeroCtx };
    });
    assert.equal(telemetryCheck.hasProcTok, true, 'Must display totalProcessed/total (5.69M proc)');
    assert.equal(telemetryCheck.hasCacheTip, true, 'Must include Processed/Cached tooltip');
    assert.equal(telemetryCheck.hasZeroCost, false, 'costUsd == null must not render $0.00');
    assert.equal(telemetryCheck.hasZeroCtx, false, 'contextUsed == 0 must hide ctx %');

    // 2. Coordinator read/post & active-subagent stuck suppression
    const coordAndStuck = await page.evaluate(() => {
      const membersText = document.getElementById('tab-members')?.textContent || '';
      const ovText = document.getElementById('tab-overview')?.textContent || '';
      const hasCoordRead = /read #7/.test(membersText) || /read #7/.test(ovText);
      const hasCoordPostSeq = /#2/.test(membersText) || /#2/.test(ovText);
      const hasStuckBadge = [...document.querySelectorAll('#tab-members .o2-mstat.bad, #tab-overview .o2-mstat.bad')]
        .some((el) => /Stuck/i.test(el.textContent || ''));
      const hasStuckAlert = [...document.querySelectorAll('.o2-item')]
        .some((el) => /backend-dev looks stuck/i.test(el.textContent || ''));
      return { hasCoordRead, hasCoordPostSeq, hasStuckBadge, hasStuckAlert };
    });
    assert.equal(coordAndStuck.hasCoordRead, true, 'Coordinator row must render read #7');
    assert.equal(coordAndStuck.hasCoordPostSeq, true, 'Coordinator row must render #2 fallback');
    assert.equal(coordAndStuck.hasStuckBadge, false, 'Active Tier-3 subagent must suppress member Stuck badge');
    assert.equal(coordAndStuck.hasStuckAlert, false, 'Active Tier-3 subagent must suppress stuck system alert');

    // 3. Save 4 fresh screenshots (overview_dark, members_light_expanded, board_channels_new, graph_hive_dark)
    if (saveScreenshots) {
      await page.click('#tabbtn-overview');
      await page.waitForSelector('#tab-overview.active', { timeout: 5000 });
      await page.waitForTimeout(260);
      await page.screenshot({ path: join(screenshotDir, 'overview_dark.png') });

      await page.emulateMedia({ colorScheme: 'light' });
      await page.waitForFunction(() => document.documentElement.dataset.theme === 'light', null, { timeout: 3000 });
      await page.click('#tabbtn-members');
      await page.waitForSelector('#tab-members.active', { timeout: 5000 });
      await page.waitForTimeout(260);
      await page.screenshot({ path: join(screenshotDir, 'members_light_expanded.png') });

      await page.click('#tabbtn-feed');
      await page.waitForSelector('#tab-feed.active', { timeout: 5000 });
      await page.waitForTimeout(260);
      await page.screenshot({ path: join(screenshotDir, 'board_channels_new.png') });

      await page.emulateMedia({ colorScheme: 'dark' });
      await page.waitForFunction(() => document.documentElement.dataset.theme === 'dark', null, { timeout: 3000 });
      await page.click('#tabbtn-graph');
      await page.waitForSelector('#tab-graph.active', { timeout: 5000 });
      await page.waitForTimeout(260);
      await page.screenshot({ path: join(screenshotDir, 'graph_hive_dark.png') });
    }

    // 4. sanitizeView preservation in Narrator view.html iframe
    await page.click('#tabbtn-overview');
    await page.waitForSelector('#tab-overview.active', { timeout: 5000 });
    await page.click('#ov-mode');
    await page.waitForSelector('#ov-frame:not(.hidden) iframe[data-testid="swarm-overview-iframe"]', { timeout: 5000 });
    const iframeHandle = await page.locator('#ov-frame iframe[data-testid="swarm-overview-iframe"]').elementHandle();
    const iframeFrame = await iframeHandle.contentFrame();
    await iframeFrame.waitForFunction(() => Boolean(window.__narratorData), null, { timeout: 5000 });
    const narratorView = await iframeFrame.evaluate(() => window.__narratorData);
    assert.equal(narratorView.running_summary, 'Phase 2 verification running smoothly.');
    assert.equal(narratorView.threads.length, 1);
    assert.equal(narratorView.threads[0].title, 'Sidecar & UI');
    assert.equal(narratorView.reel.frames.length, 1);
    assert.equal(narratorView.reel.frames[0].path, 'frames/step1.png');
    assert.equal(narratorView.screenshots.length, 1);
    assert.equal(narratorView.screenshots[0].path, 'frames/step2.png');

    await page.click('#ov-mode');
    await page.waitForSelector('#ov-plain:not(.hidden)', { timeout: 5000 });
    await page.evaluate(() => localStorage.removeItem('swarm_board.narrator_plain'));

    // 5. Anti-flicker proof: inject 2 consecutive transient /board failures (1st: {online:false, transientError:true, swarm:null}, 2nd: HTTP 503)
    await page.click('#tabbtn-feed');
    await page.waitForSelector('#tab-feed.active', { timeout: 5000 });
    await page.waitForSelector('#feed .msg', { timeout: 5000 });
    await page.evaluate(() => {
      window.__msgRef161 = document.querySelector('#feed .msg');
      const tab = document.getElementById('tab-feed');
      tab?.classList.remove('tab-fade');
    });

    let interceptedTransient = 0;
    await page.route('**/api/swarm*', async (route) => {
      if (interceptedTransient < 1) {
        interceptedTransient += 1;
        await route.fulfill({
          status: 200,
          contentType: 'application/json',
          body: JSON.stringify({ online: false, transientError: true, swarm: null, conversationId: 'conv-prim-161' }),
        });
        return;
      }
      await route.continue();
    });

    boardMode = '503';
    await page.waitForFunction(
      () => document.getElementById('live-label')?.textContent === 'Reconnecting',
      null,
      { timeout: 10000 },
    );

    // Wait for at least one more poll under HTTP 503
    await page.waitForTimeout(1600);
    const duringOutage = await page.evaluate(() => ({
      boardHidden: document.getElementById('board')?.classList.contains('hidden'),
      emptyHidden: document.getElementById('empty')?.classList.contains('hidden'),
      loadingHidden: document.getElementById('loading')?.classList.contains('hidden'),
      liveLabel: document.getElementById('live-label')?.textContent,
      msgConnected: window.__msgRef161?.isConnected === true,
    }));
    assert.equal(duringOutage.boardHidden, false, '#board must stay visible during transient errors');
    assert.equal(duringOutage.emptyHidden, true, '#empty must NEVER flash during transient errors');
    assert.equal(duringOutage.loadingHidden, true, '#loading must NEVER flash during transient errors');
    assert.equal(duringOutage.liveLabel, 'Reconnecting');
    assert.equal(duringOutage.msgConnected, true, 'Existing DOM must not be destroyed during outage');

    // Recover cleanly on next 200 without re-triggering .tab-fade or destroying DOM
    boardMode = 'ok';
    etagCounter += 1;
    await page.waitForFunction(
      () => document.getElementById('live-label')?.textContent === 'Live',
      null,
      { timeout: 10000 },
    );
    const afterRecovery = await page.evaluate(() => ({
      boardHidden: document.getElementById('board')?.classList.contains('hidden'),
      hasTabFade: document.getElementById('tab-feed')?.classList.contains('tab-fade'),
      msgConnected: window.__msgRef161?.isConnected === true,
    }));
    assert.equal(afterRecovery.boardHidden, false);
    assert.equal(afterRecovery.hasTabFade, false, 'Recovery must NOT re-trigger .tab-fade');
    assert.equal(afterRecovery.msgConnected, true, 'Recovery must NOT destroy existing DOM');

    await context.close();
    const realFailedRequests = obs.failedRequests.filter(
      (r) => !(r.reason === 'net::ERR_ABORTED' && /fonts\.g(?:static|oogleapis)\.com/.test(r.url)),
    );
    assert.deepEqual(realFailedRequests, [], `Failed network requests: ${JSON.stringify(realFailedRequests)}`);
    const realConsoleErrors = obs.consoleErrors.filter(
    (c) => !c.url.includes('/_sidecar/get-conversation-metadata') && !/Failed to resolve project ID/i.test(c.text),
  );
  assert.deepEqual(realConsoleErrors, [], `Console errors: ${JSON.stringify(realConsoleErrors)}`);
    assert.deepEqual(obs.pageErrors, [], `Page errors: ${JSON.stringify(obs.pageErrors)}`);
  }

  await verify161AtUrl(`${sidecarProc.baseUrl}/`, sidecarProc.baseUrl, true);
  await verify161AtUrl(
    `http://127.0.0.1:${proxyPort}${SUBPATH_PREFIX}/`,
    `http://127.0.0.1:${proxyPort}`,
    false,
  );
  assert.deepEqual(proxy.proxyLeaks, [], `Subpath proxy leaks: ${JSON.stringify(proxy.proxyLeaks)}`);
});

// ---------------------------------------------------------------------------
// Phase-2 Hardening Regressions (#189): md() XSS, poll() gen guard, hive.js, channels.mjs
// ---------------------------------------------------------------------------

test('#189 regressions: channels.mjs create -> archive -> create re-activates archived:false', async () => {
  const { channelRegistry } = await import(pathToFileURL(join(BOARD_UI_DIR, 'channels.mjs')).href);
  const msgs = [
    { seq: 1, agent: 'coordinator', control: { op: 'create', channel: 'dev', topic: 'initial' } },
    { seq: 2, agent: 'coordinator', control: { op: 'archive', channel: 'dev' } },
    { seq: 3, agent: 'coordinator', control: { op: 'create', channel: 'dev', topic: 'reopened' } },
  ];
  const reg = channelRegistry(msgs);
  const dev = reg.find((c) => c.name === 'dev');
  assert.ok(dev, 'dev channel must exist');
  assert.equal(dev.archived, false, 'create -> archive -> create must re-activate archived: false');
  assert.equal(dev.topic, 'reopened');
  assert.equal(dev.created_by, 'coordinator');
});

test('#189 regressions: hive.js __proto__ safety, 0 of 0 for 0 workstreams, md() attribute breakout immunity, poll() gen guard, and #o2-msin focus across 1s polls at / and subpath', async (t) => {
  const pw = await loadPlaywright();
  const executablePath = findBrowserExecutable();
  const browser = await pw.chromium.launch({
    headless: true,
    args: ['--no-proxy-server', '--proxy-bypass-list=<-loopback>'],
    ...(executablePath ? { executablePath } : {}),
  });
  t.after(() => browser.close());

  const tempState = mkdtempSync(join(tmpdir(), 'hermes-189-ui-'));
  t.after(() => rmSync(tempState, { recursive: true, force: true }));

  let etagCounter = 1;
  let activeConv = 'conv-189-a';
  let delayConvA = 0;

  const upstreamServer = createServer(async (req, res) => {
    const reqUrl = new URL(req.url, 'http://127.0.0.1');
    if (reqUrl.pathname === '/board') {
      const conv = reqUrl.searchParams.get('conversationId') || activeConv;
      if (conv === 'conv-189-slow' && delayConvA > 0) {
        await new Promise((r) => setTimeout(r, delayConvA));
      }
      if (conv === 'conv-189-b') {
        res.writeHead(200, { 'Content-Type': 'application/json', ETag: `189-b-${etagCounter}` });
        res.end(JSON.stringify({
          swarm: 'swarm-189-b',
          id: 'swarm-189-b',
          etag: `189-b-${etagCounter}`,
          head: 1,
          title: 'Swarm B Fresh',
          primaryConversationId: 'conv-189-b',
          coordinatorConversationId: 'conv-189-b-coord',
          members: [{ name: 'coordinator', role: 'coordinator', status: 'waiting', steps: 1, posts: 1 }],
          messages: [{ seq: 1, agent: 'coordinator', role: 'coordinator', text: 'Swarm B message', time: '2026-11-23T01:00:00Z' }],
        }));
        return;
      }

      const step = etagCounter++;
      res.writeHead(200, { 'Content-Type': 'application/json', ETag: `189-a-${step}` });
      res.end(JSON.stringify({
        swarm: 'swarm-189-a',
        id: 'swarm-189-a',
        etag: `189-a-${step}`,
        head: 2,
        title: 'Swarm 189 #1 @coder',
        primaryConversationId: conv,
        coordinatorConversationId: 'conv-189-coord',
        plan: {
          goal: { title: 'Zero workstream test', line: 'Verify 0 of 0 and focus' },
          threads: [],
          tasks: {},
        },
        members: [
          { name: 'coordinator', role: 'coordinator', status: 'waiting', conversationId: 'conv-189-coord', steps: 1, posts: 1 },
          { name: 'coder', role: 'worker', status: 'working #1 @coordinator', now: 'Working on #1 @coordinator', steps: step, posts: 1 },
          { name: '__proto__', role: 'worker', status: 'waiting', now: 'Proto safety', steps: 1, posts: 0 },
        ],
        messages: [
          {
            seq: 1,
            agent: 'coordinator',
            role: 'coordinator',
            text: 'Ping @coder about #1',
            time: '2026-11-23T01:00:00Z',
          },
          {
            seq: 2,
            agent: 'coder',
            role: 'worker',
            text: '@coordinator working on #1',
            time: '2026-11-23T01:00:05Z',
          },
        ],
      }));
      return;
    }
    res.writeHead(404);
    res.end('Not found');
  });

  const upstreamPort = await listenServer(upstreamServer);
  t.after(() => closeServer(upstreamServer));

  const sidecarProc = await spawnSidecarProcess({
    HERMES_A2A_STATE_DIR: tempState,
    HERMES_A2A_BOARD_URL: `http://127.0.0.1:${upstreamPort}`,
    ANTIGRAVITY_CONVERSATION_ID: 'conv-189-a',
    HERMES_SWARM_DISABLE_WAKEUP: '1',
  });
  t.after(() => sidecarProc.stop());

  const SUBPATH_PREFIX = '/remote-proxy/plugin/hermes-swarm/swarm_board';
  const proxy = createSubpathProxy(sidecarProc.port, SUBPATH_PREFIX);
  const proxyPort = await listenServer(proxy.server);
  t.after(() => closeServer(proxy.server));

  async function verify189AtUrl(pageUrl, originPrefix) {
    const context = await browser.newContext({ viewport: { width: 1280, height: 860 } });
    const page = await context.newPage();
    await page.route('**/_sidecar/get-conversation-metadata', (route) =>
      route.fulfill({ status: 200, contentType: 'application/json', body: '{}' }),
    );
    const obs = attachPageObservers(page, originPrefix);

    await page.goto(pageUrl, { waitUntil: 'networkidle' });
    await page.waitForSelector('#board:not(.hidden)', { timeout: 10000 });

    // 1) md() XSS / attribute breakout immunity: @coder mention title="coder · working #1 @coordinator" must NOT contain injected <button>/<span>
    const mdSafety = await page.evaluate(() => {
      const mention = document.querySelector('#feed .mention');
      const titleAttr = mention?.getAttribute('title') || '';
      const hasInjectedTagInTitle = /<\s*(span|button)\b/i.test(titleAttr);
      const hasRawButtonTextInTitle = titleAttr.includes('class="ref') || titleAttr.includes('class="mention');
      return {
        hasMention: Boolean(mention),
        titleAttr,
        hasInjectedTagInTitle,
        hasRawButtonTextInTitle,
        protoPolluted: ({}).polluted !== undefined,
        feedHasSeqBtn: document.querySelectorAll('#feed [data-testid="swarm-seq-ref"]').length > 0,
      };
    });
    assert.equal(mdSafety.hasMention, true, 'Mention span must render');
    assert.match(mdSafety.titleAttr, /#1/, 'Mention title must preserve #1 text');
    assert.equal(mdSafety.hasInjectedTagInTitle, false, `title attr must not contain HTML tags: ${mdSafety.titleAttr}`);
    assert.equal(mdSafety.hasRawButtonTextInTitle, false, `title attr must not break out: ${mdSafety.titleAttr}`);
    assert.equal(mdSafety.protoPolluted, false, '__proto__ member must not pollute Object.prototype');
    assert.equal(mdSafety.feedHasSeqBtn, true, 'Real text-node #1 citations must still render');

    // 2) Overview 0 of 0 (not 0 of 1) for 0 workstreams + #o2-msin focus preserved across live 1s polls
    await page.click('#tabbtn-overview');
    await page.waitForSelector('#tab-overview.active');
    const overviewText = await page.textContent('#tab-overview');
    assert.match(overviewText, /\b0 of 0\b/, `Expected '0 of 0' for 0 workstreams, got: ${overviewText}`);
    assert.doesNotMatch(overviewText, /\b0 of 1\b/, 'Must NOT show 0 of 1 when 0 workstreams exist');

    // Switch to Members tab (or focus .o2-msin in overview/members), type query, and wait across >2 live 1s polls
    await page.click('#tabbtn-members');
    await page.waitForSelector('#tab-members.active');
    const msin = page.locator('#members .o2-msin');
    await msin.click();
    await msin.fill('cod');
    await page.waitForTimeout(2300);
    const focusAfterPolls = await page.evaluate(() => {
      const ae = document.activeElement;
      return {
        isMsin: Boolean(ae && ae.classList && ae.classList.contains('o2-msin')),
        value: ae ? ae.value : null,
      };
    });
    assert.equal(focusAfterPolls.isMsin, true, '#o2-msin focus must be preserved across live 1s polls');
    assert.equal(focusAfterPolls.value, 'cod', '#o2-msin value must be preserved across live 1s polls');

    // 3) poll() out-of-order generation guard via conversation-switch / slow response
    delayConvA = 800;
    await page.evaluate(() => {
      window.sidecar = window.sidecar || {};
      window.sidecar.conversationId = 'conv-189-slow';
      document.dispatchEvent(new Event('visibilitychange'));
    });
    await page.waitForTimeout(80);
    await page.evaluate(() => {
      window.sidecar.conversationId = 'conv-189-b';
      document.dispatchEvent(new Event('visibilitychange'));
    });
    await page.waitForFunction(
      () => (document.getElementById('swarm-title')?.textContent || '').trim() === 'Swarm B Fresh',
      null,
      { timeout: 5000 },
    );
    // Wait past conv-189-slow's 800ms delay and verify stale slow response does NOT overwrite Swarm B
    await page.waitForTimeout(950);
    const titleAfterSlow = (await page.textContent('#swarm-title')).trim();
    assert.equal(titleAfterSlow, 'Swarm B Fresh', 'Stale slow poll response must be ignored by generation guard');

    await context.close();
    const realFailed = obs.failedRequests.filter(
      (r) => !(r.reason === 'net::ERR_ABORTED' && /fonts\.g(?:static|oogleapis)\.com/.test(r.url))
        && !(r.reason === 'net::ERR_ABORTED' && r.url.includes('conv-189-slow'))
        && !r.url.includes('/_sidecar/get-conversation-metadata'),
    );
    assert.deepEqual(realFailed, [], `Failed network requests: ${JSON.stringify(realFailed)}`);
    const realConsoleErrors = obs.consoleErrors.filter(
    (c) => !c.url.includes('/_sidecar/get-conversation-metadata') && !/Failed to resolve project ID/i.test(c.text),
  );
  assert.deepEqual(realConsoleErrors, [], `Console errors: ${JSON.stringify(realConsoleErrors)}`);
    assert.deepEqual(obs.pageErrors, [], `Page errors: ${JSON.stringify(obs.pageErrors)}`);
  }

  await verify189AtUrl(`${sidecarProc.baseUrl}/?conversationId=conv-189-a`, sidecarProc.baseUrl);
  await verify189AtUrl(
    `http://127.0.0.1:${proxyPort}${SUBPATH_PREFIX}/?conversationId=conv-189-a`,
    `http://127.0.0.1:${proxyPort}`,
  );
  assert.deepEqual(proxy.proxyLeaks, [], `Subpath proxy leaks: ${JSON.stringify(proxy.proxyLeaks)}`);
});

test('Phase-3 v3 UI: 400+ msg windowing & #seq jump, billable token economy, live agent history tabs, and zero forbidden terms', async (t) => {
  // e) Forbidden terms check across UI and sidecar files (excluding URLs like w3.org)
  const forbiddenRe = /\b(?:owner|tier1|cto|company|org|senior|junior|queen)\b/i;
  const filesToCheck = [
    MAIN_MJS_PATH,
    join(BOARD_UI_DIR, 'index.html'),
    join(BOARD_UI_DIR, 'styles.css'),
    join(BOARD_UI_DIR, 'hive.css'),
    join(BOARD_UI_DIR, 'app.js'),
    join(BOARD_UI_DIR, 'hive.js'),
  ];
  for (const fp of filesToCheck) {
    const raw = readFileSync(fp, 'utf-8');
    const withoutUrls = raw.replace(/https?:\/\/[^\s'"`<>)]+/gi, '');
    const m = forbiddenRe.exec(withoutUrls);
    assert.equal(m, null, `Forbidden term "${m?.[0]}" found in ${fp}`);
  }

  const tempState = mkdtempSync(join(tmpdir(), 'hermes-ui-v3-'));
  t.after(() => rmSync(tempState, { recursive: true, force: true }));

  const totalMessages = 420;
  const messages = [];
  const baseTs = Date.UTC(2026, 10, 27, 9, 0, 0);
  for (let i = 1; i <= totalMessages; i += 1) {
    const sender = i % 3 === 0 ? 'coordinator' : (i % 2 === 0 ? 'backend' : 'ui');
    messages.push({
      seq: i,
      id: `v3-msg-${i}`,
      agent: sender,
      sender,
      type: i % 10 === 0 ? 'decision' : 'post',
      channel: 'main',
      text: `Message #${i} from ${sender}: verifying 400+ message windowing and scroll behavior with enough text to ensure scrollable height.`,
      time: new Date(baseTs + i * 1000).toISOString(),
      ts: baseTs + i * 1000,
    });
  }

  const v3Snapshot = {
    swarm: 'v3-ui-swarm',
    id: 'v3-ui-swarm',
    etag: 'v3-etag-1',
    head: totalMessages,
    title: 'Phase-3 v3 Verification Swarm',
    dir: 'C:/Projects/demo-v3',
    primaryConversationId: 'conv-v3-primary',
    coordinatorConversationId: 'conv-v3-coord',
    meta: {
      swarmId: 'v3-ui-swarm',
      title: 'Phase-3 v3 Verification Swarm',
      workspace: 'C:/Projects/demo-v3',
      primaryConversationId: 'conv-v3-primary',
      coordinatorConversationId: 'conv-v3-coord',
    },
    telemetry: {
      activeWorkers: 1,
      waitingWorkers: 1,
      subagentCount: 1,
      totalSteps: 14,
      totalToolCalls: 9,
      totalProcessedTokens: 1250000,
      pureInputTokens: 320000,
      cachedInputTokens: 900000,
      outputTokens: 30000,
      cacheHitRate: 0.22,
      lowCacheHitAlert: true,
      compactionWarningCount: 1,
      liveTps: 42.5,
      totalCostUsd: 0.42,
    },
    members: [
      {
        name: 'coordinator',
        role: 'coordinator',
        status: 'working',
        conversationId: 'conv-v3-coord',
        steps: 4,
        posts: 140,
        now: 'Coordinating v3 verification',
        subagents: [],
      },
      {
        name: 'backend',
        role: 'worker',
        status: 'working',
        model: 'claude-sonnet',
        steps: 10,
        toolCalls: 9,
        posts: 140,
        tps: 38.4,
        tpsEstimated: true,
        tokens: {
          totalProcessed: 540000,
          pureInput: 140000,
          cachedInput: 390000,
          output: 10000,
          contextUsed: 150000,
          contextLimit: 200000,
          contextPct: 75,
        },
        now: 'Running history checks',
        subagents: [
          { id: 'sub-sess-1', name: 'schema-checker', task: 'Verify history JSON', status: 'working', model: 'flash', steps: 2 },
        ],
      },
      {
        name: 'ui',
        role: 'worker',
        status: 'waiting',
        model: 'gemini-pro',
        steps: 4,
        posts: 140,
        now: 'Standing by',
        subagents: [],
      },
    ],
    messages,
  };

  const upstreamServer = createServer((req, res) => {
    const reqUrl = new URL(req.url, 'http://127.0.0.1');
    if (reqUrl.pathname === '/board') {
      res.writeHead(200, { 'Content-Type': 'application/json', ETag: 'v3-etag-1' });
      res.end(JSON.stringify(v3Snapshot));
      return;
    }
    if (reqUrl.pathname === '/board/member/history') {
      const subId = reqUrl.searchParams.get('subagent_session_id') || '';
      const conv = reqUrl.searchParams.get('conversationId') || 'conv-v3-primary';
      const member = reqUrl.searchParams.get('member') || 'backend';
      res.writeHead(200, { 'Content-Type': 'application/json' });
      if (subId === 'sub-sess-1') {
        res.end(JSON.stringify({
          ok: true,
          swarm: 'v3-ui-swarm',
          primaryConversationId: conv,
          member,
          state: 'working',
          model: 'flash',
          subagents: [{ sessionId: 'sub-sess-1', label: 'schema-checker' }],
          messages: [
            {
              id: 101,
              role: 'assistant',
              content: 'Subagent **schema-checker** verified `/board/member/history` contract.',
              timestamp: '2026-11-27T09:05:00Z',
            },
          ],
          lastId: 101,
          hasMore: false,
        }));
        return;
      }
      res.end(JSON.stringify({
        ok: true,
        swarm: 'v3-ui-swarm',
        primaryConversationId: conv,
        member,
        state: 'working',
        model: 'claude-sonnet',
        tps: 38.4,
        tpsEstimated: true,
        tokens: v3Snapshot.members[1].tokens,
        subagents: [{ sessionId: 'sub-sess-1', label: 'schema-checker' }],
        messages: [
          {
            id: 1,
            role: 'system',
            content: 'You own backend history routes.',
            timestamp: '2026-11-27T09:00:00Z',
          },
          {
            id: 2,
            role: 'user',
            content: 'Verify `/board/member/history` route.',
            timestamp: '2026-11-27T09:00:05Z',
            compacted: true,
          },
          {
            id: 3,
            role: 'assistant',
            reasoning: 'Checking **route** handler in `main.mjs`.',
            reasoningTokens: 42,
            content: 'Inspecting `main.mjs` and running route verification.',
            toolCalls: [
              { id: 'tc-1', name: 'read_file', arguments: '{"path":"main.mjs"}' },
            ],
            timestamp: '2026-11-27T09:00:10Z',
          },
          {
            id: 4,
            role: 'tool',
            toolCallId: 'tc-1',
            toolName: 'read_file',
            content: '{"ok":true,"lines":1279}',
            timestamp: '2026-11-27T09:00:11Z',
          },
        ],
        lastId: 4,
        hasMore: false,
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
    HERMES_SWARM_DISABLE_WAKEUP: '1',
    SWARM_OVERVIEW: 'true',
    SWARM_GRAPH_TAB: 'true',
    SWARM_RAW_TAB: 'true',
    ANTIGRAVITY_CONVERSATION_ID: 'conv-v3-primary',
  });
  t.after(() => sidecarProc.stop());

  const pw = await loadPlaywright();
  const executablePath = findBrowserExecutable();
  const browser = await pw.chromium.launch({ headless: true, ...(executablePath ? { executablePath } : {}) });
  t.after(() => browser.close());

  const context = await browser.newContext({ viewport: { width: 1280, height: 860 }, colorScheme: 'dark' });
  const page = await context.newPage();
  const obs = attachPageObservers(page, sidecarProc.baseUrl);

  await page.goto(`${sidecarProc.baseUrl}/?conversationId=conv-v3-primary`, { waitUntil: 'domcontentloaded' });
  await page.waitForSelector('#board:not(.hidden)', { timeout: 10000 });
  await page.click('#tabbtn-feed');
  await page.waitForSelector('#tab-feed.active');

  // c) Full Billable Token-Economy Telemetry in #telemetry-strip
  const stripInfo = await page.evaluate(() => {
    const el = document.getElementById('telemetry-strip');
    return {
      text: el?.textContent || '',
      hasWarn: !!el?.querySelector('.ts-warn'),
      hasDanger: !!el?.querySelector('.ts-danger'),
    };
  });
  assert.match(stripInfo.text, /1\.25M proc/);
  assert.match(stripInfo.text, /320k pure/);
  assert.match(stripInfo.text, /900k cached/);
  assert.match(stripInfo.text, /30\.0k out/);
  assert.match(stripInfo.text, /22% cache/);
  assert.equal(stripInfo.hasWarn, true, 'Expected .ts-warn badge for Low cache');
  assert.equal(stripInfo.hasDanger, true, 'Expected .ts-danger badge for CTX warn');

  // b) 400+ message UX: 120 window, Show older, #jump-top, and #seq Enter jump
  const initialMsgCount = await page.locator('#feed .msg').count();
  assert.equal(initialMsgCount, 120, `Expected initial windowed feed to render 120 messages, got ${initialMsgCount}`);
  const olderBtnText = await page.textContent('[data-show-older]');
  assert.match(olderBtnText, /Show older \(300 more\)/);

  // Scroll #feed down to trigger #jump-top, then click #jump-top
  await page.evaluate(() => {
    const f = document.getElementById('feed');
    f.scrollTop = 650;
    f.dispatchEvent(new Event('scroll'));
  });
  await page.waitForSelector('#jump-top:not(.hidden)', { timeout: 3000 });
  await page.click('#jump-top');
  await page.waitForFunction(() => document.getElementById('feed').scrollTop < 60, null, { timeout: 3000 });

  // Click [data-show-older] and verify window expands + scroll preserved
  await page.evaluate(() => {
    const f = document.getElementById('feed');
    f.scrollTop = 0;
    f.dispatchEvent(new Event('scroll'));
  });
  await page.click('[data-show-older]');
  await page.waitForFunction(() => document.querySelectorAll('#feed .msg').length === 240, null, { timeout: 3000 });
  const afterOlder = await page.evaluate(() => ({
    btnText: document.querySelector('[data-show-older]')?.textContent || '',
    scrollTop: document.getElementById('feed').scrollTop,
  }));
  assert.match(afterOlder.btnText, /Show older \(180 more\)/);
  assert.ok(afterOlder.scrollTop > 0, `Expected scrollTop > 0 preserved after Show older, got ${afterOlder.scrollTop}`);

  // #seq search jump (Enter on #12, then #5)
  await page.fill('#f-search', '#12');
  await page.press('#f-search', 'Enter');
  await page.waitForSelector('#feed .msg[data-seq="12"].target', { timeout: 3000 });
  await page.fill('#f-search', '#5');
  await page.press('#f-search', 'Enter');
  await page.waitForSelector('#feed .msg[data-seq="5"].target', { timeout: 3000 });
  await page.fill('#f-search', '');

  // c) Member card billable telemetry in Members tab (hive.js)
  await page.click('#tabbtn-members');
  await page.waitForSelector('#tab-members.active');
  const memberMetrics = await page.evaluate(() => {
    const card = document.querySelector('[data-open-member="backend"]');
    const metrics = card?.querySelector('[data-testid="swarm-member-metrics"]');
    return {
      text: metrics?.textContent || '',
      hasCtxWarn: !!metrics?.querySelector('.o2-ctx-warn'),
    };
  });
  assert.match(memberMetrics.text, /540k proc/);
  assert.match(memberMetrics.text, /140k pure/);
  assert.match(memberMetrics.text, /390k cached/);
  assert.match(memberMetrics.text, /150k\/200k 75%/);
  assert.match(memberMetrics.text, /~38 t\/s/);
  assert.equal(memberMetrics.hasCtxWarn, true, 'Expected .o2-ctx-warn on 75% context usage');

  // d) Dynamic Live Agent History Tab
  await page.click('[data-open-member="backend"]');
  await page.waitForSelector('.tab-agent[data-agent-tab="backend"].active', { timeout: 5000 });
  await page.waitForSelector('#tab-agent.active .ag-reply', { timeout: 5000 });

  const agentPaneCheck = await page.evaluate(() => {
    const feed = document.getElementById('agent-feed');
    return {
      hasCompacted: !!feed?.querySelector('.ag-compacted'),
      workGroupSummary: feed?.querySelector('details.ag-work-group > summary')?.textContent || '',
      thinkSummary: feed?.querySelector('details.ag-think > summary')?.textContent || '',
      replyText: feed?.querySelector('.ag-reply')?.textContent || '',
      toolSummary: feed?.querySelector('details.ag-tool-card > summary')?.textContent || '',
      hasSubPill: !!document.querySelector('.agent-sub-pill[data-sub-id="sub-sess-1"]'),
    };
  });
  assert.equal(agentPaneCheck.hasCompacted, true, 'Expected .ag-compacted divider');
  assert.match(agentPaneCheck.workGroupSummary, /Worked for 1s/);
  assert.match(agentPaneCheck.thinkSummary, /Thought process · 42 tok/);
  assert.match(agentPaneCheck.replyText, /Inspecting main\.mjs/);
  assert.match(agentPaneCheck.toolSummary, /read_file/);
  assert.match(agentPaneCheck.toolSummary, /ok/);
  assert.equal(agentPaneCheck.hasSubPill, true, 'Expected Tier-3 subagent pill switcher');

  // Expand collapsible work group, reasoning & tool card
  await page.click('details.ag-work-group > summary');
  await page.waitForSelector('details.ag-work-group[open] details.ag-think');
  await page.click('details.ag-think > summary');
  await page.waitForSelector('details.ag-think[open] .ag-think-body');
  assert.match(await page.textContent('details.ag-think .ag-think-body'), /Checking route handler/);

  await page.click('details.ag-tool-card > summary');
  await page.waitForSelector('details.ag-tool-card[open] .ag-tool-detail');
  assert.match(await page.textContent('details.ag-tool-card .ag-tool-detail'), /"lines": 1279/);

  // Switch to Tier-3 subagent pill
  await page.click('.agent-sub-pill[data-sub-id="sub-sess-1"]');
  await page.waitForFunction(
    () => /schema-checker/.test(document.getElementById('agent-feed')?.textContent || ''),
    null,
    { timeout: 5000 },
  );

  // Close dynamic agent tab via ×
  await page.click('[data-close-agent="backend"]');
  await page.waitForFunction(
    () => !document.querySelector('.tab-agent[data-agent-tab="backend"]'),
    null,
    { timeout: 3000 },
  );
  assert.equal(await page.locator('#tab-agent.active').count(), 0);

  // Clicking coordinator invokes window.sidecar.ui.toggleConversation when stubbed
  await page.click('#tabbtn-members');
  await page.waitForSelector('#tab-members.active');
  await page.evaluate(() => {
    window.__toggledConvId = null;
    window.sidecar = {
      ...(window.sidecar || {}),
      ui: {
        toggleConversation: (id) => { window.__toggledConvId = id; },
      },
    };
  });
  await page.click('#members [data-open-member="coordinator"]');
  const toggledConvId = await page.evaluate(() => window.__toggledConvId);
  assert.equal(toggledConvId, 'conv-v3-coord');

  await context.close();
  const realFailed = obs.failedRequests.filter(
    (r) => !(r.reason === 'net::ERR_ABORTED' && /fonts\.g(?:static|oogleapis)\.com/.test(r.url))
      && !r.url.includes('/_sidecar/get-conversation-metadata'),
  );
  assert.deepEqual(realFailed, [], `Failed network requests: ${JSON.stringify(realFailed)}`);
  const realConsoleErrors = obs.consoleErrors.filter(
    (c) => !c.url.includes('/_sidecar/get-conversation-metadata') && !/Failed to resolve project ID/i.test(c.text),
  );
  assert.deepEqual(realConsoleErrors, [], `Console errors: ${JSON.stringify(realConsoleErrors)}`);
  assert.deepEqual(obs.pageErrors, [], `Page errors: ${JSON.stringify(obs.pageErrors)}`);
});
test('Image-2 parity worker chat view: dark/light themes, root & subpath, unwrapped TASK, Worked for Xs, rich MD, epoch-sec timestamps, object currentTool, live tail scroll/details preservation, files changed, subagent switching, compacted, jump-to-live, zero textarea', async (t) => {
  const pw = await loadPlaywright();
  const executablePath = findBrowserExecutable();
  const browser = await pw.chromium.launch({
    headless: true,
    args: ['--no-proxy-server', '--proxy-bypass-list=<-loopback>'],
    ...(executablePath ? { executablePath } : {}),
  });
  t.after(() => browser.close());

  const tempState = mkdtempSync(join(tmpdir(), 'hermes-img2-ui-'));
  t.after(() => rmSync(tempState, { recursive: true, force: true }));

  let liveTick = 0;
  let delayMainMs = 0;
  const epochSecBase = 1795799100;
  const bigOutput = 'X'.repeat(28000);

  const seenBeforeIds = [];
  const img2Snapshot = {
    swarm: 'img2-parity-swarm',
    id: 'img2-parity-swarm',
    etag: 'img2-etag-1',
    head: 2,
    title: 'Image 2 Parity Swarm',
    dir: tempState,
    workspace: 'C:/Projects/demo-img2',
    primaryConversationId: 'conv-img2-primary',
    coordinatorConversationId: 'conv-img2-coord',
    allowedFiles: {
      'C:/Projects/demo-img2': 'file:///C:/Projects/demo-img2',
    },
    meta: {
      swarmId: 'img2-parity-swarm',
      title: 'Image 2 Parity Swarm',
      workspace: 'C:/Projects/demo-img2',
      primaryConversationId: 'conv-img2-primary',
      coordinatorConversationId: 'conv-img2-coord',
    },
    members: [
      {
        name: 'coordinator',
        role: 'coordinator',
        status: 'working',
        conversationId: 'conv-img2-coord',
        posts: 1,
        subagents: [],
      },
      {
        name: 'builder',
        role: 'worker',
        status: 'TASK_STATE_WORKING',
        model: 'anthropic/claude-sonnet-4',
        steps: 6,
        toolCalls: 4,
        posts: 1,
        tps: 44.2,
        currentTool: { name: 'terminal', preview: 'npm test', startedAt: new Date(epochSecBase * 1000).toISOString() },
        tokens: { totalProcessed: 120000, pureInput: 30000, cachedInput: 85000, output: 5000, contextUsed: 45000, contextLimit: 200000 },
        subagents: [
          { id: 'tc-sub-1', sessionId: 'sess-sub-1', name: 'auditor', task: 'Audit markdown XSS', status: 'done', model: 'flash', steps: 2 },
        ],
      },
      {
        name: 'retired-worker',
        role: 'worker',
        status: 'working',
        killed: true,
        model: 'flash',
        currentTool: { name: 'bash', preview: 'stale tool' },
        posts: 0,
        subagents: [],
      },
    ],
    messages: [
      { seq: 1, id: 'm-1', agent: 'coordinator', sender: 'coordinator', text: '@builder Start parity check', ts: epochSecBase * 1000 },
      { seq: 2, id: 'm-2', agent: 'builder', sender: 'builder', text: 'Working on #1', ts: (epochSecBase + 5) * 1000 },
    ],
  };

  const upstreamServer = createServer(async (req, res) => {
    const reqUrl = new URL(req.url, 'http://127.0.0.1');
    if (reqUrl.pathname === '/board') {
      res.writeHead(200, { 'Content-Type': 'application/json', ETag: img2Snapshot.etag });
      res.end(JSON.stringify(img2Snapshot));
      return;
    }
    if (reqUrl.pathname === '/board/member/history') {
      const member = reqUrl.searchParams.get('member') || 'builder';
      const subId = reqUrl.searchParams.get('subagent_session_id') || '';
      const conv = reqUrl.searchParams.get('conversationId') || 'conv-img2-primary';
      const afterId = Number(reqUrl.searchParams.get('after_id') || '0');
      const beforeId = Number(reqUrl.searchParams.get('before_id') || '0');
      if (beforeId > 0) seenBeforeIds.push(beforeId);
      if (!subId && delayMainMs > 0) {
        await new Promise((r) => setTimeout(r, delayMainMs));
      }
      res.writeHead(200, { 'Content-Type': 'application/json' });
      if (beforeId > 0) {
        res.end(JSON.stringify({
          ok: true,
          swarm: 'img2-parity-swarm',
          primaryConversationId: conv,
          member,
          state: 'TASK_STATE_WORKING',
          model: 'anthropic/claude-sonnet-4',
          messages: [
            { id: 5, role: 'user', content: '[TASK]\nEarlier turn before cursor', timestamp: epochSecBase - 120 },
            { id: 6, role: 'assistant', content: 'Earlier reply loaded via `before_id`.', timestamp: epochSecBase - 110 },
          ],
          lastId: 6,
          hasMore: false,
        }));
        return;
      }
      if (member === 'retired-worker') {
        res.end(JSON.stringify({
          ok: true,
          swarm: 'img2-parity-swarm',
          primaryConversationId: conv,
          member,
          state: 'TASK_STATE_WORKING',
          currentTool: { name: 'bash', preview: 'stale tool' },
          live: { active: true, transcript: 'Should never shimmer', currentTool: { name: 'bash', preview: 'stale' } },
          messages: [
            { id: 1, role: 'user', content: 'Done task', timestamp: epochSecBase },
            { id: 2, role: 'assistant', content: 'Finished before kill.', timestamp: epochSecBase + 3 },
          ],
          lastId: 2,
          hasMore: false,
        }));
        return;
      }
      if (subId === 'sess-sub-1') {
        res.end(JSON.stringify({
          ok: true,
          swarm: 'img2-parity-swarm',
          primaryConversationId: conv,
          member,
          sessionId: 'sess-sub-1',
          state: 'waiting',
          model: 'flash',
          subagents: [{ sessionId: 'sess-sub-1', label: 'auditor', state: 'waiting', steps: 2 }],
          messages: [
            { id: 50, role: 'user', content: 'Audit XSS safety', timestamp: epochSecBase + 10 },
            { id: 51, role: 'assistant', content: 'Subagent `auditor` confirmed zero XSS.', timestamp: epochSecBase + 14 },
          ],
          lastId: 51,
          hasMore: false,
        }));
        return;
      }
      const committed = [
        {
          id: 10,
          role: 'user',
          content: '[SYSTEM / ROLE]\nInternal role preamble line 1\nInternal role preamble line 2\n\n[TASK]\nFix the worker chat view and verify CI #1',
          timestamp: epochSecBase + 0.445,
        },
        {
          id: 20,
          role: 'assistant',
          reasoning: 'Checking **board_ui/app.js** and `styles.css`.',
          reasoningTokens: 18,
          toolCalls: [
            { id: 'tc-edit-1', name: 'edit_file', arguments: JSON.stringify({ path: 'board_ui/app.js', old_string: 'a\nb', new_string: 'x\ny\nz' }) },
            { id: 'tc-edit-bad', name: 'edit_file', arguments: JSON.stringify({ path: '../../etc/passwd', old_string: '1', new_string: '2' }) },
            { id: 'tc-edit-sibling', name: 'edit_file', arguments: JSON.stringify({ path: 'C:/Projects/demo-img2-evil/secret.txt', old_string: '1', new_string: '2' }) },
            { id: 'tc-big', name: 'terminal', arguments: JSON.stringify({ command: 'cat big.log' }) },
          ],
          timestamp: epochSecBase + 10,
        },
        {
          id: 30,
          role: 'tool',
          toolCallId: 'tc-edit-1',
          toolName: 'edit_file',
          content: '{"ok":true}',
          timestamp: epochSecBase + 22,
        },
        {
          id: 40,
          role: 'tool',
          toolCallId: 'tc-edit-bad',
          toolName: 'edit_file',
          content: '{"ok":true}',
          timestamp: epochSecBase + 23,
        },
        {
          id: 45,
          role: 'tool',
          toolCallId: 'tc-edit-sibling',
          toolName: 'edit_file',
          content: '{"ok":true}',
          timestamp: epochSecBase + 24,
        },
        {
          id: 50,
          role: 'tool',
          toolCallId: 'tc-big',
          toolName: 'terminal',
          content: bigOutput,
          timestamp: epochSecBase + 25,
        },
        {
          id: 60,
          role: 'assistant',
          compacted: true,
          content: [
            '## Parity Summary',
            '',
            '> Verified escape-first Markdown',
            '',
            '- Root item',
            '  1. Nested step one',
            '  2. Nested step two',
            '',
            '| Check | Result |',
            '| :--- | ---: |',
            '| Markdown | pass |',
            '',
            'See [safe docs](https://github.com/MAHDI-AQ/hermes-swarm) and [bad](javascript:alert(1)).',
            '',
            '```js',
            'const ok = true;',
            '```',
            '',
            'Ping @builder on #1.',
            '',
            '---',
            'Filler line 1\n\nFiller line 2\n\nFiller line 3\n\nFiller line 4\n\nFiller line 5\n\nFiller line 6\n\nFiller line 7\n\nFiller line 8',
          ].join('\n'),
          timestamp: epochSecBase + 35,
        },
      ];
      const slice = afterId > 0 ? committed.filter((m) => m.id > afterId) : committed;
      liveTick += 1;
      res.end(JSON.stringify({
        ok: true,
        swarm: 'img2-parity-swarm',
        primaryConversationId: conv,
        member,
        state: 'TASK_STATE_WORKING',
        model: 'anthropic/claude-sonnet-4',
        tps: 44.2,
        tpsEstimated: false,
        currentTool: { name: 'terminal', preview: `tick-${liveTick}`, startedAt: new Date().toISOString() },
        tokens: img2Snapshot.members[1].tokens,
        subagents: [{ sessionId: 'sess-sub-1', label: 'auditor', state: 'waiting', steps: 2 }],
        live: {
          active: true,
          elapsedMs: 4200 + liveTick * 500,
          currentTool: { name: 'terminal', preview: `tick-${liveTick}` },
          reasoning: 'Streaming **thought** check',
          transcript: `Streaming reply tick ${liveTick}\n\n\`\`\`python\nprint("streaming"`,
          toolTrace: [
            { toolCallId: 'tc-edit-1', name: 'edit_file', preview: 'should-be-deduped', status: 'ok' },
            { toolCallId: 'tc-live-1', name: 'terminal', preview: `tick-${liveTick}`, status: 'running' },
          ],
        },
        messages: slice,
        lastId: 60,
        hasMore: afterId === 0,
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
    HERMES_SWARM_DISABLE_WAKEUP: '1',
    ANTIGRAVITY_CONVERSATION_ID: 'conv-img2-primary',
  });
  t.after(() => sidecarProc.stop());

  const SUBPATH_PREFIX = '/remote-proxy/plugin/hermes-swarm/swarm_board';
  const proxy = createSubpathProxy(sidecarProc.port, SUBPATH_PREFIX);
  const proxyPort = await listenServer(proxy.server);
  t.after(() => closeServer(proxy.server));

  async function runImg2Suite(pageUrl, originPrefix, colorScheme) {
    const context = await browser.newContext({ viewport: { width: 920, height: 640 }, colorScheme });
    const page = await context.newPage();
    await page.route('**/_sidecar/get-conversation-metadata', (route) =>
      route.fulfill({ status: 200, contentType: 'application/json', body: '{}' }),
    );
    const obs = attachPageObservers(page, originPrefix);
    await page.addInitScript(() => {
      window.__auxCalls = [];
      window.addEventListener('DOMContentLoaded', () => {
        if (window.sidecar) {
          window.sidecar.ui = window.sidecar.ui || {};
          window.sidecar.ui.toggleAuxPane = (req) => window.__auxCalls.push(req);
        }
      });
    });

    await page.goto(pageUrl, { waitUntil: 'domcontentloaded' });
    await page.waitForSelector('#board:not(.hidden)', { timeout: 10000 });
    await page.evaluate(() => {
      window.sidecar = window.sidecar || {};
      window.sidecar.ui = window.sidecar.ui || {};
      window.sidecar.ui.toggleAuxPane = (req) => window.__auxCalls.push(req);
    });

    // Open builder worker from Members tab
    await page.click('#tabbtn-members');
    await page.waitForSelector('[data-open-member="builder"]');
    await page.click('[data-open-member="builder"]');
    await page.waitForSelector('#tab-agent.active .ag-reply', { timeout: 5000 });

    const checks = await page.evaluate(() => {
      const feed = document.getElementById('agent-feed');
      const pill = feed.querySelector('.ag-user-pill');
      const sysInline = feed.querySelector('.ag-sys-inline');
      const workGroup = feed.querySelector('details.ag-work-group:not(.ag-live-work)');
      const footText = document.getElementById('agent-live-foot')?.textContent || '';
      const liveTail = document.getElementById('agent-live-tail');
      const reply = feed.querySelector('#agent-committed .ag-reply');
      const links = [...reply.querySelectorAll('a')].map((a) => ({ href: a.getAttribute('href'), text: a.textContent }));
      const openFileBtns = [...feed.querySelectorAll('[data-ag-file]')].map((b) => b.dataset.agFile);
      const liveDedupHit = [...(liveTail?.querySelectorAll('.ag-tool-prev') || [])].some((el) => el.textContent.includes('should-be-deduped'));
      return {
        theme: document.documentElement.dataset.theme,
        textareaCount: document.querySelectorAll('textarea').length,
        pillText: pill?.textContent || '',
        pillHasSystemWall: (pill?.textContent || '').includes('Internal role preamble'),
        hasCollapsedSysBrief: Boolean(sysInline && !sysInline.open),
        workSummary: workGroup?.querySelector('summary')?.textContent || '',
        hasCompacted: !!feed.querySelector('.ag-compacted'),
        hasTable: !!reply.querySelector('table.ag-table'),
        hasNestedOl: !!reply.querySelector('ul ol'),
        hasBlockquote: !!reply.querySelector('blockquote'),
        hasHr: !!reply.querySelector('hr'),
        hasCodeBlock: !!reply.querySelector('.ag-codeblock pre code'),
        codeLang: reply.querySelector('.ag-codelang')?.textContent || '',
        links,
        has1970: (feed.textContent || '').includes('1970'),
        footText,
        footVisible: !document.getElementById('agent-live-foot')?.classList.contains('hidden'),
        liveTailVisible: !liveTail?.classList.contains('hidden'),
        liveHasUnclosedFenceCode: !!liveTail?.querySelector('.ag-live-reply .ag-codeblock pre code'),
        liveDedupHit,
        filesChangedSummary: feed.querySelector('.ag-files-changed > summary')?.textContent || '',
        openFileBtns,
      };
    });

    assert.equal(checks.theme, colorScheme);
    assert.equal(checks.textareaCount, 0, 'Must have ZERO textarea elements');
    assert.match(checks.pillText, /Fix the worker chat view/);
    assert.equal(checks.pillHasSystemWall, false, '[SYSTEM / ROLE] must be unwrapped out of .ag-user-pill');
    assert.equal(checks.hasCollapsedSysBrief, true, 'System / Role brief must be collapsed by default');
    assert.match(checks.workSummary, /Worked for 15s/, `Unexpected work summary: ${checks.workSummary}`);
    assert.equal(checks.hasCompacted, true);
    assert.equal(checks.hasTable, true);
    assert.equal(checks.hasNestedOl, true);
    assert.equal(checks.hasBlockquote, true);
    assert.equal(checks.hasHr, true);
    assert.equal(checks.hasCodeBlock, true);
    assert.equal(checks.codeLang, 'js');
    assert.deepEqual(checks.links, [{ href: 'https://github.com/MAHDI-AQ/hermes-swarm', text: 'safe docs' }]);
    assert.equal(checks.has1970, false, 'Epoch-seconds timestamps must NEVER render as 1970');
    assert.equal(checks.footVisible, true, 'TASK_STATE_WORKING must normalize to working and show live foot');
    assert.equal(checks.footText.includes('[object Object]'), false, 'currentTool object must not render [object Object]');
    assert.match(checks.footText, /Running terminal\(tick-/);
    assert.equal(checks.liveTailVisible, true);
    assert.equal(checks.liveHasUnclosedFenceCode, true, 'Unclosed streaming fence must still render codeblock cleanly');
    assert.equal(checks.liveDedupHit, false, 'Committed toolCallId must be deduplicated from live.toolTrace');
    assert.match(checks.filesChangedSummary, /3 files changed/);
    assert.deepEqual(checks.openFileBtns, ['board_ui/app.js'], 'Only validated workspace-root path may be clickable; ../../etc/passwd and sibling prefix demo-img2-evil must not');

    // Expand "Worked for 15s" and verify oversized output is truncated cleanly, then scroll up and verify preservation across live ticks!
    await page.click('details.ag-work-group:not(.ag-live-work) > summary');
    await page.waitForSelector('details.ag-work-group[open] details.ag-tool-card');
    const toolCards = page.locator('details.ag-work-group:not(.ag-live-work) details.ag-tool-card');
    await toolCards.nth(3).locator('> summary').click();
    const bigPreText = await toolCards.nth(3).locator('.ag-tool-pre').last().textContent();
    assert.match(bigPreText, /truncated/);
    assert.ok(bigPreText.length < 20000, 'Oversized tool payload must be capped');

    // Expand files changed & click allowed file
    await page.click('.ag-files-changed > summary');
    await page.click('[data-ag-file="board_ui/app.js"]');
    const auxCalls = await page.evaluate(() => window.__auxCalls);
    assert.deepEqual(auxCalls, [{ tabType: 'fileView', uri: 'file:///C:/Projects/demo-img2/board_ui/app.js' }]);

    // Click Show older -> sends before_id=10, prepends older messages, hides Show older when hasMore=false
    await page.click('[data-ag-older]');
    await page.waitForFunction(
      () => /Earlier reply loaded via before_id/.test(document.getElementById('agent-committed')?.textContent || ''),
      null,
      { timeout: 4000 },
    );
    assert.equal(seenBeforeIds.includes(10), true, `Expected before_id=10 to be sent, got ${JSON.stringify(seenBeforeIds)}`);

    // Scroll up to unstick and verify open details + scrollTop preserved across live poll (~900ms), and Jump to live appears
    await page.evaluate(() => {
      const af = document.getElementById('agent-feed');
      af.scrollTop = 20;
      af.dispatchEvent(new Event('scroll'));
    });
    await page.waitForSelector('#agent-jump-live:not(.hidden)', { timeout: 3000 });
    await page.waitForTimeout(1000);
    const afterPoll = await page.evaluate(() => ({
      scrollTop: document.getElementById('agent-feed').scrollTop,
      workOpen: document.querySelector('details.ag-work-group:not(.ag-live-work)')?.open,
      jumpVisible: !document.getElementById('agent-jump-live')?.classList.contains('hidden'),
    }));
    assert.ok(afterPoll.scrollTop <= 40, `Expected scrollTop preserved near 20, got ${afterPoll.scrollTop}`);
    assert.equal(afterPoll.workOpen, true, 'Open <details> must remain open across live tail ticks');
    assert.equal(afterPoll.jumpVisible, true, 'Jump to live pill must stay visible when scrolled up');

    await page.click('#agent-jump-live');
    await page.waitForFunction(() => document.getElementById('agent-jump-live')?.classList.contains('hidden'), null, { timeout: 3000 });

    // Stale-fetch race protection + Tier-3 subagent click-through from Members tab
    await page.click('#tabbtn-members');
    await page.waitForSelector('.o2-sub-row[data-open-subagent="sess-sub-1"]');
    delayMainMs = 700;
    await page.click('.o2-sub-row[data-open-subagent="sess-sub-1"]');
    await page.waitForFunction(
      () => /confirmed zero XSS/.test(document.getElementById('agent-committed')?.textContent || ''),
      null,
      { timeout: 5000 },
    );
    // Trigger slow Main fetch then immediately switch back to subagent pill -> slow Main response must NOT overwrite subagent
    await page.click('.agent-sub-pill[data-sub-id=""]');
    await page.waitForTimeout(50);
    await page.click('.agent-sub-pill[data-sub-id="sess-sub-1"]');
    await page.waitForTimeout(900);
    const subTextAfterRace = await page.textContent('#agent-committed');
    assert.match(subTextAfterRace, /confirmed zero XSS/);
    delayMainMs = 0;

    // Killed worker must NEVER show live shimmer or #agent-live-foot
    await page.click('#tabbtn-members');
    await page.click('button.o2-mret');
    await page.waitForSelector('[data-open-member="retired-worker"]');
    await page.click('[data-open-member="retired-worker"]');
    await page.waitForSelector('#tab-agent.active .ag-reply', { timeout: 5000 });
    const killedCheck = await page.evaluate(() => ({
      footHidden: document.getElementById('agent-live-foot')?.classList.contains('hidden'),
      tailHidden: document.getElementById('agent-live-tail')?.classList.contains('hidden'),
      hasShimmer: !!document.querySelector('#tab-agent .animate-shimmer-text'),
    }));
    assert.equal(killedCheck.footHidden, true, 'Killed worker must hide #agent-live-foot');
    assert.equal(killedCheck.tailHidden, true, 'Killed worker must hide #agent-live-tail');
    assert.equal(killedCheck.hasShimmer, false, 'Killed worker must NEVER show Working shimmer');

    await context.close();
    const realFailed = obs.failedRequests.filter(
      (r) => !(r.reason === 'net::ERR_ABORTED' && /fonts\.g(?:static|oogleapis)\.com/.test(r.url))
        && !r.url.includes('/_sidecar/get-conversation-metadata')
        && !(r.reason === 'net::ERR_ABORTED' && r.url.includes('/api/member/history')),
    );
    assert.deepEqual(realFailed, [], `Failed requests: ${JSON.stringify(realFailed)}`);
    const realConsoleErrors = obs.consoleErrors.filter(
      (c) => !c.url.includes('/_sidecar/get-conversation-metadata') && !/Failed to resolve project ID/i.test(c.text),
    );
    assert.deepEqual(realConsoleErrors, [], `Console errors: ${JSON.stringify(realConsoleErrors)}`);
    assert.deepEqual(obs.pageErrors, [], `Page errors: ${JSON.stringify(obs.pageErrors)}`);
  }

  await runImg2Suite(`${sidecarProc.baseUrl}/?conversationId=conv-img2-primary`, sidecarProc.baseUrl, 'dark');
  await runImg2Suite(`http://127.0.0.1:${proxyPort}${SUBPATH_PREFIX}/?conversationId=conv-img2-primary`, `http://127.0.0.1:${proxyPort}`, 'light');
  assert.deepEqual(proxy.proxyLeaks, [], `Subpath proxy leaks: ${JSON.stringify(proxy.proxyLeaks)}`);
});
