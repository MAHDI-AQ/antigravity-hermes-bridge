/**
 * Minimal headless test double for Antigravity's `sidecar_sdk` used in CI
 * environments where Antigravity IDE is not installed.
 */

import { execFileSync } from 'node:child_process';
import { createServer } from 'node:http';

const BIND_HOST = '127.0.0.1';
const PRELOAD_JS = 'window.sidecar = { __mock: true };\n';

export class Response {
  constructor(body, { contentType = 'text/plain', status = 200, headers } = {}) {
    this.body = body;
    this.contentType = contentType;
    this.status = status;
    this.headers = headers;
  }
}

export class SidecarApp {
  #token;
  #port;
  #server;
  #pages = new Map();
  #apis = new Map();

  constructor() {
    const lsAddr = process.env.ANTIGRAVITY_LS_ADDRESS;
    const csrf = process.env.ANTIGRAVITY_CSRF_TOKEN;
    const portStr = process.env.ANTIGRAVITY_SIDECAR_WEB_PORT;
    if (!lsAddr || !csrf || !portStr) {
      throw new Error(
        'Missing required environment variables (ANTIGRAVITY_LS_ADDRESS, ' +
        'ANTIGRAVITY_CSRF_TOKEN, or ANTIGRAVITY_SIDECAR_WEB_PORT).'
      );
    }
    this.#token = process.env.ANTIGRAVITY_SIDECAR_UI_TOKEN;
    this.#port = parseInt(portStr, 10);
  }

  get config() {
    return {
      projectId: process.env.ANTIGRAVITY_PROJECT_ID,
      conversationId: process.env.ANTIGRAVITY_CONVERSATION_ID,
    };
  }

  sendMessage(conversationId, message, { title, projectId } = {}) {
    const args = ['send-message'];
    if (title) args.push('--title', title);
    args.push(conversationId, message);
    const env = { ...process.env };
    if (projectId) env.ANTIGRAVITY_PROJECT_ID = projectId;
    const exe = process.env.ANTIGRAVITY_AGENTAPI_EXE;
    const file = exe || 'agentapi';
    const execArgs = exe ? ['agentapi', ...args] : args;
    const out = execFileSync(file, execArgs, { encoding: 'utf-8', env });
    return out.trim() ? JSON.parse(out) : {};
  }

  page(path, handler) {
    this.#pages.set(path, handler);
  }

  api(path, handler, method = 'POST') {
    if (!this.#apis.has(path)) this.#apis.set(path, new Map());
    this.#apis.get(path).set(method.toUpperCase(), handler);
  }

  #validateToken(req, url) {
    if (url.pathname === '/preload.js' || !this.#token) return true;
    const provided = req.headers['x-sidecar-token'] || url.searchParams.get('token');
    return provided === this.#token;
  }

  run() {
    this.#pages.set('/preload.js', () => PRELOAD_JS);

    const server = createServer(async (req, res) => {
      const url = new URL(req.url, `http://localhost:${this.#port}`);
      if (req.method !== 'GET' && !this.#validateToken(req, url)) {
        res.writeHead(401, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({ error: 'unauthorized' }));
        return;
      }
      if (req.method === 'GET' && this.#pages.has(url.pathname)) {
        const content = await this.#pages.get(url.pathname)();
        if (content instanceof Response || (content && typeof content === 'object' && content.contentType && content.body !== undefined)) {
          res.writeHead(content.status || 200, { 'Content-Type': content.contentType, ...content.headers });
          res.end(content.body);
          return;
        }
        const isJS = url.pathname.endsWith('.js');
        res.writeHead(200, { 'Content-Type': isJS ? 'application/javascript' : 'text/html' });
        res.end(content);
        return;
      }
      if (this.#apis.has(url.pathname)) {
        const methodMap = this.#apis.get(url.pathname);
        if (!methodMap.has(req.method)) {
          res.writeHead(405, { 'Content-Type': 'application/json' });
          res.end(JSON.stringify({ error: 'Method Not Allowed' }));
          return;
        }
        const handler = methodMap.get(req.method);
        let body = '';
        req.on('data', (chunk) => { body += chunk; });
        req.on('end', async () => {
          try {
            const data = {};
            for (const [k, v] of url.searchParams.entries()) {
              if (k.toLowerCase() !== 'token') data[k] = v;
            }
            if (body) Object.assign(data, JSON.parse(body));
            const result = await handler(data);
            if (result instanceof Response || (result && typeof result === 'object' && result.contentType && result.body !== undefined)) {
              const status = result.status || 200;
              const respBody = typeof result.body === 'string' ? result.body : Buffer.from(result.body);
              res.writeHead(status, { 'Content-Type': result.contentType, ...result.headers });
              res.end(respBody);
              return;
            }
            res.writeHead(200, { 'Content-Type': 'application/json' });
            res.end(JSON.stringify(result));
          } catch (err) {
            res.writeHead(500, { 'Content-Type': 'application/json' });
            res.end(JSON.stringify({ error: err.message }));
          }
        });
        return;
      }
      res.writeHead(404);
      res.end('Not found');
    });

    return new Promise((resolveListen) => {
      this.#server = server.listen(this.#port, BIND_HOST, () => {
        console.log(`[sidecar] listening on ${BIND_HOST}:${server.address().port}`);
        resolveListen(server);
      });
    });
  }

  close() {
    return new Promise((resolveClose) => (this.#server ? this.#server.close(() => resolveClose()) : resolveClose()));
  }
}
