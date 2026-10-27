/* 真实浏览器 E2E 的静态 + 桩 API 服务器。
 *
 * 只服务构建后的 static 目录；API 返回固定的最小载荷，确保 Playwright
 * 能覆盖壳层、草稿与语言切换，而不依赖 Python 服务或真实模型。
 */

import { createServer } from 'node:http';
import { readFile } from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const STATIC = path.resolve(HERE, '../../../static');
const PORT = Number(process.env.E2E_PORT || 4173);

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

const SESSIONS = {
  sessions: [
    {
      thread_id: 'thread-e2e-a',
      title: '未读会话',
      workspace: '/tmp/ws',
      unread: true,
      pinned: false,
      archived: false,
      tags: ['e2e'],
      last_active: '2026-10-27T10:00:00+00:00',
      prompts: 1,
      tool_calls: 0,
      last_message: '历史回答',
    },
    {
      thread_id: 'thread-e2e-b',
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
};

const BASE_STUBS = {
  '/config.json': CONFIG,
  '/healthz': {
    api_version: 'v0',
    min_frontend_version: '0.1.0',
    version: '0.1.0',
    capabilities_version: 1,
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
  '/api/sessions': SESSIONS,
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
    changes: [],
    commits: [],
  },
  '/api/files': { entries: [{ name: 'a.py', path: 'a.py', is_dir: false, size: 9 }] },
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
  '/api/thread/thread-e2e-a': {
    messages: [
      { role: 'user', text: '历史问题' },
      { role: 'assistant', text: '历史回答' },
    ],
    transcript: [],
  },
  '/api/thread/thread-e2e-b': {
    messages: [{ role: 'user', text: '另一个会话' }],
    transcript: [],
  },
};

function contentType(file) {
  return {
    '.html': 'text/html; charset=utf-8',
    '.js': 'text/javascript; charset=utf-8',
    '.css': 'text/css; charset=utf-8',
    '.svg': 'image/svg+xml',
    '.json': 'application/json; charset=utf-8',
    '.webmanifest': 'application/manifest+json',
  }[path.extname(file).toLowerCase()] || 'application/octet-stream';
}

function json(res, payload, status = 200) {
  res.statusCode = status;
  res.setHeader('Content-Type', 'application/json; charset=utf-8');
  res.end(JSON.stringify(payload));
}

const server = createServer(async (req, res) => {
  const url = new URL(req.url || '/', 'http://127.0.0.1');
  if (url.pathname === '/healthz') {
    json(res, BASE_STUBS['/healthz']);
    return;
  }
  if (url.pathname === '/config.json') {
    json(res, CONFIG);
    return;
  }
  if (url.pathname.startsWith('/api/')) {
    json(res, BASE_STUBS[url.pathname] || {});
    return;
  }
  let file;
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

server.listen(PORT, '127.0.0.1', () => {
  console.log(`e2e server ready on http://127.0.0.1:${PORT}`);
});
