/* Saigon Spices KDS service worker.
   Chỉ cache "vỏ" tĩnh để cài được PWA và chịu được lúc server chớp tắt.
   KHÔNG đụng vào /api/ (bao gồm SSE /api/stream) và /webhooks/ — luôn để đi
   thẳng ra mạng, giữ dữ liệu realtime. */
const CACHE = "saigon-kds-v1";
const SHELL = [
  "/kitchen", "/expo", "/kds.css", "/kds.js",
  "/icon-kitchen-192.png", "/icon-kitchen-512.png",
  "/icon-expo-192.png", "/icon-expo-512.png",
];

self.addEventListener("install", (e) => {
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", (e) => {
  e.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k)))
    ).then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (e) => {
  const url = new URL(e.request.url);
  if (e.request.method !== "GET") return;
  // Dữ liệu động: không bao giờ chạm vào, để đi thẳng ra server.
  if (url.pathname.startsWith("/api/") || url.pathname.startsWith("/webhooks/")) return;

  // Vỏ tĩnh: ưu tiên mạng, hỏng thì lấy bản đã cache.
  e.respondWith(
    fetch(e.request)
      .then((res) => {
        const copy = res.clone();
        caches.open(CACHE).then((c) => c.put(e.request, copy)).catch(() => {});
        return res;
      })
      .catch(() => caches.match(e.request).then((m) => m || caches.match("/kitchen")))
  );
});
