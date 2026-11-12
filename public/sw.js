/*
 * Service worker: offline support and updates for Smart Money Tracker.
 *
 * The block between @build-start and @build-end is rewritten by deploy.sh
 * on every deploy (a new build id plus the current list of site files), so
 * every deploy produces a new worker and browsers pick it up. Never cached.
 *
 * Fetch strategy:
 *   page loads ............ network first, cached copy when offline
 *   our own files ......... network first, cached copy when offline (they keep
 *                           the same names between releases, so "cache first"
 *                           could pair a new page with old scripts)
 *   pinned libraries/fonts  cached, refreshed in the background
 *   Supabase reads the pages need to start: network first, last answer when offline
 *   any other API call .... always network, never cached
 */
importScripts("version.js");

// @build-start
const BUILD_ID = "20261112012636-b72edda";
const PRECACHE = ["./", "backtest.html", "icons/apple-touch-icon.png", "icons/icon-192.png", "icons/icon-512.png", "icons/icon-maskable-512.png", "icons/icon.svg", "index.html", "manifest.webmanifest", "pwa.js", "research.html", "theme.css", "theme.js", "version.js"];
// @build-end

const APP = "smt";
const VERSION = (self.SMT_VERSION && self.SMT_VERSION.APP_VERSION) || "0.0.0";
const CACHE = `${APP}-${VERSION}-${BUILD_ID}`;
const API_CACHE = `${APP}-api`;
const LIB_CACHE = `${APP}-libs`;
const KEEP = [CACHE, API_CACHE, LIB_CACHE];

// Read-only tables the pages load at start. Anything else goes straight to the network.
const OFFLINE_TABLES = ["fund_snapshots", "ai_summaries", "position_pnl", "fund_pnl_daily", "closed_positions",
  "backtest_summary", "backtest_daily", "backtest_picks", "locked_cohorts"];
const LIB_HOSTS = ["cdn.jsdelivr.net", "cdnjs.cloudflare.com", "fonts.googleapis.com", "fonts.gstatic.com"];

const SCOPE_PATH = new URL(self.registration.scope).pathname;

self.addEventListener("install", (event) => {
  event.waitUntil((async () => {
    const cache = await caches.open(CACHE);
    // One missing file must not stop the install, so add them one by one.
    await Promise.all(PRECACHE.map(async (path) => {
      try {
        const res = await fetch(new Request(path, { cache: "reload" }));
        if (res.ok) await cache.put(path, res);
      } catch (e) { /* offline or missing: skip */ }
    }));
    // First install: take over straight away. Later versions wait for the page to say so.
    if (!self.registration.active) await self.skipWaiting();
  })());
});

self.addEventListener("activate", (event) => {
  event.waitUntil((async () => {
    const names = await caches.keys();
    await Promise.all(names.filter((n) => n.startsWith(`${APP}-`) && !KEEP.includes(n)).map((n) => caches.delete(n)));
    await self.clients.claim();
  })());
});

self.addEventListener("message", (event) => {
  const msg = event.data || {};
  if (msg.type === "GET_VERSION" && event.ports && event.ports[0]) {
    event.ports[0].postMessage({ version: VERSION, build: BUILD_ID });
  } else if (msg.type === "SKIP_WAITING") {
    self.skipWaiting();
  }
});

async function networkFirst(request, cacheName, { navigation = false } = {}) {
  try {
    const res = await fetch(request);
    if (res && res.ok) {
      const cache = await caches.open(cacheName);
      cache.put(request, res.clone()).catch(() => {});
    }
    return res;
  } catch (e) {
    const hit = await caches.match(request, { ignoreVary: true, ignoreSearch: navigation });
    if (hit) return hit;
    if (navigation) {
      const shell = await caches.match(new URL("index.html", self.registration.scope).href);
      if (shell) return shell;
    }
    return Response.error();
  }
}

async function staleWhileRevalidate(event, cacheName) {
  const cache = await caches.open(cacheName);
  const cached = await cache.match(event.request);
  const network = fetch(event.request).then((res) => {
    if (res && (res.ok || res.type === "opaque")) cache.put(event.request, res.clone()).catch(() => {});
    return res;
  }).catch(() => null);
  event.waitUntil(network);
  return cached || (await network) || Response.error();
}

self.addEventListener("fetch", (event) => {
  const request = event.request;
  if (request.method !== "GET") return;
  const url = new URL(request.url);

  if (url.pathname.includes("/rest/v1/")) {
    const table = url.pathname.split("/rest/v1/")[1].split("/")[0];
    if (OFFLINE_TABLES.includes(table)) event.respondWith(networkFirst(request, API_CACHE));
    return; // other API calls: plain network
  }

  if (url.origin === self.location.origin) {
    if (!url.pathname.startsWith(SCOPE_PATH)) return;
    if (url.pathname.endsWith("/sw.js")) return; // the worker itself is never cached
    if (request.mode === "navigate") event.respondWith(networkFirst(request, CACHE, { navigation: true }));
    else event.respondWith(networkFirst(request, CACHE));
    return;
  }

  if (LIB_HOSTS.includes(url.hostname)) event.respondWith(staleWhileRevalidate(event, LIB_CACHE));
});
