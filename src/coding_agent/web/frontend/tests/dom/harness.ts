/* jsdom 交互测试脚手架：真实加载构建产物 + 本地 Alpine CSP，桩掉后端 API。 */

import { createServer } from 'node:http';
import { readFile } from 'node:fs/promises';
import path from 'node:path';
import { JSDOM } from 'jsdom';

const STATIC = path.resolve(process.cwd(), '../static');

const CONFIG = {
  schemaVersion: 1,
  apiBaseUrl: '',
  apiVersion: 'v0',
  authMode: 'desktop-token',
  auth: {
    loginUrl: '',
    refreshUrl: '',
    logoutUrl: '',
    csrfHeader: 'X-CSRF-Token',
  },
  sse: {
    heartbeatMs: 15000,
    idleTimeoutMs: 45000,
    reconnectBaseMs: 500,
    reconnectMaxMs: 30000,
    reconnectMaxAttempts: 3,
  },
  requestIdHeader: 'X-Request-ID',
};

const TREE_FILES: Record<string, any[]> = {
  '': [
    { name: 'a.py', path: 'a.py', is_dir: false, size: 10 },
    { name: 'src', path: 'src', is_dir: true, size: 0 },
    { name: 'new.txt', path: 'new.txt', is_dir: false, size: 2 },
  ],
  src: [{ name: 'b.ts', path: 'src/b.ts', is_dir: false, size: 3 }],
};

const GIT_CHANGES = [
  { status: 'M', path: 'a.py' },
  { status: '??', path: 'new.txt' },
  { status: 'A', path: 'src/b.ts' },
];

const BASE_STUBS: Record<string, any> = {
  '/config.json': CONFIG,
  '/healthz': {
    api_version: 'v0',
    min_frontend_version: '0.1.0',
    version: '0.1.0',
    capabilities_version: 1,
  },
  '/api/health': {
    reachable: true,
    model: 'stub-model',
    user: 'local',
    workspace: '/tmp/ws',
  },
  '/api/capabilities': {
    read_only: true,
    server_write_enabled: false,
    multi_user: false,
    desktop_mode: true,
    workspace_registry: false,
    host_mode: false,
    workspace_upload_max_bytes: 0,
    max_upload_bytes: 0,
    run_cost_budget_usd: 0,
    capabilities_version: 1,
  },
  '/api/providers': { providers: [] },
  '/api/provider-profiles': { profiles: [], profiles_enabled: false },
  '/api/workspace': { workspace: '/tmp/ws' },
  '/api/sessions': {
    sessions: [
      {
        thread_id: 'thread-dom-test',
        title: '未读会话',
        workspace: '/tmp/ws',
        unread: true,
        pinned: false,
        archived: false,
        tags: ['前端'],
        last_active: '2026-10-27T10:00:00+00:00',
        prompts: 2,
        tool_calls: 3,
        last_message: '上一次运行的最后一条回答',
      },
      {
        thread_id: 'thread-dom-other',
        title: '普通会话',
        workspace: '/tmp/ws',
        unread: false,
        pinned: false,
        archived: false,
        tags: [],
        last_active: '2026-10-26T09:00:00+00:00',
        prompts: 1,
        tool_calls: 0,
        last_message: '',
      },
    ],
  },
  '/api/project': {
    workspace: '/tmp/ws',
    mode: 'native',
    project: { python: '', version: '', source: 'system' },
    agent_python: '',
    agent_version: '',
    package_manager: '',
    lockfile: '',
    test_command: '',
    warnings: [],
  },
  '/api/consent/pending': { pending: [] },
  '/api/memory': { facts: [] },
  '/api/audit': { records: [] },
  '/api/recovery': { sessions: [] },
  '/api/workspaces': { workspaces: [] },
  '/api/uploads': { uploads: [] },
  '/api/approvals': { approvals: [] },
  '/api/git': {
    is_repo: true,
    branch: 'main',
    ahead: 0,
    behind: 0,
    changes: GIT_CHANGES,
    commits: [],
  },
  '/api/files': { entries: [] },
  '/api/file': {
    path: 'a.py',
    content: 'print(1)\n',
    size: 9,
    binary: false,
    truncated: false,
    image: false,
    mime: '',
    data_url: '',
    image_too_large: false,
  },
  '/api/thread/thread-dom-other': {
    messages: [
      { role: 'user', text: '分屏问题' },
      { role: 'assistant', text: '分屏回答' },
    ],
    transcript: [],
  },
};

