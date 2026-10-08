// sw.js — permite instalar la app y abrirla aunque no haya conexión
// (muestra las últimas noticias descargadas).
const CACHE = "titulares-v1";
const BASICOS = ["./", "index.html", "manifest.json", "icono-192.png", "icono-512.png"];

self.addEventListener("install", ev => {
  ev.waitUntil(caches.open(CACHE).then(c => c.addAll(BASICOS)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", ev => {
  ev.waitUntil(caches.keys()
    .then(claves => Promise.all(claves.filter(k => k !== CACHE).map(k => caches.delete(k))))
    .then(() => self.clients.claim()));
});

// Siempre intenta traer lo más nuevo de internet; si no hay conexión, usa la copia guardada.
self.addEventListener("fetch", ev => {
  const url = new URL(ev.request.url);
  if (ev.request.method !== "GET" || url.origin !== location.origin) return;
  const clave = url.pathname.endsWith("noticias.json") ? "noticias.json" : ev.request;
  ev.respondWith(
    fetch(ev.request).then(resp => {
      if (resp.ok) {
        const copia = resp.clone();
        caches.open(CACHE).then(c => c.put(clave, copia));
      }
      return resp;
    }).catch(() => caches.match(clave, { ignoreSearch: true }))
  );
});
