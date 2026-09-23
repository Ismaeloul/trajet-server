/* Trajet — interfaz. Sin frameworks: DOM a pelo. */
'use strict';

const REFRESH_MS = 30000;
// Modos de transporte que nunca traen DeparturePlatformName (comprobado con
// el sondeo de tools/platform_probe.py).
// IDFM manda el modo con acento y mayuscula ("Métro"), asi que se compara
// sin tildes.
const NO_PLATFORM_MODES = new Set(['metro', 'bus', 'tram', 'tramway', 'funicular']);
const plainMode = m => (m || '').normalize('NFD').replace(/[̀-ͯ]/g, '').toLowerCase().trim();
const DAYS = ['L', 'M', 'X', 'J', 'V', 'S', 'D'];

const $  = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];

const state = {
  routes: [],
  activeId: null,
  board: null,
  lastFetch: 0,
  timer: null,
  tick: null,
  editing: null,       // ruta en edicion
  alts: null,          // alternativas cacheadas
  altsFor: null,
  plan: { from: null, to: null, options: [], mode: 'arrival' },
  openLeg: null,       // tramo desplegado en pantallas con muchos tramos
  paintedRoute: null,  // ultima ruta pintada, para no reanimar en cada refresco
  pulling: false,      // refresco pedido a mano con el gesto
  seq: 0,              // numero de la ultima peticion de tablero lanzada
};

/* ---------------- utilidades ---------------- */

async function api(path, opts) {
  const r = await fetch(path, {
    headers: { 'Content-Type': 'application/json' },
    ...opts,
  });
  if (!r.ok) {
    let msg = r.statusText;
    try {
      const d = (await r.json()).detail;
      // FastAPI manda una cadena, o una lista de errores de validación.
      if (typeof d === 'string') msg = d;
      else if (Array.isArray(d)) msg = d.map(x => x.msg || JSON.stringify(x)).join('; ');
    } catch (_) {}
    throw new Error(msg);
  }
  return r.json();
}

function esc(s) {
  return String(s == null ? '' : s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

function show(id) {
  $$('.view').forEach(v => v.classList.toggle('active', v.id === id));
}

function debounce(fn, ms) {
  let t;
  return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); };
}

/* Color de linea legible: IDFM da el color de fondo, calculamos el texto */
function textOn(hex) {
  if (!hex) return '#0a0e14';
  const h = hex.replace('#', '');
  if (h.length < 6) return '#0a0e14';
  const r = parseInt(h.slice(0, 2), 16), g = parseInt(h.slice(2, 4), 16),
        b = parseInt(h.slice(4, 6), 16);
  return (r * 299 + g * 587 + b * 114) / 1000 > 145 ? '#0a0e14' : '#ffffff';
}

function badgeStyle(color) {
  const c = lineColor(color);
  return c ? `background:${c};color:${textOn(c)}` : '';
}

/* La forma del distintivo dice el modo sin leer, como en la señalética de la
   estación: círculo el metro, cuadrado el tren y el RER, esquinas blandas el
   tranvía y caja ancha el bus. Un código largo va siempre en caja ancha,
   sea del modo que sea: "6424" no cabe en un círculo. */
function bulletShape(mode, code) {
  const m = plainMode(mode);
  let shape = 'square';
  if (m.startsWith('metro')) shape = 'circle';
  else if (m.startsWith('tram')) shape = 'round';
  else if (m === 'bus' || m.startsWith('noctilien') || m === 'car' || m === 'autocar') shape = 'rect';
  if (code.length > 2 && shape !== 'rect') shape = 'rect';
  return shape;
}

/* El distintivo de línea. Lleva la longitud del código en un atributo para
   que el CSS baje el cuerpo: "13" cabe holgado, "N151" no. */
function bulletHtml(code, color, mode, cls) {
  const c = String(code || '?');
  return `<span class="bul${cls ? ' ' + cls : ''}" data-shape="${bulletShape(mode, c)}"`
       + ` data-len="${Math.min(c.length, 6)}" style="${badgeStyle(color)}">${esc(c)}</span>`;
}

/* El color oficial y el modo de cada línea que aparece en mis rutas, por
   código. Sirve para pintar el historial con los colores y las formas de
   verdad sin pedir nada más. */
function lineByCode(code) {
  for (const r of state.routes) {
    for (const l of r.legs || []) {
      if (l.line_code === code && l.line_color) return { color: l.line_color, mode: l.line_mode };
    }
  }
  return { color: '', mode: '' };
}

/* La tira de salidas solo lleva el borde difuminado si de verdad hay más a
   la derecha. Sin esto la última salida salía apagada aunque cupieran todas. */
function markScrollable(host) {
  if (typeof requestAnimationFrame !== 'function') return;
  requestAnimationFrame(() => {
    $$('.strip', host).forEach(s =>
      s.classList.toggle('scrollable', s.scrollWidth > s.clientWidth + 2));
  });
}

/* El color oficial de la línea, saneado. Es el único color saturado que hay
   en pantalla, así que se usa también para el hilo que une los tramos. */
function lineColor(color) {
  if (!color) return '';
  const hex = String(color).replace(/[^0-9a-fA-F]/g, '');
  return /^([0-9a-f]{3}|[0-9a-f]{6})$/i.test(hex) ? '#' + hex : '';
}


/* Aviso efímero. Nada de alert(): bloquea el navegador y en móvil es horrible. */
let toastTimer = null;
function toast(msg, kind) {
  let el = $('#toast');
  if (!el) {
    el = document.createElement('div');
    el.id = 'toast';
    document.body.appendChild(el);
  }
  el.className = 'show' + (kind ? ' is-' + kind : '');
  el.textContent = msg;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.remove('show'), 3200);
}

/* ---------------- tablero ---------------- */

/* Las salidas van en una tira horizontal, no apiladas.

   Es la idea que más cambia el tablero: apiladas, cada tramo medía ~190 px y
   con cinco tramos no cabían; en tira mide ~135 px, así que los cinco entran
   en una pantalla Y encima se ven las cuatro salidas deslizando el dedo en
   vez de esconderlas detrás de un "+N". */

/* "56 min" se lee de un vistazo; "165 min" no dice nada y no cabe. */
function bigWait(minutes) {
  if (minutes < 60) return `${minutes}<span class="u">min</span>`;
  const h = Math.floor(minutes / 60);
  const m = minutes % 60;
  return `${h}<span class="u">h</span>${String(m).padStart(2, '0')}`;
}

/* Qué hago con el tiempo que me queda. Sale de los minutos, que ya los
   tenemos, y se entiende sin leer: rayo = corre, pies = anda, taza = te da
   tiempo a un café. */
function paceIcon(d) {
  if (d.at_stop || d.minutes <= 0) return '';
  if (d.minutes <= 3) {
    return `<svg class="pace corre" viewBox="0 0 24 24" aria-label="corre">
      <path d="M13 2L4.5 13.5H11l-1.5 8.5L18 10.5h-6.5z"/></svg>`;
  }
  if (d.minutes <= 8) {
    return `<svg class="pace anda" viewBox="0 0 24 24" aria-label="anda">
      <circle cx="13" cy="4" r="1.8"/><path d="M11 21l1.6-5.4-2.6-2.4.9-4.6 3.1 2 2.5 1"/>
      <path d="M9.4 8.6L7 11.4M14.2 15.6L16 21"/></svg>`;
  }
  return `<svg class="pace calma" viewBox="0 0 24 24" aria-label="con calma">
    <path d="M4 9h13v5a4 4 0 0 1-4 4H8a4 4 0 0 1-4-4z"/>
    <path d="M17 10.5h1.6a2.4 2.4 0 0 1 0 4.8H17"/><path d="M4 21h13"/></svg>`;
}

