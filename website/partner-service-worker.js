const CACHE_VERSION = "anyaicam-partner-shell-v2";

const STATIC_SHELL = [
  "/partner.html",
  "/sales-calculator.html",
  "/styles.css",
  "/full-logo-transparent.png",
  "/partner-icon-180.png",
  "/partner-icon-192.png",
  "/partner-icon-512.png",
  "/partner-icon-maskable-512.png",
  "/partner-offline.html"
];

const ALLOWED_PATHS = new Set(STATIC_SHELL);
const PRIVATE_OR_DYNAMIC =
  /(?:api\.php|config\.php|stripe|checkout|payment|webhook|support-ticket\.php|welcome-email\.php)/i;

function isPartnerAppRequest(url, request) {
  if (url.origin !== self.location.origin) return false;
  if (PRIVATE_OR_DYNAMIC.test(url.pathname)) return false;

  if (ALLOWED_PATHS.has(url.pathname)) return true;

  // Permit only assets loaded directly by the partner portal or calculator.
  const referrer = request.referrer || "";
  const fromPartnerApp =
    referrer.includes("/partner.html") ||
    referrer.includes("/sales-calculator.html");

  return fromPartnerApp &&
    ["style", "script", "image", "font"].includes(request.destination);
}

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(CACHE_VERSION)
      .then((cache) => cache.addAll(STATIC_SHELL))
      .then(() => self.skipWaiting())
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) =>
        Promise.all(
          keys
            .filter((key) => key.startsWith("anyaicam-partner-shell-") && key !== CACHE_VERSION)
            .map((key) => caches.delete(key))
        )
      )
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (event) => {
  const request = event.request;
  if (request.method !== "GET") return;

  const url = new URL(request.url);
  if (!isPartnerAppRequest(url, request)) return;

  if (request.mode === "navigate") {
    event.respondWith(
      fetch(request)
        .then((response) => {
          if (response.ok && ALLOWED_PATHS.has(url.pathname)) {
            const copy = response.clone();
            caches.open(CACHE_VERSION).then((cache) => cache.put(url.pathname, copy));
          }
          return response;
        })
        .catch(async () =>
          (await caches.match(url.pathname)) ||
          (await caches.match("/partner-offline.html"))
        )
    );
    return;
  }

  if (["style", "script", "image", "font"].includes(request.destination)) {
    event.respondWith(
      caches.match(request).then((cached) =>
        cached ||
        fetch(request).then((response) => {
          if (response.ok) {
            const copy = response.clone();
            caches.open(CACHE_VERSION).then((cache) => cache.put(request, copy));
          }
          return response;
        })
      )
    );
  }
});

// Ready for a future server-side push subscription phase.
self.addEventListener("push", (event) => {
  if (!event.data) return;
  let payload = {};
  try {
    payload = event.data.json();
  } catch {
    payload = { title: "ANY AI CAM", body: event.data.text() };
  }

  event.waitUntil(
    self.registration.showNotification(payload.title || "ANY AI CAM", {
      body: payload.body || "Your partner portal has an update.",
      icon: "/partner-icon-192.png",
      badge: "/partner-icon-192.png",
      data: { url: payload.url || "/partner.html" }
    })
  );
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  event.waitUntil(
    clients.openWindow(event.notification.data?.url || "/partner.html")
  );
});