function jsonStub(pathname: string, search: URLSearchParams): any {
  if (pathname === '/api/files') {
    return { entries: TREE_FILES[search.get('path') || ''] || [] };
  }
  if (pathname === '/api/file') {
    const target = search.get('path') || '';
    if (/\.(png|jpe?g|gif|webp|svg|bmp|ico)$/i.test(target)) {
      return {
        path: target,
        size: 12,
        truncated: false,
        binary: true,
        content: '',
        image: true,
        mime: target.toLowerCase().endsWith('.svg')
          ? 'image/svg+xml'
          : 'image/png',
        data_url: 'data:image/png;base64,iVBORw0KGgo=',
        image_too_large: false,
      };
    }
  }
  if (pathname in BASE_STUBS) return BASE_STUBS[pathname];
  if (pathname.startsWith('/api/')) return {};
  return null;
}

function contentType(file: string): string {
  return {
    '.html': 'text/html; charset=utf-8',
    '.js': 'text/javascript',
    '.css': 'text/css',
    '.svg': 'image/svg+xml',
    '.json': 'application/json',
  }[path.extname(file).toLowerCase()] || 'application/octet-stream';
}

function startServer() {
  const server = createServer(async (req, res) => {
    const url = new URL(req.url || '/', 'http://127.0.0.1');
    let file: string;
    if (url.pathname === '/' || url.pathname === '/index.html') {
      file = path.join(STATIC, 'index.html');
    } else if (url.pathname.startsWith('/static/')) {
      file = path.join(STATIC, url.pathname.slice('/static/'.length));
    } else {
      res.statusCode = 404;
      res.end();
      return;
    }
    try {
      const body = await readFile(file);
      res.statusCode = 200;
      res.setHeader('Content-Type', contentType(file));
      res.end(body);
    } catch (err) {
      res.statusCode = 404;
      res.end();
    }
  });
  return new Promise<any>((resolve) => {
    server.listen(0, '127.0.0.1', () => resolve(server));
  });
}

function makeFetch() {
  return async (input: any) => {
    const url = new URL(String(input), 'http://127.0.0.1/');
    const body = jsonStub(url.pathname, url.searchParams);
    const payload = body === null ? {} : body;
    return new Response(JSON.stringify(payload), {
      status: 200,
      headers: { 'content-type': 'application/json' },
    });
  };
}

export function getData(dom: JSDOM): any {
  const window = dom.window as any;
  const body = window.document.body;
  if (window.Alpine && typeof window.Alpine.$data === 'function') {
    return window.Alpine.$data(body);
  }
  return body._x_dataStack ? body._x_dataStack[0] : null;
}

export async function waitFor(
  predicate: () => any,
  label = 'condition',
  timeout = 8000,
) {
  const start = Date.now();
  while (Date.now() - start < timeout) {
    try {
      if (predicate()) return;
    } catch (err) { /* 重试 */ }
    await new Promise((resolve) => setTimeout(resolve, 25));
  }
  throw new Error(`等待超时：${label}`);
}

export async function startApp() {
  const server = await startServer();
  const port = server.address().port;
  const warnings: string[] = [];
  const dom = await JSDOM.fromURL(`http://127.0.0.1:${port}/`, {
    resources: 'usable',
    runScripts: 'dangerously',
    pretendToBeVisual: true,
    beforeParse(window: any) {
      const originalWarn = window.console.warn.bind(window.console);
      window.console.warn = (...args: any[]) => {
        const line = args.map((item) => String(item)).join(' ');
        warnings.push(line);
        // CSP 表达式错误在测试末尾统一断言，避免刷屏掩盖真实失败。
        if (!line.includes('unable to interpret')) originalWarn(...args);
      };
      window.fetch = makeFetch();
      window.Headers = globalThis.Headers;
      window.structuredClone = globalThis.structuredClone;
      window.matchMedia = () => ({
        matches: false,
        media: '',
        addEventListener() {},
        removeEventListener() {},
        addListener() {},
        removeListener() {},
      });
      window.ResizeObserver = class {
        observe() {}
        unobserve() {}
        disconnect() {}
      };
      window.scrollTo = () => {};
      window.Element.prototype.scrollIntoView = () => {};
      window.confirm = () => true;
      window.prompt = () => null;
      window.alert = () => {};
    },
  });

  await waitFor(
    () => {
      const data = getData(dom);
      return Boolean(data && data.ready);
    },
    '应用启动',
  );
  const data = getData(dom);
  data.threadId = 'thread-dom-test';
  return {
    dom,
    data,
    warnings,
    window: dom.window as any,
    document: dom.window.document,
    async close() {
      dom.window.close();
      await new Promise((resolve) => server.close(resolve));
    },
  };
}