/* La casilla de la vía. El riesgo real es que una vía aprendida se lea como
   confirmada, así que no comparten ni forma ni peso ni color. */
function platformBox(d, hasPlat) {
  if (!hasPlat) return '';
  if (d.platform) {
    return `<span class="via${d.platform_new ? ' fresh' : ''}">
      <small>vía</small>${esc(d.platform)}</span>`;
  }
  if (d.guess) {
    const pct = Math.round((d.guess.share || 0) * 100);
    return `<span class="via guess"
      title="${esc(d.guess.why)}, ${d.guess.samples} veces observado, acierta el ${pct}%">
      <small>probable</small>${esc(d.guess.platform)}</span>`;
  }
  return '';
}

/* La API no da ocupación (211 salidas comprobadas, ni un campo). Sí dice si
   el tren es corto o largo, y eso se usa igual: en uno corto vas más
   apretado y para en otra parte del andén. */
function lengthTag(d) {
  if (!d.length) return '';
  const corto = d.length === 'short';
  return `<span class="tren ${corto ? 'corto' : 'largo'}">
    ${corto ? 'tren corto' : 'tren largo'}</span>`;
}

function renderBoard(b) {
  const host = $('#legs');

  if (b.empty) {
    $('#route-name').textContent = 'Sin rutas';
    $('#route-ends').innerHTML = '';
    host.innerHTML = emptyState(
      'Aún no hay ninguna ruta',
      'Escribes de dónde sales y a dónde vas, eliges el itinerario que usas de '
      + 'verdad y queda vigilado.',
      'Buscar mi trayecto', 'go-plan');
    $('#go-plan').onclick = () => showTab('view-plan');
    $('#alert-strip').classList.add('hidden');
    return;
  }

  $('#route-name').textContent = b.route.name;
  $('#route-ends').innerHTML = b.route.origin_name && b.route.dest_name
    ? `${esc(b.route.origin_name)}<span class="arr">→</span>${esc(b.route.dest_name)}`
    : '';

  // Cuando el dato es viejo se apaga el tablero ENTERO, no solo una etiqueta:
  // si estás mirando minutos que ya no valen, tiene que notarse sin leer.
  $('#view-board').classList.toggle('stale', !!b.stale);

  // La entrada escalonada solo al cambiar de ruta. El tablero se repinta cada
  // 30 s y animar en cada refresco sería un parpadeo constante.
  const nueva = state.paintedRoute !== b.route.id;
  state.paintedRoute = b.route.id;
  host.dataset.anim = nueva ? '1' : '0';

  host.innerHTML = b.legs.map((leg, idx) => {
    const st = leg.status;
    const ultimo = idx === b.legs.length - 1;
    // La banda del tramo lleva el color oficial de la línea y el texto en el
    // color que se lee encima (blanco o tinta, según la luminosidad).
    const lc = lineColor(leg.line_color) || 'var(--ink-3)';
    const lt = lineColor(leg.line_color) ? textOn(lc) : '#ffffff';
    const sig = b.legs[idx + 1];
    const lcNext = sig ? (lineColor(sig.line_color) || 'var(--ink-3)') : lc;

    // Metro, bus y tranvía no publican andén nunca: medido el 30/08 sobre 914
    // trenes, 0 de ~600 pasos de metro/bus traían vía.
    const hasPlat = !NO_PLATFORM_MODES.has(plainMode(leg.line_mode));

    const deps = leg.departures.length
      ? `<div class="strip">${leg.departures.map((d, i) => {
          let sub = esc(d.at);
          if (d.delay != null && d.delay !== 0) {
            const cls = d.delay < 0 ? 'delay neg' : 'delay';
            sub += ` · <span class="${cls}">${d.delay > 0 ? '+' : ''}${d.delay}′</span>`;
          } else if (d.status === 'delayed') {
            sub += ' · <span class="delay">retraso</span>';
          }
          // "en andén" (la API confirma el tren parado ahí) y "ya" (0 min sin
          // confirmar) son cosas distintas y las dos son urgentes.
          const mins = d.at_stop
            ? '<span class="mins now sm">en&nbsp;andén</span>'
            : (d.minutes <= 0
                ? '<span class="mins now">ya</span>'
                : `<span class="mins">${bigWait(d.minutes)}</span>`);
          const extra = platformBox(d, hasPlat) + lengthTag(d);
          return `<div class="dep${i ? '' : ' first'}${hasPlat ? '' : ' no-plat'}">
            <div class="dep-t">${mins}${paceIcon(d)}</div>
            <div class="clock">${sub}</div>
            ${extra ? `<div class="dep-x">${extra}</div>` : ''}
          </div>`;
        }).join('')}</div>`
      : emptyLeg();

    // El aviso va pegado a la línea de la que habla, no en una franja global:
    // con varios tramos tocados, una franja no dice cuál es cuál.
    let aviso = '';
    if (st.level > 0) {
      const es = st.messages_es || [];
      const texto = st.messages.length
        ? (es[0] || st.messages[0])
        : (st.level === 2 ? 'Tráfico interrumpido.' : 'Tráfico perturbado.');
      const orig = st.messages.length && es[0] ? st.messages[0] : '';
      aviso = `<div class="warn lvl${st.level}">
        <svg class="warn-ic" viewBox="0 0 24 24"><path d="M12 3L2.5 20h19z"/><path d="M12 10v4M12 17.5v.5"/></svg>
        <div class="warn-txt">${esc(texto)}
          ${orig ? `<details class="orig"><summary>ver original en francés</summary>${esc(orig)}</details>` : ''}
        </div>
        <div class="warn-foot">
          <button type="button" class="warn-btn" data-alts="1">Buscar alternativa</button>
          ${st.translating ? '<span class="traduciendo">traduciendo…</span>' : ''}
        </div>
      </div>`;
    }

    // Con la línea tocada, el estado va escrito en la propia banda; en
    // normal no hace falta decir nada.
    const estado = st.level > 0 ? `<span class="leg-st">${esc(st.label)}</span>` : '';

    return `<article class="leg${ultimo ? ' last' : ''}" data-seq="${leg.seq}"
             style="--lc:${lc};--lt:${lt};--lc-next:${lcNext};--i:${idx}">
      <header class="leg-band">
        ${bulletHtml(leg.line_code, leg.line_color, leg.line_mode, 'lg')}
        <div class="leg-txt">
          <div class="leg-stop">${esc(leg.from_name)}</div>
          <div class="leg-dir">${leg.directions.length
            ? 'hacia ' + esc(leg.directions.join(' / '))
            : esc(leg.line_mode || '')}</div>
        </div>
        ${estado}
      </header>
      ${aviso}
      ${deps}
    </article>`;
  }).join('');

  $$('.warn-btn', host).forEach(b2 => b2.onclick = loadAlternatives);
  markScrollable(host);
  renderAlerts(b);
}

