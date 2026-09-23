/* Ejecuta el service worker de verdad (app/static/sw.js) contra una red y una
   cache falsas. Sin navegador.

   Un service worker roto es de lo poco que puede dejar la app inservible
   *después* de instalarla: se queda residente y contesta él a todo. Estas
   comprobaciones son las dos promesas que hace: que /api/ pasa de largo (son
   datos de segundos y cuestan cuota) y que sin red sale el armazón guardado.

   node tools/test_sw.js
*/

const fs = require('fs');
const path = require('path');

const SRC = fs.readFileSync(
  path.join(__dirname, '..', 'app', 'static', 'sw.js'), 'utf8');

const FAILS = [];
function check(name, cond, extra = '') {
  console.log(`  ${cond ? 'ok ' : 'MAL'} ${name}` + (cond ? '' : `   <<< ${extra}`));
  if (!cond) FAILS.push(name);
}

const ORIGIN = 'https://nas.ts.net';

/* --- una cache de mentira, con la misma forma que la del navegador --- */
function fakeCaches() {
  const stores = new Map();
  const api = {
    async open(name) {
      if (!stores.has(name)) stores.set(name, new Map());
      const m = stores.get(name);
      return {
        async add(url) {
          const r = await api._fetch({ url: ORIGIN + url, method: 'GET' });
          if (!r.ok) throw new Error('no');
          m.set(url, r);
        },
        put(req, res) { m.set(new URL(req.url).pathname, res); },
        async match(req) {
          const p = typeof req === 'string' ? req : new URL(req.url).pathname;
          return m.get(p);
        },
      };
    },
    async keys() { return [...stores.keys()]; },
    async delete(n) { return stores.delete(n); },
    _stores: stores,
  };
  return api;
}

function res(body, { ok = true, type = 'basic', status = 200 } = {}) {
  return { body, ok, type, status, clone() { return this; } };
}

function arranca(fetchImpl) {
  const listeners = {};
  const caches = fakeCaches();
  caches._fetch = fetchImpl;
  const self = {
    addEventListener: (n, f) => { listeners[n] = f; },
    location: { origin: ORIGIN },
    skipWaiting() {},
    clients: { claim() {} },
  };
  // eslint-disable-next-line no-new-func
  new Function('self', 'caches', 'fetch', 'Response', 'URL', 'setTimeout',
               'clearTimeout', SRC)(
    self, caches, fetchImpl, function Res(body, init) {
      return res(body, { ok: false, status: (init && init.status) || 200 });
    }, URL, setTimeout, clearTimeout);
  return { listeners, caches };
}

function evento(url, { method = 'GET', mode = 'no-cors' } = {}) {
  const e = {
    request: { url: ORIGIN + url, method, mode },
    respondWith(p) { e._res = p; },
    waitUntil(p) { e._espera = p; },
  };
  return e;
}

