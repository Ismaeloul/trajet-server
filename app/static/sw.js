/* Trajet — service worker.

   Existe por una sola razón: que al abrir el icono desde la pantalla de
   inicio salga la interfaz aunque el móvil no tenga cobertura o el NAS esté
   apagado, en vez del dinosaurio. La pantalla aparece con su último dato y
   el aviso de «sin conexión» que ya sabe pintar la app.

   Dos reglas y ninguna más:

   1. /api/* NUNCA pasa por aquí. Son datos de hace segundos y cada llamada
      cuenta contra la cuota diaria; una respuesta cacheada sería un tren que
      ya se ha ido. Se dejan pasar sin tocarlas.
   2. El armazón (HTML, CSS, JS, iconos) va a red primero con un límite de
      espera corto, y si no contesta se sirve de la caché. Así una versión
      nueva entra sola en el primer arranque con red —nada de subir números
      de versión a mano— y sin red arranca igual de rápido.

   Solo se registra en contexto seguro (HTTPS o localhost). Por http:// a la
   IP del NAS el navegador no lo permite y la app funciona exactamente igual,
   salvo que sin red no abre. Ver app.js:registerSW(). */

const CACHE = 'trajet-shell';
const RED_MS = 2500;   // lo que se espera a la red antes de tirar de caché

const SHELL = [
  '/',
  '/static/style.css',
  '/static/app.js',
  '/manifest.webmanifest',
  '/static/icons/icon-192.png',
  '/static/icons/icon-512.png',
  '/static/icons/apple-touch-icon.png',
  '/static/icons/favicon-32.png',
  '/static/fonts/archivo.woff2',
];

self.addEventListener('install', e => {
  // addAll falla entero si falla un solo fichero; se piden de uno en uno
  // para que un icono que falte no deje la app sin armazón cacheado.
  e.waitUntil((async () => {
    const c = await caches.open(CACHE);
    await Promise.all(SHELL.map(u => c.add(u).catch(() => {})));
    self.skipWaiting();
  })());
});

self.addEventListener('activate', e => {
  e.waitUntil((async () => {
    const names = await caches.keys();
    await Promise.all(names.filter(n => n !== CACHE).map(n => caches.delete(n)));
    await self.clients.claim();
  })());
});

self.addEventListener('fetch', e => {
  const req = e.request;
  if (req.method !== 'GET') return;

  const url = new URL(req.url);
  if (url.origin !== self.location.origin) return;
  if (url.pathname.startsWith('/api/')) return;   // regla 1

  e.respondWith(redPrimero(req));
});

async function redPrimero(req) {
  const cache = await caches.open(CACHE);

  try {
    const res = await conLimite(fetch(req), RED_MS);
    // Solo se guarda lo que de verdad sirve: un 404 o un 500 en la caché
    // sería peor que no tener nada.
    if (res && res.ok && res.type === 'basic') cache.put(req, res.clone());
    return res;
  } catch (_) {
    const hit = await cache.match(req);
    if (hit) return hit;
    // Navegación sin copia de esa URL concreta: vale la portada, que es la
    // única página que hay (el resto son pestañas del mismo documento).
    if (req.mode === 'navigate') {
      const home = await cache.match('/');
      if (home) return home;
    }
    return new Response('Sin conexión y sin copia local.', {
      status: 503,
      headers: { 'Content-Type': 'text/plain; charset=utf-8' },
    });
  }
}

function conLimite(promesa, ms) {
  return new Promise((ok, ko) => {
    const t = setTimeout(() => ko(new Error('la red tarda demasiado')), ms);
    promesa.then(r => { clearTimeout(t); ok(r); },
                 e => { clearTimeout(t); ko(e); });
  });
}