/* Pantalla vacía compuesta: icono, qué pasa y qué hacer, con su botón. */
function emptyState(title, text, cta, btnId) {
  return `<div class="empty">
    <div class="ic"><svg viewBox="0 0 24 24"><circle cx="11" cy="11" r="6.5"/><path d="M20 20l-4.4-4.4"/></svg></div>
    <h3>${esc(title)}</h3>
    <p>${esc(text)}</p>
    ${cta ? `<button type="button" id="${btnId}" class="primary">${esc(cta)}</button>` : ''}
  </div>`;
}

/* Un tramo sin salidas puede ser "ya no hay más hoy" o "ahora mismo no sale
   ninguno". La API no lo dice, así que se distingue por la hora en vez de
   afirmar algo que no sabemos. */
function emptyLeg() {
  const h = new Date().getHours();
  const finDeServicio = h >= 23 || h < 5;
  return `<div class="sin-pasos">
    ${finDeServicio
      ? `<svg viewBox="0 0 24 24" class="ic"><path d="M20.5 14.5A8.5 8.5 0 0 1 9.5 3.5a8.5 8.5 0 1 0 11 11z"/></svg>
         <div><b>Servicio finalizado</b><span>no hay más pasos hoy</span></div>`
      : `<svg viewBox="0 0 24 24" class="ic"><circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/></svg>
         <div><b>Sin pasos ahora mismo</b><span>la línea no anuncia ninguno</span></div>`}
  </div>`;
}

/* El aviso de cada línea ya va dentro de su tramo. Aquí solo queda lo que sí
   es global: las alternativas, que valen para el trayecto entero. */
function renderAlerts(b) {
  const strip = $('#alert-strip');
  const troubled = b.legs.filter(l => l.status.level > 0);

  if (!troubled.length) {
    strip.classList.add('hidden');
    strip.innerHTML = '';
    state.alts = null;
    return;
  }
  if (!(state.alts && state.altsFor === b.route.id)) {
    // Sin alternativas pedidas no hay nada global que decir: el aviso ya se
    // ve pegado a su línea.
    strip.classList.add('hidden');
    strip.innerHTML = '';
    return;
  }

  strip.classList.toggle('bad', Math.max(...troubled.map(l => l.status.level)) === 2);
  strip.classList.remove('hidden');
  strip.innerHTML = '<b>Alternativas evitando lo que está tocado</b>'
                  + renderAlts(state.alts);
}

function renderAlts(a) {
  if (!a.options || !a.options.length) {
    return `<div class="alt-opt">El calculador no encuentra alternativa evitando
            esa línea.</div>`;
  }
  return a.options.map(o => {
    const chips = o.legs.map(l =>
      bulletHtml(l.code, l.color, l.mode,
                 'sm ' + (l.status === 'interrumpida' ? 'lvl2'
                          : l.status === 'perturbada' ? 'lvl1' : ''))
    ).join('<span class="arrow">›</span>');
    let dlt = '';
    if (o.delta_minutes != null) {
      const good = o.delta_minutes <= 0;
      dlt = `<span class="dlt ${good ? 'good' : ''}">${good ? '' : '+'}${o.delta_minutes} min</span>`;
    }
    const warn = !o.usable
      ? `<div class="nope">⚠ una de estas líneas también está interrumpida</div>` : '';
    return `<div class="alt-opt">
      <div class="hd"><span class="big">${o.total_minutes} min</span>${dlt}
        <span class="tr">${o.transfers} transb.</span></div>
      <div class="alt-chips">${chips}</div>${warn}
    </div>`;
  }).join('');
}

async function loadAlternatives(ev) {
  // El botón ahora vive dentro del tramo tocado, y puede haber varios.
  const btn = ev && ev.currentTarget ? ev.currentTarget : null;
  const antes = btn ? btn.textContent : '';
  if (btn) { btn.textContent = 'Calculando…'; btn.disabled = true; }
  try {
    const rid = state.board.route.id;
    state.alts = await api(`/api/alternatives/${rid}`);
    state.altsFor = rid;
    renderAlerts(state.board);
  } catch (e) {
    toast('No se pudo calcular la alternativa: ' + e.message, 'bad');
    if (btn) { btn.textContent = antes || 'Buscar alternativa'; btn.disabled = false; }
  }
}

/* ---------------- frescura ---------------- */

function paintFreshness() {
  const el = $('#freshness');
  if (!state.lastFetch) { el.textContent = '—'; return; }
  const secs = Math.round((Date.now() - state.lastFetch) / 1000);
  const b = state.board;

  let txt;
  if (secs < 5) txt = 'ahora mismo';
  else if (secs < 90) txt = `hace ${secs} s`;
  else txt = `hace ${Math.round(secs / 60)} min`;

  // Si el backend sirvió datos viejos de su caché, hay que decirlo
  if (b && b.data_age > 60) {
    txt += ` · dato de hace ${Math.round(b.data_age)} s`;
  }
  if (b && b.last_error) txt += ' · API con fallos';

  el.textContent = txt;
  el.classList.toggle('stale', secs > 90 || !!(b && b.stale));
}

/* Esqueleto de lo que va a aparecer. Es la primera pantalla que ves al abrir
   la app en la calle, y una ruedecita girando sobre negro se siente más lenta
   que la silueta de las tarjetas que ya vienen. */
function paintSkeleton() {
  const host = $('#legs');
  if (host.innerHTML.trim()) return;      // ya hay algo pintado
  host.innerHTML = '<div class="skeleton">'
    + '<div class="sk-leg"></div><div class="sk-leg"></div><div class="sk-leg"></div>'
    + '</div>';
}

async function refresh(routeId) {
  const el = $('#freshness');
  el.classList.add('loading');
  paintSkeleton();
  // Si cambio de ruta mientras llega el tablero anterior, el viejo no debe
  // pintarse encima del nuevo: solo cuenta la última petición lanzada.
  const mine = ++state.seq;
  try {
    const q = routeId != null ? `?route_id=${routeId}` : '';
    const b = await api('/api/board' + q);
    if (mine !== state.seq) return;
    state.board = b;
    state.lastFetch = Date.now();
    if (!b.empty) state.activeId = b.route.id;
    renderBoard(b);
    paintQuota(b.quota);
    restartTick();
  } catch (e) {
    if (mine !== state.seq) return;
    // No borramos la pantalla: se queda el último dato con su antigüedad.
    // El detalle va al aviso; en la pastilla no cabe y desbordaba el título.
    el.textContent = 'sin conexión';
    el.classList.add('stale');
    toast('No llega el tablero: ' + e.message, 'bad');
  } finally {
    if (mine === state.seq) {
      el.classList.remove('loading');
      paintFreshness();
    }
  }
}

function paintQuota(q) {
  if (!q) return;
  const sm = q['stop-monitoring'];
  $('#quota').textContent = sm != null ? `quedan ${sm} llamadas hoy` : '';
}

/* El refresco solo corre con la pantalla delante: si no, se come la cuota
   diaria (1000 llamadas/día) estando en un bolsillo. */
function startLoop() {
  stopLoop();
  state.timer = setInterval(() => {
    if (document.visibilityState === 'visible') refresh(state.activeId);
  }, REFRESH_MS);
  state.tick = setInterval(paintFreshness, 1000);
}
function stopLoop() {
  clearInterval(state.timer); clearInterval(state.tick);
  state.timer = state.tick = null;
}