(async () => {
  console.log('\n=== lo que no debe tocar ===');
  {
    const { listeners } = arranca(async () => res('red'));
    const casos = [
      ['/api/board', {}, 'una llamada a la API'],
      ['/api/health', {}, 'otra llamada a la API'],
      ['/', { method: 'POST' }, 'algo que no es GET'],
    ];
    for (const [url, opt, que] of casos) {
      const e = evento(url, opt);
      listeners.fetch(e);
      check(`${que} pasa de largo`, e._res === undefined);
    }
    const fuera = {
      request: { url: 'https://otro.sitio/x', method: 'GET', mode: 'no-cors' },
      respondWith(p) { this._res = p; },
    };
    listeners.fetch(fuera);
    check('otro dominio pasa de largo', fuera._res === undefined);
  }

  console.log('\n=== con red ===');
  {
    let llamadas = 0;
    const { listeners, caches } = arranca(async () => { llamadas++; return res('css nuevo'); });
    const e = evento('/static/style.css');
    listeners.fetch(e);
    const r = await e._res;
    check('sirve lo que dice la red', r.body === 'css nuevo');
    check('y lo guarda para la proxima', llamadas === 1
      && (await (await caches.open('trajet-shell')).match('/static/style.css')));
  }

  console.log('\n=== sin red ===');
  {
    let caida = false;
    const { listeners } = arranca(async () => {
      if (caida) throw new Error('sin conexion');
      return res('app.js guardado');
    });
    // primero con red, para tener copia
    await (() => { const e = evento('/static/app.js'); listeners.fetch(e); return e._res; })();
    caida = true;
    const e = evento('/static/app.js');
    listeners.fetch(e);
    const r = await e._res;
    check('sale la copia guardada', r.body === 'app.js guardado');

    // Una URL que nunca se cacheo, pero es navegacion: vale la portada, que
    // es la unica pagina que hay (el resto son pestanas del mismo documento).
    caida = false;
    await (() => { const e = evento('/'); listeners.fetch(e); return e._res; })();
    caida = true;
    const e2 = evento('/lo-que-sea', { mode: 'navigate' });
    listeners.fetch(e2);
    const r2 = await e2._res;
    check('la portada cacheada cubre cualquier navegacion',
          r2 && r2.body === 'app.js guardado', JSON.stringify(r2));

    // Y si no hay ni portada, se dice, no se cuelga.
    const { listeners: l3 } = arranca(async () => { throw new Error('nada'); });
    const e3 = evento('/', { mode: 'navigate' });
    l3.fetch(e3);
    const r3 = await e3._res;
    check('sin nada guardado, un 503 con su explicacion', r3.status === 503);
  }

  console.log('\n=== respuestas malas ===');
  {
    const { listeners, caches } = arranca(async () => res('404', { ok: false, status: 404 }));
    const e = evento('/static/no-existe.css');
    listeners.fetch(e);
    await e._res;
    const c = await caches.open('trajet-shell');
    check('un 404 no se guarda en la cache', !(await c.match('/static/no-existe.css')));
  }

  console.log('\n=== la red que se cuelga ===');
  {
    let colgada = false;
    const { listeners } = arranca(() => colgada
      ? new Promise(() => {})           // no contesta nunca
      : Promise.resolve(res('estilo')));
    await (() => { const e = evento('/static/style.css'); listeners.fetch(e); return e._res; })();
    colgada = true;
    const t0 = Date.now();
    const e = evento('/static/style.css');
    listeners.fetch(e);
    const r = await e._res;
    const ms = Date.now() - t0;
    check(`no espera a la red mas de 3 s (tardo ${ms} ms)`, ms < 3200);
    check('y mientras sirve la copia', r.body === 'estilo');
  }

  console.log('\n=== instalacion ===');
  {
    const { listeners, caches } = arranca(async req =>
      req.url.endsWith('icon-512.png')
        ? res('roto', { ok: false, status: 500 })   // uno falla a proposito
        : res('ok'));
    const e = { waitUntil(p) { this._p = p; } };
    listeners.install(e);
    await e._p;
    const c = await caches.open('trajet-shell');
    check('un fichero que falle no deja la app sin armazon',
          !!(await c.match('/static/app.js')) && !!(await c.match('/')));
  }

  console.log('\n=== limpieza de versiones viejas ===');
  {
    const { listeners, caches } = arranca(async () => res('x'));
    (await caches.open('trajet-viejo')).put({ url: ORIGIN + '/a' }, res('a'));
    await caches.open('trajet-shell');
    const e = { waitUntil(p) { this._p = p; } };
    listeners.activate(e);
    await e._p;
    check('se borra lo que sobra', !caches._stores.has('trajet-viejo'));
  }

  console.log();
  if (FAILS.length) {
    console.log(`FALLAN ${FAILS.length}: ${FAILS.join(', ')}`);
    process.exit(1);
  }
  console.log('TODAS LAS COMPROBACIONES OK');
})();
