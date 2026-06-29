const CACHE_NAME = "mastercard-pwa-v3";

const STATIC_ASSETS = [
  "/static/img/mc-apple-touch-icon.png",
  "/static/img/mc-favicon-32x32.png",
  "/static/img/mc-favicon-16x16.png",
  "/static/img/mc-icon.png",
  "/static/img/mc-icon-192.png",
  "/static/img/mc-icon-512.png",
  "/static/img/mc-manifest.json"
];

self.addEventListener("install", function(event) {
  event.waitUntil(
    caches.open(CACHE_NAME)
      .then(function(cache) {
        return cache.addAll(STATIC_ASSETS);
      })
      .catch(function() {})
      .then(function() {
        return self.skipWaiting();
      })
  );
});

self.addEventListener("activate", function(event) {
  event.waitUntil(
    caches.keys()
      .then(function(keys) {
        return Promise.all(
          keys
            .filter(function(key) { return key !== CACHE_NAME; })
            .map(function(key) { return caches.delete(key); })
        );
      })
      .then(function() {
        return self.clients.claim();
      })
  );
});

self.addEventListener("fetch", function(event) {
  const request = event.request;
  const url = new URL(request.url);

  if (request.mode === "navigate") {
    event.respondWith(fetch(request));
    return;
  }

  if (url.pathname.startsWith("/static/img/")) {
    event.respondWith(
      caches.match(request).then(function(cachedResponse) {
        return cachedResponse || fetch(request).then(function(networkResponse) {
          const clone = networkResponse.clone();
          caches.open(CACHE_NAME).then(function(cache) {
            cache.put(request, clone);
          });
          return networkResponse;
        });
      })
    );
  }
});