document.addEventListener('visibilitychange', () => {
  if (document.visibilityState === 'visible') {
    // Al volver, si el dato tiene más de media vuelta de reloj, refresca ya
    if (Date.now() - state.lastFetch > REFRESH_MS / 2) refresh(state.activeId);
    startLoop();
  } else {
    stopLoop();
  }
});

/* ---------------- lista de rutas ---------------- */

async function loadRoutes() {
  const d = await api('/api/routes');
  state.routes = d.routes;
  if (state.activeId == null) state.activeId = d.active_id;
  return d;
}

function routeSub(r) {
  const days = r.days.map(i => DAYS[i]).join('');
  const hora = r.time_mode === 'arrival'  ? `llego ${r.time_at}`
             : r.time_mode === 'departure' ? `salgo ${r.time_at}`
             : `${r.time_from}–${r.time_to}`;
  return `${days}, ${hora}`;
}

function routeWhen(r) {
  return r.time_mode === 'arrival'  ? `llego a las ${r.time_at}`
       : r.time_mode === 'departure' ? `salgo a las ${r.time_at}`
       : `${r.time_from} – ${r.time_to}`;
}

function renderRoutes() {
  const host = $('#routes-list');
  if (!state.routes.length) {
    host.innerHTML = emptyState(
      'Sin rutas todavía',
      'Busca tu trayecto de puerta a puerta y guarda el itinerario que usas de verdad.',
      'Buscar trayecto', 'routes-go-plan');
    $('#routes-go-plan').onclick = () => showTab('view-plan');
    return;
  }
  host.innerHTML = state.routes.map(r => `
    <div class="rt" data-id="${r.id}">
      <div class="rt-main">
        <div class="rt-top">
          <div class="rt-name">${esc(r.name)}</div>
          ${r.id === state.activeId ? '<span class="tag">En el tablero</span>' : ''}
        </div>
        ${r.legs.length ? `<div class="rt-lines">${r.legs.map(l =>
            bulletHtml(l.line_code, l.line_color, l.line_mode, 'sm')).join('<span class="arrow">›</span>')}</div>` : ''}
        <div class="rt-meta">
          <span class="rt-days">${DAYS.map((d, n) =>
            `<i class="${r.days.includes(n) ? 'on' : ''}">${d}</i>`).join('')}</span>
          <span class="rt-when">${esc(routeWhen(r))}</span>
        </div>
      </div>
      <svg class="rt-chev" viewBox="0 0 24 24"><path d="M9 6l6 6-6 6"/></svg>
    </div>`).join('');
  $$('.rt', host).forEach(el => {
    el.onclick = () => openEditor(state.routes.find(r => r.id === +el.dataset.id));
  });
}

/* Selector de ruta: un desplegable justo debajo del título. Son tres rutas,
   no hace falta una hoja que suba desde abajo. */
function openRouteMenu() {
  const btn = $('#route-picker');
  const viejo = $('.route-menu');
  if (viejo) { cerrarRouteMenu(); return; }

  const menu = document.createElement('div');
  menu.className = 'route-menu';
  menu.innerHTML = state.routes.map(r => `
    <button type="button" class="${r.id === state.activeId ? 'on' : ''}" data-id="${r.id}">
      <span class="rm-main">
        <span class="rm-name">${esc(r.name)}</span>
        <span class="rm-sub">${esc(routeSub(r))}</span>
      </span>
      <span class="rm-lines">${r.legs.slice(0, 4).map(l =>
        bulletHtml(l.line_code, l.line_color, l.line_mode, 'sm')).join('')}</span>
    </button>`).join('') || '<button type="button" disabled>Sin rutas</button>';

  $('.board-head').appendChild(menu);
  btn.setAttribute('aria-expanded', 'true');

  $$('button[data-id]', menu).forEach(el => el.onclick = () => {
    state.activeId = +el.dataset.id;
    state.alts = null;
    cerrarRouteMenu();
    refresh(state.activeId);
  });
  setTimeout(() => document.addEventListener('click', fueraDelMenu), 0);
}

function cerrarRouteMenu() {
  const m = $('.route-menu');
  if (m) m.remove();
  $('#route-picker').setAttribute('aria-expanded', 'false');
  document.removeEventListener('click', fueraDelMenu);
}

function fueraDelMenu(e) {
  if (!e.target.closest('.board-head')) cerrarRouteMenu();
}

/* La barra de 30 s se relanza en cada dato nuevo. Reiniciar una animación CSS
   pide forzar un reflow; sin eso el navegador no ve el cambio. */
function restartTick() {
  const el = $('#tick');
  if (!el) return;
  el.style.animation = 'none';
  void el.offsetWidth;
  el.style.animation = '';
}

/* ---------------- planificador puerta a puerta ---------------- */

/* Autocompletado de direcciones y paradas. Navitia identifica una direccion
   por sus coordenadas, asi que el id sirve tal cual para pedir el itinerario. */
function wirePlaceInput(inputId, resId, selId, slot) {
  const inp = $(inputId), res = $(resId), sel = $(selId);
  let seq = 0;      // para tirar las respuestas que llegan tarde

  const buscar = debounce(async q => {
    const mine = ++seq;
    if (q.length < 3) { res.innerHTML = ''; return; }
    res.innerHTML = '<div class="hint">Buscando…</div>';
    try {
      const d = await api('/api/search/places?q=' + encodeURIComponent(q));
      if (mine !== seq) return;
      if (!d.places.length) { res.innerHTML = '<div class="hint">Nada con ese nombre.</div>'; return; }
      res.innerHTML = d.places.map(p => `
        <button type="button" class="stop-opt" data-id="${esc(p.id)}" data-name="${esc(p.name)}">
          <span class="kind">${esc(p.kind)}</span>
          <span class="nm">${esc(p.name)}</span>
          ${p.city ? `<span class="city">${esc(p.city)}</span>` : ''}
        </button>`).join('');
      $$('.stop-opt', res).forEach(b => b.onclick = () => {
        state.plan[slot] = { id: b.dataset.id, name: b.dataset.name };
        res.innerHTML = '';
        inp.value = '';
        inp.classList.add('hidden');
        sel.classList.remove('hidden');
        sel.innerHTML = `<span>${esc(b.dataset.name)}</span>
          <button type="button" class="x">✕</button>`;
        $('.x', sel).onclick = () => {
          state.plan[slot] = null;
          sel.classList.add('hidden');
          inp.classList.remove('hidden');
          inp.focus();
        };
      });
    } catch (e) {
      if (mine !== seq) return;
      res.innerHTML = `<div class="hint bad">${esc(e.message)}</div>`;
    }
  }, 350);

  inp.oninput = e => buscar(e.target.value.trim());
}

async function runPlan() {
  const { from, to } = state.plan;
  if (!from || !to) return toast('Elige un origen y un destino.', 'bad');
  const host = $('#plan-results');
  host.innerHTML = '<div class="hint">Calculando…</div>';
  try {
    const d = await api('/api/plan'
      + '?from=' + encodeURIComponent(from.id)
      + '&to=' + encodeURIComponent(to.id)
      + '&when=' + encodeURIComponent($('#plan-when').value || '')
      + '&mode=' + encodeURIComponent(state.plan.mode));
    state.plan.options = d.options;
    renderPlanOptions();
  } catch (e) {
    host.innerHTML = `<div class="hint bad">${esc(e.message)}</div>`;
  }
}

