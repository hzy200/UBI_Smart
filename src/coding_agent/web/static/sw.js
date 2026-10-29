/* CodingAgent 应用外壳 Service Worker。
 *
 * 只缓存同源静态资源与首页；API、config、healthz、SSE 永远走网络，
 * 避免把认证数据或事件流写进缓存。
 */

const CACHE = 'codingagent-shell-v2';
const SHELL = [
  '/',
  '/static/index.html',
  '/static/app.js',
  '/static/style.css',
  '/static/manifest.webmanifest',
  '/static/favicon-32.png',
  '/static/icon-192.png',
  '/static/icon-512.png',
  '/static/apple-touch-icon.png',
  '/static/vendor/alpine-csp.min.js',
  '/static/vendor/highlight.min.js',
  '/static/vendor/marked.min.js',
  '/static/vendor/purify.min.js',
];

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(CACHE)
      .then((cache) => cache.addAll(SHELL))
      .then(() => self.skipWaiting()),
  );
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(
        keys.filter((key) => key !== CACHE).map((key) => caches.delete(key)),
      ))
      .then(() => self.clients.claim()),
  );
});

function isNetworkOnly(url) {
  return (
    url.pathname.startsWith('/api/')
    || url.pathname === '/healthz'
    || url.pathname === '/config.json'
    || url.pathname === '/sw.js'
    // 管理端是独立入口：不能回退到用户端外壳，也不缓存其页面。
    || url.pathname === '/admin'
    || url.pathname.startsWith('/admin/')
  );
}

self.addEventListener('fetch', (event) => {
  const request = event.request;
  if (request.method !== 'GET') return;
  const url = new URL(request.url);
  if (url.origin !== self.location.origin || isNetworkOnly(url)) return;

  // app.js / index.html 必须网络优先，否则部署后已安装的客户端会一直跑旧壳。
  if (
    request.mode === 'navigate'
    || url.pathname === '/static/app.js'
    || url.pathname === '/static/index.html'
  ) {
    event.respondWith(
      fetch(request)
        .then((response) => {
          if (response.ok) {
            const copy = response.clone();
            const key = request.mode === 'navigate' ? '/' : request;
            caches.open(CACHE).then((cache) => cache.put(key, copy));
          }
          return response;
        })
        .catch(() => caches.match(request).then((hit) => (
          hit || caches.match('/static/index.html')
        ))),
    );
    return;
  }

  event.respondWith(
    caches.match(request).then((hit) => {
      if (hit) return hit;
      return fetch(request).then((response) => {
        if (response.ok) {
          const copy = response.clone();
          caches.open(CACHE).then((cache) => cache.put(request, copy));
        }
        return response;
      });
    }),
  );
});
