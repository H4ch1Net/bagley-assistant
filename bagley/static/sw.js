// Bagley's service worker: makes the web UI an installable app (the phone over Tailscale).
//
// - The app shell (page, stylesheet, modules, vendor files, icons) is cached per version.
//   Navigations and same-origin static files go to the network first and fall back to the cache
//   when the server can't be reached, so an update shows up as soon as the server answers.
// - /api/ requests and the WebSocket never touch the cache.
// - A navigation that fails gets the offline page from the cache; it retries by itself.
// - Clicking a notification focuses the app, or opens it, at notification.data.url.
//
// Served from /sw.js (scope "/") by bagley/api/pwa.py, which fills in the version.

const VERSION = "{{version}}";
const CACHE = `bagley-shell-${VERSION}`;
const OFFLINE_URL = "/__offline";

const SHELL = [
  "/",
  "/static/css/app.css",
  "/static/vendor/marked.esm.js",
  "/static/vendor/purify.esm.js",
  "/static/fonts/JetBrainsMono.woff2",
  "/static/favicon.svg",
  "/static/icons.svg",
  "/static/manifest.webmanifest",
  "/static/icons/icon-192.png",
  "/static/icons/icon-512.png",
  "/static/icons/maskable-512.png",
  "/static/icons/apple-touch-icon.png",
  "/static/js/api.js",
  "/static/js/approvals.js",
  "/static/js/attachments.js",
  "/static/js/avatar.js",
  "/static/js/bar.js",
  "/static/js/chat.js",
  "/static/js/main.js",
  "/static/js/markdown.js",
  "/static/js/panels/appearance.js",
  "/static/js/panels/audit.js",
  "/static/js/panels/automations.js",
  "/static/js/panels/bench.js",
  "/static/js/panels/devices.js",
  "/static/js/panels/general.js",
  "/static/js/panels/knowledge.js",
  "/static/js/panels/life.js",
  "/static/js/panels/memory.js",
  "/static/js/panels/model.js",
  "/static/js/panels/notifications.js",
  "/static/js/panels/routines.js",
  "/static/js/panels/shell.js",
  "/static/js/panels/study.js",
  "/static/js/panels/tools.js",
  "/static/js/panels/voice.js",
  "/static/js/panels/watchdog.js",
  "/static/js/panels/work.js",
  "/static/js/routines.js",
  "/static/js/settings-kit.js",
  "/static/js/settings.js",
  "/static/js/sidebar.js",
  "/static/js/state.js",
  "/static/js/theme.js",
  "/static/js/ui.js",
  "/static/js/util.js",
  "/static/js/voice.js",
  "/static/js/watchdog.js",
];

const OFFLINE_HTML = `<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="theme-color" content="#0E0E0E">
<meta http-equiv="refresh" content="15">
<title>Bagley // NO SIGNAL</title>
<style>
  :root { color-scheme: dark; }
  html, body { margin: 0; height: 100%; background: #0E0E0E; color: #CACACA;
    font: 14px/1.6 "JetBrainsMono Nerd Font", "JetBrains Mono", ui-monospace, monospace; }
  main { box-sizing: border-box; min-height: 100%; display: grid; place-items: center; padding: 24px 16px; }
  .frame { position: relative; box-sizing: border-box; width: 100%; max-width: 420px; padding: 28px 24px; }
  .frame::before, .frame::after, .frame i::before, .frame i::after {
    content: ""; position: absolute; width: 7px; height: 7px; border: 0 solid #FFFFFF; }
  .frame::before { top: 0; left: 0; border-top-width: 1px; border-left-width: 1px; }
  .frame::after { top: 0; right: 0; border-top-width: 1px; border-right-width: 1px; }
  .frame i::before { bottom: 0; left: 0; border-bottom-width: 1px; border-left-width: 1px; }
  .frame i::after { bottom: 0; right: 0; border-bottom-width: 1px; border-right-width: 1px; }
  h1 { margin: 0; color: #FC3E38; font-size: 20px; font-weight: 500; letter-spacing: .08em; }
  .id { margin: 0 0 20px; color: #7A7A7A; font-size: 12px; letter-spacing: .08em; }
  p { margin: 0 0 12px; }
  a { display: inline-block; margin-top: 8px; padding: 8px 16px; background: #D9D9D9; color: #0E0E0E;
    text-decoration: none; letter-spacing: .08em; }
  a:focus-visible { outline: 1px solid #FFFFFF; outline-offset: 3px; }
</style>
</head>
<body>
<main>
  <div class="frame"><i></i>
    <h1>NO SIGNAL</h1>
    <p class="id">BAGLEY // LINK_LOST</p>
    <p>The Bagley server is not answering.</p>
    <p>Check that Tailscale is connected on this device and that the runner is awake. Retrying every 15 seconds.</p>
    <a href="/">RETRY</a>
  </div>
</main>
</body>
</html>
`;