function planChips(o) {
  return o.legs.map(l => bulletHtml(l.line_code, l.line_color, l.line_mode, 'sm'))
    .join('<span class="arrow">›</span>');
}

function renderPlanOptions() {
  const host = $('#plan-results');
  const opts = state.plan.options;
  if (!opts.length) { host.innerHTML = '<div class="hint">Sin opciones.</div>'; return; }

  // Planificando por llegada, el primero es el que te deja salir más tarde;
  // por salida, el más rápido. La etiqueta lo dice para que no haya dudas.
  const llegada = state.plan.mode === 'arrival';
  const rapido = Math.min(...opts.map(o => o.minutes));
  host.innerHTML = '<h2 class="sec">Elige el que uses tú</h2>' + opts.map((o, i) => {
    const extra = o.minutes - rapido;
    const tag = i === 0
      ? `<span class="tag plan-best">${llegada ? 'salgo más tarde' : 'más rápido'}</span>`
      : (extra > 0 ? `<span class="plan-extra">+${extra} min</span>` : '');
    return `<button type="button" class="plan-opt${i === 0 ? ' best' : ''}"
                    data-i="${i}">
      <div class="plan-top">
        <span class="plan-mins">${o.minutes}<span class="u">min</span></span>
        ${tag}
        <span class="plan-when">${esc(o.departure)}<span class="arr">→</span>${esc(o.arrival)}</span>
      </div>
      <div class="plan-lines">${planChips(o)}</div>
      <div class="plan-sub">${o.transfers} transbordo${o.transfers === 1 ? '' : 's'}
        · ${o.walk_minutes} min andando</div>
    </button>`;
  }).join('');

  $$('.plan-opt', host).forEach(b => b.onclick = () => openPlanSave(+b.dataset.i));
}

function openPlanSave(i) {
  const o = state.plan.options[i];
  const host = $('#plan-results');
  const via = o.legs.map(l => l.line_code).join(' → ');
  const nombre = state.plan.from.name.split(' (')[0] + ' → '
               + state.plan.to.name.split(' (')[0];
  const llegada = state.plan.mode === 'arrival';
  const w = previewWindow(state.plan.mode, $('#plan-when').value, o.minutes);

  host.innerHTML = `
    <h2 class="sec">Guardar este trayecto</h2>
    <div class="plan-chosen">
      <div class="plan-lines">${planChips(o)}</div>
      <div class="plan-sub">${o.minutes} min · ${o.transfers} transbordo${o.transfers === 1 ? '' : 's'}</div>
    </div>
    <div class="fld">
      <label for="pl-name"><span>Nombre</span></label>
      <input id="pl-name" value="${esc(nombre)}">
    </div>
    <div class="fld">
      <span>Días</span>
      <div id="pl-days" class="days">${DAYS.map((d, n) =>
        `<button type="button" class="day ${n < 5 ? 'on' : ''}" data-d="${n}">${d}</button>`).join('')}</div>
    </div>
    <div class="note sm">${llegada
      ? `Se guarda como <b>llego a las ${esc($('#plan-when').value)}</b>.`
      : `Se guarda como <b>salgo a las ${esc($('#plan-when').value)}</b>.`}
      La franja en que la ruta se enseña sola se calcula con los
      ${o.minutes} min reales de este itinerario: <b>${esc(w[0])}–${esc(w[1])}</b>.</div>
    <input type="hidden" id="pl-from-h" value="${esc(w[0])}">
    <input type="hidden" id="pl-to-h" value="${esc(w[1])}">
    <div class="row-btns">
      <button type="button" id="pl-back" class="ghost">Volver</button>
      <button type="button" id="pl-save" class="primary">Guardar ruta</button>
    </div>`;

  $$('#pl-days .day').forEach(b => b.onclick = () => b.classList.toggle('on'));
  $('#pl-back').onclick = renderPlanOptions;
  $('#pl-save').onclick = async () => {
    const btn = $('#pl-save');
    btn.disabled = true; btn.textContent = 'Guardando…';
    try {
      const d = await api('/api/routes/from-plan', {
        method: 'POST',
        body: JSON.stringify({
          option: o,
          meta: {
            name: $('#pl-name').value.trim() || via,
            origin_id: state.plan.from.id, origin_name: state.plan.from.name,
            dest_id: state.plan.to.id, dest_name: state.plan.to.name,
            days: $$('#pl-days .day.on').map(b => +b.dataset.d),
            time_mode: state.plan.mode,
            time_at: $('#plan-when').value,
            time_from: $('#pl-from-h').value,
            time_to: $('#pl-to-h').value,
          },
        }),
      });
      await loadRoutes();
      renderRoutes();
      state.activeId = d.id;
      showTab('view-board');
      await refresh();
      if (d.without_direction && d.without_direction.length) {
        toast('Guardada, pero en ' + d.without_direction.join(', ')
              + ' no pude fijar el sentido: verás todos los pasos.', 'warn');
      } else {
        toast('Ruta guardada');
      }
    } catch (e) {
      toast('No se pudo guardar: ' + e.message, 'bad');
      btn.disabled = false; btn.textContent = 'Guardar ruta';
    }
  };
}

function paintPlanMode() {
  const m = state.plan.mode;
  $$('#plan-mode button').forEach(b => b.classList.toggle('on', b.dataset.mode === m));
  $('#plan-when-lbl').textContent = m === 'arrival'
    ? 'A qué hora quieres llegar' : 'A qué hora sales';
}

function openPlanner() {
  state.plan = { from: null, to: null, options: [], mode: 'arrival' };
  paintPlanMode();
  ['from', 'to'].forEach(slot => {
    $('#plan-' + slot).value = '';
    $('#plan-' + slot).classList.remove('hidden');
    $('#plan-' + slot + '-res').innerHTML = '';
    $('#plan-' + slot + '-sel').classList.add('hidden');
  });
  $('#plan-results').innerHTML = '';
  // El cambio de vista lo hace showTab(); aquí solo se deja el formulario
  // limpio y el cursor puesto.
  $('#plan-from').focus();
}

/* ---------------- editor de rutas ---------------- */

/* Horario de una ruta: se puede pensar de tres formas y las tres acaban
   siendo una franja. La que uso de verdad es "quiero llegar a las 09:00",
   que es como se piensa ir al trabajo. */
const TIME_MODES = {
  arrival:   { lbl: 'A qué hora quieres llegar',
               hint: 'La ruta se activa sola con tiempo de sobra antes de esa hora.' },
  departure: { lbl: 'A qué hora sales de casa',
               hint: 'La ruta se activa 45 min antes y dura todo el trayecto.' },
  window:    { lbl: 'Franja', hint: '' },
};

function paintTimeMode(mode, dur) {
  $$('#f-mode button').forEach(b => b.classList.toggle('on', b.dataset.mode === mode));
  const punto = mode !== 'window';
  $('#f-window').classList.toggle('hidden', punto);
  $('#f-point').classList.toggle('hidden', !punto);
  if (punto) {
    const m = TIME_MODES[mode];
    $('#f-point-lbl').textContent = m.lbl;
    // Se enseña la franja que va a salir de esa hora, para que no sea magia.
    const w = previewWindow(mode, $('#f-at').value, dur);
    $('#f-point-hint').textContent = `${m.hint} Quedaría ${w[0]}–${w[1]}.`;
  }
}

/* Mismo cálculo que app/board.py:derive_window(), para poder enseñarlo antes
   de guardar. Si se cambia allí, hay que cambiarlo aquí. */
const TRIP_DEF = 60, ANTES = 45, DESPUES = 30;
function previewWindow(mode, at, dur) {
  const toMin = s => {
    const p = String(s || '').split(':');
    return (+p[0] || 0) * 60 + (+p[1] || 0);
  };
  const toHHMM = n => {
    n = Math.max(0, Math.min(24 * 60 - 1, n));
    return `${String(Math.floor(n / 60)).padStart(2, '0')}:${String(n % 60).padStart(2, '0')}`;
  };
  const d = (+dur || 0) || TRIP_DEF;
  const t = toMin(at);
  if (mode === 'departure') return [toHHMM(t - ANTES), toHHMM(t + d + DESPUES)];
  return [toHHMM(t - d - ANTES), toHHMM(t + DESPUES / 2)];
}


function blankRoute() {
  return {
    id: null, name: '', origin_id: '', origin_name: '',
    dest_id: '', dest_name: '', days: [0, 1, 2, 3, 4],
    time_from: '07:00', time_to: '10:00',
    time_mode: 'arrival', time_at: '09:00', duration_min: 0,
    legs: [],
  };
}

function openEditor(route) {
  state.editing = route ? JSON.parse(JSON.stringify(route)) : blankRoute();
  const r = state.editing;

  $('#editor-title').textContent = r.id ? 'Editar ruta' : 'Nueva ruta';
  $('#f-name').value = r.name;
  $('#f-from').value = r.time_from;
  $('#f-to').value = r.time_to;
  $('#f-at').value = r.time_at || '09:00';
  paintTimeMode(r.time_mode || 'window', r.duration_min);
  $$('#f-mode button').forEach(b => b.onclick = () => {
    r.time_mode = b.dataset.mode;
    paintTimeMode(r.time_mode, r.duration_min);
  });
  $('#f-at').oninput = () => paintTimeMode(r.time_mode || 'window', r.duration_min);
  $('#del-route').classList.toggle('hidden', !r.id);

  $('#f-days').innerHTML = DAYS.map((d, i) =>
    `<button type="button" class="day ${r.days.includes(i) ? 'on' : ''}" data-d="${i}">${d}</button>`
  ).join('');
  $$('#f-days .day').forEach(b => {
    b.onclick = () => {
      const i = +b.dataset.d;
      const at = r.days.indexOf(i);
      if (at >= 0) r.days.splice(at, 1); else r.days.push(i);
      b.classList.toggle('on');
    };
  });

  paintChosen('origin', r.origin_id, r.origin_name);
  paintChosen('dest', r.dest_id, r.dest_name);
  renderLegsEditor();
  showTab('view-editor');
}

function paintChosen(role, id, name) {
  const box = $(`.stop-pick[data-role="${role}"]`);
  const chosen = $('.stop-chosen', box);
  const input = $('.stop-q', box);
  if (id) {
    chosen.innerHTML = `<span>${esc(name)}</span><button class="x" type="button">×</button>`;
    chosen.classList.remove('hidden');
    input.classList.add('hidden');
    $('.x', chosen).onclick = () => {
      if (role === 'origin') { state.editing.origin_id = ''; state.editing.origin_name = ''; }
      else { state.editing.dest_id = ''; state.editing.dest_name = ''; }
      paintChosen(role, '', '');
    };
  } else {
    chosen.classList.add('hidden');
    input.classList.remove('hidden');
    input.value = '';
    $('.stop-results', box).innerHTML = '';
  }
}

const searchStops = debounce(async (box, q) => {
  const res = $('.stop-results', box);
  const mine = (box._seq = (box._seq || 0) + 1);
  if (q.length < 2) { res.innerHTML = ''; return; }
  res.innerHTML = '<div class="res">Buscando…</div>';
  try {
    const d = await api('/api/search/stops?q=' + encodeURIComponent(q));
    if (mine !== box._seq) return;
    if (!d.stops.length) { res.innerHTML = '<div class="res">Nada encontrado</div>'; return; }
    res.innerHTML = d.stops.map(s => `
      <div class="res" data-id="${esc(s.id)}" data-name="${esc(s.name)}">
        ${esc(s.name)}
        <small>${esc(s.lines.slice(0, 8).map(l => l.code).join(' · '))}</small>
      </div>`).join('');
    $$('.res', res).forEach(el => {
      el.onclick = () => {
        const role = box.dataset.role;
        const id = el.dataset.id, name = el.dataset.name;
        if (role === 'origin') {
          state.editing.origin_id = id; state.editing.origin_name = name;
        } else {
          state.editing.dest_id = id; state.editing.dest_name = name;
        }
        res.innerHTML = '';
        paintChosen(role, id, name);
      };
    });
  } catch (e) {
    if (mine !== box._seq) return;
    res.innerHTML = `<div class="res">Error: ${esc(e.message)}</div>`;
  }
}, 350);

// Las líneas de una parada no cambian en toda la sesión: se guardan para que
// el editor no vuelva a enseñar "Cargando…" en cada tramo cada vez que se
// repinta.
const linesCache = new Map();