self.addEventListener("install", (event) => {
  event.waitUntil(
    (async () => {
      const cache = await caches.open(CACHE);
      // One by one, so a file that moved doesn't keep the rest out of the cache.
      await Promise.allSettled(SHELL.map((url) => cache.add(new Request(url, { cache: "no-cache" }))));
      await cache.put(
        OFFLINE_URL,
        new Response(OFFLINE_HTML, {
          headers: {
            "Content-Type": "text/html; charset=utf-8",
            "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'",
          },
        }),
      );
      await self.skipWaiting();
    })(),
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    (async () => {
      const names = await caches.keys();
      await Promise.all(
        names.filter((name) => name.startsWith("bagley-") && name !== CACHE).map((name) => caches.delete(name)),
      );
      await self.clients.claim();
    })(),
  );
});

function passThrough(request, url) {
  return (
    request.method !== "GET" ||
    url.origin !== self.location.origin ||
    url.pathname.startsWith("/api/") ||
    url.pathname === "/sw.js" ||
    request.headers.get("upgrade") === "websocket"
  );
}

async function fromNetwork(event) {
  const cache = await caches.open(CACHE);
  try {
    // Revalidate even when the browser's HTTP cache thinks its copy is fresh.
    const response = await fetch(event.request, { cache: "no-cache" });
    if (response.ok && response.type === "basic") {
      event.waitUntil(cache.put(event.request, response.clone()));
    }
    return response;
  } catch (error) {
    const cached =
      (await cache.match(event.request)) || (await cache.match(event.request, { ignoreSearch: true }));
    if (cached) return cached;
    throw error;
  }
}

async function offline() {
  const cached = await caches.match(OFFLINE_URL, { cacheName: CACHE });
  return cached || new Response("NO SIGNAL", { status: 503, headers: { "Content-Type": "text/plain" } });
}

async function navigate(event) {
  try {
    const response = await fetch(event.request);
    // tailscale serve answers 502 while its machine is up but Bagley is not.
    return response.status >= 502 && response.status <= 504 ? offline() : response;
  } catch {
    return offline();
  }
}

self.addEventListener("fetch", (event) => {
  const url = new URL(event.request.url);
  if (passThrough(event.request, url)) return; // The browser handles it; nothing is cached.
  if (event.request.mode === "navigate") {
    event.respondWith(navigate(event));
  } else if (url.pathname.startsWith("/static/")) {
    event.respondWith(fromNetwork(event));
  }
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const data = event.notification.data || {};
  let target = new URL("/", self.location.origin);
  try {
    const wanted = new URL(data.url || "/", self.location.origin);
    if (wanted.origin === self.location.origin) target = wanted; // Only ever open this app.
  } catch {
    // Keep the app's root.
  }
  event.waitUntil(
    (async () => {
      const windows = await self.clients.matchAll({ type: "window", includeUncontrolled: true });
      const open = windows.find((client) => new URL(client.url).origin === self.location.origin);
      if (!open) {
        await self.clients.openWindow(target.href);
        return;
      }
      await open.focus();
      if (open.url !== target.href && "navigate" in open) {
        try {
          await open.navigate(target.href);
        } catch {
          open.postMessage({ type: "navigate", url: target.href }); // Not controlled by us yet.
        }
      }
    })(),
  );
});