function renderLegsEditor() {
  const host = $('#legs-editor');
  const legs = state.editing.legs;
  host.innerHTML = legs.map((leg, i) => `
    <div class="leg-card" data-i="${i}">
      <div class="hd">
        ${bulletHtml(leg.line_code, leg.line_color, leg.line_mode)}
        <span>Tramo ${i + 1}</span>
        <button class="x" type="button" data-del="${i}">×</button>
      </div>
      <label><span>Subo en</span>
        <div class="leg-stop">${leg.from_id
          ? `<div class="stop-chosen"><span>${esc(leg.from_name)}</span>
             <button class="x" type="button" data-clear="${i}">×</button></div>`
          : `<input class="leg-q" data-i="${i}" placeholder="Buscar parada…" autocomplete="off">`}
        </div>
        <div class="leg-res" data-i="${i}"></div>
      </label>
      ${leg.from_id ? `<label><span>Línea</span>
        <select class="leg-line" data-i="${i}"><option value="">Cargando…</option></select>
      </label>` : ''}
      ${leg.line_id ? `<label><span>Dirección <em class="soft">(destinos que circulan ahora)</em></span>
        <div class="dirs" data-i="${i}"><span class="soft">Cargando…</span></div>
      </label>` : ''}
    </div>`).join('');

  $$('[data-del]', host).forEach(b => b.onclick = () => {
    legs.splice(+b.dataset.del, 1); renderLegsEditor();
  });
  $$('[data-clear]', host).forEach(b => b.onclick = () => {
    const i = +b.dataset.clear;
    legs[i].from_id = ''; legs[i].from_name = '';
    legs[i].line_id = ''; legs[i].line_code = ''; legs[i].directions = [];
    renderLegsEditor();
  });

  $$('.leg-q', host).forEach(inp => {
    let seq = 0;
    inp.oninput = debounce(async () => {
      const i = +inp.dataset.i;
      const res = $(`.leg-res[data-i="${i}"]`, host);
      const q = inp.value.trim();
      const mine = ++seq;
      if (q.length < 2) { res.innerHTML = ''; return; }
      res.innerHTML = '<div class="res">Buscando…</div>';
      try {
        const d = await api('/api/search/stops?q=' + encodeURIComponent(q));
        if (mine !== seq) return;
        res.innerHTML = d.stops.map(s =>
          `<div class="res" data-id="${esc(s.id)}" data-name="${esc(s.name)}">${esc(s.name)}</div>`
        ).join('') || '<div class="res">Nada</div>';
        $$('.res', res).forEach(el => el.onclick = () => {
          legs[i].from_id = el.dataset.id;
          legs[i].from_name = el.dataset.name;
          renderLegsEditor();
        });
      } catch (e) {
        if (mine !== seq) return;
        res.innerHTML = `<div class="res">${esc(e.message)}</div>`;
      }
    }, 350);
  });

  // Rellenar las líneas de cada parada elegida
  $$('.leg-line', host).forEach(async sel => {
    const i = +sel.dataset.i;
    try {
      const stop = legs[i].from_id;
      const d = linesCache.get(stop)
        || await api(`/api/stops/${encodeURIComponent(stop)}/lines`);
      linesCache.set(stop, d);
      sel.innerHTML = '<option value="">— elegir línea —</option>' +
        d.lines.map(l => `<option value="${esc(l.id)}"
          data-code="${esc(l.code)}" data-mode="${esc(l.mode)}"
          data-color="${esc(l.color)}" data-name="${esc(l.name)}"
          ${l.id === legs[i].line_id ? 'selected' : ''}>${esc(l.mode)} ${esc(l.code)}</option>`).join('');
      sel.onchange = () => {
        const o = sel.selectedOptions[0];
        legs[i].line_id = sel.value;
        legs[i].line_code = o.dataset.code || '';
        legs[i].line_name = o.dataset.name || '';
        legs[i].line_mode = o.dataset.mode || '';
        legs[i].line_color = o.dataset.color || '';
        legs[i].directions = [];
        renderLegsEditor();
      };
    } catch (e) {
      sel.innerHTML = `<option>Error: ${esc(e.message)}</option>`;
    }
  });

  // Direcciones observadas en vivo para la línea elegida
  $$('.dirs', host).forEach(async box => {
    const i = +box.dataset.i;
    try {
      const d = await api(`/api/stops/${encodeURIComponent(legs[i].from_id)}/directions`
                          + `?line_id=${encodeURIComponent(legs[i].line_id)}`);
      if (!d.directions.length) {
        box.innerHTML = `<span class="soft">Ahora mismo no circula nada;
          podrás afinarlo más tarde.</span>`;
        return;
      }
      box.innerHTML = d.directions.map(dir =>
        `<button type="button" class="dir-chip ${legs[i].directions.includes(dir) ? 'on' : ''}"
                 data-dir="${esc(dir)}">${esc(dir)}</button>`).join('');
      $$('.dir-chip', box).forEach(c => c.onclick = () => {
        const dir = c.dataset.dir;
        const at = legs[i].directions.indexOf(dir);
        if (at >= 0) legs[i].directions.splice(at, 1);
        else legs[i].directions.push(dir);
        c.classList.toggle('on');
      });
    } catch (e) {
      box.innerHTML = `<span class="soft bad">${esc(e.message)}</span>`;
    }
  });
}

async function saveRoute() {
  const r = state.editing;
  r.name = $('#f-name').value.trim();
  r.time_from = $('#f-from').value;
  r.time_to = $('#f-to').value;
  r.time_at = $('#f-at').value;
  r.days.sort((a, b) => a - b);

  if (!r.name) return toast('Ponle un nombre a la ruta.', 'bad');
  if (!r.origin_id || !r.dest_id) return toast('Faltan el origen y el destino.', 'bad');
  if (!r.legs.length) return toast('Añade al menos un tramo.', 'bad');
  for (const [i, l] of r.legs.entries()) {
    if (!l.from_id || !l.line_id) return toast(`El tramo ${i + 1} está incompleto.`, 'bad');
  }

  const btn = $('#save-route');
  btn.disabled = true; btn.textContent = 'Guardando…';
  try {
    if (r.id) await api(`/api/routes/${r.id}`, { method: 'PUT', body: JSON.stringify(r) });
    else await api('/api/routes', { method: 'POST', body: JSON.stringify(r) });
    await loadRoutes();
    renderRoutes();
    state.alts = null;
    showTab('view-board');
    await refresh(r.id || null);
  } catch (e) {
    toast('No se pudo guardar: ' + e.message, 'bad');
  } finally {
    btn.disabled = false; btn.textContent = 'Guardar';
  }
}

/* ---------------- estadísticas ---------------- */

/* Lo que sabe la previsión del andén, sin maquillar: cuántos días lleva
   mirando, cuántas veces acertó, y qué tramos todavía no da para nada. */
function renderModel(m) {
  const a = m.accuracy || {};
  const c = m.collector || {};
  let html = '<h2 class="sec">Previsión del andén</h2>';

  if (!a.observations) {
    html += `<div class="note sm">Todavía no ha visto ningún andén.
      Sólo aprende de las estaciones de tus rutas, y sólo del tren, el RER y
      el Transilien: el metro y el bus no publican vía nunca.</div>`;
  } else {
    html += `<div class="stat-grid">
      <div class="stat"><div class="n">${a.observations}</div>
        <div class="l">andenes vistos</div></div>
      <div class="stat"><div class="n">${a.days}</div>
        <div class="l">${a.days === 1 ? 'día' : 'días'} aprendiendo</div></div>
    </div>`;
    if (a.predictions) {
      const pct = Math.round((a.rate || 0) * 100);
      html += `<div class="stat-row">
        <span class="k">Acierta</span>
        <span class="s">${a.hits} de ${a.predictions}</span>
        <span class="n">${pct}<small>%</small></span></div>`;
    } else {
      html += `<div class="note sm">Aún no ha hecho ninguna previsión que se
        haya podido comprobar: hace falta ver el mismo tren varios días.</div>`;
    }
  }

  if (m.coverage && m.coverage.length) {
    html += `<h2 class="sec">Por tramo${m.route ? ' · ' + esc(m.route.name) : ''}</h2>`;
    html += m.coverage.map(l => `<div class="stat-row">
      ${bulletHtml(l.line_code, lineByCode(l.line_code).color, lineByCode(l.line_code).mode)}
      <span class="s">${l.observations
        ? `${l.platforms} vía${l.platforms === 1 ? '' : 's'} distintas`
        : 'sin datos todavía'}</span>
      <span class="n">${l.observations}</span></div>`).join('');
  }

  const estado = c.reason ? esc(c.reason)
                          : `mirando ${c.stations} estación${c.stations === 1 ? '' : 'es'}`;
  html += `<div class="note sm">Recogida en segundo plano:
    ${c.enabled ? (c.running ? 'activa' : 'parada') : 'desactivada'}
    ${c.last_at ? `· última pasada ${esc(c.last_at)}` : ''} · ${estado}</div>`;
  return html;
}

async function loadStats() {
  const host = $('#stats-body');
  host.innerHTML = '<div class="note">Cargando…</div>';
  try {
    // La previsión no debe impedir ver el historial si falla.
    const [s, m] = await Promise.all([
      api('/api/stats'),
      api('/api/platform-model').catch(() => null),
    ]);
    const o = s.overall || {};
    let html = '';

    if (!o.n) {
      html += `<div class="note">Todavía no hay historial.<br>
        Se va guardando solo cada vez que consultas una ruta.</div>`;
    } else {
      html += `<div class="stat-grid">
        <div class="stat"><div class="n">${o.n}</div><div class="l">consultas</div></div>
        <div class="stat"><div class="n">${(o.avg_delay || 0).toFixed(1)}<span class="u">min</span></div>
          <div class="l">retraso medio</div></div>
      </div>`;

      if (s.by_month.length) {
        html += `<h2 class="sec">Días con incidencia</h2>`;
        html += s.by_month.map(x => {
          const p = x.total_days ? x.bad_days / x.total_days : 0;
          const cls = p > .5 ? 'bad' : p > .2 ? '' : 'ok';
          return `<div class="stat-row">
            <span class="s">${esc(x.month)}</span>
            <span class="meter ${cls}" style="--p:${Math.round(p * 100)}%"><i></i></span>
            <span class="n">${x.bad_days}<small> / ${x.total_days}</small></span></div>`;
        }).join('');
      }
      if (s.by_line.length) {
        html += `<h2 class="sec">Qué línea me falla más</h2>`;
        html += s.by_line.map(l => `<div class="stat-row">
          ${bulletHtml(l.worst_line, lineByCode(l.worst_line).color, lineByCode(l.worst_line).mode)}
          <span class="s">${(l.avg_delay || 0).toFixed(1)} min de media</span>
          <span class="n">${l.n}<small> días</small></span></div>`).join('');
      }
    }

    if (m) html += renderModel(m);
    host.innerHTML = html;
  } catch (e) {
    host.innerHTML = `<div class="note">Error: ${esc(e.message)}</div>`;
  }
}

/* ---------------- navegación por pestañas ---------------- */

/* El editor no es una pestaña: se entra desde Rutas y se sale con Guardar o
   con Volver, así que mientras está abierto la barra se esconde. */
const TAB_VIEWS = ['view-board', 'view-routes', 'view-plan', 'view-stats'];

function showTab(id) {
  show(id);
  $$('#tabs .tab').forEach(b => b.classList.toggle('on', b.dataset.view === id));
  $('#tabs').classList.toggle('hidden', !TAB_VIEWS.includes(id));
  if (id === 'view-routes') renderRoutes();
  if (id === 'view-stats') loadStats();
  if (id === 'view-plan' && !state.plan.from && !state.plan.to) openPlanner();
}

/* Tirar hacia abajo para refrescar. El tablero ya se actualiza solo cada 30 s,
   pero cuando estás esperando un tren quieres poder pedirlo tú. Cuesta una
   llamada de cuota por tirón, así que hay un mínimo entre tirones. */
function wirePull() {
  const scroller = $('#board-scroll');
  const inner = $('#board-inner');
  const ind = $('#pull');
  const UMBRAL = 62;
  let y0 = 0, dist = 0, activo = false, ultimo = 0;

  const soltar = () => {
    inner.style.transition = 'transform 320ms cubic-bezier(.2,.8,.2,1)';
    inner.style.transform = '';
    ind.style.transform = '';
    ind.classList.remove('ready');
    dist = 0; activo = false;
  };

  scroller.addEventListener('touchstart', e => {
    if (scroller.scrollTop > 0 || state.pulling) return;
    y0 = e.touches[0].clientY;
    activo = true;
    inner.style.transition = 'none';
  }, { passive: true });

  scroller.addEventListener('touchmove', e => {
    if (!activo) return;
    const d = e.touches[0].clientY - y0;
    if (d <= 0) { soltar(); return; }
    // Resistencia: cuanto más tiras, menos baja. Se siente elástico.
    dist = Math.min(d * 0.45, 88);
    inner.style.transform = `translateY(${dist}px)`;
    ind.style.transform = `translateY(${dist - 46}px) rotate(${dist * 4}deg)`;
    ind.classList.toggle('ready', dist > UMBRAL);
  }, { passive: true });

  scroller.addEventListener('touchend', async () => {
    if (!activo) return;
    const dispara = dist > UMBRAL && Date.now() - ultimo > 3000;
    soltar();
    if (!dispara) return;
    ultimo = Date.now();
    state.pulling = true;
    ind.classList.add('spin');
    try {
      await refresh(state.activeId);
    } finally {
      ind.classList.remove('spin');
      state.pulling = false;
    }
  });
}

/* ---------------- arranque ---------------- */

function wire() {
  $('#route-picker').onclick = e => { e.stopPropagation(); openRouteMenu(); };
  $$('#tabs .tab').forEach(b => b.onclick = () => showTab(b.dataset.view));
  wirePull();
  $('#new-route').onclick = () => openEditor(null);
  $('#new-plan').onclick = () => showTab('view-plan');
  wirePlaceInput('#plan-from', '#plan-from-res', '#plan-from-sel', 'from');
  wirePlaceInput('#plan-to', '#plan-to-res', '#plan-to-sel', 'to');
  $('#plan-go').onclick = runPlan;
  $$('#plan-mode button').forEach(b => b.onclick = () => {
    state.plan.mode = b.dataset.mode;
    paintPlanMode();
    // Al cambiar de criterio, las opciones de antes ya no valen.
    state.plan.options = [];
    $('#plan-results').innerHTML = '';
  });
  $('#save-route').onclick = saveRoute;
  $('#add-leg').onclick = () => {
    state.editing.legs.push({
      line_id: '', line_code: '', line_name: '', line_mode: '', line_color: '',
      from_id: '', from_name: '', to_id: '', to_name: '', directions: [],
    });
    renderLegsEditor();
  };
  // Borrado en dos toques: el segundo dentro de 4 s confirma.
  let armedDelete = 0;
  $('#del-route').onclick = async () => {
    const btn = $('#del-route');
    if (Date.now() - armedDelete > 4000) {
      armedDelete = Date.now();
      btn.textContent = 'Pulsa otra vez';
      setTimeout(() => {
        if (Date.now() - armedDelete >= 4000) btn.textContent = 'Borrar';
      }, 4100);
      return;
    }
    armedDelete = 0;
    btn.textContent = 'Borrando…';
    try {
      await api(`/api/routes/${state.editing.id}`, { method: 'DELETE' });
      state.activeId = null;
      await loadRoutes();
      renderRoutes();
      showTab('view-board');
      await refresh();
      toast('Ruta borrada');
    } catch (e) {
      toast('No se pudo borrar: ' + e.message, 'bad');
    } finally {
      btn.textContent = 'Borrar';
    }
  };
  $$('.back').forEach(b => b.onclick = () => showTab(b.dataset.back));

  $$('.stop-pick').forEach(box => {
    $('.stop-q', box).oninput = e => searchStops(box, e.target.value.trim());
  });
}

/* ---------------- instalada en la pantalla de inicio ----------------

   Trajet se añade a la pantalla de inicio y se abre sin barra de navegador:
   a efectos de uso es la app, sin store ni Xcode de por medio.

   El service worker solo sirve para que arranque sin red. El navegador
   únicamente lo permite en contexto seguro (https o localhost): entrando por
   http:// a la IP del NAS no se registra, y la app va exactamente igual.
   Detrás de Tailscale con TLS sí. Nunca toca /api/. */
function registerSW() {
  if (!('serviceWorker' in navigator) || !window.isSecureContext) return;
  navigator.serviceWorker.register('/sw.js').catch(() => {});
}

async function boot() {
  wire();
  registerSW();
  try { await loadRoutes(); } catch (_) {}
  await refresh();
  startLoop();
}

boot();
